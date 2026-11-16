/* FA26 ERS Deployment Optimizer -- front end.
 *
 * Talks to the local Python server over JSON and polls it for progress while
 * a solve runs. No framework, no build step: the whole point of building the
 * window this way is that a stranger can run it with nothing but the Python
 * the tool already needs.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const MAX_KW = 350;          // the car's deploy and harvest ceiling
const WATCH_MS = 4000;       // how often to look for a lap you just finished

const state = {
  detail: null,
  //: The solver's commentary. No longer shown -- it is a running account of
  //: a search, which was worth reading while the search was being built and
  //: is noise now that it works -- but still collected, so "Copy
  //: diagnostics" carries it when something needs explaining.
  log: [],
  revision: null,
  qualify: 70,
  race: 80,
  unlimited: false,
  touched: false,
  since: 0,
  running: false,
  summary: [],
  setup: null,
  pollId: 0,
  watch: null,
  ready: null,
  version: "",
  view: "session",
  //: A session the driver said was not the one they are driving.
  declined: null,
  //: The driver's own settings beyond the two recharge sliders: the other
  //: three limits, the reduction rate, which curve each session runs.
  extra: {},
  //: Whether these maps have been saved into a setup.
  saved: false,
  game: null,
  useStale: false,
};

/* --------------------------------------------------------------- plumbing */
/* No request waits forever. A fetch with no limit, against an optimizer that
 * had stopped answering, left a player's progress bar on 7% and 0 s for a
 * quarter of an hour with nothing to say why. Thirty seconds is far longer
 * than anything here takes, a session's first read of its laps included. */
const GET_TIMEOUT_MS = 30000;

async function get(path) {
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), GET_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(path, { signal: abort.signal });
  } catch (err) {
    // Refused or timed out: either way the optimizer did not answer.
    const lost = new TypeError(err.name === "AbortError"
      ? "the optimizer did not answer within 30 seconds" : String(err.message || err));
    lost.timedOut = err.name === "AbortError";
    throw lost;
  } finally {
    clearTimeout(timer);
  }
  const data = await res.json();
  if (data && data.error) {
    // The optimizer answered, with an error. Kept whole, trace and all, for
    // the diagnostics -- it used to be thrown away, and the page called it
    // "Cannot reach the optimizer".
    const err = new Error(data.error);
    err.answered = true;
    err.trace = data.trace || "";
    throw err;
  }
  return data;
}

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (data && data.error) throw new Error(data.error);
  return data;
}

function setStatus(text, kind) {
  const node = $("status");
  node.dataset.state = kind || "wait";
  node.querySelector(".status-text").textContent = text;
}

const mj = (tenths) => (tenths / 10).toFixed(1);
const pct = (kj) => Math.round(100 * kj / 4000) + "%";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/* ------------------------------------------------------------- first run */
/* The game, the car and the logger all live somewhere else on disk and a new
 * user may have none of them in place. Rather than failing on whichever is
 * reached first, the window opens and says which one. */
async function checkReady() {
  let r;
  try {
    r = await get("/api/ready");
  } catch (err) {
    setStatus("Cannot reach the optimizer", "bad");
    return false;
  }
  state.ready = r;
  // The game and the car are blockers; a missing logger is not, because
  // telemetry already recorded can still be solved.
  const blocked = !r.ok;
  // The panel covers anything not yet in place, and one thing that is: the
  // game folder, which stays changeable because detection can be confident
  // and wrong on a machine with more than one install.
  // The setup panel now carries only what is missing. Changing a folder that
  // was found correctly is not a setup step, so it lives on its own page.
  const needsLogger = !r.logger;
  $("setup").hidden = !blocked && !needsLogger;
  $("setup-path").hidden = !blocked;
  $("setup-logger").hidden = !needsLogger;
  $("setup-change").hidden = false;
  $("setup-close").hidden = true;
  $("intro").hidden = (blocked || needsLogger) || state.summary.length > 0;
  if (state.view === "settings") { paintFolders(); paintAbout(); }
  if (blocked) { $("waiting").hidden = true; showView("session"); }

  if (!blocked) {
    {
      if (needsLogger) {
        $("setup-title").textContent = "One more step";
        $("setup-problem").textContent = "Assetto Corsa found at " + r.root + ".";
        $("logger-note").textContent =
          "The logger records your laps. It goes into Assetto Corsa's "
          + "apps/lua folder.";
        $("install-logger").textContent = "Install the logger";
      }
    }
    return true;
  }
  $("setup-title").textContent = r.kind === "no_car" ? "Car not found"
    : r.kind === "car_changed" ? "The car has been updated"
    : "Assetto Corsa not found";
  $("setup-problem").textContent = r.problem;
  fillPath(r.root);
  $("run").disabled = true;
  setStatus("Not set up", "bad");
  return false;
}

async function installLogger(button) {
  button = (button && button.tagName) ? button : $("install-logger");
  button.disabled = true;
  button.textContent = "Installing\u2026";
  for (const id of ("setup-error folders-error").split(" ")) {
    if ($(id)) $(id).hidden = true;
  }
  try {
    await post("/api/installlogger", {});
  } catch (err) {
    // The one refusal worth offering a way past: the game watches this file
    // and restarts the logger when it changes, which throws away the lap
    // being recorded. Worth doing anyway between sessions, never mid-lap.
    if (String(err.message || err).indexOf("Assetto Corsa is running") === 0) {
      if (confirm(err.message + "\n\nUpdate it anyway?")) {
        try {
          await post("/api/installlogger", { force: true });
        } catch (second) {
          showFolderError(second);
          button.disabled = false;
          button.textContent = "Install the logger";
          return;
        }
      } else {
        button.disabled = false;
        button.textContent = "Install the logger";
        return;
      }
    } else {
      showFolderError(err);
      button.disabled = false;
      button.textContent = "Install the logger";
      return;
    }
  }
  button.textContent = "Installed";
  $("setup-title").textContent = "Ready";
  $("setup-problem").textContent =
    "Enable FA26 Baseline Logger from the apps bar in game, then drive.";
  setTimeout(async () => {
    if (await checkReady()) load(true);
  }, 1400);
}

function showNotice(title, text) {
  const box = el("div", "notice info");
  box.append(el("h3", null, title), el("p", null, text));
  $("warnings").append(box);
  // The section has a heading of its own, so it only appears when there is
  // something under it -- an empty "Worth knowing" is worse than no heading.
  const block = $("warnings-block");
  if (block) block.hidden = false;
}

async function saveAcPath(box) {
  box = (box && box.value !== undefined) ? box : $("ac-path");
  const value = box.value.trim();
  if (!value) return;
  $("ac-save").disabled = true;
  $("setup-error").hidden = true;
  try {
    await post("/api/setpath", { path: value });
  } catch (err) {
    showFolderError(err);
    $("ac-save").disabled = false;
    return;
  }
  $("ac-save").disabled = false;
  if (await checkReady()) load(true);
}

/* Setups are written here, and the laps normally live under it. Shown with
 * how many setups are in it, because that is the quickest way to tell a
 * correct folder from a plausible one. */
function showDocuments(info) {
  if (!info) return;
  const box = $("doc-path");
  if (box && document.activeElement !== box) box.value = info.folder || "";
  $("doc-auto").hidden = !info.chosen_by_hand;
  $("doc-hint").textContent = !info.exists
    ? "This folder does not exist yet."
    : info.setups
      ? info.setups + (info.setups === 1 ? " setup folder" : " setup folders")
        + " for this car in here"
        + (info.chosen_by_hand ? ", chosen by you." : ", found automatically.")
      : "The folder is there, but no setups for this car in it yet.";
  if (info.writable === false) {
    $("doc-hint").textContent += " Windows would not let this app write here, "
      + "so saving a map into a setup will fail. "
      + (state.ready ? state.ready.blocked_advice : "");
  }
}

async function saveDocuments(folder) {
  $("doc-save").disabled = true;
  $("setup-error").hidden = true;
  try {
    await post("/api/setdocuments", { path: folder });
  } catch (err) {
    showFolderError(err);
    $("doc-save").disabled = false;
    return;
  }
  $("doc-save").disabled = false;
  await checkReady();
  load(true);
}

/* The laps are read from here. Shown with what is actually in it, because
 * "no lap files yet" and "wrong folder" look identical from the outside and
 * the difference is the whole problem. */
