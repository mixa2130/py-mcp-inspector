"""Editing saved presets, and rendering them as something to hand to someone else."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pymcpinspector.app import create_app
from pymcpinspector.models import ConnectionConfig, SavedServer
from pymcpinspector.store import ServerStore, export_servers, redact, without_auth_secret

HTTP = {
    "transport": "streamable-http",
    "url": "https://api.example.com/mcp",
    "bearer_token": "sekrit",
    "headers": [
        {"name": "X-Trace-Id", "value": "abc123", "enabled": True},
        {"name": "X-Api-Key", "value": "kkk", "enabled": True},
    ],
    "verify_tls": False,
    "request_timeout": 90.0,
}
STDIO = {
    "transport": "stdio",
    "command": "uvx",
    "args": ["mcp-server-git"],
    "env": [
        {"name": "GITHUB_TOKEN", "value": "ghp_x", "enabled": True},
        {"name": "LOG_LEVEL", "value": "debug", "enabled": True},
    ],
}


@pytest.fixture
def store(tmp_path: Path) -> ServerStore:
    store = ServerStore(tmp_path / "servers.json")
    store.upsert("prod", ConnectionConfig.model_validate(HTTP))
    store.upsert("git", ConnectionConfig.model_validate(STDIO))
    return store


@pytest.fixture
async def client(store: ServerStore):
    app = create_app(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://inspector"
    ) as client:
        yield client


# ------------------------------------------------------------------ renaming


def test_renaming_keeps_the_configuration(store: ServerStore):
    before = {s.name: s.config for s in store.load()}["prod"]
    store.rename("prod", "production")
    after = {s.name: s.config for s in store.load()}
    assert "prod" not in after
    assert after["production"] == before


def test_renaming_re_sorts_the_list(store: ServerStore):
    assert [s.name for s in store.rename("prod", "alpha")] == ["alpha", "git"]


def test_renaming_onto_an_existing_name_is_refused(store: ServerStore):
    """Names are a preset's only handle; merging two would lose one of them."""
    with pytest.raises(ValueError, match="already exists"):
        store.rename("prod", "git")
    assert {s.name for s in store.load()} == {"prod", "git"}


def test_renaming_something_that_is_not_there_says_so(store: ServerStore):
    with pytest.raises(ValueError, match="no preset named 'ghost'"):
        store.rename("ghost", "x")


def test_renaming_to_the_same_name_is_a_no_op(store: ServerStore):
    assert [s.name for s in store.rename("git", "git")] == ["git", "prod"]


def test_a_blank_new_name_is_refused(store: ServerStore):
    with pytest.raises(ValueError, match="name is required"):
        store.rename("git", "   ")


async def test_rename_over_the_api(client, store: ServerStore):
    response = await client.post("/api/servers/prod/rename", json={"name": "production"})
    assert response.status_code == 200
    assert [s["name"] for s in response.json()["servers"]] == ["git", "production"]
    assert {s.name for s in store.load()} == {"git", "production"}


async def test_rename_reports_a_clash_as_a_bad_request(client):
    response = await client.post("/api/servers/prod/rename", json={"name": "git"})
    assert response.status_code == 400
    assert "already exists" in response.json()["error"]["message"]


async def test_saving_over_a_name_replaces_that_preset(client, store: ServerStore):
    await client.post("/api/servers", json={"name": "prod", "config": {**HTTP, "url": "https://new/mcp"}})
    assert len(store.load()) == 2, "editing in place must not add a second entry"
    assert {s.name: s.config.url for s in store.load()}["prod"] == "https://new/mcp"


# ------------------------------------------- the generator a header row keeps


def test_a_preset_remembers_how_a_header_value_is_minted(tmp_path: Path):
    """The value travels as text; the format says what refresh will mint next time."""
    store = ServerStore(tmp_path / "s.json")
    store.upsert("prod", ConnectionConfig(
        url="https://x/mcp",
        headers=[{"name": "X-Request-Id", "value": "0195f0a1-2b3c-7def-8123-456789abcdef",
                  "generator": "uuid7", "enabled": True}],
    ))
    row = store.load()[0].config.headers[0]
    assert row.generator == "uuid7"
    assert row.value == "0195f0a1-2b3c-7def-8123-456789abcdef"


def test_a_typed_header_row_has_no_generator(store: ServerStore):
    assert [h.generator for h in {s.name: s.config for s in store.load()}["prod"].headers] == ["", ""]


def test_a_generator_this_build_does_not_know_still_loads(tmp_path: Path):
    """A preset written by a newer UI must not vanish from an older one."""
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"servers": [{"name": "prod", "config": {
        "url": "https://x/mcp",
        "headers": [{"name": "X-Request-Id", "value": "v", "generator": "uuid9000", "enabled": True}],
    }}]}), encoding="utf-8")
    loaded = ServerStore(path).load()
    assert [s.name for s in loaded] == ["prod"]
    assert loaded[0].config.headers[0].generator == "uuid9000"


