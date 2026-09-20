/* The "copy as curl" dialog, from the tool panel and from the Raw request tab. */

import { boot, check, choose, click, finish, settle, type } from "./harness.mjs";

const TOOLS = {
  result: {
    tools: [{
      name: "add",
      description: "Adds two numbers",
      inputSchema: { type: "object", properties: { a: { type: "number" }, b: { type: "number" } } },
    }],
  },
};

const CURL = { command: "curl -sS -X POST http://x/mcp", notes: ["Set these before running: MCP_AUTHORIZATION."], masked: ["MCP_AUTHORIZATION"] };

const booted = () => boot({ routes: { "/api/tools/list": TOOLS, "/api/curl": CURL } });
const byId = (doc, id) => doc.getElementById(id);
const curlCalls = (calls) =>
  calls.filter((c) => c.path === "/api/curl").map((c) => JSON.parse(c.options.body));

// --- from the tool panel ----------------------------------------------------

{
  const { doc, calls, errors } = await booted();
  click(doc, byId(doc, "btn-tools-refresh"));
  await settle();
  click(doc, doc.querySelector("#tools-list .item"));
  check("the tool panel offers a curl button", !!byId(doc, "btn-tool-curl"));

  click(doc, byId(doc, "btn-tool-curl"));
  await settle();

  const sent = curlCalls(calls).pop();
  check("it renders a tools/call", !!sent && sent.method === "tools/call", JSON.stringify(sent));
  check("carrying the tool name", !!sent && sent.params.name === "add");
  check("and the form's arguments", !!sent && typeof sent.params.arguments === "object");
  check("secrets are masked by default", !!sent && sent.mask_secrets === true);
  check("the command is shown", byId(doc, "curl-text").value === CURL.command);
  check("and the notes with it",
    byId(doc, "curl-notes").textContent.includes("MCP_AUTHORIZATION"),
    byId(doc, "curl-notes").textContent);
  check("no JS errors", errors.length === 0, errors.join(" | "));
}

{
  const { doc, calls } = await booted();
  click(doc, byId(doc, "btn-tools-refresh"));
  await settle();
  click(doc, doc.querySelector("#tools-list .item"));
  click(doc, byId(doc, "btn-tool-curl"));
  await settle();

  byId(doc, "curl-secrets").checked = true;
  byId(doc, "curl-secrets").dispatchEvent(new doc.defaultView.Event("change", { bubbles: true }));
  await settle();
  check("asking for secrets turns masking off", curlCalls(calls).pop().mask_secrets === false);
}

// --- the basic methods ------------------------------------------------------

const PARAMLESS = ["tools/list", "resources/list", "resources/templates/list", "prompts/list", "ping"];

for (const [button, method] of [
  ["btn-tools-curl", "tools/list"],
  ["btn-resources-curl", "resources/list"],
  ["btn-prompts-curl", "prompts/list"],
]) {
  const { doc, calls } = await booted();
  click(doc, byId(doc, button));
  await settle();
  const sent = curlCalls(calls).pop();
  check(`${button} renders ${method}`, !!sent && sent.method === method, JSON.stringify(sent));
  check(`${method} is sent without params`, !!sent && sent.params === null);
}

{
  const { doc, calls } = await booted();
  click(doc, byId(doc, "btn-tools-curl"));
  await settle();
  const options = Array.from(byId(doc, "curl-method").options).map((o) => o.value);
  check("every parameterless method is offered", JSON.stringify(options) === JSON.stringify(PARAMLESS),
    JSON.stringify(options));
  check("the one it was opened for is selected", byId(doc, "curl-method").value === "tools/list");

  // resources/templates/list has no button of its own; the picker is how you reach it.
  choose(doc, byId(doc, "curl-method"), "resources/templates/list");
  await settle();
  check("switching method re-renders", curlCalls(calls).pop().method === "resources/templates/list");
}

{
  // `ping` is gone at 2026-07-28, so the picker must not offer it there either.
  const connected = { status: "connected", transport: "streamable-http", protocol_version: "2026-07-28", pending: [] };
  const { doc } = await boot({
    routes: { "/api/curl": CURL, "/api/tools/list": TOOLS, "/api/connect": connected },
  });
  click(doc, byId(doc, "btn-connect"));
  await settle();
  click(doc, byId(doc, "btn-tools-curl"));
  await settle();
  const options = Array.from(byId(doc, "curl-method").options).map((o) => o.value);
  check("a modern session is not offered ping", !options.includes("ping"), JSON.stringify(options));
}

{
  const { doc, calls } = await booted();
  click(doc, byId(doc, "btn-tools-refresh"));
  await settle();
  click(doc, doc.querySelector("#tools-list .item"));
  click(doc, byId(doc, "btn-tool-curl"));
  await settle();
  check("a parameterised method is offered alongside the basic ones",
    byId(doc, "curl-method").value === "tools/call");

  choose(doc, byId(doc, "curl-method"), "tools/list");
  await settle();
  check("its params are not carried onto another method", curlCalls(calls).pop().params === null);

  choose(doc, byId(doc, "curl-method"), "tools/call");
  await settle();
  check("and come back when it is reselected", curlCalls(calls).pop().params.name === "add");
}

// --- from the Raw request tab -----------------------------------------------

{
  const { doc, calls } = await booted();
  type(doc, byId(doc, "raw-method"), "resources/read");
  type(doc, byId(doc, "raw-params"), '{"uri": "demo://time"}');
  click(doc, byId(doc, "btn-raw-curl"));
  await settle();

  const sent = curlCalls(calls).pop();
  check("the raw tab renders its own method", !!sent && sent.method === "resources/read", JSON.stringify(sent));
  check("with the params as typed", !!sent && sent.params.uri === "demo://time");
}

{
  const { doc, calls } = await booted();
  type(doc, byId(doc, "raw-method"), "tools/list");
  type(doc, byId(doc, "raw-params"), "{not json");
  click(doc, byId(doc, "btn-raw-curl"));
  await settle();
  check("unparseable params never reach the backend", curlCalls(calls).length === 0);
  check("and no dialog is opened over a broken request", !byId(doc, "curl-text"));
}

{
  const { doc, calls } = await booted();
  type(doc, byId(doc, "raw-method"), "   ");
  click(doc, byId(doc, "btn-raw-curl"));
  await settle();
  check("a blank method is refused in the UI", curlCalls(calls).length === 0);
}

{
  const { doc } = await booted();
  type(doc, byId(doc, "raw-method"), "tools/list");
  click(doc, byId(doc, "btn-raw-curl"));
  await settle();
  click(doc, byId(doc, "curl-close"));
  check("closing empties the dialog", !byId(doc, "curl-text"));
}

finish();