function showTelemetry(info) {
  if (!info) return;
  const box = $("tel-path");
  if (box && document.activeElement !== box) box.value = info.folder || "";
  $("tel-auto").hidden = !info.chosen_by_hand;
  $("tel-hint").textContent = !info.exists
    ? "This folder does not exist yet."
    : info.laps
      ? info.laps + (info.laps === 1 ? " lap file" : " lap files") + " in here"
        + (info.chosen_by_hand ? ", chosen by you." : ", found automatically.")
      : "The folder is there, but no lap files in it yet."
        + (lapsMayBeBlocked()
          ? " Windows ransomware protection is on, and it stops the game "
            + "writing here unless the game is allowed. "
            + state.ready.blocked_advice
          : "");
}

/* An empty laps folder with Windows' ransomware protection on. The logger
 * can be counting laps it was never allowed to write, and the protection is
 * the usual reason -- worth saying beside the empty folder, not a verdict. */
function lapsMayBeBlocked() {
  const r = state.ready;
  return !!(r && r.protection === "on" && r.telemetry && !r.telemetry.laps);
}

async function saveTelemetry(folder) {
  $("tel-save").disabled = true;
  $("setup-error").hidden = true;
  try {
    await post("/api/settelemetry", { path: folder });
  } catch (err) {
    showFolderError(err);
    $("tel-save").disabled = false;
    return;
  }
  $("tel-save").disabled = false;
  await checkReady();
  load(true);
}

/* --------------------------------------------------------------- views */
/* One screen at a time. Everything used to be stacked down a single page --
 * the first-run panel, the introduction, the run, the results, the save form
 * and the log -- so the page was long, and which parts were live depended on
 * state you could not see from the top of it. */
const STEPS = ["session", "solve", "maps", "save"];
//: The names the old layout used, still used by code that asks for them.
const ALIAS = { home: "session", folders: "settings", runs: "history", guide: "help" };

/* A step can be opened once there is something on it: Solve needs a session,
 * Maps needs a solve, Save needs the maps. */
function reachable(name) {
  if (name === "solve") return !!state.detail || state.running;
  if (name === "maps") return state.summary.length > 0;
  if (name === "save") return state.summary.length >= 3;
  return true;
}

function paintSteps() {
  const done = { session: !!state.detail, solve: state.summary.length > 0,
                 maps: state.saved, save: state.saved };
  const steps = [...document.querySelectorAll(".step")];
  steps.forEach((step, i) => {
    const name = step.dataset.view;
    step.classList.toggle("is-on", state.view === name);
    step.classList.toggle("done", !!done[name] && state.view !== name);
    step.classList.toggle("locked", !reachable(name));
    step.querySelector("i").textContent = done[name] && state.view !== name ? "✓" : String(i + 1);
  });
  [...document.querySelectorAll(".steps .join")].forEach((join, i) => {
    join.classList.toggle("done", !!done[STEPS[i]]);
  });
  for (const tab of document.querySelectorAll(".aux .tab")) {
    tab.classList.toggle("is-on", tab.dataset.view === state.view);
  }
}

function showView(name, pane) {
  name = ALIAS[name] || name;
  if (STEPS.includes(name) && !reachable(name)) return;
  if (state.view === "save" && name !== "save") closeApply();
  state.view = name;
  for (const screen of document.querySelectorAll(".screen")) {
    screen.classList.toggle("show", screen.id === "screen-" + name);
  }
  paintSteps();
  if (name === "settings") { paintFolders(); paintAbout(); if (pane) settingsPane(pane); }
  if (name === "history") loadRuns();
  if (name === "save") openApply();
}

function settingsPane(name) {
  for (const tab of document.querySelectorAll(".pane-tab")) {
    tab.classList.toggle("is-on", tab.dataset.settings === name);
  }
  for (const pane of document.querySelectorAll(".pane")) {
    pane.classList.toggle("is-on", pane.dataset.settings === name);
  }
}

/* Version, which lap simulation is running, how fresh the circuit data is:
 * the things a report needs, where they can be read without copying anything. */
function paintAbout() {
  const r = state.ready || {}, box = $("about-facts");
  box.innerHTML = "";
  const cd = r.circuit_data || {};
  for (const [term, value] of [
    ["Version", state.version || r.version || "unknown"],
    ["Lap simulation", r.integration || "unknown"],
    ["Circuit data", (cd.result || "unknown") + (cd.checked ? " (checked " + cd.checked + ")" : "")],
    ["Car", $("car-id").textContent || "—"],
    ["Assetto Corsa", r.root || "not found"]]) {
    box.append(el("dt", null, term), el("dd", null, value));
  }
  $("logger-state").textContent = !r.logger
    ? "The logger is not installed. No laps are recorded until it is."
    : r.logger_outdated ? "The logger is installed, but this version changed how it records. Update it below."
    : "The logger is installed and up to date.";
}

/* Solving swaps the ready card for the progress ring, and back. */
function solving(on) {
  $("running").hidden = !on;
  $("ready-card").hidden = on;
}

/* A qualifying time while you are still driving, from the screening step of
 * the search on its own -- one simulation per starting map and no climb, so
 * it costs under half a second and does not take the processor the game is
 * using. It is labelled as provisional until enough laps are pooled, because
 * an early number with the confidence of a finished one is worse than none. */
async function paintLive(key) {
  const box = $("live-estimate");
  if (!box || !key) return;
  let d;
  try {
    d = await get("/api/live?key=" + encodeURIComponent(key));
  } catch (e) { box.hidden = true; return; }
  // The box itself stays -- it carries the switch, and a switch you can only
  // reach once a number has appeared is a switch nobody finds.
  box.hidden = false;
  const sw0 = $("live-search");
  if (sw0 && d && sw0.checked !== !!d.live_search) sw0.checked = !!d.live_search;
  $("le-body").hidden = !(d && d.ready);
  if (!d || !d.ready) {
    $("le-full").hidden = !(d && d.live_search);
    return;
  }
  $("le-time").textContent = d.predicted;
  const tag = $("le-tag");
  tag.textContent = d.converged ? "settled" : "provisional";
  tag.dataset.state = d.converged ? "converged" : "provisional";
  $("le-note").textContent = d.note + " Your best so far is " + d.best_real
    + ". A full run searches from here and can only do better.";

  // The switch reflects what the app has stored, not what the box was last
  // clicked to: it persists across restarts and the page is not the record.
  const sw = $("live-search");
  if (sw && sw.checked !== !!d.live_search) sw.checked = !!d.live_search;

  const full = $("le-full");
  const f = d.full || {};
  full.hidden = !d.live_search;
  if (!d.live_search) return;
  const fullTag = $("lf-tag");
  if (f.ready) {
    $("lf-time").textContent = f.predicted;
    fullTag.textContent = f.searching ? "searching" : "done";
    fullTag.dataset.state = f.searching ? "provisional" : "converged";
    $("lf-note").textContent = "From " + f.laps
      + (f.laps === 1 ? " lap, " : " laps, ")
      + f.deploy_mj.toFixed(1) + " MJ, searched in "
      + Math.round(f.took_s) + "s.";
  } else {
    $("lf-time").textContent = f.searching ? "…" : "—";
    fullTag.textContent = f.searching ? "searching" : "waiting";
    fullTag.dataset.state = "provisional";
    $("lf-note").textContent = f.searching
      ? "Running the full search on the laps so far."
      : "Starts a few seconds after your next lap.";
  }
}

/* ------------------------------------------------------------- past runs
 * A run used to replace the last one on screen and then be gone, so every
 * question worth asking -- did that change help, why is this circuit a second
 * slower than last week -- meant keeping notes by hand and trusting them. */
const compare = { picked: [] };

async function loadRuns() {
  let runs = [];
  try {
    runs = (await get("/api/history")).runs || [];
  } catch (e) { runs = []; }
  state.runs = runs;
  $("runs-empty").hidden = runs.length > 0;
  $("runs-table").hidden = runs.length === 0;
  const body = $("runs-body");
  body.textContent = "";
  for (const run of runs) {
    const tr = el("tr");
    const pick = el("td", "pick");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = compare.picked.includes(run.id);
    box.onchange = () => pickRun(run.id, box.checked);
    pick.append(box);
    tr.append(pick, el("td", null, when(run.at)),
              el("td", null, titleCase(run.circuit)),
              el("td", "num", String(run.laps_used || 0)));
    for (const n of [1, 2, 3]) {
      const s = (run.strats || []).find((x) => x.strat === n);
      tr.append(el("td", "num", s && s.lap_s ? clock(s.lap_s) : "—"));
    }
    tr.append(el("td", "num", run.best_real_s ? clock(run.best_real_s) : "—"));
    body.append(tr);
  }
  paintCompareBar();
}

