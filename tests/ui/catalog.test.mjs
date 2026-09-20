/* The tools/resources/prompts catalog: what a preset switch clears, and how a
   tool result is laid out. */

import { boot, check, choose, click, finish, settle } from "./harness.mjs";

const PRESETS = [
  { name: "alpha", config: { transport: "streamable-http", url: "http://alpha/mcp" } },
  { name: "beta", config: { transport: "streamable-http", url: "http://beta/mcp" } },
];

const TOOLS = { result: { tools: [{ name: "add", description: "Adds two numbers" }] } };

const booted = (routes = {}) => boot({ servers: PRESETS, routes: { "/api/tools/list": TOOLS, ...routes } });

const items = (doc, id) => Array.from(doc.querySelectorAll(`#${id} .item`));

// --- a preset switch empties the catalog ------------------------------------

{
  const { doc } = await booted();
  click(doc, doc.getElementById("btn-tools-refresh"));
  await settle();
  check("the listed tool shows up", items(doc, "tools-list").length === 1);
  check("the tab count follows", doc.getElementById("count-tools").textContent === "1",
    doc.getElementById("count-tools").textContent);

  click(doc, items(doc, "tools-list")[0]);
  check("selecting a tool opens its detail", !!doc.getElementById("btn-tool-call"));

  choose(doc, doc.getElementById("preset-select"), "beta");
  check("the preset's config lands in the sidebar",
    doc.getElementById("cfg-url").value === "http://beta/mcp", doc.getElementById("cfg-url").value);
  check("the previous server's tools are gone", items(doc, "tools-list").length === 0);
  check("the tab count is cleared", doc.getElementById("count-tools").textContent === "",
    doc.getElementById("count-tools").textContent);
  check("the detail pane falls back to the empty slot",
    !!doc.querySelector("#tools-detail .empty") && !doc.getElementById("btn-tool-call"),
    doc.getElementById("tools-detail").innerHTML.slice(0, 80));
  check("resources and prompts are cleared too",
    !!doc.querySelector("#resources-detail .empty") && !!doc.querySelector("#prompts-detail .empty"));
}

{
  const { doc } = await booted();
  click(doc, doc.getElementById("btn-tools-refresh"));
  await settle();
  choose(doc, doc.getElementById("preset-select"), "");
  check("picking '— none —' leaves the catalog alone", items(doc, "tools-list").length === 1);
}

// --- what a connection actually fetches -------------------------------------

const CONNECTED = { status: "connected", target: "stdio: demo", pending: [] };
const LISTS = {
  "/api/connect": CONNECTED,
  "/api/tools/list": TOOLS,
  "/api/resources/list": { result: { resources: [{ uri: "demo://time", name: "time" }] } },
  "/api/resources/templates/list": { result: { resourceTemplates: [] } },
  "/api/prompts/list": { result: { prompts: [{ name: "review", description: "" }] } },
};

const listed = (calls) => calls.map((c) => c.path).filter((path) => path.endsWith("/list"));

{
  const { doc, calls } = await boot({ servers: PRESETS, routes: LISTS });
  click(doc, doc.getElementById("btn-connect"));
  await settle();

  check("connecting lists the tools", listed(calls).includes("/api/tools/list"));
  check("and nothing else", listed(calls).length === 1, listed(calls).join(" | "));
  check("the tools show up", items(doc, "tools-list").length === 1);

  const before = calls.length;
  click(doc, doc.querySelector('#main-tabs button[data-tab="resources"]'));
  await settle();
  const onOpen = listed(calls.slice(before));
  check("opening Resources lists them then",
    onOpen.includes("/api/resources/list") && onOpen.includes("/api/resources/templates/list"), onOpen.join(" | "));
  check("the resources show up", items(doc, "resources-list").length === 1);
  check("prompts are still untouched", !listed(calls).includes("/api/prompts/list"));

  const settled = calls.length;
  click(doc, doc.querySelector('#main-tabs button[data-tab="tools"]'));
  click(doc, doc.querySelector('#main-tabs button[data-tab="resources"]'));
  await settle();
  check("coming back does not list them again", listed(calls.slice(settled)).length === 0,
    listed(calls.slice(settled)).join(" | "));

  const beforePrompts = calls.length;
  click(doc, doc.querySelector('#main-tabs button[data-tab="prompts"]'));
  await settle();
  check("opening Prompts lists them", listed(calls.slice(beforePrompts)).includes("/api/prompts/list"));
  check("the prompts show up", items(doc, "prompts-list").length === 1);
}

