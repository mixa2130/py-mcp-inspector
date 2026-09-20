[English](README.md) | [Русский](README.ru.md)
# PyMCPinspector

A Python counterpart to [`@modelcontextprotocol/inspector`](https://github.com/modelcontextprotocol/inspector).
A local web UI for talking to MCP servers, with full control over the transport and the HTTP headers.


```
pip install pymcpinspector
pymcpinspector
```

Opens `http://127.0.0.1:6288/`. From a checkout it is `pip install -e .` instead, and without
installing anything, `python -m pymcpinspector`.

![The inspector connected to the demo server: tools on the left, the result of a call on the right](https://raw.githubusercontent.com/mixa2130/py-mcp-inspector/master/docs/screenshots/overview.jpg)

Every screenshot here is the real UI against `examples/demo_server.py`, so you can reproduce
each one in a couple of minutes.

For development: `pip install -e ".[dev]"` — tests and the linter live in the `dev` extra and
are not pulled in by an ordinary install. House rules for changing the code are in
[AGENTS.md](AGENTS.md), the history is in [CHANGELOG.md](CHANGELOG.md).

## What it does

**Transports** — all three, switched in the UI:

| Transport | What you fill in | Headers |
|---|---|---|
| `streamable-http` | endpoint URL | yes — on every request, including the GET stream |
| `sse` | SSE endpoint URL | yes — on the SSE stream and on the message POSTs |
| `stdio` | command, arguments, cwd, env | no (not HTTP) — environment variables instead |

`sse` and `streamable-http` also get the TLS settings described below.

**Protocol version.** A selector in the Advanced block — `auto`, or any specific revision.
The choice decides not only the string in the request but how the connection is made:

| Value | How it connects |
|---|---|
| `auto` (default) | try `server/discover`, fall back to `initialize` if it is refused; exactly what the SDK's `Client` does |
| `2026-07-28` | `server/discover` directly (the per-request envelope era) |
| `2025-11-25` · `2025-06-18` · `2025-03-26` · `2024-11-05` | `initialize` carrying that version in `protocolVersion` |

`ClientSession.initialize()` only ever offers the newest handshake revision, so the inspector
assembles `initialize` itself for the rest. If the server answers with a different version, it
shows up in the header badge as `2024-11-05 → 2025-06-18` and a warning goes to the log — the
connection is not dropped over it.

Keep in mind that revision `2026-07-28` removed a few methods: `ping`, `resources/subscribe`
and the back-channel for `roots/list`, sampling and elicitation. On such a session they return
`-32601` or "no back-channel" — that is the server's answer, not an inspector failure. To
exercise them, pin `2025-11-25` or older.

The **Ping** button hides itself on such a session: sending it would be a guaranteed `-32601`.
The trigger is the era of the negotiated revision, not the literal string `2026-07-28`, so the
next modern version is handled on its own; an unknown version keeps the button — better to send
it and look at what the server says. The method itself stays reachable from the Raw request tab
if the refusal is precisely what you want to see.

**Headers.** A key → value editor with a checkbox per row, plus a separate authentication block:
header name (`Authorization` by default), scheme (`Bearer`) and token. The resulting set is
visible in the connection status (`sent_headers`) and in the HTTP log, where secrets are masked
as `<N chars hidden>`.

Each header row has a value format: `Fixed` (typed by hand) or UUID v1, v4, v7. With a format
selected, the ⟳ button in the row mints a new value — handy for `X-Request-Id` and other
identifiers that have to change from request to request. The value is filled in immediately if
the row was empty, and when switching between UUID versions — otherwise the selector would say
"v7" while the field held a v4; anything typed by hand is left alone until you press ⟳. v3 and
v5 are deliberately not offered: they are a hash of a namespace plus a name, so "refreshing"
would hand back the same string, and v2 (DCE) needs a POSIX uid, which a browser does not have.
Generation happens entirely in the browser and the value travels to the server as an ordinary
string; the chosen format is kept in the preset but does not survive an `mcpServers` export —
the export says so out loud.

**Sidebar.** The ☰ button on the left of the header pushes it off screen and back, and the
border between it and the work area can be dragged with the mouse or moved with ←/→ when it has
focus (Tab reaches it). Width and hidden state are remembered in the browser, and the width is
clamped to the window if the window turns out to be narrower. A hidden sidebar does not leave
the form: the fields stay in the DOM and Connect goes out with the same configuration.

**A script for the token.** The Authentication block takes a path to a script of yours: the
**Get token** button runs it and puts what it printed into the Token field, and Connect runs it
before it connects. The contract, the examples and what the log says about a run are in
[Token scripts](#token-scripts) below.

**Refreshing the token without reconnecting.** A token lives on the server's schedule, not the
session's, and reconnecting for the sake of a fresh one throws away the handshake, the
`mcp-session-id` and everything attached to it. So an edited token is applied on its own: change
the Token, the Scheme or the header name on a live HTTP session, and once the field goes quiet
the inspector swaps the credential on the HTTP clients the transport is already using. httpx
merges `client.headers` into every request as it is being built, so the new value goes out
starting with the next request while the session stays the same. There is no Apply button to
press: a credential sitting in the form but not going out is exactly the confusion this
inspector exists to remove.

An empty token simply stops the header being sent, and renaming the header removes the old name
so two variants never travel side by side. Nothing is sent while the inspector is idle — the
field is then just the credential the next Connect will carry — and a value identical to what
the session already has costs no request. What actually happened is in the log:
`Authorization replaced (20 chars); in effect from the next request`.

Worth remembering: an SSE stream opened earlier was authenticated when it was opened and carries
on with the old token — whether to extend it is the server's call. A line about that goes to the
Log. For stdio nothing is applied: a child process's environment is fixed at launch.

From a script it is a single request, e.g. on a timer next to the service that issues tokens:

```bash
curl -s localhost:6288/api/auth \
     -H 'Content-Type: application/json' \
     -d "{\"token\": \"$(get-my-token)\"}"
```

**Presets.** A saved connection can be edited in place, not just loaded. The selected preset is
filled into the sidebar; as soon as the form differs from it, an `edited` chip lights up in the
section header, and **Save** overwrites that very preset — no name dialog. **Save as…** stores
the form under a different name (that is, makes a copy), **Rename** moves the preset to a new
name, **Delete** removes it. Buttons that act on the selected preset stay disabled while nothing
is selected.

**The token never reaches a preset.** Neither from the Token field nor as an `Authorization`
row in the header list: both are blanked on write (`store.without_auth_secret`). The reason is
simple — a preset outlives a token: tokens are issued for hours, presets are kept for months, so
a live secret would sit on disk and the next load would hand you a stale value. The header name
and the scheme are kept: they are not secret, and they are exactly what tells you what to fill
in. The `Authorization` row itself stays in the list too — with an empty value, so that it is
visible that the server expects it.

In practice: pick a preset → paste the token → Connect. A pasted token does not count as an
unsaved change, the `edited` chip does not light up for it. The rule lives on the write path, so
it covers importing an `mcpServers` block with an `Authorization` header as well. Other secrets
(a key passphrase, `X-Api-Key`, environment variables) are still written to the presets file as
they are — they are only stripped on export, see below.

**Sharing a preset.** The **Share…** button (and `POST /api/servers/export`) hands you JSON —
one preset or all of them, in one of two formats:

| Format | What is inside |
|---|---|
| Inspector JSON | everything: TLS, timeouts, protocol version, roots — exactly what this inspector's import reads |
| mcpServers JSON | a Claude Desktop / VS Code style block: portable, but only what those clients understand survives |

In the second case the dialog honestly lists what did not fit (`prod: verify_tls,
prod: request_timeout`). The token goes straight into `headers` there, because `mcpServers` has
no separate field for credentials.

![The Share presets dialog: an mcpServers block, with the blanked X-Api-Key and the dropped protocol_version listed underneath](https://raw.githubusercontent.com/mixa2130/py-mcp-inspector/master/docs/screenshots/share-presets.jpg)

**Secrets are stripped by default.** Always — the Token and Key passphrase fields (the first of
which is empty by this point anyway); out of the headers and the environment variables — those
whose *name* looks like a secret (`authorization`, `token`, `secret`, `password`, `api-key`,
`cookie`, …). The names stay, only the value is wiped: whoever receives this has to see what
they are expected to fill in. For the same reason the `Authorization` header stays in the
portable format with an empty value instead of disappearing — otherwise it would read as "this
server needs no authorization". What exactly was stripped is listed under the text; the
**Include tokens and passphrases** checkbox turns stripping off entirely, and a warning appears
in the same place.

The name heuristic is exactly that, a heuristic: `X-Trace-Id` survives, `X-Api-Key` does not.
That is why what was stripped is always listed, rather than offered on trust.

**TLS.** Its own block in the sidebar for the HTTP transports: a custom CA, a client
certificate for mTLS, a passphrase for an encrypted key, and a switch that turns verification off
altogether. The fields, the errors they produce and a throwaway CA to try them against are in
[TLS and mTLS](#tls-and-mtls) below.

**Working with the server.** Connecting asks only for `tools/list`; resources and prompts are
listed the first time their tab is opened and cached from then until a reconnect (the **List**
button refreshes by hand, `notifications/*/list_changed` refreshes only lists that are already
open). Switching presets or disconnecting clears the catalog.

- `tools/list`, `tools/call` — the argument form is built from `inputSchema` (enum → select,
  boolean → select, object/array → JSON field), with a switch to raw JSON; progress and logs
  during the call show up in the bottom panel
- `resources/list`, `resources/templates/list`, `resources/read` — variable substitution into
  the URI template, `subscribe` / `unsubscribe`
- `prompts/list`, `prompts/get` — with arguments
- `ping`, `logging/setLevel`, `completion/complete` (availability depends on the protocol version)
- **Raw request** — any JSON-RPC method with arbitrary params
- **Roots** — the list of roots the client serves on `roots/list`, with a notification to the server
- **Server requests** — incoming `sampling/createMessage` and `elicitation/create`: the
  elicitation form is built from `requestedSchema`, and the answer is sent by hand

**Watching the wire.** A bottom panel with tabs: all JSON-RPC traffic in both directions
(intercepted at the transport level, not scraped from SDK logs), the HTTP exchange (method, URL,
status, headers), notifications, `notifications/message` from the server, and the child
process's stderr for stdio.

![The log panel with an HTTP entry expanded, showing the request headers exactly as httpx sent them](https://raw.githubusercontent.com/mixa2130/py-mcp-inspector/master/docs/screenshots/log-http.jpg)

**Errors.** The **Errors** tab collects everything that went wrong, whatever kind of event it
was: JSON-RPC errors and the inspector's own refusals (`kind: "error"`), transport errors and
warnings, server logs at level `error` and above. The counter on the tab shows how many have
piled up since the last clear. It also catches what the backend never sees: an unreachable
inspector, invalid JSON in the arguments, a broken event stream and any exception in the UI
itself. Everything the backend logs is mirrored into the process output (`--log-level`).

![A failing tool call: the result is marked isError and the Errors tab counter goes up](https://raw.githubusercontent.com/mixa2130/py-mcp-inspector/master/docs/screenshots/tool-error.jpg)

**Presets.** Named configurations in `~/.pymcpinspector/servers.json`, plus importing an
`mcpServers` block from Claude Desktop / VS Code configs.

## Running

```
pymcpinspector [--host 127.0.0.1] [--port 6288] [--config PATH] [--no-browser]
               [--log-level warning]
```

Or without installing: `python -m pymcpinspector`.

The path to the presets file can also be set through `PYMCPINSPECTOR_CONFIG`.

## Demo server

`examples/demo_server.py` is an MCP server with the whole feature set — including the
`show_headers` tool, which returns the headers of the request it received. It is the quickest
way to convince yourself the header editor works:

```bash
python examples/demo_server.py --transport streamable-http --port 8931
# then in the UI: Streamable HTTP -> http://127.0.0.1:8931/mcp -> add a header -> Connect
#                 -> Tools -> show_headers -> Run tool
```

The other tools: `add`, `echo`, `slow_count` (progress + logs), `boom` (an error), `ask_user`
(elicitation), `ask_model` (sampling), `show_roots`, `log_to_stderr`.

It also speaks HTTPS and can demand a client certificate, which is how the TLS block is checked
end to end — see [TLS and mTLS](#tls-and-mtls) below.

## TLS and mTLS

The TLS block sits in the sidebar for `sse` and `streamable-http`. It, like **Advanced**, is
collapsed by default — a click on the header (or Enter/Space from the keyboard) expands it, and
the choice is remembered in the browser. While a section is collapsed its header carries a chip
with whatever inside differs from the defaults, say `no verify · custom CA · client cert` or
`2024-11-05 · log debug`; the key passphrase never goes in there.

![The TLS block filled in against an HTTPS demo server, and the Server panel showing the set that is in force](https://raw.githubusercontent.com/mixa2130/py-mcp-inspector/master/docs/screenshots/tls.jpg)

| Field | What it does |
|---|---|
| Verify the server certificate | unchecking it turns verification off entirely |
| CA certificate | a path to a PEM file or to a directory (`capath`); it **replaces** the system store, like `curl --cacert` |
| Client certificate | the client certificate for mTLS |
| Client key | the private key; can be left out if it lives in the same file |
| Key passphrase | the password for an encrypted key |

Paths are checked before connecting, so a missing file or a wrong password says so instead of
timing out somewhere in the handshake:

```
CA bundle not found: certs/nope.pem
client certificate could not be loaded: [SSL] PEM lib (check the passphrase)
```

A certificate the trust store does not know about fails the way it should, naming the authority
it does not trust: `ConnectError: ('"PyMCPinspector Demo CA" certificate is not trusted',)`. Once
connected, the set that is actually in force is listed in the Server panel — including whether
the key was encrypted — because "which certificate did this session use" is a question worth
answering without reading the config back.

One subtlety this is shaped around: `httpx` deprecated both `verify=<path>` and `cert=`, so the
inspector builds the `ssl.SSLContext` itself. The key passphrase is always passed to
`load_cert_chain` explicitly (as an empty string when unset) — otherwise OpenSSL would ask for it
interactively in the terminal and hang the server.

Presets keep the paths, not the certificates, so a shared preset points at files the other side
has to have. The passphrase is stored in the presets file as plain text — and stripped on export,
like any other secret.

### Trying it against a throwaway CA

The demo server speaks HTTPS and can demand a client certificate, so the whole path is checkable
without touching anything real. Mint a CA and two certificates it signs:

```bash
mkdir certs && cd certs
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=Demo CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout ca.key -out ca.pem
openssl req -newkey rsa:2048 -nodes -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" -keyout server.key -out server.csr
openssl x509 -req -days 2 -in server.csr -CA ca.pem -CAkey ca.key -copy_extensions=copyall -out server.pem
openssl req -newkey rsa:2048 -nodes -subj "/CN=inspector-client" -keyout client.key -out client.csr
openssl x509 -req -days 2 -in client.csr -CA ca.pem -CAkey ca.key -out client.pem
openssl rsa -in client.key -aes256 -passout pass:hunter2 -out client-enc.key   # to exercise the passphrase field
cd ..
```

The two `-addext` lines on the CA are not decoration: OpenSSL 3 refuses to verify a chain whose
CA carries no `keyCertSign`, and the failure reads `CA cert does not include key usage extension`
rather than anything about the missing extension being yours to add.

Then run the server with mutual TLS on and point the inspector at it:

```bash
python examples/demo_server.py --transport streamable-http --port 8950 \
    --ssl-certfile certs/server.pem --ssl-keyfile certs/server.key \
    --ssl-ca-certs certs/ca.pem --require-client-cert
```

```
Server URL                     https://127.0.0.1:8950/mcp
TLS -> CA certificate          certs/ca.pem
       Client certificate      certs/client.pem
       Client key              certs/client-enc.key
       Key passphrase          hunter2
```

Connect. Dropping the CA field is the instructive failure: the server's certificate is perfectly
valid, the system store has simply never heard of the authority that signed it. Unchecking
**Verify the server certificate** gets past it, which is also what puts `--insecure` in the
rendered `curl` command and `no verify` in the section's chip.

## Token scripts

The inspector has no OAuth flow and does not want one: every shop mints tokens its own way. What
it has instead is a hook — a script of yours that prints a credential, which the inspector runs
and uses. Fill in **Token script** (and **Script arguments**) in the Authentication block, and:

- **Get token** runs it and puts what it printed in the Token field — applying it to a live
  session straight away;
- **Connect** runs it first and connects with what it printed, because a token minted after the
  handshake is not the token the handshake was made with.

![The Authentication block with a token script, and the log showing two runs of it](https://raw.githubusercontent.com/mixa2130/py-mcp-inspector/master/docs/screenshots/token-plugin.jpg)

The screenshot above is the whole loop: the script ran twice, once plain and once with `--json`,
and the second run also moved the credential from `Authorization` to `X-Api-Key` — which is why
the Scheme field went empty and the header name changed by itself.

### The contract

The inspector knows nothing about the script beyond its path, so the contract is thin:

| What | How |
|---|---|
| arguments | from the **Script arguments** field, split the way a shell would |
| context | `PYMCPINSPECTOR_URL`, `PYMCPINSPECTOR_TRANSPORT`, `PYMCPINSPECTOR_AUTH_HEADER`, `PYMCPINSPECTOR_AUTH_SCHEME`, plus the inspector's own environment |
| answer | **stdout**: a bare token, or JSON `{"token": …, "scheme"?: …, "header"?: …}` |
| diagnostics | **stderr** — every line lands in the log, on success as well as on failure |
| failure | a non-zero exit code; the last stderr lines become the error text |

The shortest thing that satisfies it:

```python
#!/usr/bin/env python3
import os, sys, urllib.request, json

print(f"minting for {os.environ['PYMCPINSPECTOR_URL']}", file=sys.stderr)   # goes to the log
print(json.load(urllib.request.urlopen(os.environ["TOKEN_ENDPOINT"]))["access_token"])
```

The object form is for a server that wants the credential somewhere else entirely — an API key
rather than a bearer token:

```python
json.dump({"token": token, "scheme": "", "header": "X-Api-Key"}, sys.stdout)
```

The inspector then moves the Scheme and Header name fields to match, so the sidebar keeps showing
what is actually being sent. Keys it does not know are ignored, and it says which ones in the log.

### Running, and what shows up in the log

The script is run as a **separate process** rather than imported: a script usually needs
libraries the inspector's environment does not have, a hung process has to be killable (the
timeout is 60 seconds), and a crashing one must not take the inspector down with it. An
executable file is run as itself — that way its shebang can point at the right venv; everything
else is handed to the same interpreter the inspector runs on.

The log gets the exact command, every line the script wrote to stderr, and what the inspector
decided on the script's behalf — which stdout line it took for the token, which JSON keys it
ignored. The token itself never reaches the log, only its length:

```
PLUGIN     running examples/token_plugin.py --profile prod --json
PLUGIN     stderr: minting a prod token for http://127.0.0.1:8931/mcp
PLUGIN     got a 20-character token in 0.1s
TRANSPORT  X-Api-Key replaced (20 chars); in effect from the next request
TRANSPORT  the SSE stream opened earlier still carries the previous credential
```

The current token is **not** passed to the script: whoever mints a new one has no use for the old
one. The script runs with the inspector's own rights — the same level of trust as the `command`
of the stdio transport, which the inspector also runs as given. A preset keeps the path and the
arguments (they are not a secret); the token, never.

### The two examples

[examples/token_plugin.py](examples/token_plugin.py) walks through every branch of the contract
and needs no network, so it is the one to try first:

```bash
python examples/demo_server.py --transport streamable-http --port 8931
pymcpinspector
# in the UI: Streamable HTTP -> http://127.0.0.1:8931/mcp
#            Token script    -> examples/token_plugin.py
#            Script arguments -> --profile prod
#            Connect -> Get token -> Tools -> show_headers -> Run tool
```

`show_headers` then echoes the header the script produced, which is the proof the loop closed.
Its arguments are the branches: `--profile NAME` picks what to mint, `--json` answers with the
object form (and moves the credential to `X-Api-Key`), `--fail` exits non-zero so you can see
what a failure looks like — it leaves the Token field alone and says why.

[examples/oauth_token_plugin.py](examples/oauth_token_plugin.py) is the shape that makes a script
necessary in the first place: a password is exchanged for a user token, that one for an agent
token for the right audience, and the header carrying the employee id is an argument you change
in one field. It holds no secrets — those come from the inspector's environment:

```bash
export IDP_URL=https://idp.example.com IDP_USER=svc-inspector IDP_PASSWORD=…
# Token script      -> examples/oauth_token_plugin.py
# Script arguments  -> --audience mcp-prod --employee 4815162342
```

## Reproducing a request with curl

The **curl** button assembles a command that repeats what the inspector sends: headers, TLS
flags, timeouts and — on `2026-07-28` — the per-request envelope with its routing headers. It
sits next to every basic method:

| Where | Method | What is filled in |
|---|---|---|
| Tools tab toolbar | `tools/list` | — |
| Resources tab toolbar | `resources/list` | — |
| Prompts tab toolbar | `prompts/list` | — |
| tool panel | `tools/call` | the name and the arguments you filled in |
| resource panel | `resources/read` | the resolved URI |
| prompt panel | `prompts/get` | the name and the arguments you filled in |
| Raw request tab | anything | whatever is typed in the fields |

![The Copy as curl dialog for tools/call on the 2026-07-28 revision](https://raw.githubusercontent.com/mixa2130/py-mcp-inspector/master/docs/screenshots/curl.jpg)

The dialog itself has a method selector: it switches between the parameterless ones
(`tools/list`, `resources/list`, `resources/templates/list`, `prompts/list`, `ping`), which is
how `resources/templates/list` — the one with no button of its own — is reachable. Parameters
belong to the method they were collected for: they are not carried across a switch, and they
come back when you switch back. `ping` is not offered on a session that lost it, by the same
rule that hides the Ping button.

An example from a live session:

```
curl -sS -X POST http://127.0.0.1:8931/mcp \
  -H "Authorization: Bearer $MCP_AUTHORIZATION" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -H 'mcp-protocol-version: 2026-07-28' \
  -H 'mcp-method: tools/call' \
  -H 'mcp-name: add' \
  --connect-timeout 30 \
  --max-time 300 \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "add",
       "arguments": {"a": 19, "b": 23}, "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28", …}}}'
```

Which parts of that are "protocol specifics":

| Revision | What is added |
|---|---|
| `2024-11-05` … `2025-11-25` | the `mcp-session-id` the server handed out on `initialize`; request bodies are ordinary |
| `2026-07-28` | the `params._meta` envelope (`protocolVersion`, `clientCapabilities`, `clientInfo`) plus the routing `mcp-method`, `mcp-name` and, for `tools/call`, `Mcp-Param-*` |

Without the routing headers a modern server answers `-32020 "mcp-method header does not match
the request body's method"` — a command without them looks plausible and does not work, which is
why the test in `test_curl.py` does not check the text but actually runs the resulting string
through `bash` and compares the server's answer.

`clientCapabilities` and `Mcp-Param-*` come from the live session: the first are assembled by
`ClientSession`, the second are declared by the tool's schema, which is only known after
`tools/list`. With no connection there is nowhere to take them from — then that is stated in the
notes under the command, instead of an empty value being passed off as the truth. The same notes
list everything else that is not reproduced verbatim: a missing `mcp-session-id`, a header from
the editor that the transport will override anyway, `--insecure`, and the fact that the response
may arrive as an SSE stream rather than JSON.

**Secrets are replaced with environment variables by default** — `-H "Authorization: Bearer
$MCP_AUTHORIZATION"`, `--pass "$MCP_KEY_PASSPHRASE"`. The scheme (`Bearer`) stays visible, it is
not a secret. The list of variables you have to set is the first line of the notes; the
**Include tokens and passphrases** checkbox substitutes the values as they are.

For `stdio` the button refuses: JSON-RPC there goes over the child process's pipes, and curl has
nothing to talk to. For `sse` the address to POST to is announced by the server on the stream —
the inspector digs it out of its own HTTP log, so the command works after the very first
request, and before that the notes explain why the stream URL will not accept a POST.

## HTTP API

The UI is an ordinary client of its own REST API, so the inspector can be driven from scripts
too:

| Method | Path | |
|---|---|---|
| POST | `/api/connect` | body — the connection configuration |
| POST | `/api/disconnect` | |
| GET | `/api/status` | state, capabilities, the headers that were sent |
| POST | `/api/auth` | `{"token", "scheme"?, "header"?}` — swap the credentials on a live connection |
| POST | `/api/auth/plugin` | body — the connection configuration; runs its token script → `{token, scheme, header, notes}` |
| POST | `/api/tools/list` · `/api/tools/call` | |
| POST | `/api/resources/list` · `/templates/list` · `/read` · `/subscribe` | |
| POST | `/api/prompts/list` · `/api/prompts/get` | |
| POST | `/api/ping` · `/api/logging/level` · `/api/complete` | |
| POST | `/api/request` | an arbitrary JSON-RPC method |
| POST | `/api/roots` · `/api/pending/{id}` | |
| GET | `/api/history?kinds=message,http,log,error` | the event log |
| GET/POST/DELETE | `/api/servers` | presets (POST with an existing name edits in place) |
| POST | `/api/servers/{name}/rename` | `{"name": "new one"}` |
| POST | `/api/servers/export` | `{"names"?, "portable"?, "include_secrets"?}` → `{json, redacted, dropped}` |
| POST | `/api/curl` | `{"method", "params"?, "mask_secrets"?}` → `{command, notes, masked}` |
| WS | `/ws` | the event stream |

JSON-RPC errors come back as `200 {"error": {"kind": "mcp", "code", "message", "data"}}` — a
server error is a finding, not an inspector failure. The inspector's own errors are `4xx` with
the same `error` field. Any such answer is additionally published on the event bus as
`{"kind": "error", "reason", "source", "text", "detail"}`, so the log also shows what the calling
code swallowed in silence.

Interactive schema: `http://127.0.0.1:6288/api/docs`.

## How it is put together

```
pymcpinspector/
  models.py       connection configuration (pydantic)
  transports.py   opening stdio / sse / streamable-http, TLS context, httpx hooks, stderr
  tap.py          wrappers around the transport's streams — the source of the JSON-RPC log
  curl.py         rendering a request as a curl command: headers, TLS, the 2026-07-28 envelope
  connection.py   the background task holding ClientSession; version negotiation, operations, sampling/elicitation
  events.py       the event bus and the ring buffer of history
  app.py          FastAPI: REST + WebSocket
  store.py        presets on disk
  static/         the UI (no build step: html + css + js)
tests/
  test_packaging.py  dev tools are not installed with the package; requirements has not drifted
  test_inspector.py  API, transports, protocol versions
  test_tls.py        the TLS context and real mTLS against an HTTPS server
  test_auth_refresh.py  swapping the token on a live connection (streamable-http and sse)
  test_presets.py    editing, renaming and exporting presets; stripping secrets
  test_curl.py       rendering curl for both protocol eras + running the resulting command
  test_ui.py         runs the DOM tests
  ui/                the jsdom harness, checks for the sidebar, presets, curl, catalog and log
```

The MCP session lives in a single background task (an async context manager cannot be entered
and exited from different tasks), and the HTTP handlers post operations into it; that is why a
long tool call does not block the rest of the interface.

## Tests

```
pytest -q      # 200 tests
ruff check .
```

What gets started along the way: the demo server over stdio, and for TLS over HTTPS with a
throwaway CA (needs `openssl` in `PATH`). The interface is checked in jsdom — `index.html` and
`app.js` are loaded as they are, with the backend stubbed:

```
cd tests/ui && npm install    # once
pytest -q tests/test_ui.py
```

Without `openssl`, or without `node`/`jsdom`, the corresponding tests are skipped rather than
failed.

## The install dies building `cryptography`

Symptom: `pip install` starts compiling `cryptography` and breaks on `couldn't find openssl via
pkg-config` (or `Can't find Rust compiler`).

`cryptography` is not a dependency of the inspector: it is pulled in by `mcp` through
`pyjwt[crypto]`, and imported unconditionally (`mcp/server/request_state.py`), so there is no
doing without it.

**An Intel Mac is the common case.** Since 49.0.0 the macOS wheels are built for arm64 only; for
x86_64 the last wheel (`universal2`) belongs to 48.0.1. But pip always prefers the newest
version: it takes 50.x, finds no wheel for it, falls back to the sources and goes off to build
Rust + OpenSSL. It is enough to forbid it building that one package — it will then pick the
newest version that has a wheel:

```bash
pip install --only-binary=cryptography -e .
```

Or the same thing explicitly:

```bash
pip install "cryptography==48.0.1" && pip install -e .
```

`pyjwt` only asks for `cryptography>=3.4.0`, so 48.0.1 suits everyone; the whole test suite
passes on it. If you do need a recent version, you will have to build:
`brew install openssl@3 pkg-config rust`, then
`export PKG_CONFIG_PATH="$(brew --prefix openssl@3)/lib/pkgconfig"`.

**Other causes.** First look at what kind of environment you are in:

```bash
python -VV      # "free-threading" in the output -> there are no abi3 wheels, take an ordinary CPython
pip --version   # an old pip does not understand manylinux_2_34 / musllinux_1_2 and goes off to build
pip install --only-binary=:all: cryptography   # is there a wheel for your platform at all
```

On Linux without a ready wheel (i686, riscv64, s390x) install the headers:
`apt install pkg-config libssl-dev build-essential` or
`dnf install pkgconf-pkg-config openssl-devel gcc`.

## If the interface looks out of date

The inspector used to serve its static files with only `ETag`/`Last-Modified` and no
`Cache-Control`. Chrome then applies heuristic caching and, on an ordinary navigation, may serve
`app.js` out of the cache without asking the server — an updated inspector kept showing the old
UI until a forced reload. Now the page and `/static/*` carry `Cache-Control: no-cache`: the
browser revalidates every time (cheap, a `304`) but never takes a stale copy blindly. If you
upgraded from an earlier version, one Cmd/Ctrl+Shift+R is enough to climb out of the old cache.

## If a header does not reach the server

First look at what actually went out on the wire — the inspector records it. In the Log panel
expand the `POST … → 200` line tagged `http`: the expanded JSON has `request_headers` with the
request's headers exactly as httpx sent them. Before the first request the status gives you the
same thing:

```bash
curl -s localhost:6288/api/status | python3 -m json.tool | grep -A10 sent_headers
```

If the header is in neither place, the cause is on the inspector's side — look below. If it is
there but the server does not see it, something between you and the server is dropping it (a
reverse proxy, a gateway) and the inspector has nothing to do with it.

**The header is visible in the sidebar but is not sent.** That is how everything behaved before
the version where the header editor started being read from the DOM. Rows used to live in a
separate JS array that was only updated on the `input` event, so a value filled in by
autocomplete, a password manager, an extension or form restoration after a reload made it onto
the screen but not into the request. If you see this on a current version, say so: there is a
test for that case in `tests/ui/sidebar.test.mjs`.

**A row with a value but no name** will not be sent: a header without a name is impossible. A
warning about it goes to the Log on connect.

**The Token field overrides a row in the list.** The authentication block is applied after the
header list, so a non-empty Token overwrites a row with the same name — regardless of case,
`authorization` and `Authorization` count as one header. That, too, produces a warning. If you
need your own scheme (`Token`, `ApiKey`), set it in the Scheme field rather than as a separate
row in the list.

**Protocol headers cannot be overridden.** `Accept`, `Content-Type`, `mcp-session-id` and
`mcp-protocol-version` are set by the transport on every request, and per-request values win
over client ones in httpx. Your value here silently will not apply, and setting
`mcp-protocol-version` by hand breaks initialization — the server answers `params._meta is
missing the required envelope key(s)`. The protocol version is chosen in the Advanced block, not
with a header.

**A malformed value takes the whole connection down** rather than dropping one header:

```
"X-Trace-Id: abc123" in the name field → LocalProtocolError: Illegal header name
" abc123" (leading space)              → LocalProtocolError: Illegal header value
"привет" (non-ASCII)                   → UnicodeEncodeError: ordinal not in range(128)
```

The first is the most common: the whole string gets pasted into the name field, as in
`curl --header "X-Trace-Id: abc123"`. Here the name and the value are separate fields.

Underscores in a name, spaces around a name and an empty value are all allowed and arrive as
they are.

## Limitations

- One active connection per inspector (as in the original).
- No built-in OAuth flow: the token is either typed into the authentication block or printed by
  a [token script](#token-scripts). An edit reaches a live session on its own, but the inspector
  does not track expiry — nothing is reissued unless you press **Get token** or run the script
  against `/api/auth` yourself.
- The version list comes from `mcp_types.version`, i.e. it is bounded by what the installed SDK
  knows; an arbitrary string cannot be typed in.
- The token from the authentication block is not stored in presets at all; key passphrases and
  other secrets in headers and env are stored as plain text (and stripped on export). The
  certificates themselves are not copied — the configuration holds only paths to them.
- `resources/subscribe` and sampling are marked deprecated in the SDK as of protocol
  `2026-07-28`; the inspector sends them anyway — on older servers they work.

## License

MIT — see [LICENSE](LICENSE).