function pickRun(id, on) {
  compare.picked = compare.picked.filter((x) => x !== id);
  if (on) compare.picked.push(id);
  // Two is the question being asked; a third replaces the older pick rather
  // than refusing the click, which is what people expect from two checkboxes.
  if (compare.picked.length > 2) compare.picked.shift();
  loadRuns();
}

function paintCompareBar() {
  const bar = $("compare-bar");
  bar.hidden = compare.picked.length === 0;
  $("compare-what").textContent =
    compare.picked.length === 2 ? "Two runs picked."
                                : "Pick one more to compare.";
  $("compare-go").disabled = compare.picked.length !== 2;
}

async function runCompare() {
  if (compare.picked.length !== 2) return;
  const out = $("compare-out");
  out.textContent = "";
  let data;
  try {
    data = await get("/api/compare?a=" + encodeURIComponent(compare.picked[0])
                         + "&b=" + encodeURIComponent(compare.picked[1]));
  } catch (e) {
    out.append(el("p", "warn-line", "Those runs could not be compared."));
    return;
  }
  const box = el("div", "compare");
  box.append(el("h2", null, titleCase(data.b.circuit) + ": "
                + when(data.a.at) + " against " + when(data.b.at)));
  if (!data.same_circuit) {
    box.append(el("p", "warn-line",
      "These are different circuits, so only the shape of the change means "
      + "anything — not the lap times."));
  } else if (!data.same_session) {
    box.append(el("p", "note-line",
      "Different sessions, so your own pace changed too: your best lap moved "
      + signed(data.best_real_delta_s) + "."));
  }
  const table = el("table", "compare-table");
  const head = el("tr");
  for (const h of ["", "Before", "After", "Change", "Battery before",
                   "Battery after", "Zones"]) head.append(el("th", null, h));
  table.append(head);
  for (const row of data.rows) {
    const tr = el("tr");
    tr.append(el("td", null, row.label || ("STRAT " + row.strat)),
              el("td", "num", clock(row.before_s)),
              el("td", "num", clock(row.after_s)),
              el("td", "num " + (row.delta_s < -0.005 ? "gain"
                                 : row.delta_s > 0.005 ? "loss" : ""),
                 signed(row.delta_s)),
              el("td", "num", (row.before_mj || 0).toFixed(2) + " MJ"),
              el("td", "num", (row.after_mj || 0).toFixed(2) + " MJ"),
              el("td", "num", row.before_zones + " → " + row.after_zones));
    table.append(tr);
  }
  box.append(table);
  out.append(box);
}

/* Circuit keys are normalised lower case with no spaces -- the only name the
 * record keeps, since it has to survive the session folder being gone. */
function titleCase(key) {
  if (!key) return "—";
  return key.replace(/[_-]+/g, " ").replace(/\w/g, (c) => c.toUpperCase());
}

/* Lap times, as a stopwatch reads them. */
function clock(seconds) {
  if (!seconds) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds - m * 60;
  return m + ":" + (s < 10 ? "0" : "") + s.toFixed(3);
}

function signed(s) {
  if (s === undefined || s === null) return "—";
  if (Math.abs(s) < 0.005) return "level";
  return (s < 0 ? "−" : "+") + Math.abs(s).toFixed(3) + "s";
}

