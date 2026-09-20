"""The live MCP connection: one background task owning a ``ClientSession``."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import mcp.types as types
from mcp.client.session import ClientRequestContext, ClientSession
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS
from pydantic import ValidationError

from .events import EventBus
from .models import ConnectionConfig
from .tap import TappedReadStream, TappedWriteStream

if TYPE_CHECKING:
    from .transports import LiveHeaders

try:  # the SDK's own connect-time era negotiation, used for protocol_version="auto"
    from mcp.client._probe import negotiate_auto
except ImportError:  # pragma: no cover - older SDK builds have no discover probe
    negotiate_auto = None

PENDING_TIMEOUT = 300.0


def dump(model: Any) -> Any:
    """Serialise an MCP model the way it looks on the wire."""
    if hasattr(model, "model_dump"):
        return model.model_dump(by_alias=True, mode="json", exclude_none=True)
    return model


def describe_exception(exc: BaseException) -> str:
    """Flatten exception groups so the UI shows the cause, not the wrapper."""
    if isinstance(exc, BaseExceptionGroup):
        inner = [describe_exception(e) for e in exc.exceptions]
        return "; ".join(inner) or str(exc)
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


@dataclass
class PendingServerRequest:
    """A server-initiated request (sampling or elicitation) awaiting the user."""

    id: str
    kind: str
    params: Any
    future: asyncio.Future[dict[str, Any]] = field(repr=False)
    created: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "params": self.params, "created": self.created}


class NotConnectedError(RuntimeError):
    pass


class InspectorConnection:
    """Owns the transport + session, and executes operations against them."""

    def __init__(self, config: ConnectionConfig, bus: EventBus) -> None:
        self.config = config
        self.bus = bus
        self.id = uuid.uuid4().hex[:12]
        self.status = "idle"
        self.error: str | None = None
        self.session: ClientSession | None = None
        self.init_result: types.InitializeResult | None = None
        self.discover_result: types.DiscoverResult | None = None
        self.negotiated_version: str | None = None
        self.server_info: types.Implementation | None = None
        self.server_capabilities: types.ServerCapabilities | None = None
        self.instructions: str | None = None
        self.http_session_id: str | None = None
        self.log_level: str | None = config.log_level
        self.roots = list(config.roots)
        self.started_at: float | None = None

        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._ops: set[asyncio.Task[Any]] = set()
        self._live_headers: LiveHeaders | None = None
        self._pending: dict[str, PendingServerRequest] = {}

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self.status = "connecting"
        self.started_at = time.time()
        self._task = asyncio.create_task(self._run(), name=f"mcp-conn-{self.id}")
        await self._ready.wait()
        if self.status != "connected":
            raise ConnectionError(self.error or "connection failed")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10.0)
            except TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            except Exception:
                pass

    async def _run(self) -> None:
        from .transports import LiveHeaders, open_transport

        self._live_headers = LiveHeaders()
        try:
            async with open_transport(
                self.config,
                on_stderr=self._on_stderr,
                on_session_id=self._on_http_session,
                on_http=self._on_http_exchange,
                live_headers=self._live_headers,
            ) as (read_stream, write_stream):
                read_stream = TappedReadStream(read_stream, self._observe)
                write_stream = TappedWriteStream(write_stream, self._observe)
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=self.config.request_timeout,
                    sampling_callback=self._on_sampling,
                    elicitation_callback=self._on_elicitation,
                    list_roots_callback=self._on_list_roots,
                    logging_callback=self._on_log,
                    message_handler=self._on_message,
                    client_info=types.Implementation(
                        name=self.config.client_name or "pymcpinspector",
                        version=self.config.client_version or "0.1.0",
                        title="PyMCPinspector",
                    ),
                ) as session:
                    self.session = session
                    await self._negotiate(session)
                    self.status = "connected"
                    self.error = None
                    self._ready.set()
                    self.bus.publish("status", {"status": self.status_payload()})
                    if self.log_level:
                        await self._apply_log_level(session, self.log_level)
                    await self._stop.wait()
        except asyncio.CancelledError:
            self.status = "disconnected"
            raise
        except BaseException as exc:  # noqa: BLE001 - surfaced to the UI verbatim
            self.error = describe_exception(exc)
            self.status = "error"
            self.bus.publish("transport", {"level": "error", "text": self.error})
        finally:
            self.session = None
            self._live_headers = None
            for task in list(self._ops):
                task.cancel()
            self._ops.clear()
            for pending in list(self._pending.values()):
                if not pending.future.done():
                    pending.future.cancel()
            self._pending.clear()
            if self.status not in ("error",):
                self.status = "disconnected"
            self._ready.set()
            self.bus.publish("status", {"status": self.status_payload()})

    # ---------------------------------------------------------------- handshake

    async def _negotiate(self, session: ClientSession) -> None:
        """Bring the session up at the protocol version the user picked."""
        wanted = self.config.protocol_version
        era = self.config.negotiation_era()
        if era == "auto":
            if negotiate_auto is None:
                await session.initialize()
            else:
                await negotiate_auto(session)
        elif era == "modern":
            raw = await session.send_discover(wanted)
            session.adopt(types.DiscoverResult.model_validate(raw))
        else:
            await self._handshake_at(session, wanted)

        self.init_result = session.initialize_result
        self.discover_result = session.discover_result
        self.negotiated_version = session.protocol_version
        self.server_info = session.server_info
        self.server_capabilities = session.server_capabilities
        self.instructions = session.instructions

        if era != "auto" and self.negotiated_version != wanted:
            self.bus.publish(
                "transport",
                {
                    "level": "warning",
                    "text": f"requested protocol {wanted}, server negotiated {self.negotiated_version}",
                },
            )

    async def _handshake_at(self, session: ClientSession, version: str) -> None:
        """Run `initialize` offering `version`.

        `ClientSession.initialize()` always offers the newest handshake revision,
        which is exactly what an inspector needs to be able to override.
        """
        build_capabilities = getattr(session, "_build_capabilities", None)
        client_info = getattr(session, "_client_info", None)
        if build_capabilities is None or client_info is None:
            raise RuntimeError("this MCP SDK build cannot offer a specific handshake version")

        result = await session.send_request(
            types.InitializeRequest(
                params=types.InitializeRequestParams(
                    protocol_version=version,
                    capabilities=build_capabilities(version),
                    client_info=client_info,
                ),
            ),
            types.InitializeResult,
        )
        if result.protocol_version not in HANDSHAKE_PROTOCOL_VERSIONS:
            raise RuntimeError(f"server answered with an unsupported protocol version: {result.protocol_version}")
        session.adopt(result)
        await session.send_notification(types.InitializedNotification())

    async def _apply_log_level(self, session: ClientSession, level: str) -> None:
        try:
            await session.set_logging_level(level)  # type: ignore[arg-type]
            self.log_level = level
        except Exception as exc:  # noqa: BLE001
            self.bus.publish(
                "transport",
                {"level": "warning", "text": f"logging/setLevel failed: {describe_exception(exc)}"},
            )

    # ------------------------------------------------------------- credentials

    def refresh_auth(
        self,
        token: str,
        *,
        scheme: str | None = None,
        header_name: str | None = None,
    ) -> dict[str, str]:
        """Send a new credential from the next request on, without reconnecting.

        A bearer token expires on the server's schedule, not the session's, and
        reconnecting to carry a fresh one throws away the handshake, the HTTP
        session id and everything listed under it. This swaps the header on the
        clients the transport is already using and leaves the session alone.

        Returns:
            The full header set now going out, auth header included.

        Raises:
            ValueError: The transport sends no headers.
            NotConnectedError: There is no live HTTP client to update.
        """
        if not self.config.is_http():
            raise ValueError(
                "only the HTTP transports send headers; "
                "a stdio server's environment is fixed when its process starts"
            )
        if self._live_headers is None or not self._live_headers.live:
            raise NotConnectedError(self.error or "not connected to a server")

        before = self.config.resolved_headers()
        if header_name is not None:
            self.config.auth_header_name = header_name.strip() or "Authorization"
        if scheme is not None:
            self.config.auth_scheme = scheme.strip()
        self.config.bearer_token = token
        after = self.config.resolved_headers()

        # Renaming the auth header (or clearing the token) would otherwise leave
        # the previous header going out beside the new one.
        kept = {name.lower() for name in after}
        dropped = [name for name in before if name.lower() not in kept]
        self._live_headers.replace(after, drop=dropped)

        name = (self.config.auth_header_name or "Authorization").strip()
        if token.strip():
            self.bus.publish(
                "transport",
                {"level": "info", "text": f"{name} replaced ({len(token.strip())} chars); "
                                          "in effect from the next request"},
            )
        else:
            self.bus.publish("transport", {"level": "warning", "text": f"{name} cleared"})
        # A stream that is already open was authenticated when it opened; only
        # the server can decide whether to keep honouring it.
        self.bus.publish(
            "transport",
            {"level": "info", "text": "the SSE stream opened earlier still carries the previous credential"},
        )
        self.bus.publish("status", {"status": self.status_payload()})
        return after

    # ------------------------------------------------------------------ running

    async def run(self, operation: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        """Execute ``operation`` against the live session, cancellable on shutdown."""
        session = self.session
        if session is None or self.status != "connected":
            raise NotConnectedError(self.error or "not connected to a server")
        task = asyncio.create_task(operation(session))
        self._ops.add(task)
        try:
            return await task
        finally:
            self._ops.discard(task)

    # ------------------------------------------------------------------ observing

    def _observe(self, direction: str, payload: dict[str, Any]) -> None:
        if direction == "error":
            self.bus.publish("transport", {"level": "error", "text": payload.get("error", "")})
            return
        self.bus.publish("message", {"direction": direction, "message": payload})

    def _on_stderr(self, line: str) -> None:
        self.bus.publish("stderr", {"text": line})

    def _on_http_session(self, session_id: str) -> None:
        if session_id == self.http_session_id:
            return
        self.http_session_id = session_id
        self.bus.publish("transport", {"level": "info", "text": f"session id: {session_id}"})
        if self.status == "connected":
            self.bus.publish("status", {"status": self.status_payload()})

    def _on_http_exchange(self, exchange: dict[str, Any]) -> None:
        self.bus.publish("http", exchange)

    async def _on_log(self, params: types.LoggingMessageNotificationParams) -> None:
        data = dump(params.data) if hasattr(params.data, "model_dump") else params.data
        self.bus.publish("log", {"level": params.level, "logger": params.logger, "data": data})

    async def _on_message(self, message: Any) -> None:
        if isinstance(message, Exception):
            self.bus.publish("transport", {"level": "error", "text": describe_exception(message)})
            return
        root = getattr(message, "root", message)
        method = getattr(root, "method", "?")
        if method == "notifications/message":
            return  # already surfaced through logging_callback
        self.bus.publish("notification", {"method": method, "notification": dump(root)})

    # --------------------------------------------------------- server -> client

    async def _on_list_roots(self, context: ClientRequestContext) -> types.ListRootsResult | types.ErrorData:
        try:
            roots = [types.Root(uri=r.uri, name=r.name) for r in self.roots]
        except ValidationError as exc:
            return types.ErrorData(code=types.INVALID_PARAMS, message=f"invalid root: {exc}")
        return types.ListRootsResult(roots=roots)

    async def _on_sampling(
        self, context: ClientRequestContext, params: types.CreateMessageRequestParams
    ) -> types.CreateMessageResult | types.ErrorData:
        answer = await self._ask_user("sampling", dump(params))
        if answer is None or answer.get("action") != "accept":
            return types.ErrorData(
                code=types.INVALID_REQUEST,
                message=(answer or {}).get("message") or "Sampling request rejected in the inspector",
            )
        try:
            return types.CreateMessageResult.model_validate(answer.get("result") or {})
        except ValidationError as exc:
            return types.ErrorData(code=types.INVALID_PARAMS, message=f"invalid sampling result: {exc}")

    async def _on_elicitation(
        self, context: ClientRequestContext, params: Any
    ) -> types.ElicitResult | types.ErrorData:
        answer = await self._ask_user("elicitation", dump(params))
        if answer is None:
            return types.ElicitResult(action="cancel")
        try:
            return types.ElicitResult.model_validate(answer.get("result") or {"action": "cancel"})
        except ValidationError as exc:
            return types.ErrorData(code=types.INVALID_PARAMS, message=f"invalid elicitation result: {exc}")

    async def _ask_user(self, kind: str, params: Any) -> dict[str, Any] | None:
        pending = PendingServerRequest(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            params=params,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[pending.id] = pending
        self.bus.publish("pending", {"action": "open", "request": pending.to_json()})
        try:
            return await asyncio.wait_for(pending.future, timeout=PENDING_TIMEOUT)
        except TimeoutError:
            return {"action": "reject", "message": "timed out waiting for the inspector user"}
        except asyncio.CancelledError:
            return None
        finally:
            self._pending.pop(pending.id, None)
            self.bus.publish("pending", {"action": "close", "id": pending.id})

    def pending_requests(self) -> list[dict[str, Any]]:
        return [p.to_json() for p in self._pending.values()]

    def resolve_pending(self, request_id: str, answer: dict[str, Any]) -> bool:
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(answer)
        return True

    # ------------------------------------------------------------------- status

    def status_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "error": self.error,
            "transport": self.config.transport,
            "target": self.config.describe(),
            "started_at": self.started_at,
            "http_session_id": self.http_session_id,
            "log_level": self.log_level,
            "roots": [r.model_dump() for r in self.roots],
            "requested_protocol_version": self.config.protocol_version,
            "negotiation_era": self.config.negotiation_era(),
            "protocol_version": self.negotiated_version,
            "server_info": dump(self.server_info) if self.server_info else None,
            "capabilities": dump(self.server_capabilities) if self.server_capabilities else None,
            "instructions": self.instructions,
            "handshake_result": dump(self.discover_result or self.init_result)
            if (self.discover_result or self.init_result)
            else None,
            "sent_headers": self.config.resolved_headers() if self.config.is_http() else {},
            "tls": self.config.tls_summary() if self.config.is_http() else None,
            "pending": self.pending_requests(),
        }
