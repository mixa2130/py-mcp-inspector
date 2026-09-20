/* Loads the real index.html + app.js into jsdom with the backend stubbed out. */

import { JSDOM, VirtualConsole } from "jsdom";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "pymcpinspector", "static");

const META = {
  version: "0.0.0-test",
  protocol_version: "2026-07-28",
  log_levels: ["debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"],
  protocol_versions: [
    { value: "auto", era: "auto" },
    { value: "2026-07-28", era: "modern" },
    { value: "2025-11-25", era: "handshake" },
    { value: "2025-06-18", era: "handshake" },
    { value: "2025-03-26", era: "handshake" },
    { value: "2024-11-05", era: "handshake" },
  ],
  config_path: "/tmp/servers.json",
};

/** A route that answers the way the backend's error handlers do: a status the
 *  UI treats as a failure, and a body carrying the message it should show. */
export const failsWith = (message, { status = 400, kind = "bad_request" } = {}) =>
  ({ __status: status, body: { error: { kind, message } } });

/** Boot the UI. `storage` seeds localStorage, as a returning visitor would have it;
 *  `servers` seeds the saved presets; `routes` overrides or adds endpoint bodies.
 *  A route may be a function of the request, for an endpoint whose answer changes
 *  between calls, or `failsWith(...)` for one that refuses. */
export async function boot({ storage = {}, servers = [], routes = {} } = {}) {
  const virtualConsole = new VirtualConsole();
  const errors = [];
  virtualConsole.on("jsdomError", (error) => errors.push(String(error)));
  virtualConsole.on("error", (...args) => errors.push(args.join(" ")));

  // jsdom will not fetch the external stylesheet, so inline it: without the
  // cascade, a purely visual regression (a section that never actually hides)
  // would sail past these tests.
  const markup = readFileSync(join(STATIC, "index.html"), "utf8").replace(
    '<link rel="stylesheet" href="/static/styles.css">',
    `<style>${readFileSync(join(STATIC, "styles.css"), "utf8")}</style>`,
  );

  const dom = new JSDOM(markup, {
    runScripts: "outside-only",
    url: "http://localhost:6288/",
    virtualConsole,
  });
  const { window } = dom;

  const store = { ...storage };
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key) => (key in store ? store[key] : null),
      setItem: (key, value) => { store[key] = String(value); },
      removeItem: (key) => { delete store[key]; },
    },
  });

  const calls = [];
  window.fetch = async (path, options) => {
    calls.push({ path, options });
    const override = Object.keys(routes).find((route) => route === path);
    const chosen = override !== undefined ? routes[override] : undefined;
    const answer =
      override !== undefined ? (typeof chosen === "function" ? chosen({ path, options }) : chosen)
      : path === "/api/meta" ? META
      : path === "/api/servers" ? { servers }
      : path === "/api/status" ? { status: "idle", pending: [] }
      : path.startsWith("/api/history") ? { events: [] }
      : {};
    const refused = answer && answer.__status !== undefined;
    const status = refused ? answer.__status : 200;
    const body = refused ? answer.body : answer;
    return {
      ok: status < 400,
      status,
      statusText: status === 200 ? "OK" : "Error",
      json: async () => body,
    };
  };
  window.WebSocket = class {
    constructor() { this.readyState = 0; }
    close() {}
  };

  window.eval(readFileSync(join(STATIC, "app.js"), "utf8"));
  await new Promise((resolve) => setTimeout(resolve, 60));
  return { window, doc: window.document, errors, calls, store };
}

/* A minimal assertion runner: no framework, one process exit code. */

let failures = 0;

export function check(label, condition, detail = "") {
  console.log(`${condition ? "  ok  " : "FAIL  "}${label}${condition ? "" : `   <- ${detail}`}`);
  if (!condition) failures++;
}

export function finish() {
  console.log(failures ? `\n${failures} failure(s)` : "\nall checks passed");
  process.exit(failures ? 1 : 0);
}

export const click = (doc, element) =>
  element.dispatchEvent(new doc.defaultView.MouseEvent("click", { bubbles: true }));

export const press = (doc, element, key) =>
  element.dispatchEvent(new doc.defaultView.KeyboardEvent("keydown", { key, bubbles: true }));

/** Computed `display`, i.e. what the viewer actually sees. */
export const displayOf = (doc, element) => doc.defaultView.getComputedStyle(element).display;

export const type = (doc, element, value) => {
  element.value = value;
  element.dispatchEvent(new doc.defaultView.Event("input", { bubbles: true }));
};

/** Pick an option, the way a user does: a new value plus a `change` event. */
export const choose = (doc, element, value) => {
  element.value = value;
  element.dispatchEvent(new doc.defaultView.Event("change", { bubbles: true }));
};

/** Let the pending fetch stubs and their handlers run. */
export const settle = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));
