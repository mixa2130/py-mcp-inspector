/* PyMCPinspector UI — plain ES modules-free JS, no build step. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  meta: null,
  status: { status: "idle" },
  headers: [],
  env: [],
  roots: [],
  tools: [],
  resources: [],
  resourceTemplates: [],
  prompts: [],
  selected: { tools: null, resources: null, prompts: null },
  loaded: { tools: false, resources: false, prompts: false },
  loading: { tools: false, resources: false, prompts: false },
  attached: false,
  epoch: 0,
  events: [],
  errors: 0,
  logFilter: "all",
  pending: [],
  presets: [],
  // What `presetShape(readConfig())` returned the moment the selected preset was
  // loaded or saved. Comparing against it is what the `edited` badge reports;
  // comparing the stored config directly would trip over `args` being a string here.
  presetBaseline: null,
  // The credential the live session is known to be carrying, and the timer that
  // walks an edit over to it. Null means "not known", so the next edit is sent
  // rather than compared against a value that may never have gone out.
  liveAuth: null,
  authApplyTimer: null,
  streaming: false,
};

/* ------------------------------------------------------------------ utils */

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function highlightJson(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  if (text === undefined) return "";
  return escapeHtml(text).replace(
    /("(\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(\.\d+)?([eE][+-]?\d+)?)/g,
    (match) => {
      let cls = "tok-num";
      if (/^"/.test(match)) cls = /:$/.test(match) ? "tok-key" : "tok-str";
      else if (/true|false/.test(match)) cls = "tok-bool";
      else if (/null/.test(match)) cls = "tok-null";
      return `<span class="${cls}">${match}</span>`;
    });
}

function jsonBlock(value) {
  return `<pre class="json">${highlightJson(value)}</pre>`;
}

function toast(message, kind = "") {
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  $("#toasts").appendChild(node);
  setTimeout(() => node.remove(), kind === "error" ? 8000 : 3500);
}

async function api(path, body, method) {
  const options = { method: method || (body === undefined ? "GET" : "POST") };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    throw new Error(`inspector unreachable: ${err.message}`);
  }
  let data = {};
  try { data = await response.json(); } catch { /* empty body */ }
  if (!response.ok) {
    const message = data?.error?.message || data?.detail || `${response.status} ${response.statusText}`;
    const failure = new Error(typeof message === "string" ? message : JSON.stringify(message));
    // A shaped `error` body means the backend already published the failure on
    // the event bus; logging it again here would double every row.
    failure.reported = Boolean(data?.error);
    throw failure;
  }
  return data;
}

/* --------------------------------------------------------------- failures */

let uiSeq = 0;

/** Put a failure in the log panel — the half of them the backend never sees. */
function logError(source, message, detail) {
  pushEvent({
    seq: `ui-${++uiSeq}`,
    kind: "error",
    ts: Date.now() / 1000,
    origin: "ui",
    source,
    text: String(message),
    detail: detail ?? null,
  });
}

/** Log a caught error unless the backend already did, and hand back its text. */
function logFailure(source, err) {
  const message = err && err.message ? err.message : String(err);
  if (!(err && err.reported)) logError(source, message);
  return message;
}

/** The usual pairing: a toast for now, a log row for afterwards. */
function fail(source, err) {
  toast(logFailure(source, err), "error");
}

function openModal(html) {
  $("#modal").innerHTML = html;
  $("#modal-backdrop").classList.add("open");
}
function closeModal() {
  $("#modal-backdrop").classList.remove("open");
  $("#modal").innerHTML = "";
}
$("#modal-backdrop").addEventListener("click", (e) => { if (e.target.id === "modal-backdrop") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

/* ------------------------------------------------------ connection config */

function readConfig() {
  syncRowEditors();
  return {
    transport: $("#cfg-transport").value,
    url: $("#cfg-url").value.trim(),
    headers: state.headers.filter((h) => h.name || h.value),
    auth_header_name: $("#cfg-auth-header").value.trim() || "Authorization",
    auth_scheme: $("#cfg-auth-scheme").value.trim(),
    bearer_token: $("#cfg-token").value,
    auth_plugin: $("#cfg-auth-plugin").value.trim(),
    auth_plugin_args: $("#cfg-auth-plugin-args").value,
    verify_tls: $("#cfg-verify-tls").checked,
    ca_bundle: $("#cfg-ca-bundle").value.trim(),
    client_cert: $("#cfg-client-cert").value.trim(),
    client_key: $("#cfg-client-key").value.trim(),
    client_key_password: $("#cfg-client-key-password").value,
    http_timeout: Number($("#cfg-http-timeout").value) || 30,
    sse_read_timeout: Number($("#cfg-sse-timeout").value) || 300,
    command: $("#cfg-command").value.trim(),
    args: $("#cfg-args").value,
    env: state.env.filter((e) => e.name || e.value),
    cwd: $("#cfg-cwd").value.trim() || null,
    inherit_env: $("#cfg-inherit-env").checked,
    client_name: $("#cfg-client-name").value.trim() || "pymcpinspector",
    client_version: $("#cfg-client-version").value.trim() || "0.1.0",
    request_timeout: Number($("#cfg-request-timeout").value) || 60,
    protocol_version: $("#cfg-protocol-version").value || "auto",
    log_level: $("#cfg-log-level").value || null,
    roots: state.roots,
  };
}

function writeConfig(cfg) {
  $("#cfg-transport").value = cfg.transport || "streamable-http";
  $("#cfg-url").value = cfg.url || "";
  $("#cfg-auth-header").value = cfg.auth_header_name || "Authorization";
  $("#cfg-auth-scheme").value = cfg.auth_scheme ?? "Bearer";
  $("#cfg-token").value = cfg.bearer_token || "";
  $("#cfg-auth-plugin").value = cfg.auth_plugin || "";
  $("#cfg-auth-plugin-args").value = Array.isArray(cfg.auth_plugin_args)
    ? cfg.auth_plugin_args.join(" ") : (cfg.auth_plugin_args || "");
  $("#cfg-verify-tls").checked = cfg.verify_tls !== false;
  $("#cfg-ca-bundle").value = cfg.ca_bundle || "";
  $("#cfg-client-cert").value = cfg.client_cert || "";
  $("#cfg-client-key").value = cfg.client_key || "";
  $("#cfg-client-key-password").value = cfg.client_key_password || "";
  $("#cfg-http-timeout").value = cfg.http_timeout ?? 30;
  $("#cfg-sse-timeout").value = cfg.sse_read_timeout ?? 300;
  $("#cfg-command").value = cfg.command || "";
  $("#cfg-args").value = Array.isArray(cfg.args) ? cfg.args.join(" ") : (cfg.args || "");
  $("#cfg-cwd").value = cfg.cwd || "";
  $("#cfg-inherit-env").checked = cfg.inherit_env !== false;
  $("#cfg-client-name").value = cfg.client_name || "pymcpinspector";
  $("#cfg-client-version").value = cfg.client_version || "0.1.0";
  $("#cfg-request-timeout").value = cfg.request_timeout ?? 60;
  $("#cfg-protocol-version").value = cfg.protocol_version || "auto";
  $("#cfg-log-level").value = cfg.log_level || "";
  describeProtocolChoice();
  state.headers = (cfg.headers || []).map((h) => ({ ...h }));
  state.env = (cfg.env || []).map((e) => ({ ...e }));
  state.roots = (cfg.roots || []).map((r) => ({ ...r }));
  renderKeyValues();
  renderRoots();
  onTransportChange();
  refreshSectionSummaries();
  persistDraft();
}

const PROTOCOL_NOTES = {
  auto: "Probe server/discover, fall back to the initialize handshake — what the SDK's own client does.",
  modern: "Sent as a server/discover probe (per-request envelope era).",
  handshake: "Offered in initialize; the server may counter with an older revision.",
};

function describeProtocolChoice() {
  const value = $("#cfg-protocol-version").value || "auto";
  const era = (state.meta?.protocol_versions || []).find((v) => v.value === value)?.era || "auto";
  $("#protocol-hint").textContent = PROTOCOL_NOTES[era] || "";
}

/* ------------------------------------------------- collapsible sections */

const COLLAPSIBLE_SECTIONS = ["section-advanced", "section-tls"];
const COLLAPSE_KEY = "pymcpinspector.collapsed";

/** Defaults that make a field uninteresting to report on a collapsed header. */
const FIELD_DEFAULTS = {
  request_timeout: 60,
  http_timeout: 30,
  sse_read_timeout: 300,
  client_name: "pymcpinspector",
};

function readCollapseState() {
  try { return JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "{}"); }
  catch { return {}; }
}

function setCollapsed(section, collapsed, persist) {
  section.classList.toggle("collapsed", collapsed);
  $("h2", section).setAttribute("aria-expanded", String(!collapsed));
  if (!persist) return;
  try {
    localStorage.setItem(COLLAPSE_KEY, JSON.stringify({ ...readCollapseState(), [section.id]: collapsed }));
  } catch { /* private mode: the choice just won't survive a reload */ }
}

function initCollapsibleSections() {
  const remembered = readCollapseState();
  COLLAPSIBLE_SECTIONS.forEach((id) => {
    const section = document.getElementById(id);
    if (!section) return;
    // Collapsed unless this viewer opened it before.
    setCollapsed(section, remembered[id] !== false, false);
    const header = $("h2", section);
    header.addEventListener("click", () =>
      setCollapsed(section, !section.classList.contains("collapsed"), true));
    header.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        header.click();
      }
    });
  });
}

/* ------------------------------------------------------- the sidebar's box */

const LAYOUT_KEY = "pymcpinspector.layout";
const SIDEBAR_DEFAULT = 380;
const SIDEBAR_MIN = 260;
const SIDEBAR_STEP = 16;

/** The widest the sidebar may get: never so wide that the working area goes. */
const sidebarMax = () => Math.max(SIDEBAR_MIN, window.innerWidth - 360);

/** The width in force, read from the custom property that sets it. */
const sidebarWidth = () =>
  parseInt($("#app").style.getPropertyValue("--sidebar-width"), 10) || SIDEBAR_DEFAULT;

function readLayout() {
  try { return JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}"); }
  catch { return {}; }
}

