"""FastAPI backend: a thin JSON wrapper around one live MCP connection."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import warnings
from pathlib import Path
from typing import Any

import mcp.types as types
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from mcp.shared.exceptions import MCPDeprecationWarning, MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import ValidationError

from . import __version__
from .connection import InspectorConnection, NotConnectedError, describe_exception, dump
from .curl import build_curl
from .events import EventBus
from .models import (
    LOG_LEVELS,
    SELECTABLE_PROTOCOL_VERSIONS,
    ConnectionConfig,
    RootItem,
)
from .plugins import run_auth_plugin
from .store import ServerStore, export_servers

logger = logging.getLogger("pymcpinspector")

STATIC_DIR = Path(__file__).parent / "static"

# Only `ETag`/`Last-Modified` leaves Chrome free to reuse a cached asset without
# asking, so an updated inspector can keep serving yesterday's UI. Revalidation
# is free over localhost, and correctness here is worth more than a saved 304.
NO_CACHE = {"Cache-Control": "no-cache"}


class RevalidatedStaticFiles(StaticFiles):
    """StaticFiles that asks the browser to revalidate every asset."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", NO_CACHE["Cache-Control"])
        return response


def _tool_error_text(result: Any) -> str:
    """The text a failing tool returned, flattened into one log line."""
    parts = [block.text for block in getattr(result, "content", []) or [] if getattr(block, "text", None)]
    return " ".join(" ".join(parts).split()) or "the tool reported isError with no text"


def _era(version: str) -> str:
    """Which negotiation path a selectable version uses."""
    if version == "auto":
        return "auto"
    return "modern" if version in MODERN_PROTOCOL_VERSIONS else "handshake"


class Inspector:
    """Holds the single active connection plus the shared event bus."""

    def __init__(self, store: ServerStore) -> None:
        self.bus = EventBus()
        self.store = store
        self.connection: InspectorConnection | None = None
        self.last_config: ConnectionConfig | None = None
        self._lock = asyncio.Lock()

    async def connect(self, config: ConnectionConfig) -> dict[str, Any]:
        async with self._lock:
            await self._disconnect_locked()
            for note in config.header_warnings():
                self.bus.publish("transport", {"level": "warning", "text": note})
            self.bus.publish("transport", {"level": "info", "text": f"connecting to {config.describe()}"})
            connection = InspectorConnection(config, self.bus)
            self.connection = connection
            self.last_config = config
            try:
                await connection.start()
            except Exception:
                self.connection = connection  # keep it so the UI can read `error`
                raise
            return connection.status_payload()

    async def disconnect(self) -> None:
        async with self._lock:
            await self._disconnect_locked()

    async def _disconnect_locked(self) -> None:
        if self.connection is not None:
            await self.connection.stop()
            self.connection = None

    def require(self) -> InspectorConnection:
        if self.connection is None or self.connection.status != "connected":
            raise NotConnectedError(
                self.connection.error if self.connection and self.connection.error else "not connected to a server"
            )
        return self.connection

    def status(self) -> dict[str, Any]:
        if self.connection is None:
            return {
                "status": "idle",
                "error": None,
                "transport": self.last_config.transport if self.last_config else None,
                "target": self.last_config.describe() if self.last_config else None,
                "pending": [],
            }
        return self.connection.status_payload()


