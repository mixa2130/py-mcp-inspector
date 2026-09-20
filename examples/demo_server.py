"""A small MCP server for exercising the inspector.

Run it on any transport::

    python examples/demo_server.py --transport streamable-http --port 8931
    python examples/demo_server.py --transport sse --port 8932
    python examples/demo_server.py                       # stdio

Over HTTPS, optionally demanding a client certificate, to exercise the
inspector's TLS fields::

    python examples/demo_server.py --transport streamable-http --port 8950 \
        --ssl-certfile server.pem --ssl-keyfile server.key \
        --ssl-ca-certs ca.pem --require-client-cert

`show_headers` echoes the HTTP headers the server received, which is the
quickest way to confirm the inspector's header editor does what you expect.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

import mcp.types as types
from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from pydantic import BaseModel, Field

mcp = MCPServer(
    name="inspector-demo",
    version="1.0.0",
    instructions="A demo server with one of everything, for testing PyMCPinspector.",
)


@mcp.tool(description="Echo the HTTP headers this request arrived with (HTTP transports only).")
async def show_headers(ctx: Context) -> dict[str, str]:
    headers = ctx.headers
    if headers is None:
        return {"note": "this transport carries no HTTP headers (stdio)"}
    return {key: value for key, value in headers.items()}


@mcp.tool(description="Add two numbers.")
def add(a: float, b: float) -> float:
    return a + b


@mcp.tool(description="Echo a message back, optionally shouting.")
def echo(message: str, shout: bool = False) -> str:
    return message.upper() if shout else message


@mcp.tool(description="Count to `steps`, reporting progress and log messages along the way.")
async def slow_count(ctx: Context, steps: int = 5, delay: float = 0.4) -> str:
    for step in range(1, steps + 1):
        await asyncio.sleep(delay)
        await ctx.report_progress(step, steps, f"step {step}")
        await ctx.info(f"finished step {step}")
    return f"counted to {steps}"


@mcp.tool(description="Always fails, so you can see how errors are rendered.")
def boom(reason: str = "requested failure") -> str:
    raise RuntimeError(reason)


class TravelPreference(BaseModel):
    """Schema for the elicitation demo."""

    destination: str = Field(description="Where would you like to go?")
    nights: int = Field(default=3, description="How many nights?")
    window_seat: bool = Field(default=True, description="Window seat?")


@mcp.tool(description="Ask the client's user a question (elicitation). Answer it in the inspector.")
async def ask_user(ctx: Context, question: str = "Plan a trip") -> str:
    result = await ctx.elicit(message=question, schema=TravelPreference)
    if result.action != "accept" or result.data is None:
        return f"the user {result.action}ed"
    return f"{result.data.nights} nights in {result.data.destination}"


@mcp.tool(description="Ask the client's LLM for a completion (sampling, legacy protocol versions).")
async def ask_model(ctx: Context, prompt: str = "Say hello in one word.") -> str:
    result = await ctx.request_context.session.create_message(
        messages=[types.SamplingMessage(role="user", content=types.TextContent(type="text", text=prompt))],
        max_tokens=100,
        related_request_id=ctx.request_id,
    )
    content = result.content
    return content.text if isinstance(content, types.TextContent) else str(content)


@mcp.tool(description="Show the roots the connected client advertises.")
async def show_roots(ctx: Context) -> list[str]:
    result = await ctx.request_context.session.list_roots()
    return [str(root.uri) for root in result.roots]


@mcp.tool(description="Write a line to the server's stderr (visible in the inspector on stdio).")
def log_to_stderr(text: str = "hello from stderr") -> str:
    print(text, file=sys.stderr, flush=True)
    return "written"


@mcp.resource("demo://time", name="Current time", description="The server's clock, as ISO-8601.")
def current_time() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


@mcp.resource("demo://greeting/{name}", name="Greeting", description="A greeting for {name}.")
def greeting(name: str) -> str:
    return f"Hello, {name}!"


@mcp.prompt(description="Ask for a code review in a given tone.")
def review_code(code: str, tone: str = "friendly") -> str:
    return f"Review the following code in a {tone} tone:\n\n{code}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8931)
    parser.add_argument("--ssl-certfile", help="serve over HTTPS with this certificate")
    parser.add_argument("--ssl-keyfile", help="private key for --ssl-certfile")
    parser.add_argument("--ssl-ca-certs", help="CA used to verify client certificates")
    parser.add_argument(
        "--require-client-cert",
        action="store_true",
        help="demand a client certificate signed by --ssl-ca-certs (mutual TLS)",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run("stdio")
    elif not args.ssl_certfile:
        mcp.run(args.transport, host=args.host, port=args.port)
    else:
        # MCPServer.run() has no TLS options, so drive uvicorn directly.
        import ssl

        import uvicorn

        app = mcp.sse_app() if args.transport == "sse" else mcp.streamable_http_app()
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="warning",
            ssl_certfile=args.ssl_certfile,
            ssl_keyfile=args.ssl_keyfile,
            ssl_ca_certs=args.ssl_ca_certs,
            ssl_cert_reqs=ssl.CERT_REQUIRED if args.require_client_cert else ssl.CERT_NONE,
        )


if __name__ == "__main__":
    main()