function saveLayout(patch) {
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify({ ...readLayout(), ...patch })); }
  catch { /* private mode: the choice just won't survive a reload */ }
}

function setSidebarWidth(width, persist) {
  const clamped = Math.round(Math.min(Math.max(width, SIDEBAR_MIN), sidebarMax()));
  $("#app").style.setProperty("--sidebar-width", `${clamped}px`);
  const resizer = $("#sidebar-resizer");
  resizer.setAttribute("aria-valuenow", String(clamped));
  resizer.setAttribute("aria-valuemin", String(SIDEBAR_MIN));
  resizer.setAttribute("aria-valuemax", String(sidebarMax()));
  if (persist) saveLayout({ sidebarWidth: clamped });
  return clamped;
}

function setSidebarHidden(hidden, persist) {
  $("#app").classList.toggle("sidebar-hidden", hidden);
  const button = $("#btn-sidebar");
  button.setAttribute("aria-expanded", String(!hidden));
  button.title = hidden ? "Show the settings sidebar" : "Hide the settings sidebar";
  if (persist) saveLayout({ sidebarHidden: hidden });
}

function initSidebarLayout() {
  const remembered = readLayout();
  setSidebarWidth(Number(remembered.sidebarWidth) || SIDEBAR_DEFAULT, false);
  setSidebarHidden(remembered.sidebarHidden === true, false);

  $("#btn-sidebar").addEventListener("click", () =>
    setSidebarHidden(!$("#app").classList.contains("sidebar-hidden"), true));

  const resizer = $("#sidebar-resizer");
  resizer.addEventListener("mousedown", (down) => {
    const startX = down.clientX;
    const startWidth = sidebarWidth();
    let width = startWidth;
    const move = (event) => { width = setSidebarWidth(startWidth + (event.clientX - startX), false); };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      // One write when the drag ends, rather than one per mousemove.
      saveLayout({ sidebarWidth: width });
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
    down.preventDefault(); // dragging across the page must not select its text
  });
  resizer.addEventListener("keydown", (event) => {
    const step = { ArrowLeft: -SIDEBAR_STEP, ArrowRight: SIDEBAR_STEP }[event.key];
    if (step === undefined) return;
    event.preventDefault();
    setSidebarWidth(sidebarWidth() + step, true);
  });

  // A remembered width can outlive the window it was chosen in, so re-clamp
  // rather than let the sidebar squeeze the working area off a smaller screen.
  // The stored number is left alone: the next wide window gets it back.
  window.addEventListener("resize", () => setSidebarWidth(sidebarWidth(), false));
}

function summariseTls(cfg) {
  const parts = [];
  if (!cfg.verify_tls) parts.push("no verify");
  if (cfg.ca_bundle) parts.push("custom CA");
  if (cfg.client_cert) parts.push("client cert");
  return parts.join(" · ");
}

function summariseAdvanced(cfg) {
  const parts = [];
  if (cfg.protocol_version && cfg.protocol_version !== "auto") parts.push(cfg.protocol_version);
  if (cfg.log_level) parts.push(`log ${cfg.log_level}`);
  for (const [field, fallback] of Object.entries(FIELD_DEFAULTS)) {
    if (cfg[field] !== fallback) parts.push(`${field.replace(/_/g, " ")} ${cfg[field]}`);
  }
  return parts.join(" · ");
}

/** Keep a collapsed header honest about what it is hiding. */
function refreshSectionSummaries() {
  const cfg = readConfig();
  for (const [selector, text] of [
    ["#tls-summary", summariseTls(cfg)],
    ["#advanced-summary", summariseAdvanced(cfg)],
  ]) {
    const badge = $(selector);
    badge.textContent = text;
    badge.title = text;
    badge.hidden = !text;
  }
}

function persistDraft() {
  refreshSectionSummaries();
  refreshPresetState();
  try { localStorage.setItem("pymcpinspector.draft", JSON.stringify(readConfig())); } catch { /* private mode */ }
}

/** The sidebar as a preset stores it: no `Authorization` credential, either from
 *  the Token field or from a header row spelled that way. Mirrors the backend's
 *  `without_auth_secret`, so a pasted token never reads as an unsaved edit. */
function presetShape(config) {
  return JSON.stringify({
    ...config,
    bearer_token: "",
    headers: (config.headers || []).map((h) =>
      (h.name || "").trim().toLowerCase() === "authorization" ? { ...h, value: "" } : h),
  });
}

/** Enable what a selection allows, and say when the sidebar has drifted from it. */
function refreshPresetState() {
  const selected = $("#preset-select").value;
  const dirty = Boolean(selected) && state.presetBaseline !== null
    && presetShape(readConfig()) !== state.presetBaseline;
  $("#btn-preset-rename").disabled = !selected;
  $("#btn-preset-delete").disabled = !selected;
  // Saving over a preset that already matches the sidebar would be a no-op.
  $("#btn-preset-save").disabled = !selected || !dirty;
  $("#preset-dirty").hidden = !dirty;
}

/** Remember the sidebar as the saved state of `name` (null: nothing selected). */
function markPresetSaved(name) {
  $("#preset-select").value = name || "";
  state.presetBaseline = name ? presetShape(readConfig()) : null;
  refreshPresetState();
}

function restoreDraft() {
  try {
    const raw = localStorage.getItem("pymcpinspector.draft");
    if (raw) { writeConfig(JSON.parse(raw)); return true; }
  } catch { /* ignore */ }
  return false;
}

function onTransportChange() {
  const stdio = $("#cfg-transport").value === "stdio";
  $("#http-fields").hidden = stdio;
  $("#stdio-fields").hidden = !stdio;
  $("#section-headers").hidden = stdio;
  $("#section-auth").hidden = stdio;
  $("#section-tls").hidden = stdio;
  $("#section-env").hidden = !stdio;
}

/* ------------------------------------------------- generated header values */

/** Formats a header row can mint its value in.
 *
 * Only the UUID versions that hand back something new on every call are here.
 * v3 and v5 are a hash of a namespace and a name, so refreshing one would
 * return the same string it already holds, and v2 (DCE security) needs a POSIX
 * uid and domain the browser has no notion of.
 */
const VALUE_GENERATORS = [
  { value: "uuid1", label: "UUID v1", make: () => uuid1() },
  { value: "uuid4", label: "UUID v4", make: () => uuid4() },
  { value: "uuid7", label: "UUID v7", make: () => uuid7() },
];

/** A fresh value for `format`, or "" if nothing generates that. */
function generateValue(format) {
  return VALUE_GENERATORS.find((g) => g.value === format)?.make() || "";
}

// `crypto.randomUUID` is deliberately unused: it only exists in a secure
// context, and the inspector is reachable over plain http:// on a LAN address.
// `getRandomValues` has no such restriction.
function randomBytes(count) {
  return crypto.getRandomValues(new Uint8Array(count));
}

const hex2 = (byte) => byte.toString(16).padStart(2, "0");

/** 16 bytes as a UUID string, with the version and RFC 9562 variant stamped in. */
function formatUuid(bytes, version) {
  bytes[6] = (bytes[6] & 0x0f) | (version << 4);
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, hex2).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function uuid4() {
  return formatUuid(randomBytes(16), 4);
}

/** v7: a big-endian millisecond timestamp, then random — so values sort by age. */
function uuid7() {
  const bytes = randomBytes(16);
  let ms = Date.now();
  for (let i = 5; i >= 0; i -= 1) {
    bytes[i] = ms % 256;
    ms = Math.floor(ms / 256);
  }
  return formatUuid(bytes, 7);
}

/** 100-ns ticks between the Gregorian reform (1582-10-15) and the Unix epoch. */
const GREGORIAN_OFFSET = 122192928000000000n;

/** v1: a time-based UUID with a random node, as RFC 9562 §5.1 allows. */
function uuid1() {
  const random = randomBytes(10);
  // `Date` stops at milliseconds, so the remaining 100-ns digits are random:
  // two calls inside the same millisecond still differ, which is the whole
  // point of pressing refresh twice.
  const sub = ((random[0] << 8) | random[1]) % 10000;
  const ticks = GREGORIAN_OFFSET + BigInt(Date.now()) * 10000n + BigInt(sub);
  const time = (ticks & 0x0fffffffffffffffn).toString(16).padStart(15, "0");
  const clockSeq = ((((random[2] << 8) | random[3]) & 0x3fff) | 0x8000).toString(16);
  // A browser cannot see a MAC address, and the spec's answer to that is a
  // random node id with the multicast bit set, so it can never collide with one.
  const node = Array.from(random.slice(4, 10), (b, i) => hex2(i === 0 ? b | 0x01 : b)).join("");
  return `${time.slice(7)}-${time.slice(3, 7)}-1${time.slice(0, 3)}-${clockSeq}-${node}`;
}

/* --------------------------------------------------------- key/value rows */

/** Copy one key/value editor out of the DOM and into its row array.
 *
 * The DOM is the source of truth. Autofill, a password manager, an extension
 * and a browser's form restoration all set `.value` without firing `input`, so
 * a row that looks filled on screen would otherwise still be `{name: "",
 * value: ""}` in the array -- and `readConfig()` drops those, silently sending
 * a request without the header the viewer can see. `items` is refreshed in
 * place because the row handlers close over the array itself.
 */
function syncKvRows(container, items) {
  const rows = $$(".kv-row", container);
  items.length = rows.length;
  rows.forEach((row, index) => {
    const [toggle, name, value] = row.children;
    // A row without a generator stays the three fields it always was: the
    // key only appears where it was chosen, so drafts, presets and the
    // request body of a plain header row read exactly as before.
    const format = $(".kv-generator", row)?.value || "";
    items[index] = {
      name: name.value,
      value: value.value,
      enabled: toggle.checked,
      ...(format ? { generator: format } : {}),
    };
  });
}

/** The same, for the roots editor, whose rows carry no enabled toggle. */
function syncRootRows() {
  const container = $("#roots-list");
  if (!container) return;
  const rows = $$(".kv-row", container);
  state.roots.length = rows.length;
  rows.forEach((row, index) => {
    const [uri, name] = row.children;
    state.roots[index] = { uri: uri.value, name: name.value || null };
  });
}

/** Refresh every row array from what is currently on screen. */
function syncRowEditors() {
  syncKvRows($("#headers-list"), state.headers);
  syncKvRows($("#env-list"), state.env);
  syncRootRows();
}

