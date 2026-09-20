"""The script that fetches the credential, run for real.

Nothing here fakes the subprocess: every case writes a script and lets the
inspector start it, because the parts worth doubting -- which interpreter is
used, what the child sees in its environment, what a non-zero exit looks like
from outside -- are exactly the parts a stub would paper over.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from pymcpinspector.app import create_app
from pymcpinspector.models import ConnectionConfig
from pymcpinspector.plugins import command_for, environment_for, parse_output, run_auth_plugin
from pymcpinspector.store import ServerStore, export_servers, without_auth_secret


def script(tmp_path: Path, body: str, *, name: str = "token.py", executable: bool = False) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    if executable:
        path.chmod(0o755)
    return path


def config(path: Path, args: str = "", **extra) -> ConnectionConfig:
    return ConnectionConfig.model_validate(
        {"url": "https://api.example.com/mcp", "auth_plugin": str(path), "auth_plugin_args": args, **extra}
    )


# ------------------------------------------------------------------- running


async def test_a_script_that_prints_a_token(tmp_path: Path):
    path = script(tmp_path, "print('t0k3n')\n")
    result = await run_auth_plugin(config(path))
    assert result.token == "t0k3n"
    assert result.notes == []


async def test_arguments_reach_the_script(tmp_path: Path):
    path = script(tmp_path, "import sys; print('|'.join(sys.argv[1:]))\n")
    result = await run_auth_plugin(config(path, '--profile prod --scope "a b"'))
    assert result.token == "--profile|prod|--scope|a b"


async def test_the_script_is_told_where_the_connection_points(tmp_path: Path):
    path = script(tmp_path, "import os; print(os.environ['PYMCPINSPECTOR_URL'])\n")
    result = await run_auth_plugin(config(path))
    assert result.token == "https://api.example.com/mcp"


async def test_the_inspector_environment_is_inherited(tmp_path: Path, monkeypatch):
    """A script that shells out to `aws` or `op` needs the PATH it was given."""
    monkeypatch.setenv("PYMCPINSPECTOR_TEST_MARKER", "inherited")
    path = script(tmp_path, "import os; print(os.environ['PYMCPINSPECTOR_TEST_MARKER'])\n")
    result = await run_auth_plugin(config(path))
    assert result.token == "inherited"


async def test_the_live_credential_is_not_handed_to_the_script(tmp_path: Path):
    path = script(tmp_path, "import os; print([k for k in os.environ if 'TOKEN' in k] or 'none')\n")
    result = await run_auth_plugin(config(path, bearer_token="do-not-leak"))
    assert "do-not-leak" not in result.token


async def test_a_failing_script_reports_its_last_words(tmp_path: Path):
    path = script(tmp_path, "import sys; print('no session', file=sys.stderr); sys.exit(3)\n")
    with pytest.raises(ValueError, match=r"exited 3.*no session"):
        await run_auth_plugin(config(path))


async def test_a_silent_failure_still_says_what_happened(tmp_path: Path):
    path = script(tmp_path, "raise SystemExit(2)\n")
    with pytest.raises(ValueError, match="exited 2"):
        await run_auth_plugin(config(path))


async def test_a_crash_carries_the_traceback_line(tmp_path: Path):
    path = script(tmp_path, "raise RuntimeError('the vault is locked')\n")
    with pytest.raises(ValueError, match="the vault is locked"):
        await run_auth_plugin(config(path))


async def test_a_script_that_hangs_is_killed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("pymcpinspector.plugins.TIMEOUT", 0.3)
    path = script(tmp_path, "import time; time.sleep(30)\n")
    with pytest.raises(ValueError, match="did not finish"):
        await run_auth_plugin(config(path))


async def test_a_missing_script_says_so(tmp_path: Path):
    with pytest.raises(ValueError, match="no auth plugin at"):
        await run_auth_plugin(config(tmp_path / "gone.py"))


async def test_a_directory_is_not_a_script(tmp_path: Path):
    with pytest.raises(ValueError, match="is not a file"):
        await run_auth_plugin(config(tmp_path))


async def test_no_script_configured(tmp_path: Path):
    with pytest.raises(ValueError, match="no auth plugin is configured"):
        await run_auth_plugin(ConnectionConfig(url="https://x/mcp"))


async def test_every_step_is_narrated(tmp_path: Path):
    # A credential that shares no substring with the path, so "never logged"
    # means what it says rather than passing on a coincidence.
    path = script(tmp_path, "import sys; print('warming up', file=sys.stderr); print('zQ8-secret')\n")
    said: list[tuple[str, str]] = []
    result = await run_auth_plugin(config(path), lambda level, text: said.append((level, text)))
    assert result.token == "zQ8-secret"
    assert any("running" in text and str(path) in text for _, text in said)
    assert ("info", "stderr: warming up") in said
    assert any("10-character token" in text for _, text in said), said
    # The credential itself is the one thing the log must never carry.
    assert not any("zQ8-secret" in text for _, text in said), said


# ---------------------------------------------------------- which interpreter


def test_a_plain_script_runs_on_the_inspectors_interpreter(tmp_path: Path):
    path = script(tmp_path, "print('x')\n")
    assert command_for(config(path)) == [sys.executable, str(path)]


@pytest.mark.skipif(os.name == "nt", reason="the executable bit and shebangs are POSIX")
async def test_an_executable_script_runs_itself(tmp_path: Path):
    """So a shebang can point at the virtualenv the script actually needs."""
    path = script(tmp_path, f"#!{sys.executable}\nprint('from the shebang')\n", executable=True)
    assert command_for(config(path)) == [str(path)]
    result = await run_auth_plugin(config(path))
    assert result.token == "from the shebang"


def test_the_home_directory_shorthand_is_expanded(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = script(tmp_path, "print('x')\n")
    assert command_for(config(Path("~/token.py"))) == [sys.executable, str(path)]


def test_the_context_names_the_auth_header(tmp_path: Path):
    env = environment_for(config(tmp_path, auth_header_name="X-Api-Key", auth_scheme=""))
    assert env["PYMCPINSPECTOR_AUTH_HEADER"] == "X-Api-Key"
    assert env["PYMCPINSPECTOR_AUTH_SCHEME"] == ""
    assert env["PYMCPINSPECTOR_TRANSPORT"] == "streamable-http"


# --------------------------------------------------------- reading the output


def test_a_bare_token():
    assert parse_output("  abc123\n").token == "abc123"


def test_a_json_object():
    result = parse_output(json.dumps({"token": "abc", "scheme": "Token", "header": "X-Api-Key"}))
    assert (result.token, result.scheme, result.header) == ("abc", "Token", "X-Api-Key")
    assert result.notes == []


def test_a_json_object_says_which_keys_it_ignored():
    result = parse_output(json.dumps({"token": "abc", "expires_in": 3600}))
    assert result.token == "abc"
    assert result.notes == ["ignored key(s) the contract has no place for: expires_in"]


def test_a_json_object_without_a_token():
    with pytest.raises(ValueError, match="access_token, expires_in"):
        parse_output(json.dumps({"access_token": "abc", "expires_in": 3600}))


def test_chatter_before_the_token_is_taken_but_reported():
    result = parse_output("fetching...\nstill going\nabc123\n")
    assert result.token == "abc123"
    assert result.notes == ["stdout had 3 non-empty lines; the last one was taken as the token"]


def test_nothing_printed():
    with pytest.raises(ValueError, match="printed nothing"):
        parse_output("   \n\n")


def test_a_token_that_happens_to_be_digits():
    """`json.loads` reads `12345` as a number; it is still the token."""
    assert parse_output("12345").token == "12345"


# ------------------------------------------------------------ over the API


@pytest.fixture
async def client(tmp_path: Path):
    app = create_app(ServerStore(tmp_path / "servers.json"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://inspector"
    ) as client:
        yield client


async def test_the_endpoint_returns_the_token(client, tmp_path: Path):
    path = script(tmp_path, "print('over-the-wire')\n")
    response = await client.post("/api/auth/plugin", json=config(path).model_dump(mode="json"))
    assert response.status_code == 200
    assert response.json()["token"] == "over-the-wire"


async def test_the_endpoint_reports_a_failure_as_a_bad_request(client, tmp_path: Path):
    path = script(tmp_path, "import sys; sys.exit(1)\n")
    response = await client.post("/api/auth/plugin", json=config(path).model_dump(mode="json"))
    assert response.status_code == 400
    assert "exited 1" in response.json()["error"]["message"]


async def test_the_run_reaches_the_log(client, tmp_path: Path):
    path = script(tmp_path, "print('tok')\n")
    await client.post("/api/auth/plugin", json=config(path).model_dump(mode="json"))
    events = (await client.get("/api/history?limit=50")).json()["events"]
    plugin = [e for e in events if e["kind"] == "plugin"]
    assert plugin, events
    assert any(str(path) in e["text"] for e in plugin)
    assert not any("tok" == e["text"] for e in plugin)


# ------------------------------------------------------- the worked example

EXAMPLE = Path(__file__).parent.parent / "examples" / "oauth_token_plugin.py"

USER_TOKEN = "user-tok"
AGENT_TOKEN = "agent-tok"
EXCHANGE_SECRET = "s3cret"


@pytest.fixture
def provider():
    """A stand-in for the OAuth provider `examples/oauth_token_plugin.py` talks to.

    An example nobody runs rots quietly, so this drives the real file through
    the real plugin machinery -- a real subprocess against a real HTTP server
    -- and only the provider on the far end is make-believe.
    """
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            field = {name: values[0] for name, values in form.items()}
            seen.append({**field, "person": self.headers.get("X-Person-Id", "")})
            if field.get("grant_type") == "password":
                answer = ({"access_token": USER_TOKEN} if seen[-1]["person"]
                          else {"error": "no_person", "error_description": "X-Person-Id is required"})
            elif field.get("client_secret") != EXCHANGE_SECRET:
                answer = {"error": "unauthorized_client", "error_description": "bad client secret"}
            elif field.get("subject_token") != USER_TOKEN:
                answer = {"error": "invalid_grant", "error_description": "unknown subject token"}
            else:
                answer = {"access_token": AGENT_TOKEN}

            body = json.dumps(answer).encode()
            self.send_response(200 if "access_token" in answer else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            """Uvicorn-style silence: the test's output is the assertions."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/token", seen
    server.shutdown()


