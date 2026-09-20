/* The sidebar and the header controls that depend on the live connection: the
   collapsible Advanced / TLS sections, the auth-refresh button, and Ping. */

import { boot, check, choose, click, displayOf, finish, press, type } from "./harness.mjs";

const DRAFT = "pymcpinspector.draft";
const COLLAPSED = "pymcpinspector.collapsed";
const SECTIONS = ["section-advanced", "section-tls"];

const withDraft = (config) => boot({ storage: { [DRAFT]: JSON.stringify(config) } });
const http = (extra = {}) => ({ transport: "streamable-http", url: "http://x/mcp", ...extra });

// --- collapsed by default --------------------------------------------------

{
  const { doc, errors } = await boot();
  check("the page loads without JS errors", errors.length === 0, errors.join(" | "));
  for (const id of SECTIONS) {
    const section = doc.getElementById(id);
    check(`${id} starts collapsed`, section.classList.contains("collapsed"));
    check(`${id} reports aria-expanded=false`, section.querySelector("h2").getAttribute("aria-expanded") === "false");
    check(`${id} body is actually hidden`,
      displayOf(doc, section.querySelector(".section-body")) === "none",
      displayOf(doc, section.querySelector(".section-body")));
    check(`${id} header looks clickable`,
      displayOf(doc, section.querySelector("h2")) === "flex");
    // Collapsing must hide, not remove: readConfig() still reads these fields.
    check(`${id} keeps its fields in the DOM`, !!section.querySelector(".section-body input, .section-body select"));
  }
}

// --- toggling and persistence ----------------------------------------------

{
  const { doc, store } = await boot();
  const section = doc.getElementById("section-tls");
  const header = section.querySelector("h2");

  const body = section.querySelector(".section-body");
  click(doc, header);
  check("a click expands", !section.classList.contains("collapsed"));
  check("the body becomes visible", displayOf(doc, body) !== "none", displayOf(doc, body));
  check("aria-expanded follows", header.getAttribute("aria-expanded") === "true");
  check("the open state is remembered", JSON.parse(store[COLLAPSED])["section-tls"] === false);

  click(doc, header);
  check("a second click collapses", section.classList.contains("collapsed"));
  check("the body hides again", displayOf(doc, body) === "none", displayOf(doc, body));
  check("the closed state is remembered", JSON.parse(store[COLLAPSED])["section-tls"] === true);
}

{
  const { doc } = await boot({ storage: { [COLLAPSED]: JSON.stringify({ "section-advanced": false }) } });
  check("a remembered open section reopens", !doc.getElementById("section-advanced").classList.contains("collapsed"));
  check("the other section stays collapsed", doc.getElementById("section-tls").classList.contains("collapsed"));
}

{
  const { doc } = await boot();
  const section = doc.getElementById("section-advanced");
  const header = section.querySelector("h2");
  check("the header is keyboard reachable", header.getAttribute("tabindex") === "0");
  press(doc, header, "Enter");
  check("Enter expands", !section.classList.contains("collapsed"));
  press(doc, header, " ");
  check("Space collapses", section.classList.contains("collapsed"));
}

// --- what a collapsed header advertises ------------------------------------

{
  const { doc } = await withDraft(http());
  check("no TLS badge at defaults", doc.getElementById("tls-summary").hidden);
  check("no Advanced badge at defaults", doc.getElementById("advanced-summary").hidden);
}

{
  const { doc } = await withDraft(http({ verify_tls: false, ca_bundle: "/p/ca.pem", client_cert: "/p/c.pem" }));
  const badge = doc.getElementById("tls-summary");
  check("TLS badge lists every non-default",
    badge.textContent === "no verify · custom CA · client cert", badge.textContent);
}

{
  const { doc } = await withDraft(http({ client_cert: "/p/c.pem", client_key_password: "s3cret" }));
  check("the passphrase never reaches the badge",
    !doc.getElementById("tls-summary").textContent.includes("s3cret"));
}

{
  const { doc } = await withDraft(http({ protocol_version: "2024-11-05", log_level: "debug", request_timeout: 120 }));
  const badge = doc.getElementById("advanced-summary");
  check("Advanced badge lists version, log level and changed timeouts",
    badge.textContent === "2024-11-05 · log debug · request timeout 120", badge.textContent);
}

