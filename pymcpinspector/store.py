"""Persistence for named connection presets."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .models import ConnectionConfig, SavedServer

AUTH_HEADER = "authorization"
"""The header a saved preset never carries a value for. See `without_auth_secret`."""

SECRET_FIELDS = {
    "bearer_token": "token",
    "client_key_password": "client key passphrase",
}
"""Config fields that are always a credential, whatever they are called."""

SECRET_NAME = re.compile(
    r"auth|token|secret|passwo?rd|api[-_]?key|access[-_]?key|credential|cookie|session",
    re.IGNORECASE,
)
"""Header and environment names whose *value* is presumed to be a credential.

A heuristic, so `redact` reports every name it blanked rather than leaving the
caller to trust it: `X-Trace-Id` survives, `X-Api-Key` does not.
"""


def default_config_path() -> Path:
    override = os.environ.get("PYMCPINSPECTOR_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pymcpinspector" / "servers.json"


def without_auth_secret(config: ConnectionConfig) -> ConnectionConfig:
    """A copy of `config` carrying no `Authorization` credential.

    A preset outlives the token in it: bearer tokens are minted for hours and
    presets are kept for months, so storing one buys a stale value on the next
    load and leaves a live credential sitting in a plain JSON file meanwhile.

    Both routes to that credential are cleared -- the Token field and a header
    row spelled `Authorization` -- while the header name, the scheme and the
    row itself stay, because they say what has to be filled in and none of them
    is a secret.
    """
    clean = config.model_copy(deep=True)
    clean.bearer_token = ""
    for item in clean.headers:
        if item.name.strip().lower() == AUTH_HEADER:
            item.value = ""
    return clean


class ServerStore:
    """A flat, human-editable JSON file of saved servers."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_path()

    def load(self) -> list[SavedServer]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        servers = raw.get("servers", []) if isinstance(raw, dict) else raw
        result: list[SavedServer] = []
        for entry in servers or []:
            try:
                result.append(SavedServer.model_validate(entry))
            except Exception:  # noqa: BLE001 - skip entries we cannot read
                continue
        return result

    def save_all(self, servers: list[SavedServer]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"servers": [s.model_dump(mode="json") for s in servers]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def upsert(self, name: str, config: ConnectionConfig) -> list[SavedServer]:
        servers = [s for s in self.load() if s.name != name]
        servers.append(SavedServer(name=name, config=without_auth_secret(config)))
        servers.sort(key=lambda s: s.name.lower())
        self.save_all(servers)
        return servers

    def delete(self, name: str) -> list[SavedServer]:
        servers = [s for s in self.load() if s.name != name]
        self.save_all(servers)
        return servers

    def rename(self, name: str, new_name: str) -> list[SavedServer]:
        """Move a preset to a new name, keeping its configuration.

        Raises:
            ValueError: The preset is gone, the new name is blank, or something
                else already answers to it -- names are the only handle a
                preset has, so silently merging two would lose one of them.
        """
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("a preset name is required")
        servers = self.load()
        found = next((s for s in servers if s.name == name), None)
        if found is None:
            raise ValueError(f"no preset named {name!r}")
        if new_name == name:
            return servers
        if any(s.name == new_name for s in servers):
            raise ValueError(f"a preset named {new_name!r} already exists")
        found.name = new_name
        servers.sort(key=lambda s: s.name.lower())
        self.save_all(servers)
        return servers

    def select(self, names: list[str] | None) -> list[SavedServer]:
        """The named presets in stored order, or all of them.

        Raises:
            ValueError: A requested name is not in the store.
        """
        servers = self.load()
        if names is None:
            return servers
        known = {s.name: s for s in servers}
        missing = [name for name in names if name not in known]
        if missing:
            raise ValueError(f"no preset named {', '.join(repr(m) for m in missing)}")
        return [known[name] for name in names]

    def import_mcp_json(self, data: dict[str, Any]) -> list[SavedServer]:
        """Ingest a Claude/VS Code style ``mcpServers`` block."""
        servers = {s.name: s for s in self.load()}
        entries = data.get("mcpServers") or data.get("servers") or {}
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            config = _config_from_mcp_entry(entry)
            if config is not None:
                servers[name] = SavedServer(name=name, config=without_auth_secret(config))
        ordered = sorted(servers.values(), key=lambda s: s.name.lower())
        self.save_all(ordered)
        return ordered


def redact(config: ConnectionConfig) -> tuple[ConnectionConfig, list[str]]:
    """A copy of `config` with its credentials blanked, and what was blanked.

    Names are kept and only values cleared: the point of a shared preset is
    that the person on the other end knows which secrets to go and fill in.

    Returns:
        The redacted copy, and a human-readable label per cleared value.
    """
    clean = config.model_copy(deep=True)
    cleared: list[str] = []
    for field, label in SECRET_FIELDS.items():
        if getattr(clean, field, ""):
            setattr(clean, field, "")
            cleared.append(label)
    for kind, items in (("header", clean.headers), ("env", clean.env)):
        for item in items:
            if item.value and SECRET_NAME.search(item.name):
                item.value = ""
                cleared.append(f"{kind} {item.name.strip()}")
    return clean, cleared


# Everything an `mcpServers` entry has no room for. Sharing in that format is a
# trade -- portable into Claude Desktop and VS Code, but only the fields those
# clients understand survive, so the ones being left behind get named.
PORTABLE_FIELDS = {
    "transport", "url", "headers", "auth_header_name", "auth_scheme", "bearer_token",
    "command", "args", "env", "cwd",
}


def _dropped_by_mcp_format(config: ConnectionConfig) -> list[str]:
    """Configured fields that an `mcpServers` entry cannot carry."""
    defaults = ConnectionConfig()
    dropped = []
    for field in type(config).model_fields:
        if field in PORTABLE_FIELDS:
            continue
        if getattr(config, field) != getattr(defaults, field):
            dropped.append(field)
    # `headers` is portable, but only as flat name/value pairs: the format a row
    # mints its value in has nowhere to live in an `mcpServers` entry, so the
    # value travels as the frozen string it happens to hold right now.
    for item in config.headers:
        if item.generator:
            dropped.append(f"{item.name.strip() or 'unnamed header'} generator ({item.generator})")
    return dropped


def _mcp_entry(config: ConnectionConfig, *, blank_auth_header: str | None = None) -> dict[str, Any]:
    """One `mcpServers` entry: the shape Claude Desktop and VS Code read.

    `blank_auth_header` names an auth header whose token was redacted away. It
    is listed with an empty value regardless, so the entry still says a
    credential belongs there -- dropping it entirely would read as "no auth".
    """
    if config.transport == "stdio":
        entry: dict[str, Any] = {"command": config.command, "args": list(config.args)}
        env = config.resolved_env()
        if env:
            entry["env"] = env
        if config.cwd:
            entry["cwd"] = config.cwd
        return entry
    # `mcpServers` has no separate credential field, so the auth header is
    # folded into `headers` -- which is where it ends up on the wire anyway.
    entry = {"type": "sse" if config.transport == "sse" else "http", "url": config.url}
    headers = config.resolved_headers()
    if blank_auth_header:
        headers[blank_auth_header] = ""
    if headers:
        entry["headers"] = headers
    return entry


def export_servers(
    servers: list[SavedServer],
    *,
    portable: bool = False,
    include_secrets: bool = False,
) -> dict[str, Any]:
    """Render presets as shareable JSON.

    Args:
        servers: What to export, in the order they should appear.
        portable: Emit an `mcpServers` block other MCP clients read, instead of
            the inspector's own lossless format.
        include_secrets: Keep tokens and passphrases in the output.

    Returns:
        `json` (the text to hand over), `redacted` (labels of the cleared
        values) and `dropped` (fields the portable format cannot carry).
    """
    redacted: list[str] = []
    dropped: list[str] = []
    prepared: list[tuple[SavedServer, str | None]] = []
    for saved in servers:
        config = saved.config
        blank_auth: str | None = None
        if not include_secrets:
            if config.bearer_token.strip():
                blank_auth = (config.auth_header_name or "Authorization").strip()
            config, cleared = redact(config)
            redacted += [f"{saved.name}: {label}" for label in cleared]
        if portable:
            dropped += [f"{saved.name}: {field}" for field in _dropped_by_mcp_format(config)]
        prepared.append((SavedServer(name=saved.name, config=config), blank_auth))

    if portable:
        payload: dict[str, Any] = {
            "mcpServers": {s.name: _mcp_entry(s.config, blank_auth_header=blank) for s, blank in prepared}
        }
    else:
        payload = {"servers": [s.model_dump(mode="json") for s, _ in prepared]}

    return {
        "json": json.dumps(payload, indent=2, ensure_ascii=False),
        "redacted": redacted,
        "dropped": dropped,
    }


def _config_from_mcp_entry(entry: dict[str, Any]) -> ConnectionConfig | None:
    url = entry.get("url")
    headers = [{"name": k, "value": v} for k, v in (entry.get("headers") or {}).items()]
    if url:
        kind = entry.get("type") or entry.get("transport") or "streamable-http"
        transport = "sse" if str(kind).lower() in ("sse", "http-sse") else "streamable-http"
        return ConnectionConfig(transport=transport, url=url, headers=headers)
    command = entry.get("command")
    if command:
        return ConnectionConfig(
            transport="stdio",
            command=command,
            args=entry.get("args") or [],
            env=[{"name": k, "value": v} for k, v in (entry.get("env") or {}).items()],
            cwd=entry.get("cwd"),
        )
    return None