def test_a_generated_authorization_value_is_still_cleared(tmp_path: Path):
    """Where the value came from does not make it less of a credential."""
    store = ServerStore(tmp_path / "s.json")
    store.upsert("prod", ConnectionConfig(
        url="https://x/mcp",
        headers=[{"name": "Authorization", "value": "Bearer abc", "generator": "uuid4", "enabled": True}],
    ))
    row = store.load()[0].config.headers[0]
    assert (row.value, row.generator) == ("", "uuid4")


def test_the_portable_format_says_a_generator_is_left_behind(tmp_path: Path):
    """`mcpServers` carries flat name/value pairs, so the value freezes as it is."""
    store = ServerStore(tmp_path / "s.json")
    store.upsert("prod", ConnectionConfig(
        url="https://x/mcp",
        headers=[{"name": "X-Request-Id", "value": "abc", "generator": "uuid4", "enabled": True}],
    ))
    result = export_servers(store.load(), portable=True)
    assert result["dropped"] == ["prod: X-Request-Id generator (uuid4)"]
    assert json.loads(result["json"])["mcpServers"]["prod"]["headers"] == {"X-Request-Id": "abc"}


def test_the_native_format_carries_the_generator(tmp_path: Path):
    store = ServerStore(tmp_path / "s.json")
    store.upsert("prod", ConnectionConfig(
        url="https://x/mcp",
        headers=[{"name": "X-Request-Id", "value": "abc", "generator": "uuid4", "enabled": True}],
    ))
    exported = json.loads(export_servers(store.load())["json"])
    assert exported["servers"][0]["config"]["headers"][0]["generator"] == "uuid4"


# ----------------------------------------------- the Authorization credential


def test_saving_a_preset_keeps_no_token(store: ServerStore):
    """A token expires in hours; the preset holding it is kept for months."""
    assert {s.name: s.config for s in store.load()}["prod"].bearer_token == ""


def test_saving_keeps_the_header_name_and_scheme(tmp_path: Path):
    """Neither is a secret, and together they say what has to be filled in."""
    store = ServerStore(tmp_path / "s.json")
    store.upsert("prod", ConnectionConfig(
        url="https://x/mcp", auth_header_name="X-Api-Key", auth_scheme="", bearer_token="k",
    ))
    saved = store.load()[0].config
    assert (saved.auth_header_name, saved.auth_scheme, saved.bearer_token) == ("X-Api-Key", "", "")


def test_an_authorization_header_row_is_kept_but_emptied(tmp_path: Path):
    store = ServerStore(tmp_path / "s.json")
    store.upsert("prod", ConnectionConfig(
        url="https://x/mcp",
        headers=[
            {"name": "authorization", "value": "Token abc", "enabled": True},
            {"name": "X-Trace-Id", "value": "keep-me", "enabled": True},
        ],
    ))
    rows = [(h.name, h.value) for h in store.load()[0].config.headers]
    assert rows == [("authorization", ""), ("X-Trace-Id", "keep-me")]


def test_importing_an_mcp_block_stores_no_authorization_value(tmp_path: Path):
    store = ServerStore(tmp_path / "s.json")
    store.import_mcp_json(
        {"mcpServers": {"r": {"url": "https://x/mcp", "headers": {"Authorization": "Bearer z"}}}}
    )
    assert store.load()[0].config.resolved_headers() == {"Authorization": ""}


def test_renaming_does_not_resurrect_a_credential(store: ServerStore):
    store.rename("prod", "production")
    assert {s.name: s.config for s in store.load()}["production"].bearer_token == ""


def test_without_auth_secret_leaves_the_caller_config_alone(tmp_path: Path):
    """The live connection keeps using the token the store refused to write."""
    config = ConnectionConfig(url="https://x/mcp", bearer_token="sekrit")
    assert without_auth_secret(config).bearer_token == ""
    assert config.bearer_token == "sekrit"
    assert config.resolved_headers() == {"Authorization": "Bearer sekrit"}


def test_other_header_secrets_are_still_written(store: ServerStore):
    """Only `Authorization` is refused on write; the rest is the export's job."""
    headers = {h.name: h.value for h in {s.name: s.config for s in store.load()}["prod"].headers}
    assert headers["X-Api-Key"] == "kkk"


# ------------------------------------------------------------------ redacting


def test_redact_clears_credentials_and_names_them():
    clean, cleared = redact(ConnectionConfig.model_validate(HTTP))
    assert clean.bearer_token == ""
    assert cleared == ["token", "header X-Api-Key"]


def test_redact_keeps_the_headers_a_recipient_has_to_fill_in():
    clean, _ = redact(ConnectionConfig.model_validate(HTTP))
    assert [(h.name, h.value) for h in clean.headers] == [("X-Trace-Id", "abc123"), ("X-Api-Key", "")]


def test_redact_leaves_ordinary_values_alone():
    clean, cleared = redact(ConnectionConfig.model_validate(STDIO))
    assert {e.name: e.value for e in clean.env} == {"GITHUB_TOKEN": "", "LOG_LEVEL": "debug"}
    assert cleared == ["env GITHUB_TOKEN"]


