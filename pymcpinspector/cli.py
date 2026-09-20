"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser
from pathlib import Path

import uvicorn

from . import __version__
from .app import create_app
from .store import ServerStore, default_config_path

# uvicorn's own level names; `trace` has no Python equivalent, so it maps to DEBUG.
LOG_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": logging.DEBUG,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pymcpinspector",
        description="A Python MCP Inspector: connect to MCP servers over stdio, SSE or Streamable HTTP.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=6288, help="port to bind (default: 6288)")
    parser.add_argument("--config", type=Path, default=None, help=f"presets file (default: {default_config_path()})")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    parser.add_argument(
        "--log-level",
        default="warning",
        choices=list(LOG_LEVELS),
        help="server log level (default: warning)",
    )
    parser.add_argument("--version", action="version", version=f"pymcpinspector {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # uvicorn only configures its own loggers; without this the inspector's own
    # error records would fall back to the last-resort handler and lose their timestamps.
    logging.basicConfig(
        level=LOG_LEVELS[args.log_level],
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    app = create_app(ServerStore(args.config) if args.config else None)

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    print(f"PyMCPinspector {__version__}")
    print(f"  UI:   {url}")
    print(f"  API:  {url}api/docs")
    print(f"  presets: {app.state.inspector.store.path}")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
