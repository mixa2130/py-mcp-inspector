"""Configuration models for an inspector connection."""

from __future__ import annotations

import shlex
from typing import Any, Literal

from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel, ConfigDict, Field, field_validator

TransportKind = Literal["stdio", "sse", "streamable-http"]

AUTO_VERSION = "auto"
"""Let the SDK negotiate: probe `server/discover`, fall back to the `initialize` handshake."""

SELECTABLE_PROTOCOL_VERSIONS: tuple[str, ...] = (
    AUTO_VERSION,
    *reversed(MODERN_PROTOCOL_VERSIONS),
    *reversed(HANDSHAKE_PROTOCOL_VERSIONS),
)
"""What the UI offers, newest first, with auto-negotiation on top."""

LOG_LEVELS = ("debug", "info", "notice", "warning", "error", "critical", "alert", "emergency")


class KeyValue(BaseModel):
    """One header or environment entry, toggleable from the UI."""

    name: str = ""
    value: str = ""
    enabled: bool = True


class HeaderItem(KeyValue):
    """A header row, which may remember how its value is minted."""

    generator: str = ""
    """Which generator the UI refills this row with, e.g. `uuid4`; empty means a typed value.

    Carried so a preset remembers the choice, not acted on here: the value is
    generated in the browser, on the viewer's click, and arrives already filled
    in. The vocabulary therefore belongs to the UI, and an unknown name is kept
    verbatim rather than rejected -- a preset written by a newer build must not
    become unloadable in an older one.
    """


class RootItem(BaseModel):
    uri: str
    name: str | None = None


class ConnectionConfig(BaseModel):
    """Everything needed to open (and re-open) a server connection."""

    model_config = ConfigDict(extra="ignore")

    transport: TransportKind = "streamable-http"

    # --- HTTP transports (sse, streamable-http) -------------------------------
    url: str = ""
    headers: list[HeaderItem] = Field(default_factory=list)
    auth_header_name: str = "Authorization"
    auth_scheme: str = "Bearer"
    bearer_token: str = ""
    verify_tls: bool = True
    ca_bundle: str = ""
    client_cert: str = ""
    client_key: str = ""
    client_key_password: str = ""
    http_timeout: float = 30.0
    sse_read_timeout: float = 300.0

    # --- the script that fetches the credential (see `plugins`) ---------------
    auth_plugin: str = ""
    auth_plugin_args: list[str] = Field(default_factory=list)

    # --- stdio transport ------------------------------------------------------
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: list[KeyValue] = Field(default_factory=list)
    cwd: str | None = None
    inherit_env: bool = True

    # --- session --------------------------------------------------------------
    client_name: str = "pymcpinspector"
    client_version: str = "0.1.0"
    request_timeout: float = 60.0
    protocol_version: str = AUTO_VERSION
    log_level: str | None = None
    roots: list[RootItem] = Field(default_factory=list)

    @field_validator("args", "auth_plugin_args", mode="before")
    @classmethod
    def _split_args(cls, value: Any) -> Any:
        """Accept either a list or a shell-ish string from the UI."""
        if isinstance(value, str):
            return shlex.split(value)
        return value

    @field_validator("ca_bundle", "client_cert", "client_key", "auth_plugin", mode="before")
    @classmethod
    def _tidy_path(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("protocol_version", mode="before")
    @classmethod
    def _known_version(cls, value: Any) -> Any:
        if value in (None, "", AUTO_VERSION):
            return AUTO_VERSION
        if value not in SELECTABLE_PROTOCOL_VERSIONS:
            raise ValueError(
                f"unknown protocol version {value!r}; pick one of {', '.join(SELECTABLE_PROTOCOL_VERSIONS)}"
            )
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _blank_level_is_none(cls, value: Any) -> Any:
        if value in ("", "none"):
            return None
        if value is not None and value not in LOG_LEVELS:
            raise ValueError(f"unknown log level {value!r}")
        return value

    # -- derived ---------------------------------------------------------------

    def negotiation_era(self) -> str:
        """Which negotiation path `protocol_version` selects."""
        if self.protocol_version == AUTO_VERSION:
            return "auto"
        return "modern" if self.protocol_version in MODERN_PROTOCOL_VERSIONS else "handshake"

    def is_http(self) -> bool:
        return self.transport in ("sse", "streamable-http")

    def uses_custom_tls(self) -> bool:
        """Whether anything beyond httpx's default trust store is configured."""
        return bool(self.ca_bundle or self.client_cert or self.client_key) or not self.verify_tls

    def tls_summary(self) -> dict[str, Any]:
        """What the UI shows about the TLS setup. Never includes the passphrase."""
        return {
            "verify": self.verify_tls,
            "ca_bundle": self.ca_bundle or None,
            "client_cert": self.client_cert or None,
            "client_key": self.client_key or None,
            "client_key_encrypted": bool(self.client_key_password),
        }

    def resolved_headers(self) -> dict[str, str]:
        """The final header set: explicit headers plus the auth header."""
        result: dict[str, str] = {}
        for item in self.headers:
            if item.enabled and item.name.strip():
                result[item.name.strip()] = item.value
        if self.bearer_token.strip():
            name = (self.auth_header_name or "Authorization").strip()
            scheme = self.auth_scheme.strip()
            token = self.bearer_token.strip()
            # Header names are case-insensitive, so a row spelled `authorization`
            # means the same header as `Authorization` and has to be replaced
            # rather than left to go out as a second copy beside it.
            for clash in [key for key in result if key.lower() == name.lower()]:
                del result[clash]
            result[name] = f"{scheme} {token}" if scheme else token
        return result

    def header_warnings(self) -> list[str]:
        """Header rows the editor shows but `resolved_headers` does not send.

        Both cases are silent otherwise: the request simply goes out without
        the header, which looks from the server's side like the inspector
        ignored it.
        """
        if not self.is_http():
            return []
        notes: list[str] = []
        for position, item in enumerate(self.headers, start=1):
            if item.enabled and item.value and not item.name.strip():
                notes.append(f"header row {position} has a value but no name; it was not sent")
        if self.bearer_token.strip():
            auth = (self.auth_header_name or "Authorization").strip()
            clashing = [
                i.name.strip()
                for i in self.headers
                if i.enabled and i.name.strip().lower() == auth.lower()
            ]
            if clashing:
                notes.append(
                    f"the Authentication token replaced the {clashing[0]!r} row in the header list"
                )
        return notes

    def resolved_env(self) -> dict[str, str]:
        return {i.name.strip(): i.value for i in self.env if i.enabled and i.name.strip()}

    def describe(self) -> str:
        if self.transport == "stdio":
            return " ".join([self.command, *self.args]).strip()
        return self.url


class SavedServer(BaseModel):
    """A named connection preset stored on disk."""

    name: str
    config: ConnectionConfig
