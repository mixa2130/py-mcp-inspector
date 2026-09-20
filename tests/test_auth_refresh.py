"""Swapping an expiring credential on a live connection, without reconnecting."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from pymcpinspector.app import create_app
from pymcpinspector.models import ConnectionConfig
from pymcpinspector.store import ServerStore
from pymcpinspector.transports import LiveHeaders

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo_server.py"


# ------------------------------------------------------------------- units


def test_replacing_headers_reaches_the_next_request():
    live = LiveHeaders()
    client = httpx.AsyncClient(headers={"Authorization": "Bearer old", "X-One": "1"})
    live.track(client)

    live.replace({"Authorization": "Bearer new", "X-One": "1"})

    assert client.build_request("POST", "http://x/").headers["authorization"] == "Bearer new"
    assert client.build_request("POST", "http://x/").headers["x-one"] == "1"


def test_a_renamed_auth_header_does_not_go_out_beside_the_old_one():
    live = LiveHeaders()
    client = httpx.AsyncClient(headers={"Authorization": "Bearer old"})
    live.track(client)

    live.replace({"X-Api-Key": "k"}, drop=["Authorization"])

    request = client.build_request("POST", "http://x/")
    assert "authorization" not in request.headers
    assert request.headers["x-api-key"] == "k"


def test_a_closed_transport_leaves_nothing_to_patch():
    live = LiveHeaders()
    live.track(httpx.AsyncClient())
    assert live.live
    live.forget()
    assert not live.live


def test_the_config_keeps_the_new_token_for_a_later_reconnect():
    config = ConnectionConfig(url="http://x/mcp", bearer_token="first")
    config.bearer_token = "second"
    assert config.resolved_headers() == {"Authorization": "Bearer second"}


# --------------------------------------------------------------------- e2e


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _demo_over_http(transport: str):
    """The demo server on a free port; `show_headers` echoes what arrived."""
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(DEMO), "--transport", transport, "--port", str(port)],
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

    path = "mcp" if transport == "streamable-http" else "sse"
    yield f"http://127.0.0.1:{port}/{path}"
    process.terminate()
    process.wait(timeout=10)


@pytest.fixture(scope="module")
def http_server():
    yield from _demo_over_http("streamable-http")


@pytest.fixture(scope="module")
def sse_server():
    yield from _demo_over_http("sse")


@pytest.fixture
async def client(tmp_path):
    app = create_app(ServerStore(tmp_path / "servers.json"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://inspector"
    ) as client:
        yield client


async def _connect(client, url, **config) -> dict:
    body = (
        await client.post(
            "/api/connect",
            json={"transport": "streamable-http", "url": url, **config},
            timeout=30,
        )
    ).json()
    assert body.get("status") == "connected", body
    return body


async def _seen_auth(client) -> str | None:
    """What the server says the `Authorization` header was on this request."""
    body = (await client.post("/api/tools/call", json={"name": "show_headers"}, timeout=30)).json()
    assert "error" not in body, body
    return body["result"]["structuredContent"].get("authorization")


async def test_a_fresh_token_reaches_the_server_without_reconnecting(client, http_server):
    # Pinned to a handshake revision on purpose: 2026-07-28 issues no
    # `mcp-session-id`, so the session-identity check below would compare
    # None with None and pass however badly the refresh behaved.
    connected = await _connect(client, http_server, protocol_version="2025-11-25", bearer_token="first")
    session_id = connected["http_session_id"]
    assert session_id, "this revision should have issued a session id"
    assert await _seen_auth(client) == "Bearer first"

    response = await client.post("/api/auth", json={"token": "second"})
    assert response.status_code == 200, response.text
    assert response.json()["sent_headers"] == {"Authorization": "Bearer second"}

    assert await _seen_auth(client) == "Bearer second"
    # The point of the exercise: the same session, not a re-handshaked one.
    status = (await client.get("/api/status")).json()
    assert status["http_session_id"] == session_id
    assert status["sent_headers"] == {"Authorization": "Bearer second"}
    await client.post("/api/disconnect", json={}, timeout=30)


async def test_other_headers_survive_a_token_refresh(client, http_server):
    await _connect(
        client,
        http_server,
        bearer_token="first",
        headers=[{"name": "X-Trace-Id", "value": "abc123", "enabled": True}],
    )
    await client.post("/api/auth", json={"token": "second"})

    body = (await client.post("/api/tools/call", json={"name": "show_headers"}, timeout=30)).json()
    headers = body["result"]["structuredContent"]
    assert headers["authorization"] == "Bearer second"
    assert headers["x-trace-id"] == "abc123"
    await client.post("/api/disconnect", json={}, timeout=30)


async def test_the_scheme_and_header_name_can_change_too(client, http_server):
    await _connect(client, http_server, bearer_token="first")

    await client.post("/api/auth", json={"token": "k", "scheme": "", "header": "X-Api-Key"})

    headers = (
        await client.post("/api/tools/call", json={"name": "show_headers"}, timeout=30)
    ).json()["result"]["structuredContent"]
    assert headers["x-api-key"] == "k"
    # The renamed header must not keep going out under its old name.
    assert "authorization" not in headers
    await client.post("/api/disconnect", json={}, timeout=30)


async def test_an_empty_token_stops_sending_the_header(client, http_server):
    await _connect(client, http_server, bearer_token="first")
    await client.post("/api/auth", json={"token": ""})
    assert await _seen_auth(client) is None
    await client.post("/api/disconnect", json={}, timeout=30)


async def test_sse_posts_carry_the_new_token_too(client, sse_server):
    """The SSE transport builds its own client through a factory, not `_http_client`."""
    body = (
        await client.post(
            "/api/connect",
            json={"transport": "sse", "url": sse_server, "bearer_token": "first"},
            timeout=30,
        )
    ).json()
    assert body.get("status") == "connected", body
    assert await _seen_auth(client) == "Bearer first"

    await client.post("/api/auth", json={"token": "second"})
    assert await _seen_auth(client) == "Bearer second"
    await client.post("/api/disconnect", json={}, timeout=30)


async def test_refreshing_without_a_connection_is_a_409(client):
    response = await client.post("/api/auth", json={"token": "x"})
    assert response.status_code == 409
    assert response.json()["error"]["kind"] == "not_connected"


async def test_a_token_is_required_in_the_body(client, http_server):
    await _connect(client, http_server, bearer_token="first")
    response = await client.post("/api/auth", json={})
    assert response.status_code == 400
    assert "token is required" in response.json()["error"]["message"]
    await client.post("/api/disconnect", json={}, timeout=30)


async def test_stdio_says_it_has_no_headers_to_refresh(client):
    body = (
        await client.post(
            "/api/connect",
            json={"transport": "stdio", "command": sys.executable, "args": [str(DEMO)], "cwd": str(ROOT)},
            timeout=30,
        )
    ).json()
    assert body.get("status") == "connected", body

    response = await client.post("/api/auth", json={"token": "x"})
    assert response.status_code == 400
    assert "only the HTTP transports send headers" in response.json()["error"]["message"]
    await client.post("/api/disconnect", json={}, timeout=30)
