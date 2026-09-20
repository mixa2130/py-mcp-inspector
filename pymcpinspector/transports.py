"""Opening an MCP client transport from a :class:`ConnectionConfig`."""

from __future__ import annotations

import asyncio
import os
import ssl
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from .models import ConnectionConfig


class LiveHeaders:
    """A handle on the headers of the HTTP clients a connection is using.

    The clients are built once, when the transport opens, but a bearer token
    has a lifetime of its own and usually a shorter one than the session it
    authenticates. httpx merges `client.headers` into every request as that
    request is built, so swapping them here reaches the next request without
    tearing the transport down and handshaking again.
    """

    def __init__(self) -> None:
        self._clients: list[httpx2.AsyncClient] = []

    def track(self, client: httpx2.AsyncClient) -> None:
        self._clients.append(client)

    def forget(self) -> None:
        """Called as the transport closes; the clients are about to go away."""
        self._clients.clear()

    @property
    def live(self) -> bool:
        return bool(self._clients)

    def replace(self, headers: Mapping[str, str], *, drop: Iterable[str] = ()) -> None:
        """Make `headers` the set sent from the next request onwards.

        `drop` names headers that were being sent and no longer are -- renaming
        the auth header would otherwise leave the old one going out beside it.
        """
        for client in self._clients:
            for name in drop:
                if name in client.headers:
                    del client.headers[name]
            for name, value in headers.items():
                client.headers[name] = value