function when(at) {
  if (!at) return "—";
  const d = new Date(at * 1000);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" })
    + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/* The folders page answers "where is it now" before it offers to change it,
 * which is the question the old panel never answered. */
function paintFolders() {
  const r = state.ready;
  if (!r) return;
  const set = (id, text) => { const n = $(id); if (n) n.textContent = text; };
  set("ac-now", r.root || "not found");
  const box = $("ac-path-2");
  if (box && document.activeElement !== box) box.value = r.root || "";
  set("ac-hint", r.root
    ? (r.found_automatically ? "Found automatically." : "Set by you.")
    : "Not found. Paste the folder with content and apps in it.");
  showDocuments(r.documents);
  showTelemetry(r.telemetry);
  if (r.documents) set("doc-now", r.documents.folder);
  if (r.telemetry) set("tel-now", r.telemetry.folder);
  set("app-now", (r.app_dir || ""));

  const row = $("folder-logger");
  if (row) {
    row.hidden = !!r.logger && !r.logger_outdated;
    $("folder-logger-note").textContent = !r.logger
      ? "The logger is not installed. Without it there are no laps to read."
      : "The logger in your game is older than this version. It records your "
        + "laps differently now \u2014 update it, then restart the session.";
    $("install-logger-2").textContent = r.logger ? "Update the logger"
                                                 : "Install the logger";
  }
}

/* Readiness is polled, so writing the box on every pass would wipe out
 * whatever is half-typed in it. */
/* An error from any of the three folder forms, put where the form is. */
function showFolderError(err) {
  const text = String(err.message || err);
  for (const id of ("setup-error folders-error").split(" ")) {
    const node = $(id);
    if (!node) continue;
    node.textContent = text;
    node.hidden = false;
  }
}

/* ---------------------------------------------------------- this session */
/* Only ever one session: the one being driven. A map gets saved into a setup
 * and stays there, so going back to last week's laps is not something anyone
 * needs to do, and a list of them was history to scroll past on the way to
 * the row that mattered. */
async function load(force) {
  if (state.ready && !state.ready.ok) return;
  let data;
  try {
    data = await get("/api/current");
  } catch (err) {
    // Three different things, and only one of them is "cannot reach": a
    // refused connection. An error the optimizer reported, or an answer the
    // page could not read, means it is running and something in reading the
    // laps went wrong -- which is what a player's report needs to say.
    state.unreachable = err instanceof TypeError;
    state.loadError = state.unreachable ? null
      : { message: String(err.message || err), trace: err.trace || "" };
    setStatus(state.unreachable ? "Cannot reach the optimizer"
                                : "Could not read your laps", "bad");
    return;
  }
  state.unreachable = false;
  state.loadError = null;
  $("car-id").textContent = data.car;
  $("version").textContent = data.version ? "v" + data.version : "";
  state.version = data.version || "";
  $("telemetry-path").textContent = data.folder;

  state.game = data.game;
  const session = data.session;
  // "This session" means laps driven since the sim started. A session from
  // hours ago and a reboot later is not current, however new it is on disk --
  // and a stale one is the state the map-order guard exists to catch.
  const usable = session && (session.live || state.useStale)
    && session.key !== state.declined;
  if (!usable) {
    state.detail = null;
    state.revision = null;
    showWaiting(data.game, session);
    if (state.view === "solve" && !state.running) showView("session");
    paintSteps();
    return;
  }

  const fresh = data.session.revision !== state.revision;
  if (!fresh && !force) return;
  const grew = !!state.detail && state.detail.key === data.session.key
    && data.session.recorded > state.detail.recorded;

  state.revision = data.session.revision;
  state.detail = data.session;
  if (!state.touched) {
    state.qualify = data.session.qualify;
    state.race = data.session.race;
    state.unlimited = data.session.unlimited;
    state.extra = Object.assign({}, data.session.extra);
  }
  renderSession(grew);
  Maps.loadOutline(data.session.key);
  paintSteps();
  paintLive(data.session.key);
  if (!state.running) {
    $("run").disabled = false;
    setStatus("Ready", "ready");
  }
}

function showWaiting(game, session) {
  $("waiting").hidden = false;
  $("waiting").dataset.live = String(!!(game && game.running));
  $("session-card").hidden = true;
  $("live").hidden = !(game && game.running);
  $("limits-block").hidden = true;
  $("options-block").hidden = true;
  $("warnings").innerHTML = "";
  $("warnings-block").hidden = true;
  $("run").disabled = true;

  if (game && game.running) {
    $("waiting-title").textContent = "Waiting for laps";
    $("waiting-text").textContent =
      "Assetto Corsa is running. Laps appear here as you finish them."
      + (lapsMayBeBlocked()
        ? " Windows ransomware protection is on, and it can stop the game "
          + "saving them. If the logger window says LAPS ARE NOT BEING SAVED, "
          + "see “Laps not showing up?” below."
        : "");
    setStatus("Waiting for laps", "wait");
  } else {
    $("waiting-title").textContent = "Assetto Corsa is not running";
    $("waiting-text").textContent =
      "Start the game and drive, and your laps appear here.";
    setStatus("Game not running", "wait");
  }

  // Closing the window by accident should not mean driving the laps again.
  $("last-session").hidden = !session;
  if (session) {
    $("last-name").textContent = session.name;
    $("last-meta").textContent =
      session.recorded + (session.recorded === 1 ? " lap" : " laps")
      + " \u00b7 best " + session.best + " \u00b7 " + session.when;
  }
}

function renderSession(grew) {
  const d = state.detail;
  $("waiting").hidden = true;
  $("session-card").hidden = false;
  $("live").hidden = state.running || !d.live;
  $("limits-block").hidden = false;
  $("options-block").hidden = false;

  $("circuit-name").textContent = d.name;
  $("solve-eyebrow").textContent = d.name + " · " + d.pooled
    + (d.pooled === 1 ? " lap" : " laps");

  const dl = $("facts");
  dl.innerHTML = "";
  const fact = (term, value) => {
    dl.append(el("dt", null, term), el("dd", null, value));
  };
  fact("Laps recorded", String(d.recorded));
  fact("Laps being used", d.pooled === d.recorded
    ? "all " + d.pooled
    : d.pooled + " of " + d.recorded);
  fact("Best lap", d.best);
  // Fuel is read from the telemetry, never typed in. The old box defaulted to
  // 45 L while a Monza push lap ran at 6.9 -- 28 kg of car that was not there.
  fact("Fuel on board", d.fuel_l == null ? "—" : d.fuel_l.toFixed(1) + " L");
  fact("Battery at start",
       d.charge == null ? "—" : Math.round(d.charge * 100) + "%");
  fact("Recorded", d.when);

  if (grew) {
    dl.classList.remove("just-grew");
    void dl.offsetWidth;               // restart the highlight
    dl.classList.add("just-grew");
  }

  $("lap-summary").textContent = d.pooled === d.recorded
    ? "See the " + d.recorded + " " + (d.recorded === 1 ? "lap" : "laps")
    : "See which " + d.pooled + " of " + d.recorded + " are used";
  const ul = $("lap-list");
  ul.innerHTML = "";
  for (const lap of d.laps) {
    const li = el("li");
    li.append(el("span", null, lap.label), el("span", null, lap.time));
    ul.append(li);
  }

  const warn = $("warnings");
  warn.innerHTML = "";
  $("warnings-block").hidden = true;
  if (d.warnings.length) $("warnings-block").hidden = false;
  for (const w of d.warnings) {
    const box = el("div", "notice" + (w.level === "info" ? " info" : ""));
    box.append(el("h3", null, w.title), el("p", null, w.text));
    warn.append(box);
  }
  if (!d.live) {
    showNotice("An earlier session",
               "These laps were not driven in the session now running, so "
               + "they may have been on a different map.");
  }
  if (state.ready && state.ready.logger && state.ready.logger_outdated) {
    showNotice("The logger needs updating",
               "It records your laps, and this version changed how. "
               + "Update it in Settings, under Logger, then restart the session.");
  }
  if (state.ready && !state.ready.logger) {
    showNotice("The logger is not installed",
               "No new laps will be recorded until it is.");
  }
  if (state.ready && state.ready.documents
      && state.ready.documents.writable === false) {
    showNotice("This app can’t save your setup",
               "Windows refused a test write into your Assetto Corsa folder, "
               + "so saving these maps will fail. "
               + state.ready.blocked_advice);
  }

  renderLimits();
}

/* ---------------------------------------------------------------- limits */
/* What the FIA set for this race, shown as it stands: the five recharge
 * figures, the reduction rate and the power curves, with where they came
 * from said first. They are locked until "Change", because for a circuit
 * with a notice there is nothing to set -- and unlocked, every one of them
 * can be, from what the car's own menu offers. */
const CURVE_FIELDS = [["curve_override", "#ff5c7a"], ["curve_standard", "#5ec8ff"],
                      ["curve_alternative", "#b08cff"]];
const OTHER_LIMITS = ["race_overtake", "practice", "outlap"];

/* One of the three limits the sliders do not cover: the driver's figure if
 * they set one, the notice's while qualifying and race are the notice's, and
 * otherwise the way the notices relate them to those two. */
function otherLimit(name) {
  const d = state.detail, f = d.fia;
  if (state.extra[name] != null) return state.extra[name];
  if (state.qualify === f.limits.qualify && state.race === f.limits.race) return f.limits[name];
  if (name === "race_overtake") return Math.min(state.race + 5, d.limits.race.max);
  return Math.min(Math.max(state.qualify, state.race) + 5, d.limits.qualify.max);
}

function curveName(field) {
  return state.extra[field] || state.detail.fia.curves[field] || "";
}

/* The curve the solve will really use for a name: its own points where the
 * notices have drawn it, otherwise the Base curve that stands in. */
function curvePoints(field) {
  const f = state.detail.fia, name = curveName(field);
  if (f.curve_points[name]) return { points: f.curve_points[name], stand: false };
  if (field === "curve_alternative") return null;
  const base = name.endsWith("Standard") ? "Base - Standard" : "Base - Overtake";
  return { points: f.curve_points[base], stand: true };
}

function drawCurves() {
  const svg = $("curve-chart"), NS = "http://www.w3.org/2000/svg";
  const X0 = 34, X1 = 352, Y0 = 6, Y1 = 104, V0 = 240, V1 = 360, P1 = 350;
  const x = (v) => X0 + (X1 - X0) * (Math.max(V0, Math.min(V1, v)) - V0) / (V1 - V0);
  const y = (p) => Y1 - (Y1 - Y0) * p / P1;
  const add = (tag, attrs, text) => {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    svg.append(n);
  };
  svg.textContent = "";
  for (const p of [0, 100, 200, 350]) {
    add("line", { x1: X0, x2: X1, y1: y(p), y2: y(p), class: "grid" });
    add("text", { x: X0 - 6, y: y(p) + 3, class: "tick", "text-anchor": "end" }, p);
  }
  for (const v of [250, 290, 340, 360]) {
    add("line", { x1: x(v), x2: x(v), y1: Y0, y2: Y1, class: "grid" });
    add("text", { x: x(v), y: Y1 + 13, class: "tick", "text-anchor": "middle" }, v);
  }
  // The alternative first, so the two every lap runs on are drawn over it.
  for (const [field, colour] of CURVE_FIELDS.slice().reverse()) {
    const c = curvePoints(field);
    if (!c || !curveName(field) && field === "curve_alternative") continue;
    const pts = [[V0, c.points[0][1]], ...c.points, [V1, 0]];
    add("polyline", { points: pts.map((q) => x(q[0]).toFixed(1) + "," + y(q[1]).toFixed(1)).join(" "),
                      class: "curve" + (c.stand ? " stand" : ""), stroke: colour });
  }
}

function renderLimits() {
  const d = state.detail, f = d.fia;
  const locked = d.known && !d.overridden && !state.touched;
  // Unlocked is not yet changed: it is the driver's only once a figure moves.
  const changed = state.unlimited || Object.keys(state.extra).length > 0
    || state.qualify !== d.published.qualify
    || state.race !== Math.min(d.published.race, d.limits.race.max);
  const yours = d.known && !locked && changed;

  const badge = $("fia-badge");
  badge.dataset.kind = !d.known ? "none" : yours ? "yours" : "official";
  badge.textContent = !d.known ? "No official figure" : yours ? "Your settings" : "Official";
  $("fia-title").textContent = !d.known ? "Nothing published for this circuit"
    : yours ? "Not the FIA's figures for this race"
    : f.notice ? "FIA Power Unit notice · round " + f.notice
    : "Official 2026 figures";
  $("limits-source").textContent = !d.known
    ? "So this is yours to set. How much you can recover decides how much "
      + "you can spend, which makes it the number that matters most here."
    : yours ? "Everything is planned around what is set here, and it is what "
      + "gets written into your setup."
    : !locked ? "Unlocked, and nothing is changed yet. These are still the FIA's figures."
    : f.notice ? "These come from the FIA's document for this race. You don't need to change them."
    : "These are the FIA's figures for this circuit. You don't need to change them.";

  const toggle = $("limits-toggle");
  toggle.hidden = !d.known;
  toggle.textContent = locked ? "Change" : "Back to official";
  toggle.onclick = () => {
    if (locked) {
      state.touched = true;
    } else {
      state.touched = false;
      state.qualify = d.published.qualify;
      state.race = Math.min(d.published.race, d.limits.race.max);
      state.unlimited = false;
      state.extra = {};
      d.overridden = false;
    }
    renderLimits();
  };

  $("limits-block").dataset.locked = String(locked);
  $("sliders").dataset.locked = String(locked);
  $("remember-wrap").hidden = locked;
  $("fia-effect").hidden = locked;
  $("unlimited-hint").hidden = !state.unlimited;

  slider("qualify", d.limits.qualify);
  slider("race", d.limits.race);

  const edited = (name, value, official) => {
    state.touched = true;
    if (value === official || value == null) delete state.extra[name];
    else state.extra[name] = value;
    renderLimits();
  };
  const range = d.limits.other || d.limits.qualify;
  for (const name of OTHER_LIMITS) {
    const input = $("x-" + name);
    input.disabled = locked;
    input.min = range.min / 10;
    input.max = range.max / 10;
    input.value = (otherLimit(name) / 10).toFixed(1);
    input.onchange = () => {
      const raw = Math.round(Number(input.value) * 10);
      const value = Math.max(range.min, Math.min(range.max, isFinite(raw) ? raw : f.limits[name]));
      // Set outright, so it stops following the two sliders.
      state.touched = true;
      state.extra[name] = value;
      renderLimits();
    };
  }
  const rate = $("x-rate_kw_s");
  rate.disabled = locked;
  rate.value = String(state.extra.rate_kw_s || f.rate_kw_s || 100);
  rate.onchange = () => edited("rate_kw_s", Number(rate.value), f.rate_kw_s);

  for (const [field] of CURVE_FIELDS) {
    const select = $("x-" + field), official = f.curves[field] || "";
    select.textContent = "";
    const names = f.curve_names.slice();
    if (official && !names.includes(official)) names.push(official);
    if (!official) {
      select.append(new Option(field === "curve_alternative" ? "None"
        : "Not named (Base - Overtake)", ""));
    }
    for (const name of names) select.append(new Option(name, name));
    select.value = curveName(field);
    select.disabled = locked;
    select.onchange = () => edited(field, select.value || null, official || null);
  }
  drawCurves();

  // Where the alternative curve applies, on the circuit and in metres.
  const zones = curveName("curve_alternative") ? f.alt_zones : [];
  $("fia-sectors").hidden = !zones.length;
  if (zones.length) {
    $("fia-sectors-text").textContent = curveName("curve_alternative") + " replaces the race curve in "
      + zones.length + (zones.length === 1 ? " sector: " : " sectors: ")
      + f.alt_zones_m.map((z) => z[0] + "–" + z[1] + " m").join(", ") + ".";
    Maps.sectors("fia-track", zones, "#b08cff");
  }
}

function slider(which, limit) {
  const input = $(which + "-range");
  const out = $(which + "-value");
  // One step past the car's own maximum is "no limit at all" -- the
  // ERS_UNLIMITED_MODE switch. The range comes from the car's setup.ini rather
  // than a constant here, so a slider can never offer a number the car would
  // silently clamp.
  const top = limit.max + limit.step;
  input.min = limit.min;
  input.max = top;
  input.step = limit.step;
  input.value = state.unlimited ? top : state[which];

  const fill = 100 * (input.value - limit.min) / (top - limit.min);
  input.style.setProperty("--fill", fill.toFixed(1) + "%");

  out.classList.toggle("unlimited", state.unlimited);
  out.textContent = "";
  if (state.unlimited) {
    out.append(document.createTextNode("∞"), el("span", null, "no limit"));
  } else {
    out.append(document.createTextNode(mj(state[which])), el("span", null, "MJ"));
  }

  input.oninput = () => {
    const raw = Number(input.value);
    state.touched = true;
    if (raw > limit.max) {
      state.unlimited = true;
    } else {
      state.unlimited = false;
      state[which] = raw;
    }
    $("unlimited-hint").hidden = !state.unlimited;
    renderLimits();
  };
}

/* ------------------------------------------------------------------ solve */
async function run() {
  const d = state.detail;
  if (!d) return;
  $("run").disabled = true;
  $("intro").hidden = true;
  $("results").hidden = true;
  $("apply-panel").hidden = true;
  $("applied").hidden = true;
  $("trouble").hidden = true;
  $("switch-call").hidden = true;
  state.log = [];
  $("cards").innerHTML = "";
  $("no-maps").hidden = false;
  state.saved = false;
  state.mapsKey = d.key;
  showView("solve");
  solving(true);
  $("running").dataset.stopping = "false";
  $("stop").disabled = false;
  $("stop").textContent = "Stop";
  $("live").hidden = true;
  showProgress(0, "Starting", 0);
  state.since = 0;
  state.summary = [];
  state.setup = null;
  setStatus("Starting", "run");

  const remember = $("remember").checked && !$("remember-wrap").hidden;
  try {
    await post("/api/solve", {
      key: d.key,
      circuit: d.circuit,
      qualify: state.qualify,
      race: state.race,
      unlimited: state.unlimited,
      overrides: $("limits-block").dataset.locked === "true" ? {} : state.extra,
      remember: remember,
      forget: !remember && d.overridden,
      write_alloc: $("write-alloc").checked,
    });
  } catch (err) {
    setStatus("Could not start", "bad");
    solving(false);
    $("run").disabled = false;
    failure(err);
    return;
  }
  state.running = true;
  // A failed poll retries forever, so a second run would otherwise leave two
  // loops reading the same queue and stepping on each other's cursor.
  state.pollId += 1;
  poll(state.pollId);
}

function showProgress(fraction, phase, elapsed) {
  const value = Math.round(100 * fraction);
  $("bar").style.width = value + "%";
  $("bar-outer").setAttribute("aria-valuenow", String(value));
  $("pct").textContent = value + "%";
  $("ring-arc").style.strokeDashoffset = String(402 * (1 - Math.max(0, Math.min(1, fraction))));
  if (phase) $("phase").textContent = phase;
  $("elapsed").textContent = elapsed >= 60
    ? Math.floor(elapsed / 60) + "m " + Math.round(elapsed % 60) + "s"
    : Math.round(elapsed) + "s";
}

async function poll(id) {
  if (id !== state.pollId) return;
  let data;
  try {
    data = await get("/api/poll?since=" + state.since
                     + "&have=" + state.summary.length);
  } catch (err) {
    // Keep trying -- a busy moment is not a lost optimizer -- but say so
    // after a few misses instead of leaving the bar where it stopped.
    state.pollMisses = (state.pollMisses || 0) + 1;
    if (state.pollMisses >= 3 && err instanceof TypeError) {
      state.unreachable = true;
      setStatus("The optimizer is not responding", "bad");
      $("phase").textContent = "Not responding — close the optimizer and "
        + "start it again, then Copy diagnostics if it happens twice";
    }
    setTimeout(() => poll(id), 800);
    return;
  }
  state.pollMisses = 0;
  if (id !== state.pollId) return;
  if (data.reset) { state.log = []; state.since = 0; }
  state.since = data.since;
  // The page stopped showing the solver's commentary, but support still
  // wants it: it is kept here and goes out with the diagnostics instead.
  for (const line of data.lines || []) {
    if (line.text.trim()) state.log.push(line.text);
  }
  if (state.log.length > 400) state.log.splice(0, state.log.length - 400);
  paintSwitch(data.switch);

  // The server sends the summary only when ours is out of date.
  if (data.summary) {
    state.summary = data.summary;
    renderCards();
  }

  if (data.running) {
    // While it winds down the bar is frozen where it got to rather than
    // creeping on, because it is no longer measuring anything.
    if (data.stopping) {
      $("running").dataset.stopping = "true";
      $("stop").disabled = true;
      $("stop").textContent = "Stopping";
      $("phase").textContent = "Stopping";
    } else {
      showProgress(data.progress, data.phase, data.elapsed);
    }
    setStatus(data.status, "run");
    setTimeout(() => poll(id), 250);
    return;
  }

  state.running = false;
  $("run").disabled = false;
  $("live").hidden = false;

  if (data.cancelled) {
    solving(false);
    paintSteps();
    // Nothing survives a stop, so the page goes back to where it started
    // rather than leaving an empty results heading behind.
    $("results").hidden = true;
    $("intro").hidden = false;
    setStatus("Stopped", "stopped");
    return;
  }

  showProgress(1, "Finished", data.elapsed);
  // A beat on 100% so the ring is seen to finish, then on to what it made.
  setTimeout(() => {
    solving(false);
    if (!data.error && state.summary.length && state.view === "solve") showView("maps");
  }, 700);
  if (data.error) failure(data.error);
  setStatus(data.error ? "Something went wrong" : "Ready",
            data.error ? "bad" : "ready");
}

async function stop() {
  $("stop").disabled = true;
  $("stop").textContent = "Stopping";
  $("phase").textContent = "Stopping";
  $("running").dataset.stopping = "true";
  try {
    await post("/api/cancel", {});
  } catch (err) {
    $("stop").disabled = false;
    $("stop").textContent = "Stop";
    $("running").dataset.stopping = "false";
  }
}

/* The one instruction on this page that has to be carried out in the car,
 * so it is set large and sits above the maps. The server sends it as a
 * sentence naming the landmark followed by what it is worth; the landmark is
 * what a driver needs at speed, and the rest can be read afterwards. */
function paintSwitch(text) {
  const box = $("switch-call");
  if (!text) { box.hidden = true; return; }
  const cut = text.indexOf(". ");
  box.hidden = false;
  $("switch-where").textContent = cut < 0 ? text : text.slice(0, cut + 1);
  $("switch-why").textContent = cut < 0 ? "" : text.slice(cut + 2);
}

function failure(err) {
  if (!err) return;
  $("trouble-text").textContent = String((err && err.message) || err);
  $("trouble").hidden = false;
}

/* ------------------------------------------------------------------ cards */
function renderCards() {
  const have = state.summary.length > 0;
  $("results").hidden = !have;
  $("no-maps").hidden = have;
  if (!have) { $("cards").innerHTML = ""; paintSteps(); return; }

  // The maps outlive the session that produced them -- reopen the window
  // after driving something else and the run is still there to be saved,
  // while the session behind it is gone. Reading its name regardless threw,
  // and a thrown renderer draws nothing at all: the whole result vanished
  // rather than losing its subtitle.
  const d = state.detail;
  $("results-title").textContent = d
    ? d.name + " · " + d.pooled + (d.pooled === 1 ? " lap" : " laps")
      + (state.unlimited ? " · no recharge limit"
                         : " · recharge " + mj(state.qualify) + "/" + mj(state.race) + " MJ")
    : "Maps from an earlier session";
  $("apply-open").disabled = state.summary.length < 3;
  Maps.render(state.summary);
  paintSteps();
}

/* The lap drawn against distance, under the circuit: speed as a line, the
 * map's zones as bands behind it, braking as the darker stretches. Against
 * the speed trace the bands can be seen to sit on the straights; the braking
 * zones are the landmarks that say which straight. */
function strip(s) {
  const W = 1000, H = 84;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "strip");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label",
    s.label + ": " + s.map.length + " deployment zones over "
    + Math.round(s.length_m) + " metres, peaking at "
    + Math.round(s.peak_kph) + " kph");

  const x = (m) => Math.max(0, Math.min(W, W * m / s.length_m));
  const rect = (a, b, fill, opacity) => {
    if (b <= a) return;
    const r = document.createElementNS(ns, "rect");
    r.setAttribute("x", a.toFixed(2));
    r.setAttribute("y", "0");
    r.setAttribute("width", Math.max(0.8, b - a).toFixed(2));
    r.setAttribute("height", String(H));
    r.setAttribute("fill", fill);
    if (opacity) r.setAttribute("opacity", String(opacity));
    svg.append(r);
  };
  // A zone may wrap the start/finish line, so it is drawn as two pieces.
  const band = (from, to, fill, opacity) => {
    if (to >= from) rect(x(from), x(to), fill, opacity);
    else { rect(x(from), W, fill, opacity); rect(0, x(to), fill, opacity); }
  };

  for (const z of (s.braking || [])) rect(x(z[0]), x(z[1]), "var(--brake)");
  for (const sp of s.map) {
    if (sp.deploy_kw > 0) {
      band(sp.start, sp.deploy_end, "var(--deploy)", 0.10 + 0.16 * Math.min(1, sp.deploy_kw / MAX_KW));
    }
    if (sp.clip_kw > 0) band(sp.clip_start, sp.end, "var(--harvest)", 0.2);
  }

  if (s.speed && s.speed.length > 1) {
    const top = Math.max(s.peak_kph, 1) * 1.08;
    const pts = s.speed.map((v, i) =>
      (W * i / (s.speed.length - 1)).toFixed(1) + "," + (H - (H - 8) * (v / top)).toFixed(1));
    const area = document.createElementNS(ns, "polygon");
    area.setAttribute("points", "0," + H + " " + pts.join(" ") + " " + W + "," + H);
    area.setAttribute("fill", "rgba(234,240,248,.07)");
    const line = document.createElementNS(ns, "polyline");
    line.setAttribute("points", pts.join(" "));
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", "#c9d4e3");
    line.setAttribute("stroke-width", "1.4");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(area, line);
  }
  return svg;
}