function renderKeyValues() {
  renderKvList($("#headers-list"), state.headers, "Header name", "Value", { generators: true });
  renderKvList($("#env-list"), state.env, "VAR_NAME", "value");
}

function renderKvList(container, items, namePlaceholder, valuePlaceholder, { generators = false } = {}) {
  container.innerHTML = "";
  items.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = `kv-row${generators ? " generated" : ""}${item.enabled === false ? " disabled" : ""}`;
    row.innerHTML = `
      <input type="checkbox" ${item.enabled === false ? "" : "checked"} title="Enabled">
      <input type="text" value="${escapeHtml(item.name || "")}" placeholder="${namePlaceholder}" spellcheck="false">
      <input type="text" value="${escapeHtml(item.value || "")}" placeholder="${valuePlaceholder}" spellcheck="false">
      ${generators ? generatorControls(item) : ""}
      <button class="icon small kv-remove" title="Remove">✕</button>`;
    const [toggle, name, value] = row.children;
    // Every re-render rebuilds these rows from `items`, so anything the viewer
    // has on screen has to reach the array first or the redraw discards it.
    toggle.addEventListener("change", () => { syncRowEditors(); renderKeyValues(); persistDraft(); });
    name.addEventListener("input", persistDraft);
    value.addEventListener("input", persistDraft);
    $(".kv-remove", row).addEventListener("click", () => {
      syncRowEditors(); items.splice(index, 1); renderKeyValues(); persistDraft();
    });
    bindGenerator(row, items, index, value);
    container.appendChild(row);
  });
  if (!items.length) {
    container.innerHTML = '<p class="hint" style="margin:0">None.</p>';
  }
}

/** The format picker and its refresh button, for one row. */
function generatorControls(item) {
  const options = VALUE_GENERATORS.map((g) =>
    `<option value="${g.value}"${item.generator === g.value ? " selected" : ""}>${g.label}</option>`).join("");
  return `
      <select class="kv-generator" title="Mint this value instead of typing it">
        <option value=""${item.generator ? "" : " selected"}>Fixed</option>${options}
      </select>
      <button class="icon small kv-refresh" title="Generate a new value"
        ${item.generator ? "" : "disabled"}>⟳</button>`;
}

/** Wire one row's format picker, if it has one.
 *
 * Both handlers write the new value straight into the input instead of
 * re-rendering the list: a redraw would throw away the focus the viewer just
 * put on the refresh button, and nothing else about the row has changed.
 */
function bindGenerator(row, items, index, value) {
  const generator = $(".kv-generator", row);
  if (!generator) return;
  const refresh = $(".kv-refresh", row);
  // A UUID is 36 characters and the column is nowhere near that wide, so the
  // whole of it lives in the tooltip -- otherwise the viewer cannot read back
  // what was just minted without selecting the text.
  const mint = (format) => {
    const fresh = generateValue(format);
    if (!fresh) return;
    items[index].value = fresh;
    value.value = fresh;
    value.title = fresh;
  };
  if (items[index].generator) value.title = value.value;
  // What the row was set to before this change, which decides whether the
  // value on screen is ours to replace. Re-reading it from the array would
  // not do: `syncRowEditors` has already overwritten it with the new choice.
  let previous = items[index].generator || "";
  generator.addEventListener("change", () => {
    syncRowEditors();
    refresh.disabled = !generator.value;
    // A value typed by hand is not ours to overwrite -- that waits for the
    // refresh button. One we minted is: the row now says v7, so leaving a v4
    // sitting in it would make the picker a lie.
    if (generator.value && (previous || !value.value)) mint(generator.value);
    previous = generator.value;
    persistDraft();
  });
  refresh.addEventListener("click", () => {
    syncRowEditors();
    mint(items[index].generator);
    persistDraft();
  });
}

/* ----------------------------------------------------------- auth plugin */

/** Run the configured script and put what it printed in the Token field.
 *
 * The token lands in the field rather than going straight out, because the
 * sidebar is supposed to show what is being sent: a connection carrying a
 * credential the viewer cannot see in the form would be the one thing this
 * inspector is not for.
 */
async function runAuthPlugin(config) {
  const result = await api("/api/auth/plugin", config);
  $("#cfg-token").value = result.token;
  persistDraft();
  return result;
}

/** Whether there is a script to run at all. */
function refreshAuthPluginState() {
  $("#btn-auth-plugin").disabled = !$("#cfg-auth-plugin").value.trim();
}

/* ------------------------------------------------ credentials on the wire */

const AUTH_APPLY_DELAY = 250;   // ms of quiet before an edit reaches the session

/** What the authentication block currently says to send. */
function currentAuth() {
  return {
    token: $("#cfg-token").value,
    scheme: $("#cfg-auth-scheme").value.trim(),
    header: $("#cfg-auth-header").value.trim() || "Authorization",
  };
}

/** Whether a live session can take a new credential at all: an idle inspector
 *  has nothing to apply it to, and a stdio child keeps the environment it was
 *  spawned with. */
function authIsLive() {
  return state.status.status === "connected" && state.status.transport !== "stdio";
}

/** Put what the sidebar shows on the HTTP clients the transport already uses.
 *
 *  There is no button for this: a credential sitting in the form but not going
 *  out is exactly the confusion this inspector exists to remove. Returns whether
 *  a request actually went out, so the caller can say so.
 */
async function applyAuthToSession(auth = currentAuth()) {
  if (!authIsLive()) return false;
  if (state.liveAuth && JSON.stringify(state.liveAuth) === JSON.stringify(auth)) return false;
  state.liveAuth = auth;
  try {
    await api("/api/auth", auth);
    return true;
  } catch (err) {
    // It never reached the session, so the next edit has to try again instead of
    // comparing itself against a value that only ever existed here.
    state.liveAuth = null;
    fail("auth", err);
    return false;
  }
}

/** A token is typed or pasted character by character, and a keystroke is not a
 *  credential; the swap goes out once the field has been quiet. */
function scheduleAuthApply() {
  clearTimeout(state.authApplyTimer);
  if (!authIsLive()) return;
  state.authApplyTimer = setTimeout(async () => {
    const auth = currentAuth();
    if (await applyAuthToSession(auth)) {
      toast(auth.token.trim() ? "Token applied to the live session" : "Auth header cleared", "ok");
    }
  }, AUTH_APPLY_DELAY);
}

/* ------------------------------------------------------------- connection */

async function connect() {
  const config = readConfig();
  persistDraft();
  $("#connect-error").hidden = true;
  $("#btn-connect").disabled = true;
  $("#btn-connect").textContent = "Connecting…";
  try {
    if (config.auth_plugin) {
      // A token minted after the connection went out would be one the
      // handshake never carried, so the script runs first and the connection
      // waits for it. Its failure is a connection failure: no script, no token.
      $("#btn-connect").textContent = "Running the token script…";
      config.bearer_token = (await runAuthPlugin(config)).token;
      $("#btn-connect").textContent = "Connecting…";
    }
    const result = await api("/api/connect", config);
    if (result.error) {
      showConnectError(result.error.message);
      applyStatus(result.status || { status: "error", error: result.error.message });
    } else {
      applyStatus(result);
      // The connection carries this credential already; only an edit after it
      // is worth a swap.
      state.liveAuth = currentAuth();
      toast("Connected", "ok");
      clearCatalog();
      state.attached = true;
      // Only the tools go out at connect time; a server with slow or noisy
      // resource/prompt listings should not pay for them until they are opened.
      await listTools().catch((err) => fail("tools/list", err));
      ensureCatalog(activeTab());
    }
  } catch (err) {
    showConnectError(logFailure("connect", err));
  } finally {
    $("#btn-connect").disabled = false;
    $("#btn-connect").textContent = "Connect";
  }
}

function showConnectError(message) {
  const box = $("#connect-error");
  box.hidden = false;
  box.innerHTML = `<p class="banner">${escapeHtml(message)}</p>`;
}

async function disconnect() {
  applyStatus(await api("/api/disconnect", {}));
  clearCatalog();
}

/** Drop everything the last server told us about, back to the empty slots.
 *  A tool list belongs to one server: keeping it around after we point the
 *  sidebar somewhere else invites running yesterday's tool on today's target. */
function clearCatalog() {
  state.tools = []; state.resources = []; state.resourceTemplates = []; state.prompts = [];
  state.selected = { tools: null, resources: null, prompts: null };
  state.loaded = { tools: false, resources: false, prompts: false };
  // Detached: opening a tab must not re-list what we just cleared. Only a fresh
  // connection re-attaches the catalog; the List buttons stay available meanwhile.
  state.attached = false;
  state.epoch += 1;  // anything still in flight belongs to the previous server
  $("#count-tools").textContent = "";
  $("#count-resources").textContent = "";
  $("#count-prompts").textContent = "";
  renderTools(); renderResources(); renderPrompts();
}

function applyStatus(status) {
  state.status = status || { status: "idle" };
  const connected = state.status.status === "connected";
  const dot = $("#status-dot");
  dot.className = `status-dot ${state.status.status}`;
  $("#status-text").textContent = state.status.status;
  $("#status-target").textContent = state.status.target || "";
  $("#btn-disconnect").disabled = !connected;
  // `ping` was removed in the 2026-07-28 revision, so on such a session the
  // button could only ever produce -32601. The protocol badge sits right beside
  // it and says which version is in force, so its absence is not a mystery.
  const pingable = connected && sessionHasPing();
  $("#btn-ping").hidden = connected && !pingable;
  $("#btn-ping").disabled = !pingable;
  // With the session gone, nothing is carrying a credential any more: a pending
  // swap has nowhere to land, and the next session must not be compared against
  // what an earlier one was sending.
  if (!connected) {
    clearTimeout(state.authApplyTimer);
    state.liveAuth = null;
  }

  const negotiated = state.status.protocol_version;
  const requested = state.status.requested_protocol_version;
  const countered = negotiated && requested && requested !== "auto" && requested !== negotiated;
  const protocolBadge = $("#badge-protocol");
  protocolBadge.hidden = !negotiated;
  protocolBadge.textContent = countered ? `${requested} → ${negotiated}` : (negotiated || "");
  protocolBadge.className = `badge ${countered ? "warn" : "on"}`;
  protocolBadge.title = countered
    ? `Requested ${requested}; the server negotiated ${negotiated}.`
    : `Negotiated protocol version${requested === "auto" ? " (auto)" : ""}`;

  const sessionBadge = $("#badge-session");
  const sessionId = String(state.status.http_session_id || "");
  sessionBadge.hidden = !sessionId;
  // The badge only has room for a prefix, and a session id is worth reading in
  // full -- it is what says whether the server kept the session or started one.
  sessionBadge.textContent = sessionId ? `sid ${sessionId.slice(0, 12)}` : "";
  sessionBadge.title = sessionId ? `mcp-session-id: ${sessionId}` : "";

  if (state.status.error && !connected) showConnectError(state.status.error);
  else if (connected) $("#connect-error").hidden = true;

  renderServerPanel();
  state.pending = state.status.pending || state.pending;
  renderPending();
}

