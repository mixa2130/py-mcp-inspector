"""Stream wrappers that mirror every JSON-RPC frame to a callback.

The MCP client transports hand back a plain ``(read_stream, write_stream)``
pair, so the cheapest place to observe raw traffic -- which is the whole point
of an inspector -- is between the transport and the session.
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Any

from mcp.shared.message import SessionMessage

Observer = Callable[[str, dict[str, Any]], None]
"""Called as ``observer(direction, payload)`` with direction 'in' or 'out'."""


def _payload(message: SessionMessage) -> dict[str, Any]:
    return message.message.model_dump(by_alias=True, mode="json", exclude_none=True)


class TappedReadStream:
    """Wraps a transport read stream, reporting everything that comes back."""

    def __init__(self, stream: Any, observe: Observer) -> None:
        self._stream = stream
        self._observe = observe

    async def receive(self) -> Any:
        item = await self._stream.receive()
        if isinstance(item, SessionMessage):
            self._observe("in", _payload(item))
        elif isinstance(item, Exception):
            self._observe("error", {"error": f"{type(item).__name__}: {item}"})
        return item

    async def aclose(self) -> None:
        await self._stream.aclose()

    def __aiter__(self) -> TappedReadStream:
        return self

    async def __anext__(self) -> Any:
        item = await self._stream.__anext__()
        if isinstance(item, SessionMessage):
            self._observe("in", _payload(item))
        return item

    async def __aenter__(self) -> TappedReadStream:
        await self._stream.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return await self._stream.__aexit__(exc_type, exc_val, exc_tb)

    def __getattr__(self, item: str) -> Any:
        # `last_context` and friends are read off the concrete stream.
        return getattr(self._stream, item)


class TappedWriteStream:
    """Wraps a transport write stream, reporting everything the client sends."""

    def __init__(self, stream: Any, observe: Observer) -> None:
        self._stream = stream
        self._observe = observe

    async def send(self, item: SessionMessage, /) -> None:
        self._observe("out", _payload(item))
        await self._stream.send(item)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def __aenter__(self) -> TappedWriteStream:
        await self._stream.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return await self._stream.__aexit__(exc_type, exc_val, exc_tb)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._stream, item)