@pytest.fixture
def example(provider, monkeypatch):
    """The example, pointed at the stand-in and given its credentials."""
    url, seen = provider
    monkeypatch.setenv("TOKEN_URL_DEV", url)
    monkeypatch.setenv("TOKEN_PASSWORD", "hunter2")
    monkeypatch.setenv("TOKEN_EXCHANGE_SECRET", EXCHANGE_SECRET)
    return seen


def example_config(args: str) -> ConnectionConfig:
    return ConnectionConfig.model_validate(
        {"url": "https://api.example.com/mcp", "auth_plugin": str(EXAMPLE), "auth_plugin_args": args}
    )


async def test_the_example_fetches_a_user_token(example):
    result = await run_auth_plugin(example_config("--env dev --token user --person-id 42"))
    assert result.token == USER_TOKEN
    assert [call["grant_type"] for call in example] == ["password"]
    assert example[0]["person"] == "42", "the person id has to reach the provider as a header"


async def test_the_example_exchanges_it_for_an_agent_token(example):
    result = await run_auth_plugin(example_config("--env dev --token agent --person-id 42"))
    assert result.token == AGENT_TOKEN
    grants = [call["grant_type"] for call in example]
    assert grants == ["password", "urn:ietf:params:oauth:grant-type:token-exchange"]
    assert example[1]["subject_token"] == USER_TOKEN