/* ------------------------------------------------------------ diagnostics */
/* One button, so a report carries what is needed to act on it. Without this a
 * problem arrives as "the map felt wrong", with no way to tell which build
 * produced it, at which circuit, on what settings. */
function diagnostics() {
  const d = state.detail || {};
  const g = (state.game || {});
  const out = [];
  out.push("FA26 ERS Deployment Optimizer v" + (state.version || "unknown"));
  out.push(new Date().toISOString());
  if (state.ready && state.ready.documents) {
    const d = state.ready.documents;
    out.push("documents: " + d.folder + (d.chosen_by_hand ? " (set by hand)" : ""));
    out.push("  folder exists: " + d.exists + ", setup folders: " + d.setups
             + ", this app can write there: " + d.writable);
  }
  if (state.ready) {
    out.push("lap integration: " + (state.ready.integration || "unknown"));
    out.push("ransomware protection (Controlled folder access): "
             + (state.ready.protection || "unknown"));
  }
  if (state.ready && state.ready.telemetry) {
    const w = state.ready.telemetry;
    out.push("watching: " + w.folder
             + (w.chosen_by_hand ? " (set by hand)" : ""));
    out.push("  folder exists: " + w.exists + ", lap files: " + w.laps);
    for (const other of (w.looked_in || [])) {
      if (other !== w.folder) out.push("  also considered: " + other);
    }
  }
  if (state.ready) {
    out.push("Assetto Corsa: " + (state.ready.root || "not found")
             + (state.ready.found_automatically ? " (found automatically)"
                                                : " (set by hand)"));
    out.push("logger installed: " + state.ready.logger);
    const cd = state.ready.circuit_data || {};
    out.push("circuit data: " + (cd.result || "unknown")
             + (cd.checked ? " (checked " + cd.checked + ")" : ""));
  }
  // Unknown is not "no". A page whose optimizer had already shut down -- the
  // Edge window that lost it -- reported "game running: false" to a player
  // who was on track, and the report pointed at the wrong bug.
  if (state.unreachable) {
    out.push("optimizer reachable: NO -- the page lost the program behind it");
  }
  if (state.loadError) {
    out.push("reading the laps failed: " + state.loadError.message);
    const trace = state.loadError.trace.trim().split("\n").slice(-14);
    for (const line of trace) if (line.trim()) out.push("  | " + line);
  }
  out.push("game running: " + (state.game ? !!g.running
    : "unknown (the page never heard from the optimizer)"));
  out.push("");
  out.push("SESSION");
  out.push("  circuit: " + (d.name || "-") + "   (" + (d.circuit || "-") + ")");
  out.push("  recorded: " + d.when + "   laps: " + d.recorded
           + ", using " + d.pooled);
  out.push("  best lap: " + d.best);
  out.push("  fuel: " + (d.fuel_l == null ? "-" : d.fuel_l.toFixed(1) + " L")
           + "   battery at start: "
           + (d.charge == null ? "-" : Math.round(d.charge * 100) + "%"));
  out.push("  live: " + d.live);
  out.push("  recharge limit: " + (state.unlimited ? "unlimited"
           : mj(state.qualify) + " MJ qualifying / " + mj(state.race) + " race")
           + "   source: " + (d.source || "-")
           + (d.overridden || state.touched ? "   (changed by hand)" : ""));
  for (const w of (d.warnings || [])) out.push("  warning: " + w.title);

  if (state.summary.length) {
    out.push("");
    out.push("RESULT");
    for (const s of state.summary) {
      out.push("  STRAT " + s.strat + "  " + s.label + "  " + s.lap
               + "   deploy " + s.deploy_mj.toFixed(2) + " MJ"
               + "   harvest " + s.harvest_mj.toFixed(2) + " MJ"
               + "   battery " + pct(s.store_start) + " -> " + pct(s.store_end)
               + "   " + s.map.length + " zones");
      for (const n of (s.notes || [])) out.push("      note: " + n);
    }
  }

  const where = $("switch-where").textContent;
  if (where) {
    out.push("");
    out.push("IN THE CAR");
    out.push("  " + where + " " + $("switch-why").textContent);
  }
  if (state.log.length) {
    out.push("");
    out.push("ACTIVITY");
    for (const line of state.log) out.push("  " + line);
  }
  return out.join("\n");
}