/** Whether the negotiated protocol still carries `ping`.

 *  Keyed on the revision's era rather than on the string "2026-07-28", so the
 *  next modern revision needs no edit here. An unrecognised version counts as
 *  pingable: better to send it and let the server answer than to hide the
 *  button over a version this build has never heard of. */
function sessionHasPing() {
  const negotiated = state.status.protocol_version;
  if (!negotiated) return true;
  return (state.meta?.protocol_versions || [])
    .find((v) => v.value === negotiated)?.era !== "modern";
}

function renderServerPanel() {
  const section = $("#section-server");
  if (state.status.status !== "connected") { section.hidden = true; return; }
  section.hidden = false;

  const info = state.status.server_info || {};
  const requested = state.status.requested_protocol_version || "auto";
  const negotiated = state.status.protocol_version || "—";
  const rows = [
    ["Name", info.name || "—"],
    ["Version", info.version || "—"],
    ["Protocol", requested === "auto" ? `${negotiated} (auto)` : negotiated],
    ["Requested", requested],
    ["Transport", state.status.transport || "—"],
  ];
  if (state.status.log_level) rows.push(["Log level", state.status.log_level]);
  const tls = state.status.tls;
  if (tls) {
    if (!tls.verify) rows.push(["TLS", "verification off"]);
    if (tls.ca_bundle) rows.push(["CA", tls.ca_bundle]);
    if (tls.client_cert) rows.push(["Client cert", tls.client_cert]);
    if (tls.client_key) rows.push(["Client key", tls.client_key + (tls.client_key_encrypted ? " (encrypted)" : "")]);
  }
  $("#server-meta").innerHTML = rows
    .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("");

  const caps = state.status.capabilities || {};
  $("#server-caps").innerHTML = Object.keys(caps)
    .filter((key) => caps[key])
    .map((key) => `<span class="badge on">${escapeHtml(key)}</span>`)
    .join("") || '<span class="badge">no capabilities</span>';

  const instructions = state.status.instructions;
  const handshake = state.status.handshake_result;
  $("#server-instructions").innerHTML = [
    instructions
      ? `<details style="margin-top:10px"><summary class="hint">Instructions</summary>${jsonBlock(instructions)}</details>`
      : "",
    handshake
      ? `<details style="margin-top:6px"><summary class="hint">Handshake result</summary>${jsonBlock(handshake)}</details>`
      : "",
  ].join("");
}

/* ------------------------------------------------------------------ tools */

async function listTools() {
  const epoch = state.epoch;
  const response = await api("/api/tools/list", {});
  if (epoch !== state.epoch) return;
  if (response.error) { toast(`tools/list: ${response.error.message}`, "error"); return; }
  state.tools = response.result?.tools || [];
  state.loaded.tools = true;
  $("#count-tools").textContent = state.tools.length ? state.tools.length : "";
  renderTools();
}

function renderTools() {
  renderList("#tools-list", "#tools-filter", state.tools, "tools", (tool) => ({
    key: tool.name,
    name: tool.title || tool.name,
    desc: tool.description || "",
  }));
  renderToolDetail();
}

function renderToolDetail() {
  const container = $("#tools-detail");
  const tool = state.tools.find((t) => t.name === state.selected.tools);
  if (!tool) {
    container.innerHTML = state.tools.length
      ? '<div class="empty">Select a tool.</div>'
      : '<div class="empty">No tools listed yet.</div>';
    return;
  }
  const schema = tool.inputSchema || { type: "object", properties: {} };
  container.innerHTML = `
    <h3>${escapeHtml(tool.name)}</h3>
    <div class="subtitle">${escapeHtml(tool.description || "No description.")}</div>
    <fieldset>
      <legend>Arguments</legend>
      <div id="tool-form"></div>
      <label class="check" style="margin:8px 0"><input type="checkbox" id="tool-raw-toggle"> edit as raw JSON</label>
      <textarea id="tool-raw" rows="8" hidden spellcheck="false">{}</textarea>
      <div class="btn-row" style="max-width:460px;margin-top:8px">
        <button class="primary" id="btn-tool-call">Run tool</button>
        <button id="btn-tool-schema">Schema</button>
        <button id="btn-tool-curl" title="Reproduce this call with curl">curl</button>
      </div>
    </fieldset>
    <div id="tool-result"></div>`;

  const form = buildSchemaForm($("#tool-form"), schema);
  $("#tool-raw-toggle").addEventListener("change", (e) => {
    const raw = e.target.checked;
    $("#tool-raw").hidden = !raw;
    $("#tool-form").hidden = raw;
    if (raw) $("#tool-raw").value = JSON.stringify(form.read(), null, 2);
  });
  $("#btn-tool-schema").addEventListener("click", () => openModal(
    `<h3>${escapeHtml(tool.name)}</h3>${jsonBlock(tool)}
     <div class="btn-row" style="margin-top:10px"><button onclick="document.getElementById('modal-backdrop').classList.remove('open')">Close</button></div>`));
  $("#btn-tool-call").addEventListener("click", () => callTool(tool, form));
  $("#btn-tool-curl").addEventListener("click", () => {
    let args;
    try {
      args = $("#tool-raw-toggle").checked ? JSON.parse($("#tool-raw").value || "{}") : form.read();
    } catch (err) {
      fail("curl", new Error(`arguments are not valid JSON: ${err.message}`));
      return;
    }
    openCurlModal("tools/call", { name: tool.name, arguments: args });
  });
}

async function callTool(tool, form) {
  let args;
  try {
    args = $("#tool-raw-toggle").checked
      ? JSON.parse($("#tool-raw").value || "{}")
      : form.read();
  } catch (err) {
    fail("tools/call", `Invalid JSON in the arguments: ${err.message}`);
    return;
  }
  const output = $("#tool-result");
  output.innerHTML = '<p class="hint">Running…</p>';
  try {
    const response = await api("/api/tools/call", { name: tool.name, arguments: args });
    if (response.error) { output.innerHTML = renderRpcError(response.error); return; }
    const result = response.result || {};
    const failed = result.isError;
    output.innerHTML = `
      <fieldset>
        <legend>Result ${failed ? '<span class="badge err">isError</span>' : '<span class="badge on">ok</span>'}
          <span class="hint">${response.elapsed_ms} ms</span></legend>
        ${result.structuredContent
          ? `<p class="hint">structuredContent</p>${jsonBlock(result.structuredContent)}<p class="hint">content</p>`
          : ""}
        ${renderContentBlocks(result.content || [])}
        <details style="margin-top:8px"><summary class="hint">Raw result</summary>${jsonBlock(result)}</details>
      </fieldset>`;
  } catch (err) {
    output.innerHTML = `<p class="banner">${escapeHtml(logFailure("tools/call", err))}</p>`;
  }
}

function renderContentBlocks(blocks) {
  if (!blocks.length) return '<p class="hint">Empty content.</p>';
  return blocks.map((block) => {
    if (block.type === "text") return `<pre class="json">${escapeHtml(block.text ?? "")}</pre>`;
    if (block.type === "image" || block.type === "audio") {
      const src = `data:${block.mimeType};base64,${block.data}`;
      return block.type === "image"
        ? `<img src="${src}" style="max-width:100%;border-radius:6px;border:1px solid var(--border)">`
        : `<audio controls src="${src}"></audio>`;
    }
    return jsonBlock(block);
  }).join("");
}

function renderRpcError(error) {
  return `<p class="banner">JSON-RPC error ${error.code}: ${escapeHtml(error.message)}${
    error.data ? `\n\n${escapeHtml(JSON.stringify(error.data, null, 2))}` : ""}</p>`;
}

/* -------------------------------------------------------------- resources */

async function listResources() {
  const epoch = state.epoch;
  const [listed, templated] = await Promise.allSettled([
    api("/api/resources/list", {}),
    api("/api/resources/templates/list", {}),
  ]);
  if (epoch !== state.epoch) return;
  state.resources = listed.status === "fulfilled" ? (listed.value.result?.resources || []) : [];
  state.resourceTemplates = templated.status === "fulfilled"
    ? (templated.value.result?.resourceTemplates || []) : [];
  state.loaded.resources = true;
  const total = state.resources.length + state.resourceTemplates.length;
  $("#count-resources").textContent = total ? total : "";
  renderResources();
}

function renderResources() {
  const entries = [
    ...state.resources.map((r) => ({ kind: "resource", data: r })),
    ...state.resourceTemplates.map((r) => ({ kind: "template", data: r })),
  ];
  renderList("#resources-list", "#resources-filter", entries, "resources", (entry) => ({
    key: entry.kind === "resource" ? entry.data.uri : entry.data.uriTemplate,
    name: entry.kind === "resource" ? entry.data.uri : entry.data.uriTemplate,
    desc: `${entry.kind === "template" ? "[template] " : ""}${entry.data.name || ""} ${entry.data.description || ""}`.trim(),
  }));
  renderResourceDetail(entries);
}