{
  // Tabs that hold no catalog must not trigger anything.
  const { doc, calls } = await boot({ servers: PRESETS, routes: LISTS });
  click(doc, doc.getElementById("btn-connect"));
  await settle();
  const before = calls.length;
  click(doc, doc.querySelector('#main-tabs button[data-tab="roots"]'));
  click(doc, doc.querySelector('#main-tabs button[data-tab="raw"]'));
  await settle();
  check("Roots and Raw request fetch nothing", calls.length === before, listed(calls.slice(before)).join(" | "));
}

{
  const { doc, calls } = await boot({ servers: PRESETS, routes: LISTS });
  click(doc, doc.getElementById("btn-connect"));
  await settle();
  click(doc, doc.querySelector('#main-tabs button[data-tab="resources"]'));
  await settle();

  choose(doc, doc.getElementById("preset-select"), "beta");
  check("switching preset drops the resources", items(doc, "resources-list").length === 0);

  const before = calls.length;
  click(doc, doc.querySelector('#main-tabs button[data-tab="tools"]'));
  click(doc, doc.querySelector('#main-tabs button[data-tab="resources"]'));
  await settle();
  check("and they are not re-listed against the old connection",
    listed(calls.slice(before)).length === 0, listed(calls.slice(before)).join(" | "));
}

{
  // Nothing is listed until there is a connection to list against.
  const { doc, calls } = await boot({ servers: PRESETS, routes: LISTS });
  click(doc, doc.querySelector('#main-tabs button[data-tab="prompts"]'));
  await settle();
  check("an unconnected inspector fetches no catalog", listed(calls).length === 0, listed(calls).join(" | "));
}

{
  // A server that takes its time must not read as an empty one.
  const { doc, window } = await boot({ servers: PRESETS, routes: LISTS });
  click(doc, doc.getElementById("btn-connect"));
  await settle();

  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const realFetch = window.fetch;
  window.fetch = async (path, options) => {
    if (path === "/api/resources/list") await held;
    return realFetch(path, options);
  };

  click(doc, doc.querySelector('#main-tabs button[data-tab="resources"]'));
  await settle();
  check("a pending list says so", doc.querySelector("#resources-list .empty").textContent === "Listing…",
    doc.querySelector("#resources-list .empty").textContent);

  release();
  await settle();
  check("and gives way to the listing", items(doc, "resources-list").length === 1);
}

// --- list_changed notifications --------------------------------------------

{
  const { doc, window, calls } = await boot({ servers: PRESETS, routes: LISTS });
  click(doc, doc.getElementById("btn-connect"));
  await settle();

  const before = calls.length;
  window.pushEvent({ seq: 1, kind: "notification", ts: 1, method: "notifications/resources/list_changed" });
  await settle();
  check("a list_changed for an unopened tab fetches nothing",
    listed(calls.slice(before)).length === 0, listed(calls.slice(before)).join(" | "));

  window.pushEvent({ seq: 2, kind: "notification", ts: 2, method: "notifications/tools/list_changed" });
  await settle();
  check("a list_changed for the open tab refreshes it",
    listed(calls.slice(before)).includes("/api/tools/list"));
}

// --- structuredContent comes first ------------------------------------------

const CALL = {
  result: {
    content: [{ type: "text", text: "UNSTRUCTURED-TEXT" }],
    structuredContent: { sum: 7 },
  },
  elapsed_ms: 3,
};

{
  const { doc } = await booted({ "/api/tools/call": CALL });
  click(doc, doc.getElementById("btn-tools-refresh"));
  await settle();
  click(doc, items(doc, "tools-list")[0]);
  click(doc, doc.getElementById("btn-tool-call"));
  await settle();

  const html = doc.getElementById("tool-result").innerHTML;
  const structured = html.indexOf("structuredContent");
  const text = html.indexOf("UNSTRUCTURED-TEXT");
  check("both halves of the result are rendered", structured !== -1 && text !== -1, html.slice(0, 200));
  check("structuredContent is shown above the content blocks", structured < text, `${structured} vs ${text}`);
  check("the content blocks are labelled once structured output is present",
    html.indexOf(">content<") > structured, html);
  check("the raw result is still available", html.includes("Raw result"));
}

{
  const { doc } = await booted({
    "/api/tools/call": { result: { content: [{ type: "text", text: "PLAIN" }] }, elapsed_ms: 1 },
  });
  click(doc, doc.getElementById("btn-tools-refresh"));
  await settle();
  click(doc, items(doc, "tools-list")[0]);
  click(doc, doc.getElementById("btn-tool-call"));
  await settle();

  const html = doc.getElementById("tool-result").innerHTML;
  check("a result without structured output gains no stray labels",
    html.includes("PLAIN") && !html.includes("structuredContent") && !html.includes(">content<"), html.slice(0, 200));
}

finish();
