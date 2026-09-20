"""Rendering one MCP request as a ``curl`` command.

The point is a command you can paste into a terminal, a ticket or a message to
whoever runs the server, and have it reproduce what the inspector sends --
protocol headers, TLS material and the per-request envelope included. Anything
that cannot be reproduced faithfully is said out loud in ``notes`` rather than
quietly left out.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from mcp.shared.inbound import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    MCP_METHOD_HEADER,
    MCP_NAME_HEADER,
    MCP_PROTOCOL_VERSION_HEADER,
    NAME_BEARING_METHODS,
    PROTOCOL_VERSION_META_KEY,
    encode_header_value,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from .models import ConnectionConfig
from .store import SECRET_NAME

SESSION_HEADER = "mcp-session-id"

PROTOCOL_HEADERS = {
    "accept",
    "content-type",
    SESSION_HEADER,
    MCP_PROTOCOL_VERSION_HEADER,
    MCP_METHOD_HEADER,
    MCP_NAME_HEADER,
}
"""Headers the transport sets itself, which win over anything in the editor."""


def _var_name(header: str) -> str:
    """A shell variable name for a header whose value is being masked."""
    return "MCP_" + re.sub(r"[^A-Za-z0-9]+", "_", header).strip("_").upper()


def _header_arg(name: str, value: str) -> str:
    """``-H 'Name: value'``, single-quoted so the shell leaves it alone."""
    return f"-H {shlex.quote(f'{name}: {value}')}"


def _masked_header_arg(name: str, variable: str, prefix: str = "") -> str:
    """``-H "Name: prefix $VAR"`` -- double-quoted so the shell expands it."""
    return f'-H "{name}: {prefix}${variable}"'


def _routing_headers(method: str, params: dict[str, Any]) -> dict[str, str]:
    """The `mcp-method` / `mcp-name` pair a 2026-07-28 request is routed by.

    The server cross-checks both against the body and answers -32020 on a
    mismatch, so a curl that carries the envelope but not these headers is
    refused before it reaches the method at all.
    """
    headers = {MCP_METHOD_HEADER: method}
    name_key = NAME_BEARING_METHODS.get(method)
    if name_key is not None and isinstance(name := params.get(name_key), str):
        # Non-ASCII and edge whitespace would not survive an HTTP field, so the
        # spec's `=?base64?...?=` sentinel is used for anything but plain ASCII.
        headers[MCP_NAME_HEADER] = encode_header_value(name)
    return headers


def _modern_envelope(version: str, cfg: ConnectionConfig, capabilities: dict[str, Any]) -> dict[str, Any]:
    """The `_meta` block every 2026-07-28 request carries.

    `protocolVersion` alone is what routes a request as modern; without
    `clientCapabilities` beside it the server answers INVALID_PARAMS naming the
    missing key, so both go in even when capabilities are empty.
    """
    return {
        PROTOCOL_VERSION_META_KEY: version,
        CLIENT_INFO_META_KEY: {
            "name": cfg.client_name or "pymcpinspector",
            "version": cfg.client_version or "0.1.0",
            "title": "PyMCPinspector",
        },
        CLIENT_CAPABILITIES_META_KEY: capabilities,
    }


def _tls_args(cfg: ConnectionConfig, mask_secrets: bool, notes: list[str], masked: list[str]) -> list[str]:
    args: list[str] = []
    if not cfg.verify_tls:
        args.append("--insecure")
        notes.append("--insecure mirrors the cleared 'Verify the server certificate' box.")
    if cfg.ca_bundle:
        # curl splits what the inspector keeps in one field, so the choice is
        # made the same way `build_ssl_context` makes it: by asking the disk.
        ca = Path(cfg.ca_bundle).expanduser()
        args.append(f"{'--capath' if ca.is_dir() else '--cacert'} {shlex.quote(cfg.ca_bundle)}")
        if not ca.exists():
            notes.append(
                f"{cfg.ca_bundle} is not on this machine, so --cacert was assumed; a hashed "
                "certificate directory needs --capath instead."
            )
    if cfg.client_cert:
        args.append(f"--cert {shlex.quote(cfg.client_cert)}")
    if cfg.client_key:
        args.append(f"--key {shlex.quote(cfg.client_key)}")
    if cfg.client_key_password:
        if mask_secrets:
            args.append('--pass "$MCP_KEY_PASSPHRASE"')
            masked.append("MCP_KEY_PASSPHRASE")
        else:
            args.append(f"--pass {shlex.quote(cfg.client_key_password)}")
    return args


def _headers(
    cfg: ConnectionConfig,
    session_id: str | None,
    protocol_version: str | None,
    extra: dict[str, str],
    mask_secrets: bool,
    notes: list[str],
    masked: list[str],
) -> list[str]:
    """Every `-H` the request carries, protocol headers last so they read as final."""
    args: list[str] = []
    auth_name = (cfg.auth_header_name or "Authorization").strip()
    token = cfg.bearer_token.strip()
    scheme = cfg.auth_scheme.strip()

    overridden: list[str] = []
    for name, value in cfg.resolved_headers().items():
        if name.lower() in PROTOCOL_HEADERS:
            overridden.append(name)
            continue
        is_auth = name.lower() == auth_name.lower() and bool(token)
        if mask_secrets and (is_auth or SECRET_NAME.search(name)):
            variable = _var_name(name)
            # The scheme is not the secret, so it stays readable.
            args.append(_masked_header_arg(name, variable, f"{scheme} " if is_auth and scheme else ""))
            masked.append(variable)
        else:
            args.append(_header_arg(name, value))

    if overridden:
        notes.append(
            f"{', '.join(overridden)} came from the header editor but the transport sets it per request, "
            "so the inspector's own value is what goes out; it is left out here for the same reason."
        )

    args.append(_header_arg("Accept", "application/json, text/event-stream"))
    args.append(_header_arg("Content-Type", "application/json"))
    if session_id:
        args.append(_header_arg(SESSION_HEADER, session_id))
    else:
        notes.append(
            f"No {SESSION_HEADER}: the server hands one out on `initialize` (or `server/discover`), and most "
            "servers answer anything else without it with 400. Connect first, or run the handshake by hand."
        )
    if protocol_version:
        args.append(_header_arg(MCP_PROTOCOL_VERSION_HEADER, protocol_version))
    args += [_header_arg(name, value) for name, value in extra.items()]
    return args


def build_curl(
    cfg: ConnectionConfig,
    *,
    method: str,
    params: dict[str, Any] | None = None,
    session_id: str | None = None,
    protocol_version: str | None = None,
    capabilities: dict[str, Any] | None = None,
    param_headers: dict[str, str] | None = None,
    endpoint: str | None = None,
    request_id: int | str = 1,
    mask_secrets: bool = True,
) -> dict[str, Any]:
    """Render one JSON-RPC request as a runnable ``curl`` invocation.

    Args:
        cfg: The connection whose headers, TLS material and timeouts to carry.
        method: The JSON-RPC method, e.g. ``tools/call``.
        params: Its params, before any protocol envelope is added.
        session_id: The live ``mcp-session-id``, when there is one.
        protocol_version: The negotiated revision; decides the envelope.
        capabilities: The client capabilities to stamp into a modern envelope.
        param_headers: `Mcp-Param-*` headers a `tools/call` mirrors from its
            arguments, which only the tool's declared schema can supply.
        endpoint: Where to POST. Needed for SSE, whose message endpoint is
            announced by the server and is not the URL you connect to.
        mask_secrets: Replace credentials with shell variables.

    Returns:
        ``command``, plus ``notes`` (what could not be reproduced faithfully)
        and ``masked`` (the shell variables that have to be set first).

    Raises:
        ValueError: The transport is stdio, or the method is blank.
    """
    if not method.strip():
        raise ValueError("a JSON-RPC method is required")
    if not cfg.is_http():
        raise ValueError(
            "stdio speaks JSON-RPC over the child process' pipes, so there is no HTTP request "
            "to reproduce; curl has nothing to talk to"
        )

    notes: list[str] = []
    masked: list[str] = []
    extra: dict[str, str] = {}
    body_params = dict(params or {})

    if protocol_version in MODERN_PROTOCOL_VERSIONS:
        extra.update(_routing_headers(method, body_params))
        extra.update(param_headers or {})
        body_params["_meta"] = {
            **_modern_envelope(protocol_version, cfg, capabilities or {}),
            **body_params.get("_meta", {}),
        }
        if capabilities is None:
            notes.append(
                "Client capabilities in the envelope are empty: they are built from the live session, "
                "and this command was rendered without one."
            )
        if method == "tools/call" and param_headers is None:
            notes.append(
                "Any Mcp-Param-* headers this tool declares are missing: they come from the tool's "
                "schema, which is only known to a live session that has listed it."
            )
        if method == "ping":
            notes.append(
                f"`ping` was removed in {protocol_version}; a server on that revision answers -32601."
            )
        if method == "initialize":
            notes.append(
                f"`initialize` does not exist at {protocol_version}; that revision opens with "
                "`server/discover` instead, and a server on it will answer -32601."
            )
    elif protocol_version is None:
        notes.append(
            "No negotiated protocol version, so no per-request envelope was added. A 2026-07-28 server "
            "needs one and will answer INVALID_PARAMS without it."
        )

    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if body_params:
        body["params"] = body_params

    target = endpoint or cfg.url.strip()
    if cfg.transport == "sse":
        if endpoint:
            notes.append(
                "SSE posts messages to the endpoint the server announced on the stream, not to the URL "
                "you connect to; the address below was taken from the inspector's HTTP log."
            )
        else:
            notes.append(
                "SSE posts messages to an endpoint the server announces on the stream, which this "
                f"inspector has not seen yet -- {target} is the stream URL and will not accept a POST. "
                "Send one request from the inspector first, then generate this again."
            )

    args = [f"curl -sS -X POST {shlex.quote(target)}"]
    args += _headers(cfg, session_id, protocol_version, extra, mask_secrets, notes, masked)
    args += _tls_args(cfg, mask_secrets, notes, masked)
    args.append(f"--connect-timeout {cfg.http_timeout:g}")
    args.append(f"--max-time {cfg.sse_read_timeout:g}")
    args.append(f"-d {shlex.quote(json.dumps(body, ensure_ascii=False))}")

    if masked:
        notes.insert(0, f"Set these before running: {', '.join(dict.fromkeys(masked))}.")
    notes.append(
        "The response may be an SSE stream rather than JSON, which is why Accept lists both; "
        "--max-time is the transport's read timeout, so a long stream will be cut off by it."
    )

    return {
        "command": " \\\n  ".join(args),
        "notes": notes,
        "masked": list(dict.fromkeys(masked)),
    }
