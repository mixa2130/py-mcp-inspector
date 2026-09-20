/* The page's own furniture: the settings sidebar as a box (hiding it, resizing
   it, remembering both) and the badges in the header. The fields inside the
   sidebar are sidebar.test.mjs's business. */

import { readFileSync } from "node:fs";

import { boot, check, click, displayOf, finish, press } from "./harness.mjs";

const LAYOUT = "pymcpinspector.layout";
const DRAFT = "pymcpinspector.draft";

const widthOf = (doc) => doc.getElementById("app").style.getPropertyValue("--sidebar-width");
const stored = (store) => JSON.parse(store[LAYOUT] || "{}");

/** Drag the divider from `from` to `to`, as a mouse does: down, move, up. */
const drag = (doc, from, to) => {
  const view = doc.defaultView;
  const resizer = doc.getElementById("sidebar-resizer");
  resizer.dispatchEvent(new view.MouseEvent("mousedown", { clientX: from, bubbles: true }));
  doc.dispatchEvent(new view.MouseEvent("mousemove", { clientX: to, bubbles: true }));
  doc.dispatchEvent(new view.MouseEvent("mouseup", { clientX: to, bubbles: true }));
};

// --- the default box -------------------------------------------------------

{
  const { doc, errors } = await boot();
  check("the page loads without JS errors", errors.length === 0, errors.join(" | "));
  check("the sidebar starts visible", !doc.getElementById("app").classList.contains("sidebar-hidden"));
  check("at its default width", widthOf(doc) === "380px", widthOf(doc));
  check("the divider is there", displayOf(doc, doc.getElementById("sidebar-resizer")) !== "none");
  check("and says how wide the sidebar is",
    doc.getElementById("sidebar-resizer").getAttribute("aria-valuenow") === "380");
  check("the toggle reports an open sidebar",
    doc.getElementById("btn-sidebar").getAttribute("aria-expanded") === "true");
}

// --- hiding ----------------------------------------------------------------

{
  const { doc, store, calls } = await boot({
    storage: { [DRAFT]: JSON.stringify({ transport: "streamable-http", url: "http://x/mcp" }) },
  });
  const app = doc.getElementById("app");
  click(doc, doc.getElementById("btn-sidebar"));

  check("the toggle hides the sidebar", displayOf(doc, doc.getElementById("sidebar")) === "none",
    displayOf(doc, doc.getElementById("sidebar")));
  check("and the divider with it", displayOf(doc, doc.getElementById("sidebar-resizer")) === "none");
  check("the button says what it will do now",
    doc.getElementById("btn-sidebar").getAttribute("aria-expanded") === "false"
    && /Show/.test(doc.getElementById("btn-sidebar").title), doc.getElementById("btn-sidebar").title);
  check("the choice is remembered", stored(store).sidebarHidden === true, store[LAYOUT]);

  // Hidden, not removed: what the sidebar holds is the connection config.
  click(doc, doc.getElementById("btn-connect"));
  await new Promise((resolve) => setTimeout(resolve, 20));
  const sent = calls.filter((c) => c.path === "/api/connect").pop();
  check("connecting still sends the hidden config",
    !!sent && JSON.parse(sent.options.body).url === "http://x/mcp",
    sent && sent.options.body);

  click(doc, doc.getElementById("btn-sidebar"));
  check("clicking again brings it back", !app.classList.contains("sidebar-hidden"));
  check("and that is remembered too", stored(store).sidebarHidden === false, store[LAYOUT]);
}

{
  const { doc } = await boot({ storage: { [LAYOUT]: JSON.stringify({ sidebarHidden: true }) } });
  check("a sidebar hidden last time stays hidden", displayOf(doc, doc.getElementById("sidebar")) === "none");
  check("and its fields are still readable", !!doc.getElementById("cfg-url"));
}

// --- resizing --------------------------------------------------------------