def create_app(store: ServerStore | None = None) -> FastAPI:
    inspector = Inspector(store or ServerStore())
    app = FastAPI(title="PyMCPinspector", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.inspector = inspector

    # ----------------------------------------------------------- error shaping

    def report_error(reason: str, message: str, *, source: str, detail: Any = None, trace: bool = False) -> None:
        """Put a failure on the event bus and in the process log.

        A failed call used to exist only in the HTTP response that carried it,
        so whatever the UI did not paint was lost. The log panel is where you
        look when something misbehaves, so every error goes through here.
        """
        if trace:
            logger.exception("%s: %s", source, message)
        else:
            logger.error("%s: %s", source, message)
        inspector.bus.publish("error", {"reason": reason, "source": source, "text": message, "detail": detail})

    def _source(request: Any) -> str:
        """The endpoint a failure came from, e.g. `POST /api/tools/call`."""
        url = getattr(request, "url", None)
        method = getattr(request, "method", None)
        return f"{method} {url.path}" if url is not None and method else "inspector"

    @app.exception_handler(NotConnectedError)
    async def _not_connected(request: Request, exc: NotConnectedError) -> JSONResponse:
        report_error("not_connected", str(exc), source=_source(request))
        return JSONResponse(status_code=409, content={"error": {"message": str(exc), "kind": "not_connected"}})

    @app.exception_handler(MCPError)
    async def _mcp_error(request: Request, exc: MCPError) -> JSONResponse:
        report_error("mcp", f"JSON-RPC error {exc.code}: {exc.message}", source=_source(request), detail=exc.data)
        return JSONResponse(
            status_code=200,
            content={
                "error": {
                    "kind": "mcp",
                    "code": exc.code,
                    "message": exc.message,
                    "data": exc.data,
                }
            },
        )

    @app.exception_handler(ValidationError)
    async def _validation_error(request: Request, exc: ValidationError) -> JSONResponse:
        report_error("validation", str(exc), source=_source(request))
        return JSONResponse(status_code=400, content={"error": {"kind": "validation", "message": str(exc)}})

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError) -> JSONResponse:
        report_error("bad_request", str(exc), source=_source(request))
        return JSONResponse(status_code=400, content={"error": {"kind": "bad_request", "message": str(exc)}})

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Starlette re-raises after this returns, so uvicorn still prints the
        # traceback; the bus event is what makes the failure visible in the UI.
        message = describe_exception(exc)
        report_error("internal", message, source=_source(request), trace=True)
        return JSONResponse(status_code=500, content={"error": {"kind": "internal", "message": message}})

    # ----------------------------------------------------------------- meta

    @app.get("/api/meta")
    async def meta() -> dict[str, Any]:
        return {
            "version": __version__,
            "protocol_version": types.LATEST_PROTOCOL_VERSION,
            "log_levels": list(LOG_LEVELS),
            "protocol_versions": [
                {"value": value, "era": _era(value)}
                for value in SELECTABLE_PROTOCOL_VERSIONS
            ],
            "config_path": str(inspector.store.path),
        }

    # ----------------------------------------------------------- connection

    @app.post("/api/connect")
    async def connect(config: ConnectionConfig) -> dict[str, Any]:
        try:
            return await inspector.connect(config)
        except (ConnectionError, OSError) as exc:
            # `start()` already raises with the described cause; don't wrap it twice.
            detail = str(exc) or describe_exception(exc)
            return JSONResponse(  # type: ignore[return-value]
                status_code=200,
                content={"error": {"kind": "connect", "message": detail}, "status": inspector.status()},
            )

    @app.post("/api/disconnect")
    async def disconnect() -> dict[str, Any]:
        await inspector.disconnect()
        return inspector.status()

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return inspector.status()

    @app.post("/api/auth")
    async def refresh_auth(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Replace the credential the live connection sends, without reconnecting.

        Bodies are `{"token": ..., "scheme"?: ..., "header"?: ...}`; an omitted
        `scheme` or `header` keeps what the connection was opened with, and an
        empty token stops sending the header at all.
        """
        if "token" not in body:
            raise ValueError("token is required (pass an empty string to stop sending the header)")
        token = body["token"]
        scheme = body.get("scheme")
        header = body.get("header")
        for label, value in (("token", token), ("scheme", scheme), ("header", header)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{label} must be a string")
        connection = inspector.require()
        headers = connection.refresh_auth(token, scheme=scheme, header_name=header)
        return {"sent_headers": headers, "status": connection.status_payload()}

    @app.post("/api/auth/plugin")
    async def fetch_auth_token(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Run the configured script and hand back the credential it printed.

        The body is a connection config, the same shape `/api/connect` takes:
        the script is told where the connection points, and the answer comes
        back to the caller rather than being applied here. Filling the token in
        and deciding whether to send it is the caller's business -- the sidebar
        would otherwise show an empty Token field while a token went out.
        """
        config = ConnectionConfig.model_validate(body)
        result = await run_auth_plugin(
            config,
            lambda level, text: inspector.bus.publish("plugin", {"level": level, "text": text}),
        )
        return {
            "token": result.token,
            "scheme": result.scheme,
            "header": result.header,
            "notes": result.notes,
        }

    # ---------------------------------------------------------------- tools

    @app.post("/api/tools/list")
    async def list_tools(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        cursor = body.get("cursor")
        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
        result = await inspector.require().run(lambda s: s.list_tools(params=params))
        return {"result": dump(result)}

    @app.post("/api/tools/call")
    async def call_tool(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        name = body.get("name")
        if not name:
            raise ValueError("tool name is required")
        arguments = body.get("arguments") or {}
        timeout = body.get("timeout")
        started = time.perf_counter()

        connection = inspector.require()

        async def progress(progress: float, total: float | None, message: str | None) -> None:
            connection.bus.publish(
                "progress",
                {"tool": name, "progress": progress, "total": total, "message": message},
            )

        result = await connection.run(
            lambda s: s.call_tool(
                name,
                arguments,
                read_timeout_seconds=float(timeout) if timeout else None,
                progress_callback=progress,
            )
        )
        if getattr(result, "is_error", False):  # `isError` on the wire, snake_case on the model
            # A tool that answers `isError` is not a protocol failure, but it is
            # the failure the person at the keyboard is usually looking for.
            report_error("tool_error", _tool_error_text(result), source=f"tools/call {name}")
        return {"result": dump(result), "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}

    # ------------------------------------------------------------ resources

    @app.post("/api/resources/list")
    async def list_resources(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        cursor = body.get("cursor")
        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
        result = await inspector.require().run(lambda s: s.list_resources(params=params))
        return {"result": dump(result)}

    @app.post("/api/resources/templates/list")
    async def list_resource_templates(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        cursor = body.get("cursor")
        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
        result = await inspector.require().run(lambda s: s.list_resource_templates(params=params))
        return {"result": dump(result)}

    @app.post("/api/resources/read")
    async def read_resource(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        uri = body.get("uri")
        if not uri:
            raise ValueError("resource uri is required")
        result = await inspector.require().run(lambda s: s.read_resource(uri))
        return {"result": dump(result)}

    @app.post("/api/resources/subscribe")
    async def subscribe_resource(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        uri = body.get("uri")
        if not uri:
            raise ValueError("resource uri is required")
        unsubscribe = bool(body.get("unsubscribe"))
        connection = inspector.require()

        # resources/subscribe is a 2025-era method; the SDK warns about it rather
        # than removing it, and the inspector's job is to let you send it anyway.
        def call(session: Any) -> Any:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", MCPDeprecationWarning)
                return session.unsubscribe_resource(uri) if unsubscribe else session.subscribe_resource(uri)

        result = await connection.run(call)
        return {"result": dump(result)}

    # -------------------------------------------------------------- prompts

    @app.post("/api/prompts/list")
    async def list_prompts(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        cursor = body.get("cursor")
        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
        result = await inspector.require().run(lambda s: s.list_prompts(params=params))
        return {"result": dump(result)}

    @app.post("/api/prompts/get")
    async def get_prompt(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        name = body.get("name")
        if not name:
            raise ValueError("prompt name is required")
        arguments = {k: str(v) for k, v in (body.get("arguments") or {}).items()}
        result = await inspector.require().run(lambda s: s.get_prompt(name, arguments))
        return {"result": dump(result)}

    # ------------------------------------------------------------- misc ops

    @app.post("/api/ping")
    async def ping() -> dict[str, Any]:
        started = time.perf_counter()
        result = await inspector.require().run(lambda s: s.send_ping())
        return {"result": dump(result), "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}

    @app.post("/api/logging/level")
    async def set_level(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        level = body.get("level")
        if level not in LOG_LEVELS:
            raise ValueError(f"level must be one of {', '.join(LOG_LEVELS)}")
        connection = inspector.require()
        result = await connection.run(lambda s: s.set_logging_level(level))
        connection.log_level = level
        return {"result": dump(result)}

    @app.post("/api/complete")
    async def complete(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        ref_raw = body.get("ref") or {}
        argument = body.get("argument") or {}
        ref_type = ref_raw.get("type")
        if ref_type == "ref/prompt":
            ref: Any = types.PromptReference(type="ref/prompt", name=ref_raw.get("name", ""))
        elif ref_type == "ref/resource":
            ref = types.ResourceTemplateReference(type="ref/resource", uri=ref_raw.get("uri", ""))
        else:
            raise ValueError("ref.type must be 'ref/prompt' or 'ref/resource'")
        context_arguments = body.get("context_arguments") or None
        wanted = {"name": argument.get("name", ""), "value": argument.get("value", "")}
        result = await inspector.require().run(lambda s: s.complete(ref, wanted, context_arguments))
        return {"result": dump(result)}

    @app.post("/api/request")
    async def raw_request(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Send an arbitrary JSON-RPC request over the live session."""
        method = body.get("method")
        if not method:
            raise ValueError("method is required")
        params = body.get("params")
        if isinstance(params, str):
            params = json.loads(params) if params.strip() else None
        if params is not None and not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
        connection = inspector.require()

        async def send(session: Any) -> Any:
            dispatcher = getattr(session, "_dispatcher", None)
            if dispatcher is None:
                raise ValueError("this SDK build does not expose a raw dispatcher")
            return await dispatcher.send_raw_request(method, params, {})

        started = time.perf_counter()
        result = await connection.run(send)
        return {"result": result, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}

    def _client_capabilities(connection: InspectorConnection | None, version: str | None) -> dict[str, Any] | None:
        """What the live session offers, for a modern request's envelope.

        `None` rather than `{}` when there is no session to ask: the renderer
        says so in its notes instead of passing off an empty set as the truth.
        """
        session = connection.session if connection is not None else None
        build = getattr(session, "_build_capabilities", None)
        if build is None or version is None:
            return None
        try:
            return dump(build(version))
        except Exception:  # noqa: BLE001 - an envelope hint is not worth failing the render
            return None

    def _param_headers(
        connection: InspectorConnection | None, method: str, params: dict[str, Any] | None
    ) -> dict[str, str] | None:
        """`Mcp-Param-*` for a `tools/call`, which only a listed tool's schema defines.

        `None` when there is nothing to ask, so the renderer can say the headers
        may be missing rather than imply the tool declares none.
        """
        session = connection.session if connection is not None else None
        resolve = getattr(session, "_resolve_param_headers", None)
        if resolve is None or method != "tools/call" or not isinstance(params, dict):
            return None
        name = params.get("name")
        if not isinstance(name, str):
            return None
        try:
            return resolve(name, params.get("arguments") or {})
        except Exception:  # noqa: BLE001 - a routing hint is not worth failing the render
            return None

    def _message_endpoint(config: ConnectionConfig) -> str | None:
        """Where SSE actually posts messages, recovered from the HTTP log.

        The SDK learns it from an `endpoint` event on the stream and never hands
        it back, but every POST through it is in the exchange log, so the last
        one is the address.
        """
        if config.transport != "sse":
            return None
        posts = [e.get("url") for e in inspector.bus.history({"http"}) if e.get("method") == "POST"]
        return posts[-1] if posts else None

    @app.post("/api/curl")
    async def as_curl(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Render a JSON-RPC request as a `curl` carrying this connection's setup."""
        params = body.get("params")
        if isinstance(params, str):
            params = json.loads(params) if params.strip() else None
        if params is not None and not isinstance(params, dict):
            raise ValueError("params must be a JSON object")

        connection = inspector.connection
        config = connection.config if connection is not None else inspector.last_config
        if config is None:
            raise ValueError("configure a connection first: curl is rendered from its headers and TLS setup")

        version = connection.negotiated_version if connection is not None else None
        return build_curl(
            config,
            method=str(body.get("method") or ""),
            params=params,
            session_id=connection.http_session_id if connection is not None else None,
            protocol_version=version,
            capabilities=_client_capabilities(connection, version),
            param_headers=_param_headers(connection, str(body.get("method") or ""), params),
            endpoint=_message_endpoint(config),
            mask_secrets=body.get("mask_secrets") is not False,
        )

    @app.post("/api/roots")
    async def set_roots(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        roots = [RootItem.model_validate(r) for r in (body.get("roots") or [])]
        connection = inspector.require()
        connection.roots = roots
        connection.config.roots = roots
        notified = True
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", MCPDeprecationWarning)
                await connection.run(lambda s: s.send_roots_list_changed())
        except Exception as exc:  # noqa: BLE001 - the roots themselves are already updated
            notified = False
            connection.bus.publish(
                "transport",
                {"level": "warning", "text": f"roots/list_changed not delivered: {describe_exception(exc)}"},
            )
        return {"roots": [r.model_dump() for r in roots], "notified": notified}

    @app.post("/api/pending/{request_id}")
    async def answer_pending(request_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        connection = inspector.require()
        if not connection.resolve_pending(request_id, body):
            raise HTTPException(status_code=404, detail="no such pending request")
        return {"ok": True}

    # -------------------------------------------------------------- history

    @app.get("/api/history")
    async def history(kinds: str | None = None, limit: int = 500) -> dict[str, Any]:
        wanted = set(kinds.split(",")) if kinds else None
        return {"events": inspector.bus.history(wanted, limit)}

    @app.post("/api/history/clear")
    async def clear_history() -> dict[str, Any]:
        inspector.bus.clear()
        return {"ok": True}

    # -------------------------------------------------------- saved servers

    @app.get("/api/servers")
    async def list_servers() -> dict[str, Any]:
        return {"servers": [s.model_dump(mode="json") for s in inspector.store.load()]}

    @app.post("/api/servers")
    async def save_server(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        name = (body.get("name") or "").strip()
        if not name:
            raise ValueError("a preset name is required")
        config = ConnectionConfig.model_validate(body.get("config") or {})
        servers = inspector.store.upsert(name, config)
        return {"servers": [s.model_dump(mode="json") for s in servers]}

    @app.delete("/api/servers/{name}")
    async def delete_server(name: str) -> dict[str, Any]:
        servers = inspector.store.delete(name)
        return {"servers": [s.model_dump(mode="json") for s in servers]}

    @app.post("/api/servers/{name}/rename")
    async def rename_server(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Move a preset to another name, keeping its configuration."""
        servers = inspector.store.rename(name, str(body.get("name") or ""))
        return {"servers": [s.model_dump(mode="json") for s in servers]}

    @app.post("/api/servers/export")
    async def export_preset_json(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Render presets as JSON to hand to someone else.

        `names` picks which presets (all of them when omitted), `portable`
        chooses an `mcpServers` block over the inspector's own format, and
        `include_secrets` keeps tokens and passphrases in the output.
        """
        names = body.get("names")
        if names is not None and not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
            raise ValueError("names must be a list of preset names")
        return export_servers(
            inspector.store.select(names),
            portable=bool(body.get("portable")),
            include_secrets=bool(body.get("include_secrets")),
        )

    @app.post("/api/servers/import")
    async def import_servers(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        raw = body.get("json")
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object with an 'mcpServers' key")
        servers = inspector.store.import_mcp_json(data)
        return {"servers": [s.model_dump(mode="json") for s in servers]}

    # ------------------------------------------------------------ websocket

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        queue = inspector.bus.subscribe()
        try:
            await socket.send_json({"kind": "status", "status": inspector.status()})
            while True:
                event = await queue.get()
                await socket.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001 - a dead socket must not kill the server
            # Not published: the socket that would have carried the event is the
            # one that just broke. The reconnecting tab reports it on its side.
            logger.warning("event socket dropped: %s", describe_exception(exc))
        finally:
            inspector.bus.unsubscribe(queue)

    # ----------------------------------------------------------------- UI

    app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)

    return app