async function copyDiagnostics(button) {
  button = button && button.tagName ? button : $("copy-diag");
  const text = diagnostics();
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch (err) {
    // Some viewers refuse the clipboard outright. Show the text selected so
    // it can be copied by hand -- and make sure it can be got rid of again,
    // because an undismissable box over the app is worse than no button.
    const box = el("textarea", "diag-fallback");
    box.value = text;
    box.readOnly = true;
    const close = () => {
      box.remove();
      hint.remove();
      document.removeEventListener("keydown", onKey, true);
      button.textContent = "Copy diagnostics";
    };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    const hint = el("button", "diag-close", "Close");
    hint.type = "button";
    hint.onclick = close;
    document.body.append(box, hint);
    document.addEventListener("keydown", onKey, true);
    box.focus();
    box.select();
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    if (ok) close();
  }
  button.textContent = ok ? "Copied" : "Press Ctrl+C";
  if (ok) setTimeout(() => { button.textContent = "Copy diagnostics"; }, 2000);
}

/* ------------------------------------------------------------------ apply */
function closeApply() {
  $("apply-panel").hidden = true;
  $("apply-back").hidden = true;
  document.removeEventListener("keydown", applyKeys, true);
  clearInterval(state.setupWatch);
  window.removeEventListener("focus", refreshSetups);
}

