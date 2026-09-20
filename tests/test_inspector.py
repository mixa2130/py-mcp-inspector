"""End-to-end tests driving the inspector's API against the bundled demo server."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import mcp.types as types
import pytest

from pymcpinspector.app import create_app
from pymcpinspector.connection import InspectorConnection
from pymcpinspector.events import EventBus
from pymcpinspector.models import ConnectionConfig
from pymcpinspector.store import ServerStore

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo_server.py"


@pytest.fixture
def app(tmp_path: Path):
    return create_app(ServerStore(tmp_path / "servers.json"))


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://inspector") as client:
        yield client


async def _connect(client, protocol_version: str = "auto") -> dict:
    response = await client.post(
        "/api/connect",
        json={
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(DEMO)],
            "cwd": str(ROOT),
            "protocol_version": protocol_version,
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("status") == "connected", response.text
    return body


@pytest.fixture
async def connected(client):
    """Default connection: whatever auto-negotiation lands on (modern, for this server)."""
    await _connect(client)
    yield client
    await client.post("/api/disconnect", json={}, timeout=30)


@pytest.fixture
async def legacy(client):
    """A connection pinned to the newest handshake-era revision."""
    await _connect(client, "2025-11-25")
    yield client
    await client.post("/api/disconnect", json={}, timeout=30)


# ------------------------------------------------------------------- units


def test_headers_include_auth():
    config = ConnectionConfig(
        url="http://x",
        headers=[
            {"name": "X-One", "value": "1"},
            {"name": "X-Off", "value": "2", "enabled": False},
        ],
        bearer_token="abc",
    )
    assert config.resolved_headers() == {"X-One": "1", "Authorization": "Bearer abc"}


def test_a_header_row_without_a_name_is_reported():
    config = ConnectionConfig(url="http://x", headers=[{"name": "  ", "value": "abc123"}])
    assert config.resolved_headers() == {}
    assert config.header_warnings() == ["header row 1 has a value but no name; it was not sent"]


def test_the_token_field_reports_that_it_overrode_a_header_row():
    config = ConnectionConfig(
        url="http://x",
        headers=[{"name": "authorization", "value": "Token abc"}],
        bearer_token="zzz",
    )
    # One header, not two spellings of it going out side by side.
    assert config.resolved_headers() == {"Authorization": "Bearer zzz"}
    assert config.header_warnings() == [
        "the Authentication token replaced the 'authorization' row in the header list"
    ]


def test_well_formed_headers_warn_about_nothing():
    config = ConnectionConfig(
        url="http://x",
        headers=[{"name": "X-One", "value": "1"}, {"name": "", "value": ""}],
        bearer_token="abc",
    )
    assert config.header_warnings() == []


def test_stdio_never_warns_about_headers():
    config = ConnectionConfig(transport="stdio", command="echo", headers=[{"name": "", "value": "x"}])
    assert config.header_warnings() == []


def test_custom_auth_header_and_scheme():
    config = ConnectionConfig(url="http://x", auth_header_name="X-Api-Key", auth_scheme="", bearer_token="k")
    assert config.resolved_headers() == {"X-Api-Key": "k"}


def test_args_accept_a_shell_string():
    assert ConnectionConfig(command="uvx", args='mcp-server-git --repository "/tmp/a b"').args == [
        "mcp-server-git",
        "--repository",
        "/tmp/a b",
    ]


def test_store_imports_mcp_servers_block(tmp_path: Path):
    store = ServerStore(tmp_path / "servers.json")
    servers = store.import_mcp_json(
        {
            "mcpServers": {
                "git": {"command": "uvx", "args": ["mcp-server-git"]},
                "remote": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer z"}},
            }
        }
    )
    by_name = {s.name: s.config for s in servers}
    assert by_name["git"].transport == "stdio"
    assert by_name["remote"].transport == "streamable-http"
    # The row survives the import so you can see the server wants it; the
    # credential does not -- a preset never stores an Authorization value.
    assert by_name["remote"].resolved_headers() == {"Authorization": ""}
    assert {s.name for s in store.load()} == {"git", "remote"}


# --------------------------------------------------------------------- e2e


async def test_status_starts_idle(client):
    assert (await client.get("/api/status")).json()["status"] == "idle"


async def test_operations_refuse_without_a_connection(client):
    response = await client.post("/api/tools/list", json={})
    assert response.status_code == 409
    assert response.json()["error"]["kind"] == "not_connected"


async def test_tools_round_trip(connected):
    listed = (await connected.post("/api/tools/list", json={})).json()
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert {"add", "echo", "show_headers"} <= names

    called = (await connected.post("/api/tools/call", json={"name": "add", "arguments": {"a": 2, "b": 40}})).json()
    assert called["result"]["structuredContent"] == {"result": 42.0}
    assert called["result"]["isError"] is False


async def test_resources_and_prompts(connected):
    resources = (await connected.post("/api/resources/list", json={})).json()["result"]["resources"]
    assert any(r["uri"] == "demo://time" for r in resources)

    templates = (await connected.post("/api/resources/templates/list", json={})).json()["result"]
    assert any(t["uriTemplate"] == "demo://greeting/{name}" for t in templates["resourceTemplates"])

    read = (await connected.post("/api/resources/read", json={"uri": "demo://greeting/Ada"})).json()
    assert read["result"]["contents"][0]["text"] == "Hello, Ada!"

    prompts = (await connected.post("/api/prompts/list", json={})).json()["result"]["prompts"]
    assert prompts[0]["name"] == "review_code"

    got = (await connected.post("/api/prompts/get", json={"name": "review_code", "arguments": {"code": "x=1"}})).json()
    assert "x=1" in got["result"]["messages"][0]["content"]["text"]


async def test_ping_and_raw_request(legacy):
    assert (await legacy.post("/api/ping", json={})).json()["result"] == {}

    raw = (await legacy.post("/api/request", json={"method": "tools/list", "params": {}})).json()
    assert "tools" in raw["result"]

    unknown = (await legacy.post("/api/request", json={"method": "no/such"})).json()
    assert unknown["error"]["code"] == -32601


async def test_traffic_is_recorded(legacy):
    await legacy.post("/api/ping", json={})
    events = (await legacy.get("/api/history", params={"kinds": "message"})).json()["events"]
    methods = [e["message"].get("method") for e in events if e["direction"] == "out"]
    assert "initialize" in methods
    assert "ping" in methods


async def test_stdio_stderr_is_captured(connected):
    await connected.post("/api/tools/call", json={"name": "log_to_stderr", "arguments": {"text": "probe"}})
    events = (await connected.get("/api/history", params={"kinds": "stderr"})).json()["events"]
    assert any(e["text"] == "probe" for e in events)


async def test_roots_are_served_to_the_server(legacy):
    await legacy.post("/api/roots", json={"roots": [{"uri": "file:///tmp/example", "name": "example"}]})
    called = (await legacy.post("/api/tools/call", json={"name": "show_roots", "arguments": {}})).json()
    assert called["result"]["structuredContent"]["result"] == ["file:///tmp/example"]


async def test_tool_errors_surface_as_is_error(connected):
    called = (await connected.post("/api/tools/call", json={"name": "boom", "arguments": {}})).json()
    assert called["result"]["isError"] is True


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/static/styles.css"])
async def test_ui_assets_are_always_revalidated(client, path):
    """Without this, Chrome happily runs yesterday's UI after an update."""
    response = await client.get(path)
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-cache"