function renderResourceDetail(entries) {
  const container = $("#resources-detail");
  const entry = entries.find((e) =>
    (e.kind === "resource" ? e.data.uri : e.data.uriTemplate) === state.selected.resources);
  if (!entry) {
    container.innerHTML = entries.length
      ? '<div class="empty">Select a resource.</div>'
      : '<div class="empty">No resources listed yet.</div>';
    return;
  }
  const data = entry.data;
  const isTemplate = entry.kind === "template";
  const uriField = isTemplate ? data.uriTemplate : data.uri;
  const variables = isTemplate ? Array.from(uriField.matchAll(/\{([^}]+)\}/g)).map((m) => m[1]) : [];

  container.innerHTML = `
    <h3>${escapeHtml(data.name || uriField)}</h3>
    <div class="subtitle">${escapeHtml(data.description || (isTemplate ? "URI template" : "Resource"))}</div>
    <dl class="meta-grid">
      <dt>${isTemplate ? "Template" : "URI"}</dt><dd>${escapeHtml(uriField)}</dd>
      ${data.mimeType ? `<dt>MIME</dt><dd>${escapeHtml(data.mimeType)}</dd>` : ""}
      ${data.size != null ? `<dt>Size</dt><dd>${data.size}</dd>` : ""}
    </dl>
    <fieldset>
      <legend>Read</legend>
      ${variables.map((v) => `
        <label class="field"><span>${escapeHtml(v)}</span>
          <input type="text" class="tpl-var" data-var="${escapeHtml(v)}" spellcheck="false"></label>`).join("")}
      <label class="field"><span>Resolved URI</span>
        <input type="text" id="resource-uri" value="${escapeHtml(uriField)}" spellcheck="false"></label>
      <div class="btn-row" style="max-width:520px">
        <button class="primary" id="btn-resource-read">Read</button>
        <button id="btn-resource-sub">Subscribe</button>
        <button id="btn-resource-unsub">Unsubscribe</button>
        <button id="btn-resource-curl" title="Reproduce this read with curl">curl</button>
      </div>
    </fieldset>
    <div id="resource-result"></div>`;

  const refreshUri = () => {
    let uri = uriField;
    $$(".tpl-var", container).forEach((input) => {
      uri = uri.replace(`{${input.dataset.var}}`, encodeURIComponent(input.value));
    });
    $("#resource-uri").value = uri;
  };
  $$(".tpl-var", container).forEach((input) => input.addEventListener("input", refreshUri));

  $("#btn-resource-read").addEventListener("click", async () => {
    const output = $("#resource-result");
    output.innerHTML = '<p class="hint">Reading…</p>';
    try {
      const response = await api("/api/resources/read", { uri: $("#resource-uri").value });
      if (response.error) { output.innerHTML = renderRpcError(response.error); return; }
      const contents = response.result?.contents || [];
      output.innerHTML = `<fieldset><legend>Contents (${contents.length})</legend>${
        contents.map((c) => c.text != null
          ? `<p class="hint">${escapeHtml(c.uri || "")} ${escapeHtml(c.mimeType || "")}</p><pre class="json">${escapeHtml(c.text)}</pre>`
          : jsonBlock(c)).join("")}</fieldset>`;
    } catch (err) {
      output.innerHTML = `<p class="banner">${escapeHtml(logFailure("resources/read", err))}</p>`;
    }
  });

  const subscribe = async (unsubscribe) => {
    try {
      const response = await api("/api/resources/subscribe", { uri: $("#resource-uri").value, unsubscribe });
      if (response.error) toast(response.error.message, "error");
      else toast(unsubscribe ? "Unsubscribed" : "Subscribed", "ok");
    } catch (err) { fail("resources/subscribe", err); }
  };
  $("#btn-resource-curl").addEventListener("click", () =>
    openCurlModal("resources/read", { uri: $("#resource-uri").value.trim() }));
  $("#btn-resource-sub").addEventListener("click", () => subscribe(false));
  $("#btn-resource-unsub").addEventListener("click", () => subscribe(true));
}

/* ---------------------------------------------------------------- prompts */

async function listPrompts() {
  const epoch = state.epoch;
  const response = await api("/api/prompts/list", {});
  if (epoch !== state.epoch) return;
  if (response.error) { toast(`prompts/list: ${response.error.message}`, "error"); return; }
  state.prompts = response.result?.prompts || [];
  state.loaded.prompts = true;
  $("#count-prompts").textContent = state.prompts.length ? state.prompts.length : "";
  renderPrompts();
}

function renderPrompts() {
  renderList("#prompts-list", "#prompts-filter", state.prompts, "prompts", (prompt) => ({
    key: prompt.name,
    name: prompt.title || prompt.name,
    desc: prompt.description || "",
  }));
  renderPromptDetail();
}

function renderPromptDetail() {
  const container = $("#prompts-detail");
  const prompt = state.prompts.find((p) => p.name === state.selected.prompts);
  if (!prompt) {
    container.innerHTML = state.prompts.length
      ? '<div class="empty">Select a prompt.</div>'
      : '<div class="empty">No prompts listed yet.</div>';
    return;
  }
  const args = prompt.arguments || [];
  container.innerHTML = `
    <h3>${escapeHtml(prompt.name)}</h3>
    <div class="subtitle">${escapeHtml(prompt.description || "No description.")}</div>
    <fieldset>
      <legend>Arguments</legend>
      ${args.length ? args.map((a) => `
        <label class="field">
          <span>${escapeHtml(a.name)}${a.required ? " *" : ""}${a.description ? ` — ${escapeHtml(a.description)}` : ""}</span>
          <input type="text" class="prompt-arg" data-name="${escapeHtml(a.name)}" spellcheck="false">
        </label>`).join("") : '<p class="hint">This prompt takes no arguments.</p>'}
      <div class="btn-row" style="max-width:320px">
        <button class="primary" id="btn-prompt-get">Get prompt</button>
        <button id="btn-prompt-curl" title="Reproduce this call with curl">curl</button>
      </div>
    </fieldset>
    <div id="prompt-result"></div>`;

  $("#btn-prompt-curl").addEventListener("click", () => {
    const values = {};
    $$(".prompt-arg", container).forEach((i) => { if (i.value !== "") values[i.dataset.name] = i.value; });
    openCurlModal("prompts/get", { name: prompt.name, arguments: values });
  });

  $("#btn-prompt-get").addEventListener("click", async () => {
    const values = {};
    $$(".prompt-arg", container).forEach((input) => { if (input.value !== "") values[input.dataset.name] = input.value; });
    const output = $("#prompt-result");
    output.innerHTML = '<p class="hint">Fetching…</p>';
    try {
      const response = await api("/api/prompts/get", { name: prompt.name, arguments: values });
      if (response.error) { output.innerHTML = renderRpcError(response.error); return; }
      const result = response.result || {};
      output.innerHTML = `<fieldset><legend>Messages</legend>${
        (result.messages || []).map((m) => `
          <p class="hint">${escapeHtml(m.role)}</p>${renderContentBlocks([m.content])}`).join("")
      }<details style="margin-top:8px"><summary class="hint">Raw result</summary>${jsonBlock(result)}</details></fieldset>`;
    } catch (err) {
      output.innerHTML = `<p class="banner">${escapeHtml(logFailure("prompts/get", err))}</p>`;
    }
  });
}

/* ------------------------------------------------------------- list helper */

function renderList(listSelector, filterSelector, items, tabKey, project) {
  const container = $(listSelector);
  const query = ($(filterSelector).value || "").toLowerCase();
  container.innerHTML = "";
  const visible = items
    .map(project)
    .filter((entry) => !query || `${entry.name} ${entry.desc}`.toLowerCase().includes(query));
  if (!visible.length) {
    // A slow server must not be reported as an empty one.
    container.innerHTML = `<div class="empty">${
      state.loading[tabKey] ? "Listing…" : items.length ? "Nothing matches the filter." : "Nothing listed."}</div>`;
    return;
  }
  visible.forEach((entry) => {
    const node = document.createElement("div");
    node.className = `item${state.selected[tabKey] === entry.key ? " selected" : ""}`;
    node.innerHTML = `<div class="name">${escapeHtml(entry.name)}</div>${
      entry.desc ? `<div class="desc">${escapeHtml(entry.desc)}</div>` : ""}`;
    node.addEventListener("click", () => {
      state.selected[tabKey] = entry.key;
      if (tabKey === "tools") renderTools();
      else if (tabKey === "resources") renderResources();
      else renderPrompts();
    });
    container.appendChild(node);
  });
}

/* ------------------------------------------------------- schema-based form */

function buildSchemaForm(container, schema) {
  container.innerHTML = "";
  const properties = schema.properties || {};
  const required = new Set(schema.required || []);
  const controls = [];

  if (!Object.keys(properties).length) {
    container.innerHTML = '<p class="hint">This tool takes no arguments.</p>';
    return { read: () => ({}) };
  }

  for (const [name, spec] of Object.entries(properties)) {
    const label = document.createElement("label");
    label.className = "field";
    const title = spec.title || name;
    const hint = spec.description ? ` — ${spec.description}` : "";
    label.innerHTML = `<span>${escapeHtml(title)}${required.has(name) ? " *" : ""}<span class="hint" style="display:inline">${escapeHtml(hint)}</span></span>`;

    let input;
    const type = Array.isArray(spec.type) ? spec.type.find((t) => t !== "null") : spec.type;
    if (spec.enum) {
      input = document.createElement("select");
      if (!required.has(name)) input.appendChild(new Option("— unset —", ""));
      spec.enum.forEach((value) => input.appendChild(new Option(String(value), String(value))));
    } else if (type === "boolean") {
      input = document.createElement("select");
      if (!required.has(name)) input.appendChild(new Option("— unset —", ""));
      input.appendChild(new Option("true", "true"));
      input.appendChild(new Option("false", "false"));
    } else if (type === "number" || type === "integer") {
      input = document.createElement("input");
      input.type = "number";
      if (type === "integer") input.step = "1";
    } else if (type === "object" || type === "array") {
      input = document.createElement("textarea");
      input.rows = 4;
      input.placeholder = type === "array" ? "[]" : "{}";
    } else {
      input = document.createElement("input");
      input.type = "text";
    }
    if (spec.default !== undefined && input.tagName !== "SELECT") {
      input.value = typeof spec.default === "object" ? JSON.stringify(spec.default, null, 2) : String(spec.default);
    }
    input.spellcheck = false;
    label.appendChild(input);
    container.appendChild(label);
    controls.push({ name, spec, type, input, required: required.has(name) });
  }

  return {
    read() {
      const out = {};
      for (const control of controls) {
        const raw = control.input.value;
        if (raw === "" && !control.required) continue;
        if (control.spec.enum) {
          const match = control.spec.enum.find((v) => String(v) === raw);
          out[control.name] = match !== undefined ? match : raw;
        } else if (control.type === "boolean") {
          out[control.name] = raw === "true";
        } else if (control.type === "number" || control.type === "integer") {
          out[control.name] = raw === "" ? null : Number(raw);
        } else if (control.type === "object" || control.type === "array") {
          out[control.name] = raw === "" ? (control.type === "array" ? [] : {}) : JSON.parse(raw);
        } else {
          out[control.name] = raw;
        }
      }
      return out;
    },
  };
}