{
  const { doc } = await withDraft(http());
  const badge = doc.getElementById("tls-summary");
  check("the badge starts hidden", badge.hidden);
  // `change` only fires on blur, so the badge is wired to `input` as well.
  type(doc, doc.getElementById("cfg-client-cert"), "/p/client.pem");
  check("the badge updates while typing", !badge.hidden && badge.textContent === "client cert", badge.textContent);
}

// --- interaction with the transport switch ---------------------------------

{
  const { doc } = await withDraft({ transport: "stdio", command: "echo" });
  check("TLS is hidden entirely on stdio", doc.getElementById("section-tls").hidden);
  check("Advanced stays available on stdio", !doc.getElementById("section-advanced").hidden);
}

// --- the key/value editors read from the DOM -------------------------------

/** Set a field the way autofill does: a new value, but no `input` event. */
const fill = (element, value) => { element.value = value; };

const headerRows = (doc) => Array.from(doc.querySelectorAll("#headers-list .kv-row"));

const sentConfig = (calls) => {
  const call = calls.filter((c) => c.path === "/api/connect").pop();
  return call && JSON.parse(call.options.body);
};

{
  const { doc, calls } = await withDraft(http({ headers: [{ name: "", value: "", enabled: true }] }));
  const [toggle, name, value] = headerRows(doc)[0].children;
  fill(name, "X-Trace-Id");
  fill(value, "abc123");
  click(doc, doc.getElementById("btn-connect"));
  await new Promise((resolve) => setTimeout(resolve, 20));

  const config = sentConfig(calls);
  check("an autofilled header still reaches /api/connect",
    !!config && JSON.stringify(config.headers) === JSON.stringify([{ name: "X-Trace-Id", value: "abc123", enabled: true }]),
    JSON.stringify(config && config.headers));
  check("the enabled toggle comes along", toggle.checked);
}

{
  // A redraw rebuilds the rows from the row array, so the array has to catch up first.
  const { doc } = await withDraft(http({
    headers: [{ name: "X-One", value: "1", enabled: true }, { name: "", value: "", enabled: true }],
  }));
  const [, secondName, secondValue] = headerRows(doc)[1].children;
  fill(secondName, "X-Two");
  fill(secondValue, "2");

  click(doc, headerRows(doc)[0].children[0]); // toggle the first row off -> re-render
  const rows = headerRows(doc);
  check("a re-render keeps an autofilled row", rows.length === 2 && rows[1].children[1].value === "X-Two",
    rows.map((r) => r.children[1].value).join(","));
  check("the toggled row is the one that flipped", rows[0].children[0].checked === false);
}

{
  const { doc } = await withDraft(http({
    headers: [{ name: "X-One", value: "1", enabled: true }, { name: "X-Two", value: "2", enabled: true }],
  }));
  const [, secondName] = headerRows(doc)[1].children;
  fill(secondName, "X-Renamed");
  click(doc, headerRows(doc)[0].querySelector(".kv-remove")); // remove the first row
  const rows = headerRows(doc);
  check("removing a row keeps the other row's autofilled name",
    rows.length === 1 && rows[0].children[1].value === "X-Renamed",
    rows.map((r) => r.children[1].value).join(","));
}

{
  const { doc, store } = await withDraft(http({ headers: [{ name: "", value: "", enabled: true }] }));
  fill(headerRows(doc)[0].children[1], "X-Late");
  click(doc, doc.getElementById("btn-header-add"));
  const rows = headerRows(doc);
  check("adding a row keeps what was autofilled into the previous one",
    rows.length === 2 && rows[0].children[1].value === "X-Late",
    rows.map((r) => r.children[1].value).join(","));
  check("the draft records it too",
    JSON.parse(store[DRAFT]).headers[0].name === "X-Late", store[DRAFT]);
}

{
  // The typed path has to keep working: `input` still drives the draft.
  const { doc, store } = await withDraft(http({ headers: [{ name: "", value: "", enabled: true }] }));
  type(doc, headerRows(doc)[0].children[1], "X-Typed");
  check("typing a header name still persists",
    JSON.parse(store[DRAFT]).headers[0].name === "X-Typed", store[DRAFT]);
}

// --- generated header values -----------------------------------------------

const uuidOf = (version) =>
  new RegExp(`^[0-9a-f]{8}-[0-9a-f]{4}-${version}[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`);