async def test_saved_presets_round_trip(client):
    await client.post("/api/servers", json={"name": "demo", "config": {"transport": "stdio", "command": "echo"}})
    assert [s["name"] for s in (await client.get("/api/servers")).json()["servers"]] == ["demo"]
    assert (await client.delete("/api/servers/demo")).json()["servers"] == []


# ------------------------------------------------------------ error logging


async def _errors(client) -> list[dict]:
    return (await client.get("/api/history", params={"kinds": "error"})).json()["events"]


async def test_a_refused_call_is_logged(client):
    """A failure used to live only in the HTTP response nobody kept."""
    await client.post("/api/tools/list", json={})
    logged = await _errors(client)
    assert [(e["reason"], e["source"]) for e in logged] == [("not_connected", "POST /api/tools/list")]
    assert "not connected" in logged[0]["text"]


async def test_a_bad_request_is_logged(client):
    await client.post("/api/tools/call", json={"arguments": {}})  # no name
    logged = await _errors(client)
    assert logged[-1]["reason"] == "bad_request"
    assert "tool name is required" in logged[-1]["text"]


async def test_a_json_rpc_error_is_logged_with_its_code(legacy):
    answered = (await legacy.post("/api/request", json={"method": "no/such"})).json()
    assert answered["error"]["code"] == -32601

    logged = await _errors(legacy)
    assert logged[-1]["reason"] == "mcp"
    assert "-32601" in logged[-1]["text"]
    assert logged[-1]["source"] == "POST /api/request"


