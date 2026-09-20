/* Editing a saved preset in place, and the Share dialog that renders one to
   hand to someone else. */

import { boot, check, choose, click, finish, settle, type } from "./harness.mjs";

const PRESETS = [
  { name: "alpha", config: { transport: "streamable-http", url: "http://alpha/mcp" } },
  { name: "beta", config: { transport: "streamable-http", url: "http://beta/mcp" } },
];

const EXPORT = {
  json: '{\n  "servers": []\n}',
  redacted: ["alpha: token"],
  dropped: [],
};

const booted = (routes = {}) =>
  boot({ servers: PRESETS, routes: { "/api/servers/export": EXPORT, ...routes } });

const byId = (doc, id) => doc.getElementById(id);
const sentTo = (calls, path) =>
  calls.filter((c) => c.path === path && c.options?.body).map((c) => JSON.parse(c.options.body));

// --- what a selection enables ----------------------------------------------

{
  const { doc } = await booted();
  for (const id of ["btn-preset-save", "btn-preset-rename", "btn-preset-delete"]) {
    check(`${id} is off with nothing selected`, byId(doc, id).disabled);
  }
  check("Save as… stays available", !byId(doc, "btn-preset-saveas").disabled);
  check("Share… stays available", !byId(doc, "btn-preset-share").disabled);

  choose(doc, byId(doc, "preset-select"), "beta");
  check("selecting one enables Rename", !byId(doc, "btn-preset-rename").disabled);
  check("and Delete", !byId(doc, "btn-preset-delete").disabled);
  check("but not Save: nothing has changed yet", byId(doc, "btn-preset-save").disabled);
  check("and nothing claims to be edited", byId(doc, "preset-dirty").hidden);
}

// --- the edited badge -------------------------------------------------------

{
  const { doc } = await booted();
  choose(doc, byId(doc, "preset-select"), "beta");
  type(doc, byId(doc, "cfg-url"), "http://beta/changed");
  check("editing the sidebar marks the preset edited", !byId(doc, "preset-dirty").hidden);
  check("and offers to save over it", !byId(doc, "btn-preset-save").disabled);

  type(doc, byId(doc, "cfg-url"), "http://beta/mcp");
  check("putting it back clears the mark", byId(doc, "preset-dirty").hidden);
  check("and disables Save again", byId(doc, "btn-preset-save").disabled);
}

{
  const { doc } = await booted();
  choose(doc, byId(doc, "preset-select"), "beta");
  type(doc, byId(doc, "cfg-url"), "http://beta/changed");
  choose(doc, byId(doc, "preset-select"), "");
  check("deselecting drops the edited mark", byId(doc, "preset-dirty").hidden);
  check("and the preset-scoped buttons go off", byId(doc, "btn-preset-delete").disabled);
}

// --- the Authentication token is not part of a preset ----------------------

{
  const { doc } = await booted();
  choose(doc, byId(doc, "preset-select"), "beta");
  type(doc, byId(doc, "cfg-token"), "a-token-that-expires-tonight");
  check("a pasted token does not read as an unsaved edit", byId(doc, "preset-dirty").hidden);
  check("and does not offer to save over the preset", byId(doc, "btn-preset-save").disabled);

  // A header row spelled `Authorization` is the same credential by another route.
  type(doc, byId(doc, "cfg-url"), "http://beta/changed");
  check("a real edit still registers", !byId(doc, "preset-dirty").hidden);
}

// --- saving over the selected preset ---------------------------------------

{
  const { doc, calls } = await booted({ "/api/servers": { servers: PRESETS } });
  choose(doc, byId(doc, "preset-select"), "beta");
  type(doc, byId(doc, "cfg-url"), "http://beta/changed");
  click(doc, byId(doc, "btn-preset-save"));
  await settle();

  const saved = sentTo(calls, "/api/servers").pop();
  check("Save writes to the selected name", !!saved && saved.name === "beta", JSON.stringify(saved));
  check("with the edited config", !!saved && saved.config.url === "http://beta/changed");
  check("no name is asked for", !doc.getElementById("preset-name"));
  check("and the sidebar is the saved state again", byId(doc, "preset-dirty").hidden);
}

// --- renaming ---------------------------------------------------------------

{
  const renamed = [{ name: "alpha", config: PRESETS[0].config }, { name: "gamma", config: PRESETS[1].config }];
  let done = false;
  const { doc, calls } = await booted({
    "/api/servers/beta/rename": () => { done = true; return { servers: renamed }; },
    "/api/servers": () => ({ servers: done ? renamed : PRESETS }),
  });
  choose(doc, byId(doc, "preset-select"), "beta");
  click(doc, byId(doc, "btn-preset-rename"));
  const field = byId(doc, "rename-name");
  check("the rename dialog starts from the current name", !!field && field.value === "beta");

  field.value = "gamma";
  click(doc, byId(doc, "rename-confirm"));
  await settle();

  const sent = sentTo(calls, "/api/servers/beta/rename").pop();
  check("the new name is posted", !!sent && sent.name === "gamma", JSON.stringify(sent));
  check("the dialog closes", !doc.getElementById("rename-name"));
  check("the selection follows the preset", byId(doc, "preset-select").value === "gamma",
    byId(doc, "preset-select").value);
}

// --- sharing ----------------------------------------------------------------

{
  const { doc, calls } = await booted();
  choose(doc, byId(doc, "preset-select"), "alpha");
  click(doc, byId(doc, "btn-preset-share"));
  await settle();

  const first = sentTo(calls, "/api/servers/export").pop();
  check("a selected preset is the default scope", !!first && JSON.stringify(first.names) === '["alpha"]',
    JSON.stringify(first));
  check("secrets are left out by default", !!first && first.include_secrets === false);
  check("the inspector's own format is the default", !!first && first.portable === false);
  check("the rendered JSON is shown", byId(doc, "share-json").value === EXPORT.json);
  check("and what was blanked is named",
    byId(doc, "share-notes").textContent.includes("alpha: token"),
    byId(doc, "share-notes").textContent);

  choose(doc, byId(doc, "share-format"), "mcp");
  await settle();
  check("switching format re-renders", sentTo(calls, "/api/servers/export").pop().portable === true);

  byId(doc, "share-secrets").checked = true;
  choose(doc, byId(doc, "share-secrets"), true);
  await settle();
  const withSecrets = sentTo(calls, "/api/servers/export").pop();
  check("asking for secrets asks the backend for them", withSecrets.include_secrets === true);
  check("and the text warns they are in there",
    byId(doc, "share-notes").textContent.includes("treat it as one"),
    byId(doc, "share-notes").textContent);

  choose(doc, byId(doc, "share-scope"), "all");
  await settle();
  check("the all-presets scope sends no names", sentTo(calls, "/api/servers/export").pop().names === null);
}

{
  // Nothing selected: there is no single preset to offer, only the whole set.
  const { doc, calls } = await booted();
  click(doc, byId(doc, "btn-preset-share"));
  await settle();
  const options = Array.from(byId(doc, "share-scope").options).map((o) => o.value);
  check("only 'all' is offered", JSON.stringify(options) === '["all"]', JSON.stringify(options));
  check("and that is what is fetched", sentTo(calls, "/api/servers/export").pop().names === null);
}

{
  const { doc, errors } = await booted();
  click(doc, byId(doc, "btn-preset-share"));
  await settle();
  click(doc, byId(doc, "share-close"));
  check("closing leaves the dialog empty", !doc.getElementById("share-json"));
  check("no JS errors along the way", errors.length === 0, errors.join(" | "));
}

finish();