/* ------------------------------------------------------------------ roots */

function renderRoots() {
  const container = $("#roots-list");
  if (!container) return;
  container.innerHTML = "";
  state.roots.forEach((root, index) => {
    const row = document.createElement("div");
    row.className = "kv-row";
    row.style.gridTemplateColumns = "2fr 1fr 22px";
    row.innerHTML = `
      <input type="text" value="${escapeHtml(root.uri || "")}" placeholder="file:///path/to/project" spellcheck="false">
      <input type="text" value="${escapeHtml(root.name || "")}" placeholder="display name" spellcheck="false">
      <button class="icon small">✕</button>`;
    const [uri, name, remove] = row.children;
    uri.addEventListener("input", persistDraft);
    name.addEventListener("input", persistDraft);
    remove.addEventListener("click", () => {
      syncRowEditors(); state.roots.splice(index, 1); renderRoots(); persistDraft();
    });
    container.appendChild(row);
  });
  if (!state.roots.length) container.innerHTML = '<p class="hint">No roots advertised.</p>';
}

/* -------------------------------------------------------- server requests */

function renderPending() {
  $("#count-pending").textContent = state.pending.length ? state.pending.length : "";
  const container = $("#pending-body");
  if (!state.pending.length) {
    container.innerHTML = '<div class="empty">No sampling or elicitation requests are waiting.<br>They appear here when the server asks the client for something.</div>';
    return;
  }
  container.innerHTML = "";
  state.pending.forEach((request) => {
    container.appendChild(request.kind === "elicitation"
      ? buildElicitationCard(request)
      : buildSamplingCard(request));
  });
}

function pendingShell(request, title) {
  const node = document.createElement("fieldset");
  node.dataset.id = request.id;
  node.innerHTML = `<legend>${escapeHtml(title)} <span class="hint">${escapeHtml(request.id)}</span></legend>`;
  return node;
}

async function answerPending(id, payload) {
  try {
    await api(`/api/pending/${id}`, payload);
  } catch (err) {
    fail("pending", err);
  }
}

function buildElicitationCard(request) {
  const params = request.params || {};
  const node = pendingShell(request, "elicitation");

  if (params.mode === "url") {
    node.insertAdjacentHTML("beforeend", `
      <p>${escapeHtml(params.message || "")}</p>
      <p><a href="${escapeHtml(params.url || "")}" target="_blank" rel="noreferrer" class="mono">${escapeHtml(params.url || "")}</a></p>
      <div class="btn-row" style="max-width:320px">
        <button class="primary act-accept">I completed it</button>
        <button class="act-decline">Decline</button>
        <button class="act-cancel">Cancel</button>
      </div>`);
  } else {
    const schema = params.requestedSchema || { type: "object", properties: {} };
    node.insertAdjacentHTML("beforeend", `
      <p>${escapeHtml(params.message || "")}</p>
      <div class="form-slot"></div>
      <details style="margin:8px 0"><summary class="hint">Requested schema</summary>${jsonBlock(schema)}</details>
      <div class="btn-row" style="max-width:320px">
        <button class="primary act-accept">Accept</button>
        <button class="act-decline">Decline</button>
        <button class="act-cancel">Cancel</button>
      </div>`);
    node.__form = buildSchemaForm($(".form-slot", node), schema);
  }

  $(".act-accept", node).addEventListener("click", () => {
    let content = {};
    if (node.__form) {
      try { content = node.__form.read(); }
      catch (err) { fail("elicitation", `Invalid JSON in the reply: ${err.message}`); return; }
    }
    answerPending(request.id, { action: "accept", result: { action: "accept", content } });
  });
  $(".act-decline", node).addEventListener("click", () =>
    answerPending(request.id, { action: "accept", result: { action: "decline" } }));
  $(".act-cancel", node).addEventListener("click", () =>
    answerPending(request.id, { action: "accept", result: { action: "cancel" } }));
  return node;
}

function buildSamplingCard(request) {
  const params = request.params || {};
  const node = pendingShell(request, "sampling (createMessage)");
  node.insertAdjacentHTML("beforeend", `
    ${params.systemPrompt ? `<p class="hint">system prompt</p><pre class="json">${escapeHtml(params.systemPrompt)}</pre>` : ""}
    <p class="hint">messages</p>
    ${(params.messages || []).map((m) =>
      `<p class="hint">${escapeHtml(m.role)}</p>${renderContentBlocks([m.content])}`).join("")}
    <label class="field" style="margin-top:10px"><span>Your reply (as the assistant)</span>
      <textarea rows="5" class="sampling-text" spellcheck="false"></textarea></label>
    <div class="row" style="margin-bottom:8px">
      <label class="field" style="margin:0"><span>Model name to report</span>
        <input type="text" class="sampling-model" value="inspector-manual" spellcheck="false"></label>
      <label class="field" style="margin:0"><span>Stop reason</span>
        <input type="text" class="sampling-stop" value="endTurn" spellcheck="false"></label>
    </div>
    <details style="margin-bottom:8px"><summary class="hint">Raw request params</summary>${jsonBlock(params)}</details>
    <div class="btn-row" style="max-width:320px">
      <button class="primary act-send">Send</button>
      <button class="act-reject">Reject</button>
    </div>`);

  $(".act-send", node).addEventListener("click", () =>
    answerPending(request.id, {
      action: "accept",
      result: {
        role: "assistant",
        content: { type: "text", text: $(".sampling-text", node).value },
        model: $(".sampling-model", node).value || "inspector-manual",
        stopReason: $(".sampling-stop", node).value || "endTurn",
      },
    }));
  $(".act-reject", node).addEventListener("click", () =>
    answerPending(request.id, { action: "reject", message: "rejected in the inspector" }));
  return node;
}

/* --------------------------------------------------------------- log panel */

function pushEvent(event) {
  if (event.kind === "status") { applyStatus(event.status); return; }
  if (event.kind === "pending") {
    if (event.action === "open") state.pending.push(event.request);
    else state.pending = state.pending.filter((r) => r.id !== event.id);
    renderPending();
    if (event.action === "open") { toast(`Server asked for ${event.request.kind}`, ""); selectTab("pending"); }
    return;
  }
  state.events.push(event);
  if (state.events.length > 2000) state.events.splice(0, state.events.length - 2000);
  if (isFailure(event)) { state.errors += 1; renderErrorCount(); }
  if (matchesLogFilter(event)) appendLogRow(event);

  if (event.kind === "notification") {
    const changed = LIST_CHANGED[event.method || ""];
    // A list nobody has opened yet has nothing to refresh: it will be fetched,
    // already up to date, the moment its tab is opened.
    if (changed && state.loaded[changed]) CATALOG_LOADERS[changed]().catch(() => {});
  }
}

// Server log levels that mean something went wrong, per the MCP logging spec.
const ERROR_LOG_LEVELS = new Set(["error", "critical", "alert", "emergency"]);

/** Did this event report a failure, whoever noticed it? */
function isFailure(event) {
  if (event.kind === "error") return true;
  if (event.kind === "transport" || event.kind === "plugin") {
    return event.level === "error" || event.level === "warning";
  }
  if (event.kind === "log") return ERROR_LOG_LEVELS.has(event.level);
  return false;
}

const LIST_CHANGED = {
  "notifications/tools/list_changed": "tools",
  "notifications/resources/list_changed": "resources",
  "notifications/prompts/list_changed": "prompts",
};

function matchesLogFilter(event) {
  if (state.logFilter === "all") return true;
  // The Errors tab collects failures across kinds, not one kind of its own.
  if (state.logFilter === "error") return isFailure(event);
  return event.kind === state.logFilter;
}

function renderErrorCount() {
  $("#count-errors").textContent = state.errors ? state.errors : "";
}

function summarise(event) {
  switch (event.kind) {
    case "message": {
      const m = event.message || {};
      if (m.method) return `${m.method}${m.id != null ? ` #${m.id}` : ""}`;
      if (m.error) return `error #${m.id}: ${m.error.message}`;
      return `result #${m.id}`;
    }
    case "notification": return `${event.method}`;
    case "log": return `[${event.level}] ${typeof event.data === "string" ? event.data : JSON.stringify(event.data)}`;
    case "error": return `${event.source ? `${event.source}: ` : ""}${event.text}`;
    case "stderr": return event.text;
    case "transport": case "plugin": return event.text;
    case "http": return `${event.method} ${event.url} → ${event.status}`;
    case "progress": return `${event.tool}: ${event.progress}${event.total ? `/${event.total}` : ""} ${event.message || ""}`;
    default: return JSON.stringify(event);
  }
}

function tagFor(event) {
  if (event.kind === "message") return event.direction;
  return event.kind;
}

function appendLogRow(event) {
  const list = $("#log-list");
  const row = document.createElement("div");
  row.className = "log-row";
  const time = new Date(event.ts * 1000).toLocaleTimeString("en-GB", { hour12: false });
  const tag = tagFor(event);
  if (isFailure(event)) row.classList.add("failure");
  row.innerHTML = `
    <span class="time">${time}</span>
    <span class="tag ${tag}">${tag === "out" ? "→ out" : tag === "in" ? "← in" : tag}</span>
    <span class="summary">${escapeHtml(summarise(event))}</span>`;
  row.addEventListener("click", () => {
    if (row.classList.contains("expanded")) {
      row.classList.remove("expanded");
      $("pre", row)?.remove();
    } else {
      row.classList.add("expanded");
      const pre = document.createElement("pre");
      pre.className = "json";
      pre.innerHTML = highlightJson(event.message || event.notification || event.detail || event);
      row.appendChild(pre);
    }
  });
  list.appendChild(row);
  while (list.children.length > 2000) list.removeChild(list.firstChild);
  if ($("#log-autoscroll").checked) list.scrollTop = list.scrollHeight;
}