/* The note says "save one in Assetto Corsa, then come back" -- so coming back
 * has to find it. The list used to be read once, when the panel opened, and a
 * setup saved while it was open never appeared until it was closed and opened
 * again. It is re-read every few seconds and whenever the window regains focus,
 * and redrawn only when something on disk changed. */
async function refreshSetups() {
  if ($("apply-panel").hidden || !(state.detail || state.mapsKey)) return;
  let setups = [];
  try {
    setups = (await get(state.detail
      ? "/api/setups?circuit=" + encodeURIComponent(state.detail.circuit)
      : "/api/setups?key=" + encodeURIComponent(state.mapsKey))).setups;
  } catch (err) { return; }
  const stamp = setups.map((s) => s.path + "@" + s.mtime).join("|");
  if (stamp === state.setupStamp) return;
  state.setupStamp = stamp;
  renderSetups(setups);
}

function applyKeys(e) {
  if (e.key === "Escape") closeApply();
}

async function openApply() {
  $("apply-panel").hidden = false;
  document.addEventListener("keydown", applyKeys, true);
  $("applied").hidden = true;
  state.setup = null;
  state.setupStamp = null;
  $("apply-go").disabled = true;
  $("apply-go").textContent = "Save maps";
  $("setup-list").innerHTML = "";
  await refreshSetups();
  if (state.setupStamp === null) renderSetups([]);
  clearInterval(state.setupWatch);
  state.setupWatch = setInterval(refreshSetups, 3000);
  window.addEventListener("focus", refreshSetups);
}

function renderSetups(setups) {
  const list = $("setup-list");
  list.innerHTML = "";
  // A setup chosen before the list was redrawn stays chosen if it is still
  // there; one that has gone is let go of.
  const kept = state.setup && setups.find((s) => s.path === state.setup.path);
  state.setup = kept || null;
  $("apply-go").disabled = !kept;
  if (!kept) $("apply-go").textContent = "Save maps";

  if (!setups.length) {
    $("apply-note").textContent =
      "There is no setup saved for this circuit yet. Save one from the pit "
      + "setup screen in Assetto Corsa — any name will do — then "
      + "come back. The maps go into a setup you already have; they are never "
      + "created from nothing.";
    return;
  }
  const onlyGeneric = setups.every((s) => s.generic);
  $("apply-note").textContent =
    (onlyGeneric
      ? "There is no setup saved for this circuit, only ones saved for all "
        + "tracks. Maps saved into one of those go with it to every circuit — "
        + "save a setup for this circuit in Assetto Corsa if you would rather "
        + "keep them here. "
      : "")
    + "Only the deployment maps are replaced. Nothing else in your setup is "
    + "touched, and a backup copy is made first.";

  let headed = false;
  for (const s of setups) {
    if (s.generic && !headed && !onlyGeneric) {
      list.append(el("p", "setup-heading", "Saved for all tracks"));
      headed = true;
    }
    const b = el("button", "setup");
    b.type = "button";
    b.setAttribute("aria-pressed", String(!!(state.setup && state.setup.path === s.path)));
    b.append(el("span", "setup-name", s.name));
    if (s.generic) b.append(el("span", "tag", "all tracks"));
    if (s.ours) b.append(el("span", "tag", "saved by this tool"));
    b.append(el("span", "setup-meta", s.folder + "  ·  " + s.when));
    b.onclick = () => {
      state.setup = s;
      for (const other of list.querySelectorAll("button.setup")) {
        other.setAttribute("aria-pressed", String(other === b));
      }
      $("apply-go").disabled = false;
      $("apply-go").textContent = "Save into " + s.name
        + (s.generic ? " (all tracks)" : "");
    };
    list.append(b);
  }
  if (state.setup) {
    $("apply-go").textContent = "Save into " + state.setup.name
      + (state.setup.generic ? " (all tracks)" : "");
  }
}

async function applyGo() {
  if (!state.setup) return;
  $("apply-go").disabled = true;
  setStatus("Saving", "run");
  let res;
  try {
    res = await post("/api/apply", {
      path: state.setup.path,
      write_alloc: $("write-alloc").checked,
    });
  } catch (err) {
    setStatus("Could not save", "bad");
    failure(err);
    $("apply-go").disabled = false;
    return;
  }
  setStatus("Saved", "ready");
  state.saved = true;
  $("apply-panel").hidden = true;
  clearInterval(state.setupWatch);
  window.removeEventListener("focus", refreshSetups);
  renderApplied(res);
}