const pickerOf = (row) => row.querySelector(".kv-generator");
const refreshOf = (row) => row.querySelector(".kv-refresh");
const valueOf = (row) => row.children[2];

{
  const { doc } = await withDraft(http({ headers: [{ name: "X-Trace-Id", value: "", enabled: true }] }));
  const row = headerRows(doc)[0];
  check("a header row offers a format picker", !!pickerOf(row));
  check("which starts on a typed value", pickerOf(row).value === "");
  check("with nothing to refresh yet", refreshOf(row).disabled);
  check("an env row has no picker", !doc.querySelector("#env-list .kv-generator"));
}

{
  const { doc, store } = await withDraft(http({ headers: [{ name: "X-Trace-Id", value: "", enabled: true }] }));
  const row = headerRows(doc)[0];
  choose(doc, pickerOf(row), "uuid4");
  const first = valueOf(row).value;
  check("choosing a format fills an empty row", uuidOf(4).test(first), first);
  check("and arms the refresh button", !refreshOf(row).disabled);

  const draft = JSON.parse(store[DRAFT]).headers[0];
  check("the draft keeps the value", draft.value === first, store[DRAFT]);
  check("and the format that made it", draft.generator === "uuid4", store[DRAFT]);

  click(doc, refreshOf(row));
  const second = valueOf(row).value;
  check("refreshing mints a different UUID", uuidOf(4).test(second) && second !== first, `${first} -> ${second}`);
  check("and the draft follows", JSON.parse(store[DRAFT]).headers[0].value === second, store[DRAFT]);
}

{
  const { doc } = await withDraft(http({ headers: [{ name: "X-Request-Id", value: "", enabled: true }] }));
  const row = headerRows(doc)[0];
  choose(doc, pickerOf(row), "uuid4");
  const four = valueOf(row).value;
  choose(doc, pickerOf(row), "uuid7");
  const seven = valueOf(row).value;
  check("switching versions re-mints the value", uuidOf(7).test(seven) && seven !== four, `${four} -> ${seven}`);
  check("the tooltip follows it", valueOf(row).title === seven, valueOf(row).title);

  // Back to a typed value: what is in the row is now a plain string, and the
  // picker has stopped claiming to describe it.
  choose(doc, pickerOf(row), "");
  check("going back to Fixed keeps the last value", valueOf(row).value === seven, valueOf(row).value);
  check("and disarms refresh", refreshOf(row).disabled);
  check("the draft drops the format", !("generator" in JSON.parse(doc.defaultView.localStorage
    .getItem("pymcpinspector.draft")).headers[0]));
}

{
  const { doc } = await withDraft(http({ headers: [{ name: "X-Trace-Id", value: "typed", enabled: true }] }));
  const row = headerRows(doc)[0];
  choose(doc, pickerOf(row), "uuid4");
  check("a value already there is left alone", valueOf(row).value === "typed", valueOf(row).value);
  click(doc, refreshOf(row));
  check("until refresh is asked for it", uuidOf(4).test(valueOf(row).value), valueOf(row).value);
}

{
  const { doc } = await withDraft(http({
    headers: [{ name: "X-One", value: "", enabled: true }, { name: "X-Two", value: "", enabled: true }],
  }));
  for (const [index, version] of [[0, 1], [1, 7]]) {
    const row = headerRows(doc)[index];
    choose(doc, pickerOf(row), `uuid${version}`);
    check(`UUID v${version} looks like one`, uuidOf(version).test(valueOf(row).value), valueOf(row).value);
  }
  // v1 is time-based but only millisecond-resolved; two calls in the same tick
  // must still differ, which is what the random low digits are for.
  const first = valueOf(headerRows(doc)[0]).value;
  click(doc, refreshOf(headerRows(doc)[0]));
  check("two v1 values in the same millisecond differ",
    valueOf(headerRows(doc)[0]).value !== first, first);
}