{
  const { doc, store } = await boot();
  drag(doc, 380, 500);
  check("dragging the divider widens the sidebar", widthOf(doc) === "500px", widthOf(doc));
  check("the width is remembered", stored(store).sidebarWidth === 500, store[LAYOUT]);
  check("the divider reports the new width",
    doc.getElementById("sidebar-resizer").getAttribute("aria-valuenow") === "500");

  drag(doc, 500, 100);
  check("it cannot be dragged narrower than its minimum", widthOf(doc) === "260px", widthOf(doc));
  drag(doc, 260, 4000);
  // jsdom's window is 1024 wide, and the working area keeps 360 of it.
  check("nor wide enough to swallow the working area", widthOf(doc) === "664px", widthOf(doc));
}

{
  const { doc, store } = await boot();
  const resizer = doc.getElementById("sidebar-resizer");
  press(doc, resizer, "ArrowRight");
  press(doc, resizer, "ArrowRight");
  check("the arrow keys resize it too", widthOf(doc) === "412px", widthOf(doc));
  press(doc, resizer, "ArrowLeft");
  check("in both directions", widthOf(doc) === "396px", widthOf(doc));
  check("and each press is remembered", stored(store).sidebarWidth === 396, store[LAYOUT]);
  press(doc, resizer, "Enter");
  check("other keys leave the width alone", widthOf(doc) === "396px", widthOf(doc));
}

{
  const { doc, store } = await boot({ storage: { [LAYOUT]: JSON.stringify({ sidebarWidth: 500 }) } });
  check("a remembered width comes back", widthOf(doc) === "500px", widthOf(doc));
  check("and is not rewritten on the way", stored(store).sidebarWidth === 500, store[LAYOUT]);
}

{
  // A width chosen on a wide screen must not eat a narrow one -- but it is
  // still what the viewer asked for, so the stored number survives.
  const { doc, store } = await boot({ storage: { [LAYOUT]: JSON.stringify({ sidebarWidth: 5000 }) } });
  check("too wide for this window is clamped", widthOf(doc) === "664px", widthOf(doc));
  check("without forgetting the chosen width", stored(store).sidebarWidth === 5000, store[LAYOUT]);
}

{
  const { doc, store } = await boot({ storage: { [LAYOUT]: "{not json" } });
  check("a corrupt layout falls back to the default", widthOf(doc) === "380px", widthOf(doc));
  check("and is left for the next write to replace", store[LAYOUT] === "{not json");
}

// --- badges that have nothing to say ---------------------------------------

{
  const { doc } = await boot();
  const badge = doc.getElementById("badge-session");
  check("an unconnected inspector shows no session id", badge.hidden);
  check("and carries no leftover label", badge.textContent === "", JSON.stringify(badge.textContent));
  check("the protocol badge is empty too", doc.getElementById("badge-protocol").textContent === "");
}

{
  const status = {
    status: "connected", transport: "streamable-http", target: "http://x/mcp",
    protocol_version: "2025-06-18", http_session_id: "0123456789abcdef-and-more", pending: [],
  };
  const { doc } = await boot({ routes: { "/api/status": status } });
  const badge = doc.getElementById("badge-session");
  check("a session id reaches the header", !badge.hidden && badge.textContent === "sid 0123456789ab",
    JSON.stringify(badge.textContent));
  check("and the whole of it is in the tooltip",
    badge.title === "mcp-session-id: 0123456789abcdef-and-more", badge.title);
}

{
  // jsdom hides `[hidden]` on its own, so it cannot reproduce what a browser
  // does here: an author rule like `.badge { display: inline-block }` outranks
  // the user-agent `[hidden]` rule and the attribute stops hiding anything.
  // What is checkable here is that the stylesheet still overrides it back.
  const css = readFileSync(new URL("../../pymcpinspector/static/styles.css", import.meta.url), "utf8");
  check("the stylesheet keeps `hidden` in charge",
    /\[hidden\]\s*\{[^}]*display:\s*none\s*!important/.test(css));
}

finish();