async def test_errors_reach_the_log_stream_live(app, client):
    """The panel is fed by the socket, so the event has to be published, not just stored."""
    queue = app.state.inspector.bus.subscribe()
    await client.post("/api/resources/read", json={})  # no uri
    event = queue.get_nowait()
    assert event["kind"] == "error" and event["reason"] == "bad_request"


async def test_a_failed_connection_is_logged(client):
    response = await client.post(
        "/api/connect",
        json={"transport": "stdio", "command": "definitely-not-a-real-command-xyz"},
        timeout=30,
    )
    assert response.json()["error"]["kind"] == "connect"
    # The connection task reports the cause itself; the log must carry it.
    transport = (await client.get("/api/history", params={"kinds": "transport"})).json()["events"]
    assert any(e["level"] == "error" for e in transport)


async def test_a_tool_that_answers_is_error_is_logged(connected):
    called = (await connected.post("/api/tools/call", json={"name": "boom", "arguments": {}})).json()
    assert called["result"]["isError"] is True

    logged = await _errors(connected)
    assert logged[-1]["reason"] == "tool_error"
    assert logged[-1]["source"] == "tools/call boom"
    assert "boom" in logged[-1]["text"]


async def test_a_successful_call_logs_nothing(connected):
    await connected.post("/api/tools/call", json={"name": "add", "arguments": {"a": 1, "b": 1}})
    assert await _errors(connected) == []


# --------------------------------------------------- protocol version choice


def test_unknown_protocol_version_is_rejected():
    with pytest.raises(ValueError, match="unknown protocol version"):
        ConnectionConfig(protocol_version="1999-01-01")


def test_negotiation_era_per_version():
    assert ConnectionConfig(protocol_version="auto").negotiation_era() == "auto"
    assert ConnectionConfig(protocol_version="2026-07-28").negotiation_era() == "modern"
    assert ConnectionConfig(protocol_version="2025-06-18").negotiation_era() == "handshake"


async def test_meta_lists_selectable_versions(client):
    versions = (await client.get("/api/meta")).json()["protocol_versions"]
    assert versions[0] == {"value": "auto", "era": "auto"}
    assert {"value": "2026-07-28", "era": "modern"} in versions
    assert {"value": "2024-11-05", "era": "handshake"} in versions


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
async def test_handshake_versions_are_offered_verbatim(client, version):
    """The pinned revision must reach the wire, not just the SDK's newest."""
    status = await _connect(client, version)
    assert status["protocol_version"] == version
    assert status["negotiation_era"] == "handshake"

    events = (await client.get("/api/history", params={"kinds": "message"})).json()["events"]
    offered = [
        e["message"]["params"]["protocolVersion"]
        for e in events
        if e["direction"] == "out" and e["message"].get("method") == "initialize"
    ]
    assert offered == [version]
    await client.post("/api/disconnect", json={}, timeout=30)


