/* The token script as the sidebar sees it: the Get token button, and Connect
   waiting for the script before it opens anything. */

import { boot, check, click, failsWith, finish, settle, type } from "./harness.mjs";

const DRAFT = "pymcpinspector.draft";

const http = (extra = {}) => ({ transport: "streamable-http", url: "http://x/mcp", ...extra });
const withPlugin = (extra = {}) => http({ auth_plugin: "/tmp/token.py", ...extra });

const CONNECTED = {
  status: "connected", transport: "streamable-http", target: "http://x/mcp", pending: [],
};
const PRINTED = { token: "from-the-script", scheme: null, header: null, notes: [] };

/** Boot with a draft and the endpoints this feature talks to. */
const start = (config, routes = {}) => boot({
  storage: { [DRAFT]: JSON.stringify(config) },
  routes: { "/api/auth/plugin": PRINTED, "/api/tools/list": { result: { tools: [] } }, ...routes },
});

const pathsOf = (calls) => calls.map((c) => c.path).join(", ");
const bodyOf = (calls, path) => {
  const call = calls.filter((c) => c.path === path).pop();
  return call && JSON.parse(call.options.body);
};

// --- the button ------------------------------------------------------------

{
  const { doc } = await start(http());
  check("with no script there is nothing to run", doc.getElementById("btn-auth-plugin").disabled);
  type(doc, doc.getElementById("cfg-auth-plugin"), "/tmp/token.py");
  check("typing a path arms the button", !doc.getElementById("btn-auth-plugin").disabled);
  type(doc, doc.getElementById("cfg-auth-plugin"), "   ");
  check("and blanking it disarms the button again", doc.getElementById("btn-auth-plugin").disabled);
}

{
  const { doc, calls, store } = await start(withPlugin({ auth_plugin_args: "--profile prod" }));
  click(doc, doc.getElementById("btn-auth-plugin"));
  await settle(40);

  const sent = bodyOf(calls, "/api/auth/plugin");
  check("the button runs the script", !!sent, pathsOf(calls));
  check("and says which script, with which arguments",
    !!sent && sent.auth_plugin === "/tmp/token.py" && sent.auth_plugin_args === "--profile prod",
    JSON.stringify(sent && [sent.auth_plugin, sent.auth_plugin_args]));
  check("what it printed lands in the Token field",
    doc.getElementById("cfg-token").value === "from-the-script",
    doc.getElementById("cfg-token").value);
  check("and in the draft, like any other typed token",
    JSON.parse(store[DRAFT]).bearer_token === "from-the-script", store[DRAFT]);
  check("an idle inspector has nothing to apply it to",
    !calls.some((c) => c.path === "/api/auth"), pathsOf(calls));
  check("the button is usable again", !doc.getElementById("btn-auth-plugin").disabled);
  check("and back to its own label",
    doc.getElementById("btn-auth-plugin").textContent === "Get token",
    doc.getElementById("btn-auth-plugin").textContent);
}

{
  // On a live session a token is worth nothing until it is actually going out.
  const { doc, calls } = await start(withPlugin(), {
    "/api/connect": CONNECTED,
    "/api/auth/plugin": { token: "fresh", scheme: "Token", header: "X-Api-Key", notes: [] },
  });
  click(doc, doc.getElementById("btn-connect"));
  await settle(40);
  click(doc, doc.getElementById("btn-auth-plugin"));
  await settle(40);

  const applied = bodyOf(calls, "/api/auth");
  check("a fetched token is applied to the live session", !!applied, pathsOf(calls));
  check("with the scheme and header the script asked for",
    !!applied && applied.token === "fresh" && applied.scheme === "Token" && applied.header === "X-Api-Key",
    JSON.stringify(applied));
  check("and the sidebar shows what is now being sent",
    doc.getElementById("cfg-auth-scheme").value === "Token"
    && doc.getElementById("cfg-auth-header").value === "X-Api-Key",
    `${doc.getElementById("cfg-auth-scheme").value} / ${doc.getElementById("cfg-auth-header").value}`);
}

{
  const { doc, calls } = await start(withPlugin(), {
    "/api/auth/plugin": failsWith("the auth plugin exited 1: no session"),
  });
  click(doc, doc.getElementById("btn-auth-plugin"));
  await settle(40);
  check("a script that fails leaves the Token field alone",
    doc.getElementById("cfg-token").value === "", doc.getElementById("cfg-token").value);
  check("and says why", /exited 1: no session/.test(doc.body.textContent));
  check("nothing was connected behind it", !calls.some((c) => c.path === "/api/connect"), pathsOf(calls));
  check("the button can be tried again", !doc.getElementById("btn-auth-plugin").disabled);
}

// --- Connect ---------------------------------------------------------------

{
  const { doc, calls } = await start(withPlugin(), { "/api/connect": CONNECTED });
  click(doc, doc.getElementById("btn-connect"));
  await settle(40);

  const order = calls.map((c) => c.path).filter((p) => p === "/api/auth/plugin" || p === "/api/connect");
  check("Connect runs the script first", order[0] === "/api/auth/plugin" && order[1] === "/api/connect",
    order.join(" -> "));
  check("and connects with what it printed",
    bodyOf(calls, "/api/connect").bearer_token === "from-the-script",
    JSON.stringify(bodyOf(calls, "/api/connect").bearer_token));
  check("the field shows the token that went out",
    doc.getElementById("cfg-token").value === "from-the-script");
}

{
  const { doc, calls } = await start(http(), { "/api/connect": CONNECTED });
  click(doc, doc.getElementById("btn-connect"));
  await settle(40);
  check("without a script Connect is unchanged",
    !calls.some((c) => c.path === "/api/auth/plugin"), pathsOf(calls));
}

{
  const { doc, calls } = await start(withPlugin(), {
    "/api/connect": CONNECTED,
    "/api/auth/plugin": failsWith("no auth plugin at /tmp/token.py"),
  });
  click(doc, doc.getElementById("btn-connect"));
  await settle(40);
  check("a script that fails stops the connection",
    !calls.some((c) => c.path === "/api/connect"), pathsOf(calls));
  check("and says why, where connection errors go",
    /no auth plugin at/.test(doc.getElementById("connect-error").textContent),
    doc.getElementById("connect-error").textContent);
  check("Connect is usable again", !doc.getElementById("btn-connect").disabled);
  check("and back to its own label", doc.getElementById("btn-connect").textContent === "Connect",
    doc.getElementById("btn-connect").textContent);
}

finish();
