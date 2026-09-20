/* The log panel: what counts as a failure, and which ones the UI reports itself. */

import { boot, check, click, finish, settle } from "./harness.mjs";

const rows = (doc) => Array.from(doc.querySelectorAll("#log-list .log-row"));
const summaries = (doc) => rows(doc).map((row) => row.querySelector(".summary").textContent);
const showTab = (doc, name) => click(doc, doc.querySelector(`#log-tabs button[data-log="${name}"]`));

// --- failures the backend never sees --------------------------------------

{
  // A dead fetch: the response never arrives, so nothing could have been logged server side.
  const { doc, window } = await boot();
  window.fetch = async () => { throw new TypeError("Failed to fetch"); };
  click(doc, doc.getElementById("btn-tools-refresh"));
  await settle();

  const failures = summaries(doc).filter((text) => text.includes("unreachable"));
  check("a dead fetch lands in the log", failures.length === 1, summaries(doc).join(" | "));
  check("the row names what was being done", failures[0].startsWith("tools/list:"), failures[0]);
  check("the row is tagged as an error",
    rows(doc).some((row) => row.querySelector(".tag").textContent === "error"));
  check("the row is marked as a failure", rows(doc).some((row) => row.classList.contains("failure")));
  check("the Errors tab counts it", doc.getElementById("count-errors").textContent === "1",
    doc.getElementById("count-errors").textContent);
  check("the user still gets a toast", !!doc.querySelector("#toasts .toast.error"));
}

{
  // Arguments that never leave the browser.
  const { doc } = await boot({
    routes: { "/api/tools/list": { result: { tools: [{ name: "add", description: "" }] } } },
  });
  click(doc, doc.getElementById("btn-tools-refresh"));
  await settle();
  click(doc, doc.querySelector("#tools-list .item"));
  doc.getElementById("tool-raw-toggle").checked = true;
  doc.getElementById("tool-raw").value = "{not json";
  click(doc, doc.getElementById("btn-tool-call"));
  await settle();

  check("a client-side parse failure is logged too",
    summaries(doc).some((text) => text.startsWith("tools/call: Invalid JSON")), summaries(doc).join(" | "));
}

// --- failures the backend already reported ---------------------------------

{
  const { doc, window } = await boot();
  window.fetch = async () => ({
    ok: false,
    status: 409,
    statusText: "Conflict",
    json: async () => ({ error: { kind: "not_connected", message: "not connected to a server" } }),
  });
  click(doc, doc.getElementById("btn-tools-refresh"));
  await settle();

  check("a backend-shaped error is not logged twice", summaries(doc).length === 0, summaries(doc).join(" | "));
  check("but the user is still told", !!doc.querySelector("#toasts .toast.error"));
}

// --- the Errors tab collects failures across kinds -------------------------

const HISTORY = {
  events: [
    { seq: 1, kind: "message", ts: 1, direction: "out", message: { method: "ping", id: 1 } },
    { seq: 2, kind: "error", ts: 2, source: "POST /api/tools/call", text: "JSON-RPC error -32601: no such tool" },
    { seq: 3, kind: "transport", ts: 3, level: "info", text: "connecting" },
    { seq: 4, kind: "transport", ts: 4, level: "error", text: "connection refused" },
    { seq: 5, kind: "transport", ts: 5, level: "warning", text: "header dropped" },
    { seq: 6, kind: "log", ts: 6, level: "info", data: "hello" },
    { seq: 7, kind: "log", ts: 7, level: "critical", data: "the disk is on fire" },
    { seq: 8, kind: "stderr", ts: 8, text: "traceback noise" },
  ],
};

{
  const { doc } = await boot({ routes: { "/api/history?limit=500": HISTORY } });
  check("every event shows up under All", rows(doc).length === 8, String(rows(doc).length));
  check("the error count covers the whole history", doc.getElementById("count-errors").textContent === "4",
    doc.getElementById("count-errors").textContent);

  showTab(doc, "error");
  const shown = summaries(doc);
  check("the Errors tab keeps only the failures", shown.length === 4, shown.join(" | "));
  check("an rpc error is one of them", shown.some((t) => t.includes("-32601")));
  check("so is a transport error", shown.some((t) => t.includes("connection refused")));
  check("a transport warning counts as well", shown.some((t) => t.includes("header dropped")));
  check("so does a server log at critical", shown.some((t) => t.includes("disk is on fire")));
  check("ordinary traffic is filtered out", !shown.some((t) => t.includes("ping")));
  check("an info-level server log is filtered out", !shown.some((t) => t.includes("hello")));

  showTab(doc, "log");
  check("the other tabs still filter by kind", summaries(doc).length === 2, summaries(doc).join(" | "));

  showTab(doc, "all");
  check("switching back restores the full log", rows(doc).length === 8, String(rows(doc).length));
}

{
  const { doc } = await boot({ routes: { "/api/history?limit=500": HISTORY } });
  showTab(doc, "error");
  click(doc, doc.getElementById("btn-log-clear"));
  await settle();
  check("clearing empties the log", rows(doc).length === 0);
  check("clearing resets the error count", doc.getElementById("count-errors").textContent === "");
}

// --- an error row carries its detail ----------------------------------------

{
  const { doc } = await boot({
    routes: {
      "/api/history?limit=500": {
        events: [{ seq: 1, kind: "error", ts: 1, source: "POST /api/tools/call", text: "boom", detail: { hint: "look here" } }],
      },
    },
  });
  click(doc, rows(doc)[0]);
  check("expanding an error shows the server's detail",
    rows(doc)[0].querySelector("pre").textContent.includes("look here"),
    rows(doc)[0].querySelector("pre").textContent);
}

finish();