async def test_modern_version_uses_discover(client):
    status = await _connect(client, "2026-07-28")
    assert status["protocol_version"] == "2026-07-28"
    assert status["negotiation_era"] == "modern"

    events = (await client.get("/api/history", params={"kinds": "message"})).json()["events"]
    methods = [e["message"].get("method") for e in events if e["direction"] == "out"]
    assert "server/discover" in methods
    assert "initialize" not in methods
    await client.post("/api/disconnect", json={}, timeout=30)


async def test_auto_reports_what_it_landed_on(client):
    status = await _connect(client, "auto")
    assert status["requested_protocol_version"] == "auto"
    assert status["protocol_version"] in ("2026-07-28", "2025-11-25")
    await client.post("/api/disconnect", json={}, timeout=30)


async def test_pinning_an_old_version_hides_newer_tools(client):
    """A 2024-era session still lists tools, which is the point of pinning."""
    await _connect(client, "2024-11-05")
    names = {t["name"] for t in (await client.post("/api/tools/list", json={})).json()["result"]["tools"]}
    assert "add" in names
    await client.post("/api/disconnect", json={}, timeout=30)


# ------------------------------------------------- counter-offer handling


class FakeSession:
    """Just enough ClientSession to drive `_handshake_at` without a server."""

    def __init__(self, answers: str):
        self._answers = answers
        self.offered: str | None = None
        self.adopted: types.InitializeResult | None = None
        self.notifications: list[str] = []
        self._client_info = types.Implementation(name="test", version="0")

    def _build_capabilities(self, version: str) -> types.ClientCapabilities:
        return types.ClientCapabilities()

    async def send_request(self, request, result_type):
        params = request.model_dump(by_alias=True, mode="json", exclude_none=True)["params"]
        self.offered = params["protocolVersion"]
        return types.InitializeResult(
            protocolVersion=self._answers,
            capabilities=types.ServerCapabilities(),
            serverInfo=types.Implementation(name="fake", version="1"),
        )

    def adopt(self, result):
        self.adopted = result

    @property
    def initialize_result(self):
        return self.adopted

    @property
    def discover_result(self):
        return None

    @property
    def protocol_version(self):
        return self.adopted.protocol_version if self.adopted else None

    @property
    def server_info(self):
        return self.adopted.server_info if self.adopted else None

    @property
    def server_capabilities(self):
        return self.adopted.capabilities if self.adopted else None

    @property
    def instructions(self):
        return None

    async def send_notification(self, notification):
        self.notifications.append(type(notification).__name__)


def _connection(version: str) -> InspectorConnection:
    return InspectorConnection(ConnectionConfig(protocol_version=version), EventBus())


async def test_handshake_offers_the_requested_version():
    connection = _connection("2024-11-05")
    session = FakeSession(answers="2024-11-05")
    await connection._handshake_at(session, "2024-11-05")
    assert session.offered == "2024-11-05"
    assert session.adopted is not None
    assert session.notifications == ["InitializedNotification"]


async def test_counter_offer_is_reported_not_fatal():
    connection = _connection("2024-11-05")
    session = FakeSession(answers="2025-06-18")
    await connection._negotiate(session)
    assert connection.negotiated_version == "2025-06-18"
    warnings_logged = [
        e["text"] for e in connection.bus.history({"transport"}) if e.get("level") == "warning"
    ]
    assert any("requested protocol 2024-11-05" in text for text in warnings_logged)


async def test_answer_outside_the_handshake_era_is_rejected():
    connection = _connection("2025-06-18")
    session = FakeSession(answers="2026-07-28")
    with pytest.raises(RuntimeError, match="unsupported protocol version"):
        await connection._handshake_at(session, "2025-06-18")
