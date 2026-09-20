"""User scripts the inspector runs on the viewer's behalf.

There is one hook so far: a script that prints the auth credential. It is run
as a separate process rather than imported, which decides three things at once
-- a token script usually needs libraries the inspector's own environment does
not carry (`boto3`, `msal`, a company SDK), a script that hangs has to be
killable, and one that raises must not take the inspector down with it.

The contract is deliberately thin, because the inspector knows nothing about
the script beyond where it is: arguments go in on the command line, context in
the environment, and the credential comes back on stdout. Anything the
inspector decides on the script's behalf -- which interpreter ran it, which of
several printed lines was taken as the token -- is reported rather than
assumed, the same way the header redaction lists what it cleared.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import ConnectionConfig

TIMEOUT = 60.0
"""Seconds a script gets before it is killed.

Long enough for a network round trip and a token refresh, short enough that a
script sitting on a terminal prompt nobody can answer gives up instead of
wedging the button it was started from.
"""

STDERR_TAIL = 10
"""How many trailing stderr lines a failure carries into its message."""

Publish = Callable[[str, str], None]
"""`(level, text)` -- how a run narrates itself. See `run_auth_plugin`."""


@dataclass
class AuthToken:
    """What a plugin produced, and what the inspector had to decide itself."""

    token: str
    scheme: str | None = None
    header: str | None = None
    notes: list[str] = field(default_factory=list)


def _script_path(config: ConnectionConfig) -> Path:
    path = Path(config.auth_plugin.strip()).expanduser()
    if not path.exists():
        raise ValueError(f"no auth plugin at {path}")
    if not path.is_file():
        raise ValueError(f"the auth plugin {path} is not a file")
    return path


def command_for(config: ConnectionConfig) -> list[str]:
    """The exact argv the plugin will be started with.

    An executable file is run as itself, so its shebang can point at the
    virtualenv it needs; anything else is handed to the interpreter running the
    inspector, which is the only one this process can be sure exists.
    """
    path = _script_path(config)
    if os.access(path, os.X_OK):
        return [str(path), *config.auth_plugin_args]
    return [sys.executable, str(path), *config.auth_plugin_args]


def environment_for(config: ConnectionConfig) -> dict[str, str]:
    """The child's environment: the inspector's own, plus where it is pointed.

    The current credential is deliberately not among these. A script that mints
    one does not need the stale one, and passing a secret to a child process is
    not something to do by default.
    """
    return {
        **os.environ,
        "PYMCPINSPECTOR_URL": config.url,
        "PYMCPINSPECTOR_TRANSPORT": config.transport,
        "PYMCPINSPECTOR_AUTH_HEADER": (config.auth_header_name or "Authorization").strip(),
        "PYMCPINSPECTOR_AUTH_SCHEME": config.auth_scheme.strip(),
    }


def _from_json(payload: Any) -> AuthToken | None:
    """Read the object form of the contract, or None if this is not it."""
    if not isinstance(payload, dict):
        return None
    token = payload.get("token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError(
            "the auth plugin printed a JSON object without a usable `token`; "
            f"it had {', '.join(sorted(map(str, payload))) or 'no keys'}"
        )
    known = {"token", "scheme", "header"}
    result = AuthToken(token=token.strip())
    for name in ("scheme", "header"):
        value = payload.get(name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"the auth plugin's `{name}` must be a string, not {type(value).__name__}")
        setattr(result, name, value.strip() if isinstance(value, str) else None)
    ignored = sorted(set(payload) - known)
    if ignored:
        result.notes.append(f"ignored key(s) the contract has no place for: {', '.join(map(str, ignored))}")
    return result


def parse_output(stdout: str) -> AuthToken:
    """Turn what the script printed into a credential.

    Two shapes are accepted: a bare token, or a JSON object carrying `token`
    and optionally `scheme` and `header`. A script that also prints progress to
    stdout gets the benefit of the doubt -- the last non-empty line is taken --
    but the guess is written down in the notes rather than left to be noticed.

    Raises:
        ValueError: Nothing usable was printed.
    """
    text = stdout.strip()
    if not text:
        raise ValueError("the auth plugin printed nothing on stdout; the token is what it should print")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        structured = _from_json(payload)
        if structured is not None:
            return structured

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    notes = []
    if len(lines) > 1:
        notes.append(f"stdout had {len(lines)} non-empty lines; the last one was taken as the token")
    return AuthToken(token=lines[-1], notes=notes)


async def run_auth_plugin(config: ConnectionConfig, publish: Publish | None = None) -> AuthToken:
    """Run the configured script and return what it printed.

    Args:
        config: The connection being prepared. Its `auth_plugin` says what to
            run, and the rest of it is what the script is told about.
        publish: Called with `(level, text)` for every step worth seeing --
            the command, each line of stderr, how long it took. The token
            itself is never passed to it; only its length is.

    Raises:
        ValueError: There is no script, it failed, it timed out, or it printed
            nothing a credential could be read from. The message is what the
            viewer sees, so it carries the script's own last words.
    """
    if not config.auth_plugin.strip():
        raise ValueError("no auth plugin is configured")
    argv = command_for(config)
    say = publish or (lambda level, text: None)
    say("info", f"running {shlex.join(argv)}")

    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment_for(config),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), TIMEOUT)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ValueError(f"the auth plugin did not finish within {TIMEOUT:.0f}s and was killed") from None
    elapsed = time.monotonic() - started

    failed = process.returncode != 0
    complaints = [line for line in stderr.decode(errors="replace").splitlines() if line.strip()]
    for line in complaints:
        say("error" if failed else "info", f"stderr: {line}")
    if failed:
        tail = " / ".join(complaints[-STDERR_TAIL:])
        raise ValueError(
            f"the auth plugin exited {process.returncode}" + (f": {tail}" if tail else " and said nothing")
        )

    result = parse_output(stdout.decode(errors="replace"))
    for note in result.notes:
        say("warning", note)
    say("info", f"got a {len(result.token)}-character token in {elapsed:.1f}s")
    return result