{
  const { doc, calls } = await withDraft(http({
    headers: [{ name: "X-Trace-Id", value: "", enabled: true }, { name: "X-Plain", value: "p", enabled: true }],
  }));
  choose(doc, pickerOf(headerRows(doc)[0]), "uuid7");
  const minted = valueOf(headerRows(doc)[0]).value;

  click(doc, headerRows(doc)[1].children[0]); // toggle the other row -> re-render
  const redrawn = headerRows(doc)[0];
  check("a re-render keeps the chosen format", pickerOf(redrawn).value === "uuid7");
  check("and the value it minted", valueOf(redrawn).value === minted, valueOf(redrawn).value);
  check("the refresh button stays armed after a redraw", !refreshOf(redrawn).disabled);

  click(doc, doc.getElementById("btn-connect"));
  await new Promise((resolve) => setTimeout(resolve, 20));
  const sent = sentConfig(calls);
  check("the generated value reaches /api/connect",
    !!sent && sent.headers[0].value === minted && sent.headers[0].generator === "uuid7",
    JSON.stringify(sent && sent.headers));
  check("a plain row carries no generator key",
    !!sent && !("generator" in sent.headers[1]), JSON.stringify(sent && sent.headers));
}

// --- refreshing the token on a live session --------------------------------

const authCall = (calls) => {
  const call = calls.filter((c) => c.path === "/api/auth").pop();
  return call && JSON.parse(call.options.body);
};

{
  const { doc } = await withDraft(http());
  check("the Apply button is off before connecting", doc.getElementById("btn-auth-apply").disabled);
  check("and says why", !doc.getElementById("auth-apply-hint").hidden);
}

{
  const connected = { status: "connected", transport: "streamable-http", target: "http://x/mcp", pending: [] };
  const { doc, calls } = await boot({
    storage: { [DRAFT]: JSON.stringify(http({ bearer_token: "first" })) },
    routes: { "/api/connect": connected, "/api/tools/list": { result: { tools: [] } } },
  });
  click(doc, doc.getElementById("btn-connect"));
  await new Promise((resolve) => setTimeout(resolve, 20));
  check("connecting enables Apply", !doc.getElementById("btn-auth-apply").disabled);

  fill(doc.getElementById("cfg-token"), "second");
  click(doc, doc.getElementById("btn-auth-apply"));
  await new Promise((resolve) => setTimeout(resolve, 20));

  const sent = authCall(calls);
  check("the new token goes to /api/auth", !!sent && sent.token === "second", JSON.stringify(sent));
  check("the scheme and header name come along",
    !!sent && sent.scheme === "Bearer" && sent.header === "Authorization", JSON.stringify(sent));
  check("no reconnect was triggered", calls.filter((c) => c.path === "/api/connect").length === 1);
  check("the button is usable again", !doc.getElementById("btn-auth-apply").disabled);
}

{
  // stdio has no headers to re-send; the button must not offer to.
  const connected = { status: "connected", transport: "stdio", target: "python demo.py", pending: [] };
  const { doc } = await boot({
    storage: { [DRAFT]: JSON.stringify({ transport: "stdio", command: "python" }) },
    routes: { "/api/connect": connected, "/api/tools/list": { result: { tools: [] } } },
  });
  click(doc, doc.getElementById("btn-connect"));
  await new Promise((resolve) => setTimeout(resolve, 20));
  check("a stdio connection leaves Apply off", doc.getElementById("btn-auth-apply").disabled);
}

// --- Ping, on a protocol that still has it ---------------------------------

const connectedAt = (version) => ({
  status: "connected", transport: "streamable-http", target: "http://x/mcp",
  protocol_version: version, pending: [],
});

async function connectOn(version) {
  const { doc } = await boot({
    storage: { [DRAFT]: JSON.stringify(http()) },
    routes: { "/api/connect": connectedAt(version), "/api/tools/list": { result: { tools: [] } } },
  });
  click(doc, doc.getElementById("btn-connect"));
  await new Promise((resolve) => setTimeout(resolve, 20));
  return doc.getElementById("btn-ping");
}

{
  const { doc } = await boot();
  const ping = doc.getElementById("btn-ping");
  check("Ping is present but off before connecting", !ping.hidden && ping.disabled);
}

{
  const ping = await connectOn("2025-11-25");
  check("a handshake-era session can ping", !ping.hidden && !ping.disabled);
}

{
  // `ping` was removed in 2026-07-28; the button could only produce -32601.
  const ping = await connectOn("2026-07-28");
  check("a 2026-07-28 session hides Ping", ping.hidden);
  check("and it stays disabled while hidden", ping.disabled);
}

{
  const ping = await connectOn("2099-01-01");
  check("a version this build does not know keeps Ping", !ping.hidden && !ping.disabled);
}

finish();