async def test_the_example_needs_both_choices_made(example):
    """Neither the stand nor the kind of token has a default worth guessing."""
    with pytest.raises(ValueError, match="exited 2.*required.*--token"):
        await run_auth_plugin(example_config("--env dev --person-id 42"))
    with pytest.raises(ValueError, match="exited 2.*required.*--env"):
        await run_auth_plugin(example_config("--token user --person-id 42"))
    assert example == [], "nothing should have been asked of the provider"


async def test_the_example_stops_before_asking_without_credentials(provider, monkeypatch):
    url, seen = provider
    monkeypatch.setenv("TOKEN_URL_DEV", url)
    monkeypatch.delenv("TOKEN_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="нет учётных данных"):
        await run_auth_plugin(example_config("--env dev --token user --person-id 42"))
    assert seen == []


async def test_the_example_repeats_what_the_provider_said(provider, monkeypatch):
    url, _ = provider
    monkeypatch.setenv("TOKEN_URL_DEV", url)
    monkeypatch.setenv("TOKEN_PASSWORD", "hunter2")
    monkeypatch.setenv("TOKEN_EXCHANGE_SECRET", "wrong")
    with pytest.raises(ValueError, match="unauthorized_client.*bad client secret"):
        await run_auth_plugin(example_config("--env dev --token agent --person-id 42"))


async def test_the_example_carries_no_secret_of_its_own():
    """It is an example in a repository; the only credentials it knows are the environment's."""
    source = EXAMPLE.read_text(encoding="utf-8")
    assert 'os.environ.get("TOKEN_PASSWORD"' in source
    assert 'os.environ.get("TOKEN_EXCHANGE_SECRET"' in source
    assert "example.com" in source, "the stands it ships with must be nobody's"


# ------------------------------------------------------------------- presets


def test_a_preset_remembers_the_script_but_never_its_token(tmp_path: Path):
    store = ServerStore(tmp_path / "s.json")
    store.upsert("prod", config(script(tmp_path, "print('x')\n"), "--profile prod", bearer_token="fetched"))
    saved = store.load()[0].config
    assert saved.auth_plugin.endswith("token.py")
    assert saved.auth_plugin_args == ["--profile", "prod"]
    assert saved.bearer_token == ""


def test_the_portable_format_says_the_script_is_left_behind(tmp_path: Path):
    """`mcpServers` has nowhere to put it, and a silent drop would read as `no auth`."""
    store = ServerStore(tmp_path / "s.json")
    store.upsert("prod", config(script(tmp_path, "print('x')\n"), "--profile prod"))
    dropped = export_servers(store.load(), portable=True)["dropped"]
    assert "prod: auth_plugin" in dropped
    assert "prod: auth_plugin_args" in dropped


def test_without_auth_secret_keeps_the_script(tmp_path: Path):
    clean = without_auth_secret(config(tmp_path / "token.py", bearer_token="secret"))
    assert clean.auth_plugin.endswith("token.py")
    assert clean.bearer_token == ""
