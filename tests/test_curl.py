"""Rendering a request as `curl`, including running the result against a server."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from pymcpinspector.app import create_app
from pymcpinspector.curl import build_curl
from pymcpinspector.models import ConnectionConfig
from pymcpinspector.store import ServerStore

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo_server.py"

HTTP = {"transport": "streamable-http", "url": "https://api.example.com/mcp"}


def config(**kwargs) -> ConnectionConfig:
    return ConnectionConfig.model_validate({**HTTP, **kwargs})


def args_of(command: str) -> list[str]:
    """The rendered command split the way a shell would split it."""
    return shlex.split(command.replace("\\\n", " "))


def headers_of(command: str) -> dict[str, str]:
    out = {}
    tokens = args_of(command)
    for i, token in enumerate(tokens):
        if token == "-H":
            name, _, value = tokens[i + 1].partition(": ")
            out[name] = value
    return out


def body_of(command: str) -> dict:
    tokens = args_of(command)
    return json.loads(tokens[tokens.index("-d") + 1])


# ------------------------------------------------------------------ shape


def test_the_command_is_a_post_to_the_configured_url():
    command = build_curl(config(), method="tools/list")["command"]
    assert command.startswith("curl -sS -X POST https://api.example.com/mcp")


def test_the_body_is_a_json_rpc_request():
    body = body_of(build_curl(config(), method="tools/list", params={"cursor": "c1"})["command"])
    assert body == {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"cursor": "c1"}}


def test_params_are_left_out_when_there_are_none():
    assert "params" not in body_of(build_curl(config(), method="ping")["command"])


def test_the_protocol_headers_are_always_present():
    headers = headers_of(build_curl(config(), method="tools/list")["command"])
    assert headers["Accept"] == "application/json, text/event-stream"
    assert headers["Content-Type"] == "application/json"


def test_a_blank_method_is_refused():
    with pytest.raises(ValueError, match="method is required"):
        build_curl(config(), method="  ")


def test_stdio_has_no_http_request_to_reproduce():
    with pytest.raises(ValueError, match="no HTTP request"):
        build_curl(ConnectionConfig(transport="stdio", command="echo"), method="tools/list")


# ------------------------------------------------------------ the session


def test_the_session_id_is_carried_when_there_is_one():
    command = build_curl(config(), method="tools/list", session_id="sid-1")["command"]
    assert headers_of(command)["mcp-session-id"] == "sid-1"


def test_no_session_id_is_called_out_rather_than_ignored():
    result = build_curl(config(), method="tools/list")
    assert "mcp-session-id" not in headers_of(result["command"])
    assert any("No mcp-session-id" in note for note in result["notes"])


# ----------------------------------------------------------- the envelope


def test_a_modern_session_gets_the_per_request_envelope():
    result = build_curl(
        config(), method="tools/list", protocol_version="2026-07-28", capabilities={"roots": {}}
    )
    meta = body_of(result["command"])["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert meta["io.modelcontextprotocol/clientCapabilities"] == {"roots": {}}
    assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "pymcpinspector"


def test_a_handshake_session_gets_no_envelope():
    body = body_of(build_curl(config(), method="tools/list", protocol_version="2025-11-25")["command"])
    assert "params" not in body


def test_a_modern_session_gets_the_routing_headers():
    """Without these the server answers -32020 before it looks at the method."""
    command = build_curl(
        config(), method="tools/call", params={"name": "add", "arguments": {}},
        protocol_version="2026-07-28",
    )["command"]
    headers = headers_of(command)
    assert headers["mcp-method"] == "tools/call"
    assert headers["mcp-name"] == "add"
    assert headers["mcp-protocol-version"] == "2026-07-28"


@pytest.mark.parametrize(
    ("method", "params", "expected"),
    [
        ("resources/read", {"uri": "demo://time"}, "demo://time"),
        ("prompts/get", {"name": "review", "arguments": {}}, "review"),
        ("tools/call", {"name": "add"}, "add"),
    ],
)
def test_the_name_header_follows_the_method_that_bears_it(method, params, expected):
    command = build_curl(config(), method=method, params=params, protocol_version="2026-07-28")["command"]
    assert headers_of(command)["mcp-name"] == expected


@pytest.mark.parametrize("method", ["tools/list", "resources/list", "prompts/list", "ping"])
def test_a_method_that_bears_no_name_gets_no_name_header(method):
    headers = headers_of(build_curl(config(), method=method, protocol_version="2026-07-28")["command"])
    assert headers["mcp-method"] == method
    assert "mcp-name" not in headers


def test_ping_on_a_modern_session_is_called_out():
    """Same fact the hidden Ping button reflects, for anyone calling the API directly."""
    notes = build_curl(config(), method="ping", protocol_version="2026-07-28")["notes"]
    assert any("`ping` was removed" in note for note in notes)


def test_a_non_ascii_name_is_wrapped_so_it_survives_an_http_field():
    command = build_curl(
        config(), method="tools/call", params={"name": "добавить"}, protocol_version="2026-07-28",
    )["command"]
    assert headers_of(command)["mcp-name"] == "=?base64?0LTQvtCx0LDQstC40YLRjA==?="


def test_a_handshake_session_gets_no_routing_headers():
    headers = headers_of(build_curl(config(), method="tools/list", protocol_version="2025-11-25")["command"])
    assert "mcp-method" not in headers


def test_initialize_on_a_modern_session_is_called_out():
    notes = build_curl(config(), method="initialize", protocol_version="2026-07-28")["notes"]
    assert any("server/discover" in note for note in notes)


# --------------------------------------------------------------- secrets


def test_credentials_become_shell_variables_by_default():
    result = build_curl(config(bearer_token="sekrit", client_key_password="hunter2"), method="tools/list")
    assert "sekrit" not in result["command"] and "hunter2" not in result["command"]
    assert result["masked"] == ["MCP_AUTHORIZATION", "MCP_KEY_PASSPHRASE"]
    # The scheme is not the secret, so it stays readable.
    assert 'Authorization: Bearer $MCP_AUTHORIZATION' in result["command"]


def test_a_secret_looking_header_is_masked_and_an_ordinary_one_is_not():
    result = build_curl(
        config(headers=[
            {"name": "X-Api-Key", "value": "kkk"},
            {"name": "X-Trace-Id", "value": "abc123"},
        ]),
        method="tools/list",
    )
    assert "kkk" not in result["command"]
    assert "'X-Trace-Id: abc123'" in result["command"]


def test_secrets_can_be_included_on_purpose():
    result = build_curl(config(bearer_token="sekrit"), method="tools/list", mask_secrets=False)
    assert "'Authorization: Bearer sekrit'" in result["command"]
    assert result["masked"] == []


def test_the_masked_variables_are_named_up_front():
    notes = build_curl(config(bearer_token="s"), method="tools/list")["notes"]
    assert notes[0].startswith("Set these before running:")


# -------------------------------------------------------------- tls, time


def test_tls_options_are_translated_to_curl_flags(certs):
    result = build_curl(
        config(
            verify_tls=False,
            ca_bundle=str(certs["ca"]),
            client_cert=str(certs["client_cert"]),
            client_key=str(certs["client_key"]),
        ),
        method="tools/list",
    )
    tokens = args_of(result["command"])
    assert "--insecure" in tokens
    assert tokens[tokens.index("--cacert") + 1] == str(certs["ca"])
    assert tokens[tokens.index("--cert") + 1] == str(certs["client_cert"])
    assert tokens[tokens.index("--key") + 1] == str(certs["client_key"])


def test_a_certificate_directory_becomes_capath(certs):
    """curl splits by flag what the inspector keeps in one field."""
    tokens = args_of(build_curl(config(ca_bundle=str(certs["ca_dir"])), method="tools/list")["command"])
    assert tokens[tokens.index("--capath") + 1] == str(certs["ca_dir"])


def test_the_timeouts_mirror_the_transport():
    tokens = args_of(build_curl(config(http_timeout=5, sse_read_timeout=90), method="tools/list")["command"])
    assert tokens[tokens.index("--connect-timeout") + 1] == "5"
    assert tokens[tokens.index("--max-time") + 1] == "90"


# ------------------------------------------------------- header precedence


def test_a_header_the_transport_owns_is_left_out_and_explained():
    """The editor shows it, but a per-request value wins in httpx; see the README."""
    result = build_curl(
        config(headers=[{"name": "Content-Type", "value": "text/plain"}]), method="tools/list"
    )
    assert headers_of(result["command"])["Content-Type"] == "application/json"
    assert any("transport sets it per request" in note for note in result["notes"])


def test_sse_without_a_known_endpoint_says_the_url_will_not_take_a_post():
    notes = build_curl(config(transport="sse", url="https://x/sse"), method="tools/list")["notes"]
    assert any("announces on the stream" in note for note in notes)


def test_sse_with_a_known_endpoint_uses_it():
    result = build_curl(
        config(transport="sse", url="https://x/sse"),
        method="tools/list",
        endpoint="https://x/messages?session_id=9",
    )
    assert result["command"].startswith("curl -sS -X POST 'https://x/messages?session_id=9'")


# ------------------------------------------------------------------- e2e


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def http_server():
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(DEMO), "--transport", "streamable-http", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"demo server exited: {(process.stdout.read() or b'').decode()}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        process.kill()
        pytest.fail("demo server did not start")
    yield f"http://127.0.0.1:{port}/mcp"
    process.terminate()
    process.wait(timeout=10)


@pytest.fixture
async def client(tmp_path):
    app = create_app(ServerStore(tmp_path / "servers.json"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://inspector"
    ) as client:
        yield client


def _run(command: str) -> str:
    """Hand the rendered command to a shell, exactly as a person would."""
    if shutil.which("curl") is None:
        pytest.skip("curl is not on PATH")
    done = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


async def _rendered(client, **body) -> dict:
    response = await client.post("/api/curl", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("version", ["auto", "2025-11-25"])
async def test_the_generated_command_actually_works(client, http_server, version):
    """The only test that matters: paste it into a shell and get a real answer."""
    connected = (
        await client.post(
            "/api/connect",
            json={"transport": "streamable-http", "url": http_server, "protocol_version": version},
            timeout=30,
        )
    ).json()
    assert connected.get("status") == "connected", connected
    await client.post("/api/tools/list", json={})  # so the session knows the tool schemas

    rendered = await _rendered(
        client,
        method="tools/call",
        params={"name": "add", "arguments": {"a": 19, "b": 23}},
        mask_secrets=False,
    )
    output = _run(rendered["command"])
    # 2025-era servers frame the answer as SSE; the modern one answers plain JSON.
    payload = json.loads(re.sub(r"^event: .*\ndata: ", "", output.strip(), flags=re.MULTILINE))
    assert payload["result"]["structuredContent"] == {"result": 42.0}
    await client.post("/api/disconnect", json={}, timeout=30)


@pytest.mark.parametrize("method", ["tools/list", "resources/list", "prompts/list"])
async def test_the_basic_listings_render_a_command_that_works(client, http_server, method):
    connected = (
        await client.post(
            "/api/connect", json={"transport": "streamable-http", "url": http_server}, timeout=30
        )
    ).json()
    assert connected.get("status") == "connected", connected

    rendered = await _rendered(client, method=method, mask_secrets=False)
    payload = json.loads(re.sub(r"^event: .*\ndata: ", "", _run(rendered["command"]).strip(), flags=re.MULTILINE))
    assert "result" in payload, payload
    await client.post("/api/disconnect", json={}, timeout=30)


async def test_the_rendered_command_carries_the_live_session(client, http_server):
    await client.post(
        "/api/connect",
        json={"transport": "streamable-http", "url": http_server, "protocol_version": "2025-11-25"},
        timeout=30,
    )
    status = (await client.get("/api/status")).json()
    rendered = await _rendered(client, method="tools/list")
    assert headers_of(rendered["command"])["mcp-session-id"] == status["http_session_id"]
    await client.post("/api/disconnect", json={}, timeout=30)


async def test_the_envelope_carries_the_real_client_capabilities(client, http_server):
    await client.post(
        "/api/connect", json={"transport": "streamable-http", "url": http_server}, timeout=30
    )
    rendered = await _rendered(client, method="tools/list")
    meta = body_of(rendered["command"])["params"]["_meta"]
    # Built from the live session, not guessed: the inspector offers all three.
    assert set(meta["io.modelcontextprotocol/clientCapabilities"]) == {"sampling", "elicitation", "roots"}
    await client.post("/api/disconnect", json={}, timeout=30)


async def test_curl_before_any_connection_is_a_bad_request(client):
    response = await client.post("/api/curl", json={"method": "tools/list"})
    assert response.status_code == 400
    assert "configure a connection first" in response.json()["error"]["message"]


async def test_params_must_be_an_object(client, http_server):
    await client.post(
        "/api/connect", json={"transport": "streamable-http", "url": http_server}, timeout=30
    )
    response = await client.post("/api/curl", json={"method": "tools/list", "params": [1, 2]})
    assert response.status_code == 400
    assert "params must be a JSON object" in response.json()["error"]["message"]
    await client.post("/api/disconnect", json={}, timeout=30)