function redrawLog() {
  $("#log-list").innerHTML = "";
  state.errors = state.events.filter(isFailure).length;
  renderErrorCount();
  state.events.filter(matchesLogFilter).forEach(appendLogRow);
}

/* ------------------------------------------------------------------- tabs */

function selectTab(name) {
  $$("#main-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$("#main .pane").forEach((p) => p.classList.toggle("active", p.id === `pane-${name}`));
  ensureCatalog(name);
}

function activeTab() {
  return $("#main-tabs button.active")?.dataset.tab || "tools";
}

const CATALOG_LOADERS = { tools: listTools, resources: listResources, prompts: listPrompts };
const CATALOG_VIEWS = { tools: renderTools, resources: renderResources, prompts: renderPrompts };

/** List a tab's catalog the first time it is opened, and not again. */
function ensureCatalog(name) {
  const load = CATALOG_LOADERS[name];
  if (!load || !state.attached || state.status.status !== "connected") return;
  if (state.loaded[name] || state.loading[name]) return;
  state.loading[name] = true;
  CATALOG_VIEWS[name]();
  load()
    .catch((err) => fail(`${name}/list`, err))
    .finally(() => { state.loading[name] = false; CATALOG_VIEWS[name](); });
}

/* ---------------------------------------------------------------- presets */

async function loadPresets() {
  const { servers } = await api("/api/servers");
  state.presets = servers;
  const select = $("#preset-select");
  const current = select.value;
  select.innerHTML = '<option value="">— none —</option>';
  servers.forEach((s) => select.appendChild(new Option(s.name, s.name)));
  select.value = current;
  refreshPresetState();
}

/** Load a preset into the sidebar and make it the state we compare against. */
function loadPreset(name) {
  const preset = state.presets.find((p) => p.name === name);
  if (!preset) return false;
  writeConfig(preset.config);
  markPresetSaved(name);
  return true;
}

/** Copy `text`, by whichever route this browser and origin allow. */
async function copyText(text, fallbackSelector = "#share-json") {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // No clipboard API (or a non-secure origin): fall back to a selection.
    const area = $(fallbackSelector);
    if (!area) return false;
    area.select();
    try { return document.execCommand("copy"); } catch { return false; }
  }
}

