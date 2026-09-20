/* The credential on a live session: there is no Apply button, so an edited
   token, scheme or header name has to reach the session by itself. */

import { boot, check, choose, click, failsWith, finish, settle, type } from "./harness.mjs";

const DRAFT = "pymcpinspector.draft";

const CONNECTED = {
  status: "connected", transport: "streamable-http", target: "http://x/mcp", pending: [],
};
const CONNECTED_STDIO = { status: "connected", transport: "stdio", target: "python srv.py", pending: [] };

const config = (extra = {}) => ({ transport: "streamable-http", url: "http://x/mcp", ...extra });

const start = (draft = config(), routes = {}) => boot({
  storage: { [DRAFT]: JSON.stringify(draft) },
  routes: { "/api/connect": CONNECTED, "/api/tools/list": { result: { tools: [] } }, ...routes },
});

const pathsOf = (calls) => calls.map((c) => c.path).join(", ");
const authCalls = (calls) => calls.filter((c) => c.path === "/api/auth");
const lastAuth = (calls) => {
  const call = authCalls(calls).pop();
  return call && JSON.parse(call.options.body);
};

/** Long enough for the debounce (250 ms) to fire and its request to be seen. */
const quiet = () => settle(400);

// --- there is no button ----------------------------------------------------

{
  const { doc } = await start();
  check("the Apply button is gone", !doc.getElementById("btn-auth-apply"));
  check("and the hint says the credential applies itself",
    /applied on its own/.test(doc.getElementById("auth-apply-hint").textContent),
    doc.getElementById("auth-apply-hint").textContent);
}

// --- an edit on a live session ---------------------------------------------

{
  const { doc, calls } = await start();
  click(doc, doc.getElementById("btn-connect"));
  await settle(40);
  type(doc, doc.getElementById("cfg-token"), "fresh-token");
  await quiet();

  const sent = lastAuth(calls);
  check("a typed token reaches the live session on its own", !!sent, pathsOf(calls));
  check("with the scheme and header name the sidebar shows",
    !!sent && sent.token === "fresh-token" && sent.scheme === "Bearer" && sent.header === "Authorization",
    JSON.stringify(sent));
  check("and the session is not reconnected for it",
    calls.filter((c) => c.path === "/api/connect").length === 1, pathsOf(calls));
}

{
  const { doc, calls } = await start();
  click(doc, doc.getElementById("btn-connect"));
  await settle(40);
  for (const value of ["t", "to", "tok", "toke", "token"]) {
    type(doc, doc.getElementById("cfg-token"), value);
    await settle(20);
  }
  await quiet();

  check("a token typed character by character is one swap, not five",
    authCalls(calls).length === 1, `${authCalls(calls).length} calls: ${pathsOf(calls)}`);
  check("carrying the whole token", lastAuth(calls).token === "token", JSON.stringify(lastAuth(calls)));
}

{
  const { doc, calls } = await start();
  click(doc, doc.getElementById("btn-connect"));
  await settle(40);
  type(doc, doc.getElementById("cfg-auth-header"), "X-Api-Key");
  type(doc, doc.getElementById("cfg-auth-scheme"), "");
  type(doc, doc.getElementById("cfg-token"), "k");
  await quiet();

  const sent = lastAuth(calls);
  check("the header name and the scheme are part of the credential",
    !!sent && sent.header === "X-Api-Key" && sent.scheme === "" && sent.token === "k",
    JSON.stringify(sent));
}

{
  const { doc, calls } = await start(config({ bearer_token: "live" }));
  click(doc, doc.getElementById("btn-connect"));
  await settle(40);
  type(doc, doc.getElementById("cfg-token"), "");
  await quiet();

  const sent = lastAuth(calls);
  check("clearing the field stops the header being sent",
    !!sent && sent.token === "", JSON.stringify(sent));
}

{
  const { doc, calls } = await start(config({ bearer_token: "live" }));
  click(doc, doc.getElementById("btn-connect"));
  await settle(40);
  type(doc, doc.getElementById("cfg-token"), "live");
  await quiet();

  check("retyping what the connection already carries costs nothing",
    authCalls(calls).length === 0, pathsOf(calls));
}

{
  // A password manager or form restoration puts a value there without a
  // keystroke; `change` on blur is the only event some of them fire.
  const { doc, calls } = await start();
  click(doc, doc.getElementById("btn-connect"));
  await settle(40);
  choose(doc, doc.getElementById("cfg-token"), "autofilled");
  await quiet();

  const sent = lastAuth(calls);
  check("a token that arrived without a keystroke is still sent",
    !!sent && sent.token === "autofilled", JSON.stringify(sent));
}

// --- when there is nothing to apply it to ----------------------------------

{
  const { doc, calls } = await start();
  type(doc, doc.getElementById("cfg-token"), "typed-while-idle");
  await quiet();
  check("an idle inspector sends nothing", authCalls(calls).length === 0, pathsOf(calls));
  check("and the token is still in the field, ready for Connect",
    doc.getElementById("cfg-token").value === "typed-while-idle");
}

{
  const { doc, calls } = await start(config({ transport: "stdio", command: "python", args: "srv.py" }),
    { "/api/connect": CONNECTED_STDIO });
  click(doc, doc.getElementById("btn-connect"));
  await settle(40);
  type(doc, doc.getElementById("cfg-token"), "fresh-token");
  await quiet();

  check("a stdio child keeps the environment it was spawned with",
    authCalls(calls).length === 0, pathsOf(calls));
}

{
  const { doc, calls } = await start();
  click(doc, doc.getElementById("btn-connect"));
  await settle(40);
  type(doc, doc.getElementById("cfg-token"), "first");
  await quiet();
  click(doc, doc.getElementById("btn-disconnect"));
  await settle(40);
  const before = authCalls(calls).length;
  type(doc, doc.getElementById("cfg-token"), "second");
  await quiet();

  check("after disconnecting an edit has nowhere to go",
    authCalls(calls).length === before, pathsOf(calls));
}

// --- a swap the session refused --------------------------------------------

{
  const { doc, calls } = await start(config(), { "/api/auth": failsWith("no live connection") });
  click(doc, doc.getElementById("btn-connect"));
  await settle(40);
  type(doc, doc.getElementById("cfg-token"), "refused");
  await quiet();

  check("a refused swap says so", /no live connection/.test(doc.body.textContent));
  check("and it was actually attempted", authCalls(calls).length === 1, pathsOf(calls));

  type(doc, doc.getElementById("cfg-token"), "refused-again");
  await quiet();
  check("the next edit tries again rather than trusting a value that never went out",
    authCalls(calls).length === 2, `${authCalls(calls).length} calls`);
}

finish();