def test_redact_clears_a_key_passphrase():
    _, cleared = redact(ConnectionConfig(url="http://x", client_key_password="hunter2"))
    assert cleared == ["client key passphrase"]


def test_redact_does_not_touch_the_original():
    config = ConnectionConfig.model_validate(HTTP)
    redact(config)
    assert config.bearer_token == "sekrit"
    assert config.headers[1].value == "kkk"


# ------------------------------------------------------------------ exporting


def _exported(store: ServerStore, **kwargs) -> dict:
    result = export_servers(store.load(), **kwargs)
    return {**result, "data": json.loads(result["json"])}


def test_the_native_format_round_trips_through_import(tmp_path: Path, store: ServerStore):
    exported = export_servers(store.load(), include_secrets=True)["json"]
    other = ServerStore(tmp_path / "other.json")
    other.save_all([SavedServer.model_validate(e) for e in json.loads(exported)["servers"]])
    assert other.load() == store.load()


def test_the_portable_format_is_an_mcpservers_block(store: ServerStore):
    result = _exported(store, portable=True, include_secrets=True)
    assert result["data"]["mcpServers"]["git"] == {
        "command": "uvx",
        "args": ["mcp-server-git"],
        "env": {"GITHUB_TOKEN": "ghp_x", "LOG_LEVEL": "debug"},
    }
    assert result["data"]["mcpServers"]["prod"]["type"] == "http"
    # Even asked for secrets, there is no token to emit: the store never kept one.
    assert "Authorization" not in result["data"]["mcpServers"]["prod"]["headers"]


def test_the_portable_format_reimports(tmp_path: Path, store: ServerStore):
    exported = _exported(store, portable=True, include_secrets=True)
    other = ServerStore(tmp_path / "other.json")
    other.import_mcp_json(exported["data"])
    back = {s.name: s.config for s in other.load()}
    assert back["prod"].transport == "streamable-http"
    assert back["prod"].url == "https://api.example.com/mcp"
    assert back["git"].command == "uvx" and back["git"].args == ["mcp-server-git"]


def test_an_sse_preset_keeps_its_transport_through_the_portable_format(tmp_path: Path):
    store = ServerStore(tmp_path / "s.json")
    store.upsert("stream", ConnectionConfig(transport="sse", url="https://x/sse"))
    other = ServerStore(tmp_path / "other.json")
    other.import_mcp_json(json.loads(export_servers(store.load(), portable=True)["json"]))
    assert other.load()[0].config.transport == "sse"


def test_export_blanks_secrets_by_default(store: ServerStore):
    result = _exported(store)
    assert "sekrit" not in result["json"] and "ghp_x" not in result["json"]
    # No `prod: token` here -- saving already dropped it, so there is nothing left to blank.
    assert result["redacted"] == ["git: env GITHUB_TOKEN", "prod: header X-Api-Key"]


def test_a_portable_entry_still_shows_where_an_auth_header_goes(tmp_path: Path):
    """Dropping the header entirely would read as 'this server needs no auth'.

    Only a preset whose auth was written as a header row can say this: one that
    used the Token field keeps `auth_header_name`, which the portable format has
    no slot for.
    """
    store = ServerStore(tmp_path / "s.json")
    store.upsert("prod", ConnectionConfig(
        transport="streamable-http",
        url="https://x/mcp",
        headers=[{"name": "Authorization", "value": "Bearer sekrit", "enabled": True}],
    ))
    entry = json.loads(export_servers(store.load(), portable=True)["json"])["mcpServers"]["prod"]
    assert entry["headers"] == {"Authorization": ""}


def test_the_portable_format_names_what_it_cannot_carry(store: ServerStore):
    assert _exported(store, portable=True)["dropped"] == ["prod: verify_tls", "prod: request_timeout"]


def test_the_native_format_drops_nothing(store: ServerStore):
    assert _exported(store)["dropped"] == []


def test_exporting_a_subset(store: ServerStore):
    result = export_servers(store.select(["git"]), include_secrets=True)
    assert [s["name"] for s in json.loads(result["json"])["servers"]] == ["git"]


async def test_export_over_the_api(client):
    result = (await client.post("/api/servers/export", json={"names": ["prod"]})).json()
    assert [s["name"] for s in json.loads(result["json"])["servers"]] == ["prod"]
    assert "sekrit" not in result["json"]


async def test_export_with_no_body_takes_everything(client):
    result = (await client.post("/api/servers/export", json={})).json()
    assert len(json.loads(result["json"])["servers"]) == 2


async def test_exporting_an_unknown_preset_says_which(client):
    response = await client.post("/api/servers/export", json={"names": ["ghost"]})
    assert response.status_code == 400
    assert "no preset named 'ghost'" in response.json()["error"]["message"]


async def test_names_must_be_a_list(client):
    response = await client.post("/api/servers/export", json={"names": "prod"})
    assert response.status_code == 400
    assert "must be a list" in response.json()["error"]["message"]