function downloadText(filename, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Render presets as JSON to hand to someone else. */
function openShareModal() {
  const selected = $("#preset-select").value;
  openModal(`
    <h3>Share presets</h3>
    <div class="row" style="margin-bottom:8px">
      <label class="field" style="margin:0"><span>What</span>
        <select id="share-scope">
          ${selected ? `<option value="one">Only “${escapeHtml(selected)}”</option>` : ""}
          <option value="all">All presets</option>
        </select>
      </label>
      <label class="field" style="margin:0"><span>Format</span>
        <select id="share-format">
          <option value="inspector">Inspector JSON (everything)</option>
          <option value="mcp">mcpServers JSON (portable)</option>
        </select>
      </label>
    </div>
    <label class="check" style="margin-bottom:8px">
      <input type="checkbox" id="share-secrets"> Include tokens and passphrases
    </label>
    <textarea id="share-json" rows="12" spellcheck="false" readonly></textarea>
    <p class="hint" id="share-notes"></p>
    <div class="btn-row" style="margin-top:12px">
      <button class="primary" id="share-copy">Copy</button>
      <button id="share-download">Download</button>
      <button id="share-close">Close</button>
    </div>`);

  let current = "";
  async function render() {
    const portable = $("#share-format").value === "mcp";
    const includeSecrets = $("#share-secrets").checked;
    try {
      const result = await api("/api/servers/export", {
        names: $("#share-scope").value === "one" ? [selected] : null,
        portable,
        include_secrets: includeSecrets,
      });
      current = result.json;
      $("#share-json").value = current;
      const notes = [];
      if (result.redacted.length) notes.push(`Blanked: ${result.redacted.join(", ")}.`);
      if (includeSecrets) notes.push("Secrets are in this text — treat it as one.");
      if (result.dropped.length) {
        notes.push(`Not carried by the mcpServers format: ${result.dropped.join(", ")}.`);
      }
      $("#share-notes").textContent = notes.join(" ") || "Nothing sensitive to blank.";
    } catch (err) {
      current = "";
      $("#share-json").value = "";
      $("#share-notes").textContent = logFailure("presets/export", err);
    }
  }

  $("#share-scope").addEventListener("change", render);
  $("#share-format").addEventListener("change", render);
  $("#share-secrets").addEventListener("change", render);
  $("#share-close").addEventListener("click", closeModal);
  $("#share-copy").addEventListener("click", async () => {
    if (!current) return;
    toast(await copyText(current) ? "Copied" : "Could not copy — select the text instead", "ok");
  });
  $("#share-download").addEventListener("click", () => {
    if (!current) return;
    const one = $("#share-scope").value === "one";
    const stem = one ? selected.replace(/[^\w.-]+/g, "-") : "pymcpinspector-presets";
    downloadText(`${stem}.json`, current);
  });
  render();
}

/** The methods that need no params, so the dialog can switch between them freely.
 *  Anything parameterised (tools/call, resources/read, prompts/get) only appears
 *  when the dialog was opened from a panel that can fill its params in. */
const PARAMLESS_METHODS = [
  "tools/list",
  "resources/list",
  "resources/templates/list",
  "prompts/list",
  "ping",
];

/** Show a request as a `curl` carrying this connection's headers, TLS and envelope. */
function openCurlModal(method, params) {
  // Same rule as the Ping button: a session that dropped `ping` is not offered it.
  const offered = PARAMLESS_METHODS.filter((m) => m !== "ping" || sessionHasPing());
  const choices = offered.includes(method) ? offered : [method, ...offered];
  openModal(`
    <h3>Copy as curl</h3>
    <label class="field"><span>Method</span>
      <select id="curl-method">
        ${choices.map((m) => `<option value="${escapeHtml(m)}"${m === method ? " selected" : ""}>${escapeHtml(m)}</option>`).join("")}
      </select>
    </label>
    <p class="hint" style="margin-top:0">Rendered from the live connection: its headers, TLS flags,
      timeouts and, on 2026-07-28, the per-request envelope and routing headers. Any other method can
      be rendered from the Raw request tab.</p>
    <label class="check" style="margin-bottom:8px">
      <input type="checkbox" id="curl-secrets"> Include tokens and passphrases
    </label>
    <textarea id="curl-text" rows="13" spellcheck="false" readonly></textarea>
    <p class="hint" id="curl-notes"></p>
    <div class="btn-row" style="margin-top:12px">
      <button class="primary" id="curl-copy">Copy</button>
      <button id="curl-close">Close</button>
    </div>`);

  let current = "";
  async function render() {
    const chosen = $("#curl-method").value;
    try {
      const result = await api("/api/curl", {
        method: chosen,
        // The params belong to the method they were collected for; switching
        // away from it renders the bare request rather than carrying them over.
        params: chosen === method ? params : null,
        mask_secrets: !$("#curl-secrets").checked,
      });
      current = result.command;
      $("#curl-text").value = current;
      $("#curl-notes").textContent = (result.notes || []).join(" ");
    } catch (err) {
      current = "";
      $("#curl-text").value = "";
      $("#curl-notes").textContent = logFailure("curl", err);
    }
  }

  $("#curl-method").addEventListener("change", render);
  $("#curl-secrets").addEventListener("change", render);
  $("#curl-close").addEventListener("click", closeModal);
  $("#curl-copy").addEventListener("click", async () => {
    if (!current) return;
    toast(await copyText(current, "#curl-text") ? "Copied" : "Could not copy — select the text instead", "ok");
  });
  render();
}

/* ------------------------------------------------------------------ wire */

function connectWebSocket() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/ws`);
  socket.onmessage = (raw) => {
    try { pushEvent(JSON.parse(raw.data)); } catch (err) { logError("event stream", `undecodable event: ${err.message}`); }
  };
  socket.onclose = () => {
    // Nothing else can report this one: the channel that would carry the
    // report is the channel that just died.
    if (state.streaming) logError("event stream", "disconnected from the inspector, retrying");
    state.streaming = false;
    setTimeout(connectWebSocket, 1500);
  };
  socket.onopen = () => { state.streaming = true; };
}

function bindEvents() {
  // A UI bug used to be invisible unless the devtools console happened to be open.
  window.addEventListener("error", (event) =>
    logError("ui", event.message || String(event.error || "script error"),
      event.filename ? `${event.filename}:${event.lineno}` : null));
  window.addEventListener("unhandledrejection", (event) =>
    logError("ui", `unhandled rejection: ${event.reason?.message || event.reason}`));

  $("#cfg-transport").addEventListener("change", () => { onTransportChange(); persistDraft(); });
  $("#cfg-protocol-version").addEventListener("change", () => { describeProtocolChoice(); persistDraft(); });
  $$("#sidebar input, #sidebar select").forEach((node) => {
    node.addEventListener("change", persistDraft);
    // `change` only fires on blur for text fields; keep the header badges and
    // the `edited` marker live as the field is typed into.
    node.addEventListener("input", () => { refreshSectionSummaries(); refreshPresetState(); });
  });

  $("#btn-header-add").addEventListener("click", () => {
    syncRowEditors();
    state.headers.push({ name: "", value: "", enabled: true }); renderKeyValues(); persistDraft();
  });
  $("#btn-env-add").addEventListener("click", () => {
    syncRowEditors();
    state.env.push({ name: "", value: "", enabled: true }); renderKeyValues(); persistDraft();
  });
  $("#btn-root-add").addEventListener("click", () => {
    syncRowEditors();
    state.roots.push({ uri: "", name: null }); renderRoots(); persistDraft();
  });
  $("#btn-roots-apply").addEventListener("click", async () => {
    try {
      syncRowEditors();
      await api("/api/roots", { roots: state.roots.filter((r) => r.uri) });
      toast("Roots updated, server notified", "ok");
    } catch (err) { fail("roots", err); }
  });

  $("#cfg-auth-plugin").addEventListener("input", refreshAuthPluginState);
  // The credential applies itself, and these three fields are what it is made
  // of. `change` as well as `input`: a value put there by a password manager or
  // by form restoration arrives without a keystroke, and the whole point is that
  // what the field shows is what the session sends.
  ["#cfg-token", "#cfg-auth-scheme", "#cfg-auth-header"].forEach((selector) => {
    $(selector).addEventListener("input", scheduleAuthApply);
    $(selector).addEventListener("change", scheduleAuthApply);
  });
  $("#btn-auth-plugin").addEventListener("click", async () => {
    const button = $("#btn-auth-plugin");
    button.disabled = true;
    button.textContent = "Running…";
    try {
      const result = await runAuthPlugin(readConfig());
      // The script may choose the header and the scheme as well, and the sidebar
      // has to show what is being sent before it goes out.
      if (result.scheme != null) $("#cfg-auth-scheme").value = result.scheme;
      if (result.header) $("#cfg-auth-header").value = result.header;
      persistDraft();
      // A fetched token goes out now rather than after the debounce: the click
      // already said when, and nothing more is coming.
      clearTimeout(state.authApplyTimer);
      toast(await applyAuthToSession()
        ? "Token fetched and applied to the live session" : "Token fetched", "ok");
    } catch (err) {
      fail("auth plugin", err);
    } finally {
      button.textContent = "Get token";
      refreshAuthPluginState();
    }
  });

  $("#btn-connect").addEventListener("click", connect);
  $("#btn-disconnect").addEventListener("click", disconnect);
  $("#btn-ping").addEventListener("click", async () => {
    try {
      const response = await api("/api/ping", {});
      if (response.error) toast(`Ping failed: ${response.error.message}`, "error");
      else toast(`Pong in ${response.elapsed_ms} ms`, "ok");
    } catch (err) { fail("ping", err); }
  });

  $("#btn-tools-curl").addEventListener("click", () => openCurlModal("tools/list", null));
  $("#btn-resources-curl").addEventListener("click", () => openCurlModal("resources/list", null));
  $("#btn-prompts-curl").addEventListener("click", () => openCurlModal("prompts/list", null));

  $("#btn-tools-refresh").addEventListener("click", () => listTools().catch((e) => fail("tools/list", e)));
  $("#btn-resources-refresh").addEventListener("click", () => listResources().catch((e) => fail("resources/list", e)));
  $("#btn-prompts-refresh").addEventListener("click", () => listPrompts().catch((e) => fail("prompts/list", e)));
  $("#tools-filter").addEventListener("input", renderTools);
  $("#resources-filter").addEventListener("input", renderResources);
  $("#prompts-filter").addEventListener("input", renderPrompts);

  $("#btn-raw-curl").addEventListener("click", () => {
    const method = $("#raw-method").value.trim();
    if (!method) { toast("A method is required", "error"); return; }
    let params = null;
    try {
      const raw = $("#raw-params").value.trim();
      params = raw ? JSON.parse(raw) : null;
    } catch (err) {
      fail("curl", new Error(`params are not valid JSON: ${err.message}`));
      return;
    }
    openCurlModal(method, params);
  });

  $("#btn-raw-send").addEventListener("click", async () => {
    const output = $("#raw-result");
    output.innerHTML = '<p class="hint">Sending…</p>';
    try {
      const response = await api("/api/request", {
        method: $("#raw-method").value.trim(),
        params: $("#raw-params").value.trim() || null,
      });
      output.innerHTML = response.error
        ? renderRpcError(response.error)
        : `<p class="hint">${response.elapsed_ms} ms</p>${jsonBlock(response.result)}`;
    } catch (err) {
      output.innerHTML = `<p class="banner">${escapeHtml(logFailure("raw request", err))}</p>`;
    }
  });

  $$("#main-tabs button[data-tab]").forEach((button) =>
    button.addEventListener("click", () => selectTab(button.dataset.tab)));

  $$("#log-tabs button[data-log]").forEach((button) =>
    button.addEventListener("click", () => {
      state.logFilter = button.dataset.log;
      $$("#log-tabs button[data-log]").forEach((b) => b.classList.toggle("active", b === button));
      redrawLog();
    }));

  $("#btn-log-clear").addEventListener("click", async () => {
    state.events = [];
    $("#log-list").innerHTML = "";
    state.errors = 0;
    renderErrorCount();
    await api("/api/history/clear", {});
  });

  $("#preset-select").addEventListener("change", (e) => {
    if (!e.target.value) { markPresetSaved(null); return; }
    if (!loadPreset(e.target.value)) return;
    clearCatalog();
  });

  $("#btn-preset-save").addEventListener("click", async () => {
    const name = $("#preset-select").value;
    if (!name) return;
    try {
      await api("/api/servers", { name, config: readConfig() });
      await loadPresets();
      markPresetSaved(name);
      toast(`Saved over “${name}”`, "ok");
    } catch (err) { fail("presets", err); }
  });

  $("#btn-preset-saveas").addEventListener("click", () => {
    const suggested = $("#preset-select").value || state.status.target || "";
    openModal(`
      <h3>Save preset as</h3>
      <label class="field"><span>Name</span>
        <input type="text" id="preset-name" value="${escapeHtml(suggested)}"></label>
      <p class="hint">Stored in ${escapeHtml(state.meta?.config_path || "the presets file")}. The Authentication
        token is left out — paste it again after loading. An existing name is overwritten; a new one keeps both.</p>
      <div class="btn-row" style="margin-top:12px">
        <button class="primary" id="preset-confirm">Save</button>
        <button id="preset-cancel">Cancel</button>
      </div>`);
    $("#preset-cancel").addEventListener("click", closeModal);
    $("#preset-confirm").addEventListener("click", async () => {
      const name = $("#preset-name").value.trim();
      if (!name) return;
      try {
        await api("/api/servers", { name, config: readConfig() });
        await loadPresets();
        markPresetSaved(name);
        closeModal();
        toast("Preset saved", "ok");
      } catch (err) { fail("presets", err); }
    });
  });

  $("#btn-preset-rename").addEventListener("click", () => {
    const name = $("#preset-select").value;
    if (!name) return;
    openModal(`
      <h3>Rename preset</h3>
      <label class="field"><span>Name</span>
        <input type="text" id="rename-name" value="${escapeHtml(name)}"></label>
      <p class="hint">Renames the stored preset. Unsaved sidebar edits are not written by this.</p>
      <div class="btn-row" style="margin-top:12px">
        <button class="primary" id="rename-confirm">Rename</button>
        <button id="rename-cancel">Cancel</button>
      </div>`);
    $("#rename-cancel").addEventListener("click", closeModal);
    $("#rename-confirm").addEventListener("click", async () => {
      const next = $("#rename-name").value.trim();
      if (!next || next === name) { closeModal(); return; }
      try {
        await api(`/api/servers/${encodeURIComponent(name)}/rename`, { name: next });
        await loadPresets();
        // The sidebar may hold edits; only the name it belongs to has moved.
        $("#preset-select").value = next;
        refreshPresetState();
        closeModal();
        toast(`Renamed to “${next}”`, "ok");
      } catch (err) { fail("presets/rename", err); }
    });
  });

  $("#btn-preset-delete").addEventListener("click", async () => {
    const name = $("#preset-select").value;
    if (!name) return;
    try {
      await api(`/api/servers/${encodeURIComponent(name)}`, undefined, "DELETE");
      await loadPresets();
      markPresetSaved(null);
      toast("Preset deleted", "ok");
    } catch (err) { fail("presets", err); }
  });

  $("#btn-preset-share").addEventListener("click", () => openShareModal());

  $("#btn-import").addEventListener("click", () => {
    openModal(`
      <h3>Import mcpServers JSON</h3>
      <p class="hint">Paste a Claude Desktop / VS Code style configuration block.</p>
      <textarea id="import-json" rows="14" spellcheck="false" placeholder='{"mcpServers": {"git": {"command": "uvx", "args": ["mcp-server-git"]}}}'></textarea>
      <div class="btn-row" style="margin-top:12px">
        <button class="primary" id="import-confirm">Import</button>
        <button id="import-cancel">Cancel</button>
      </div>`);
    $("#import-cancel").addEventListener("click", closeModal);
    $("#import-confirm").addEventListener("click", async () => {
      try {
        await api("/api/servers/import", { json: $("#import-json").value });
        await loadPresets();
        closeModal();
        toast("Imported", "ok");
      } catch (err) { fail("presets/import", err); }
    });
  });

  // Draggable divider between the working area and the log panel.
  const resizer = $("#log-resizer");
  resizer.addEventListener("mousedown", (down) => {
    const main = $("#main");
    const startY = down.clientY;
    const startHeight = $("#log-panel").getBoundingClientRect().height;
    const move = (event) => {
      const height = Math.min(Math.max(startHeight - (event.clientY - startY), 60), window.innerHeight - 220);
      main.style.gridTemplateRows = `auto minmax(0, 1fr) auto ${height}px`;
    };
    const up = () => { document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
    down.preventDefault();
  });
}

/* ------------------------------------------------------------------ start */

async function init() {
  bindEvents();
  state.meta = await api("/api/meta");
  $("#version").textContent = `v${state.meta.version} · MCP ${state.meta.protocol_version}`;
  const levelSelect = $("#cfg-log-level");
  state.meta.log_levels.forEach((level) => levelSelect.appendChild(new Option(level, level)));

  const versionSelect = $("#cfg-protocol-version");
  const labels = { auto: "auto — negotiate", modern: " (discover)", handshake: " (initialize)" };
  state.meta.protocol_versions.forEach(({ value, era }) =>
    versionSelect.appendChild(new Option(era === "auto" ? labels.auto : value + labels[era], value)));
  describeProtocolChoice();

  initCollapsibleSections();
  initSidebarLayout();
  refreshAuthPluginState();
  if (!restoreDraft()) { renderKeyValues(); renderRoots(); onTransportChange(); }
  refreshSectionSummaries();
  await loadPresets();
  applyStatus(await api("/api/status"));
  const { events } = await api("/api/history?limit=500");
  events.forEach((event) => { state.events.push(event); });
  redrawLog();
  connectWebSocket();
  renderPending();
}

init().catch((err) => fail("startup", err));