function renderApplied(res) {
  const panel = $("applied");
  panel.hidden = false;
  panel.innerHTML = "";
  panel.append(el("h2", null, "Saved into " + state.setup.name));

  const p = el("p");
  p.append(document.createTextNode(
    "All " + state.summary.length + " maps are in your setup, and the PU mode "
    + "switch on the wheel picks between them: Q qualifying, RACE race, SLO "
    + "and SC recharge, AD1 attack, AD2 defend, K2 push, ES save. It will load "
    + "on STRAT " + res.active + ". In FS, FW, IN, K2+, T4 and RS the "));
  p.append(el("code", null, "STRAT Map"));
  p.append(document.createTextNode(
    " rotary decides instead, which is where to compare maps of your own."));
  panel.append(p);

  panel.append(el("p", "loud", "Restart the session before you drive"));
  panel.append(el("p", null,
    "Assetto Corsa only reads a setup when the car loads, so a file saved "
    + "while you are already on track never reaches it. Reloading the setup in "
    + "the pit menu is not enough."));

  if (res.stale) {
    panel.append(el("p", "loud",
      "The car has not picked this up yet — " + res.stale));
  }

  // What went onto the Power & Energy Limits page, in the menu's own words,
  // so it can be checked against the setup screen without opening the file.
  if (res.unlimited) {
    panel.append(el("p", null,
      "Per-lap recharge limit switched off, as you asked."));
  } else if (res.limits && res.limits.length) {
    panel.append(el("p", null, {
      notice: "ERS limits set to the FIA notice for this circuit:",
      yours: "ERS limits set to the figures you chose. Race + OT, practice "
        + "and the out lap follow from them:",
      default: "There is no FIA notice for this circuit, so the ERS limits "
        + "are set to the regulation defaults:",
    }[res.limits_from] || "ERS limits set:"));
    const dl = el("dl", "facts");
    for (const [label, value] of res.limits) {
      dl.append(el("dt", null, label), el("dd", null, value));
    }
    panel.append(dl);
    panel.append(el("p", "hint",
      "Power and Energy Limits is left as you set it. Qualify makes a "
      + "practice session hold you to qualifying's limits."));
  }

  const ul = el("ul");
  const items = [
    "Check it worked: STRAT 3 should feel several seconds a lap slower and "
      + "give you almost no extra power. If all three feel the same, the maps "
      + "did not reach the car.",
    "Drive a few laps on this map, then run the optimizer again. The second "
      + "answer is fitted to a car that was actually using its battery, so it "
      + "is the better one.",
  ];
  if (res.backup) items.push("Your previous setup is saved at " + res.backup);
  for (const t of items) ul.append(el("li", null, t));
  panel.append(ul);
  const row = el("div", "btns");
  const back = el("button", "btn", "Back to the maps");
  back.type = "button";
  back.onclick = () => showView("maps");
  row.append(back);
  panel.append(row);
  paintSteps();
}

/* ------------------------------------------------------------------- wire */
$("run").onclick = run;
$("stop").onclick = stop;
$("use-last").onclick = () => { state.useStale = true; state.declined = null; load(true); };
$("session-yes").onclick = () => { state.useStale = true; showView("solve"); };
$("session-no").onclick = () => {
  if (state.detail) state.declined = state.detail.key;
  state.useStale = false;
  load(true);
};
$("trouble-close").onclick = () => { $("trouble").hidden = true; };
for (const tab of document.querySelectorAll(".pane-tab")) {
  tab.onclick = () => settingsPane(tab.dataset.settings);
}
document.addEventListener("click", (e) => {
  const go = e.target.closest("[data-go]");
  if (go) showView(go.dataset.go);
});
$("ac-save").onclick = saveAcPath;
for (const tab of document.querySelectorAll(".tab")) {
  tab.onclick = () => showView(tab.dataset.view);
}
$("live-search").onchange = async (e) => {
  try {
    await post("/api/livesearch", { on: e.target.checked });
  } catch (err) {
    e.target.checked = !e.target.checked;
    return;
  }
  // Repaint now rather than at the next lap. The block this reveals is the
  // whole feedback for the switch, and a switch that appears to do nothing
  // for four minutes gets clicked again.
  if (state.detail) paintLive(state.detail.key);
};
$("compare-go").onclick = runCompare;
$("compare-clear").onclick = () => { compare.picked = []; $("compare-out").textContent = ""; loadRuns(); };
$("setup-change").onclick = () => showView("settings", "folders");
$("find-laps").onclick = () => showView("settings", "folders");
/* The path is chosen in Explorer rather than typed: it is long, easy to get
 * subtly wrong, and the program could only say so afterwards. */
const PICKS = {
  ac: ["ac", (path) => saveAcPath({ value: path })],
  doc: ["documents", saveDocuments],
  tel: ["telemetry", saveTelemetry],
};

for (const button of document.querySelectorAll("[data-pick]")) {
  button.onclick = async () => {
    const [which, save] = PICKS[button.dataset.pick];
    const was = button.textContent;
    button.disabled = true;
    button.textContent = "Choose a folder\u2026";
    let chosen = "";
    try {
      chosen = (await post("/api/pickfolder", { which: which,
                                                start: startFor(which) })).path;
    } catch (err) {
      showFolderError(err);
    }
    button.disabled = false;
    button.textContent = was;
    if (chosen) await save(chosen);
  };
}

function startFor(which) {
  const r = state.ready || {};
  if (which === "documents") return (r.documents || {}).folder || "";
  if (which === "telemetry") return (r.telemetry || {}).folder || "";
  return r.root || "";
}

for (const button of document.querySelectorAll("[data-edit]")) {
  button.onclick = () => {
    const box = $(button.dataset.edit + "-edit");
    box.hidden = !box.hidden;
    button.textContent = box.hidden ? "Change" : "Cancel";
    if (!box.hidden) {
      const field = box.querySelector("input");
      field.focus();
      field.select();
    }
  };
}
$("ac-save-2").onclick = () => saveAcPath($("ac-path-2"));
$("ac-path-2").onkeydown = (e) => {
  if (e.key === "Enter") saveAcPath($("ac-path-2"));
};
$("doc-save").onclick = () => saveDocuments($("doc-path").value.trim());
$("doc-auto").onclick = () => saveDocuments("");
$("doc-path").onkeydown = (e) => {
  if (e.key === "Enter") saveDocuments($("doc-path").value.trim());
};
$("tel-save").onclick = () => saveTelemetry($("tel-path").value.trim());
$("tel-auto").onclick = () => saveTelemetry("");
$("tel-path").onkeydown = (e) => {
  if (e.key === "Enter") saveTelemetry($("tel-path").value.trim());
};
$("install-logger").onclick = installLogger;
$("install-logger-2").onclick = () => installLogger($("install-logger-2"));
$("ac-path").onkeydown = (e) => { if (e.key === "Enter") saveAcPath(); };
$("apply-open").onclick = () => showView("save");
$("copy-diag-about").onclick = () => copyDiagnostics($("copy-diag-about"));
$("copy-diag").onclick = () => copyDiagnostics($("copy-diag"));
$("copy-diag-wait").onclick = () => copyDiagnostics($("copy-diag-wait"));
$("apply-cancel").onclick = () => showView("maps");
$("apply-go").onclick = applyGo;

/* The maps live in the server, so reopening the window gets them back rather
 * than making anyone sit through the solve a second time. */
async function restore() {
  let data;
  try {
    data = await get("/api/poll?since=0&have=-1");
  } catch (err) {
    return;
  }
  if (data.running) {
    state.running = true;
    $("intro").hidden = true;
    showView("solve");
    solving(true);
    $("running").dataset.stopping = String(!!data.stopping);
    $("stop").disabled = !!data.stopping;
    $("run").disabled = true;
    state.pollId += 1;
    poll(state.pollId);
    return;
  }
  if (!data.summary || !data.summary.length) return;
  state.summary = data.summary;
  state.since = data.since;
  for (const line of data.lines || []) {
    if (line.text.trim()) state.log.push(line.text);
  }
  $("intro").hidden = true;
  paintSwitch(data.switch);
  // The session behind these maps may be over; the circuit is still theirs.
  state.mapsKey = data.key || "";
  if (data.key) Maps.loadOutline(data.key);
  renderCards();
  showView("maps");
}

showView("session");
/* A screen can be asked for by name in the address, as in `#settings`. The
 * app never does; it is how a screen is opened directly when checking it. */
checkReady().then((ok) => (ok ? load(true).then(restore) : null)).then(() => {
  const want = location.hash.slice(1);
  if (want) showView(want);
});

/* A newer version, when there is one. The optimizer asks once at start-up,
 * in the background, so the answer arrives a few seconds after the page. */
function newer(a, b) {
  const pa = String(a).split(".").map(Number), pb = String(b).split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0, y = pb[i] || 0;
    if (x !== y) return x > y;
  }
  return false;
}
async function checkForUpdate() {
  let r;
  try { r = await get("/api/ready"); } catch (err) { return; }
  const cd = r.circuit_data || {};
  if (!cd.latest || !newer(cd.latest, r.version)) return;
  const banner = $("update-banner");
  banner.textContent = "Version " + cd.latest + " of the optimizer is out. "
    + (cd.where || "Download it from the same place you got this one.");
  banner.hidden = false;
}
setTimeout(checkForUpdate, 6000);
setTimeout(checkForUpdate, 30000);
// Watch for laps as they are driven, so alt-tabbing back after a run shows
// them without anyone having to press anything.
state.watch = setInterval(() => {
  if (state.running) return;
  if (state.ready && !state.ready.ok) { checkReady(); return; }
  load(false);
}, WATCH_MS);