def _require_file(path: str, label: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise ValueError(f"{label} not found: {resolved}")
    if not os.access(resolved, os.R_OK):
        raise ValueError(f"{label} is not readable: {resolved}")
    return resolved


def build_ssl_context(cfg: ConnectionConfig) -> ssl.SSLContext | bool:
    """Turn the TLS fields into something httpx2 accepts as `verify=`.

    `True` keeps httpx2's own default (the system trust store). Anything the
    user configured needs a real context, because httpx2 deprecated both
    `verify=<path>` and `cert=`.

    Raises:
        ValueError: A configured path is missing, unreadable, or rejected by
            OpenSSL -- reported before the connection is attempted.
    """
    if not cfg.uses_custom_tls():
        return True
    if cfg.client_key and not cfg.client_cert:
        raise ValueError("a client key was given without a client certificate")

    if not cfg.verify_tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif cfg.ca_bundle:
        # Replacing the trust store, as curl's --cacert and requests' verify= do.
        ca = _require_file(cfg.ca_bundle, "CA bundle")
        try:
            context = (
                ssl.create_default_context(capath=str(ca))
                if ca.is_dir()
                else ssl.create_default_context(cafile=str(ca))
            )
        except ssl.SSLError as exc:
            raise ValueError(f"CA bundle {ca} could not be loaded: {exc}") from exc
    else:
        context = httpx2.create_ssl_context(verify=True)

    if cfg.client_cert:
        cert = _require_file(cfg.client_cert, "Client certificate")
        key = _require_file(cfg.client_key, "Client key") if cfg.client_key else None
        try:
            context.load_cert_chain(
                certfile=str(cert),
                keyfile=str(key) if key else None,
                # Never None: OpenSSL prompts on the server's terminal for an
                # encrypted key, which would hang the inspector.
                password=cfg.client_key_password or "",
            )
        except (ssl.SSLError, OSError) as exc:
            raise ValueError(f"client certificate could not be loaded: {_cert_hint(cfg, exc)}") from exc

    return context


def _cert_hint(cfg: ConnectionConfig, exc: Exception) -> str:
    """Turn OpenSSL's terse errors into something actionable."""
    if not cfg.client_key and not cfg.client_key_password:
        return f"{exc} (no key file given -- is the private key inside the certificate file?)"
    if not cfg.client_key_password:
        return f"{exc} (if the key is encrypted, fill in the passphrase)"
    return f"{exc} (check the passphrase)"


def _event_hooks(
    on_http: Callable[[dict[str, Any]], None] | None,
    on_session_id: Callable[[str], None] | None,
) -> dict[str, list[Any]]:
    """httpx hooks that mirror the HTTP exchange into the inspector's log."""
    if on_http is None and on_session_id is None:
        return {}

    async def response_hook(response: httpx2.Response) -> None:
        request = response.request
        if on_http is not None:
            on_http(
                {
                    "method": request.method,
                    "url": str(request.url),
                    "status": response.status_code,
                    "request_headers": _redacted(dict(request.headers)),
                    "response_headers": dict(response.headers),
                }
            )
        session_id = response.headers.get("mcp-session-id")
        if session_id and on_session_id is not None:
            on_session_id(session_id)

    return {"response": [response_hook]}


SENSITIVE = {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key"}


def _redacted(headers: dict[str, str]) -> dict[str, str]:
    """Keep secrets out of the persistent event log, but show they were sent."""
    return {
        key: (f"<{len(value)} chars hidden>" if key.lower() in SENSITIVE else value)
        for key, value in headers.items()
    }


def _http_client(
    cfg: ConnectionConfig,
    headers: dict[str, str],
    verify: ssl.SSLContext | bool,
    on_http: Callable[[dict[str, Any]], None] | None,
    on_session_id: Callable[[str], None] | None,
) -> httpx2.AsyncClient:
    """An httpx2 client carrying the user's headers and TLS/timeout choices."""
    return httpx2.AsyncClient(
        headers=headers,
        timeout=httpx2.Timeout(cfg.http_timeout, read=cfg.sse_read_timeout),
        verify=verify,
        event_hooks=_event_hooks(on_http, on_session_id),
    )


@asynccontextmanager
async def _stderr_pipe(on_line: Callable[[str], None]) -> AsyncIterator[Any]:
    """A writable file object for a subprocess' stderr, streamed to ``on_line``.

    The child needs a real file descriptor, so this is an OS pipe drained by a
    reader thread rather than a Python file-like shim.
    """
    read_fd, write_fd = os.pipe()
    write_file = os.fdopen(write_fd, "w", buffering=1, errors="replace")
    loop = asyncio.get_running_loop()
    done = threading.Event()

    def pump() -> None:
        try:
            with os.fdopen(read_fd, "r", errors="replace") as reader:
                for line in reader:
                    loop.call_soon_threadsafe(on_line, line.rstrip("\n"))
        except (OSError, ValueError):  # pipe torn down under us
            pass
        finally:
            done.set()

    thread = threading.Thread(target=pump, name="mcp-stderr", daemon=True)
    thread.start()
    try:
        yield write_file
    finally:
        try:
            write_file.close()
        except OSError:
            pass
        await asyncio.to_thread(done.wait, 2.0)


@asynccontextmanager
async def open_transport(
    cfg: ConnectionConfig,
    *,
    on_stderr: Callable[[str], None],
    on_session_id: Callable[[str], None] | None = None,
    on_http: Callable[[dict[str, Any]], None] | None = None,
    live_headers: LiveHeaders | None = None,
) -> AsyncIterator[tuple[Any, Any]]:
    """Yield the ``(read_stream, write_stream)`` pair for the configured transport.

    ``live_headers``, if given, is handed the HTTP clients this opens, so the
    caller can swap an expiring credential while the connection stays up.
    """
    if cfg.transport == "stdio":
        if not cfg.command.strip():
            raise ValueError("stdio transport needs a command")
        # The SDK already layers a minimal safe environment underneath; inheriting
        # adds the inspector's own environment on top of that.
        env = cfg.resolved_env()
        if cfg.inherit_env:
            env = {**os.environ, **env}
        params = StdioServerParameters(
            command=cfg.command.strip(),
            args=list(cfg.args),
            env=env,
            cwd=cfg.cwd or None,
        )
        async with _stderr_pipe(on_stderr) as errlog:
            async with stdio_client(params, errlog=errlog) as streams:
                yield streams
        return

    if not cfg.url.strip():
        raise ValueError(f"{cfg.transport} transport needs a URL")
    headers = cfg.resolved_headers()
    verify = build_ssl_context(cfg)

    if cfg.transport == "sse":
        def factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
        ) -> httpx2.AsyncClient:
            client = httpx2.AsyncClient(
                headers=headers,
                timeout=timeout or httpx2.Timeout(cfg.http_timeout, read=cfg.sse_read_timeout),
                auth=auth,
                verify=verify,
                event_hooks=_event_hooks(on_http, None),
            )
            if live_headers is not None:
                live_headers.track(client)
            return client

        try:
            async with sse_client(
                cfg.url.strip(),
                headers=headers,
                timeout=cfg.http_timeout,
                sse_read_timeout=cfg.sse_read_timeout,
                httpx_client_factory=factory,
                on_session_created=on_session_id,
            ) as streams:
                yield streams
        finally:
            if live_headers is not None:
                live_headers.forget()
        return

    async with _http_client(cfg, headers, verify, on_http, on_session_id) as client:
        if live_headers is not None:
            live_headers.track(client)
        try:
            async with streamable_http_client(cfg.url.strip(), http_client=client) as streams:
                yield streams
        finally:
            if live_headers is not None:
                live_headers.forget()
