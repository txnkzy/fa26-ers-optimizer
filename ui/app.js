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
const FLOOR_KW = 200;        // the floor held for a second at throttle
const WATCH_MS = 4000;       // how often to look for a lap you just finished

const state = {
  detail: null,
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
  game: null,
  useStale: false,
};

/* --------------------------------------------------------------- plumbing */
async function get(path) {
  const res = await fetch(path);
  const data = await res.json();
  if (data && data.error) throw new Error(data.error);
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
  $("setup").hidden = blocked ? false : r.logger;
  $("setup-path").hidden = !blocked;
  $("setup-logger").hidden = !!r.logger;
  $("intro").hidden = (blocked || !r.logger) || state.summary.length > 0;

  if (!blocked) {
    if (!r.logger) {
      $("setup-title").textContent = "One more step";
      $("setup-problem").textContent =
        "Assetto Corsa found at " + r.root + ".";
    }
    return true;
  }
  $("setup-title").textContent = r.kind === "no_car"
    ? "Car not found" : "Assetto Corsa not found";
  $("setup-problem").textContent = r.problem;
  $("ac-path").value = r.root || "";
  $("run").disabled = true;
  setStatus("Not set up", "bad");
  return false;
}

async function installLogger() {
  const button = $("install-logger");
  button.disabled = true;
  button.textContent = "Installing\u2026";
  $("setup-error").hidden = true;
  try {
    await post("/api/installlogger", {});
  } catch (err) {
    $("setup-error").textContent = String(err.message || err);
    $("setup-error").hidden = false;
    button.disabled = false;
    button.textContent = "Install the logger";
    return;
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
}

async function saveAcPath() {
  const value = $("ac-path").value.trim();
  if (!value) return;
  $("ac-save").disabled = true;
  $("setup-error").hidden = true;
  try {
    await post("/api/setpath", { path: value });
  } catch (err) {
    $("setup-error").textContent = String(err.message || err);
    $("setup-error").hidden = false;
    $("ac-save").disabled = false;
    return;
  }
  $("ac-save").disabled = false;
  if (await checkReady()) load(true);
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
    setStatus("Cannot reach the optimizer", "bad");
    return;
  }
  $("car-id").textContent = data.car;
  $("telemetry-path").textContent = data.folder;

  state.game = data.game;
  const session = data.session;
  // "This session" means laps driven since the sim started. A session from
  // hours ago and a reboot later is not current, however new it is on disk --
  // and a stale one is the state the map-order guard exists to catch.
  const usable = session && (session.live || state.useStale);
  if (!usable) {
    state.detail = null;
    state.revision = null;
    showWaiting(data.game, session);
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
  }
  renderSession(grew);
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
  $("run").disabled = true;

  if (game && game.running) {
    $("waiting-title").textContent = "Waiting for laps";
    $("waiting-text").textContent =
      "Assetto Corsa is running. Laps appear here as you finish them.";
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
  if (state.ready && !state.ready.logger) {
    showNotice("The logger is not installed",
               "No new laps will be recorded until it is.");
  }

  renderLimits();
}

/* ---------------------------------------------------------------- limits */
function renderLimits() {
  const d = state.detail;
  const locked = d.known && !d.overridden && !state.touched;

  $("limits-source").textContent = d.known
    ? "Official figures for this circuit. Everything below is planned around "
      + "them, so changing one changes every lap time."
    : "Nothing official covers this circuit, so this is yours to set. How much "
      + "you can recover decides how much you can spend, which makes it the "
      + "number that matters most here.";

  const toggle = $("limits-toggle");
  toggle.hidden = !d.known;
  toggle.textContent = locked ? "Change" : "Use official";
  toggle.onclick = () => {
    if (locked) {
      state.touched = true;
    } else {
      state.touched = false;
      state.qualify = d.published.qualify;
      state.race = Math.min(d.published.race, d.limits.race.max);
      state.unlimited = false;
      d.overridden = false;
    }
    renderLimits();
  };

  $("sliders").dataset.locked = String(locked);
  $("remember-wrap").hidden = locked;
  $("unlimited-hint").hidden = !state.unlimited;

  slider("qualify", d.limits.qualify);
  slider("race", d.limits.race);
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
    $("remember-wrap").hidden = false;
    slider("qualify", state.detail.limits.qualify);
    slider("race", state.detail.limits.race);
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
  $("activity-wrap").hidden = false;
  $("activity").textContent = "";
  $("cards").innerHTML = "";
  $("running").hidden = false;
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
      remember: remember,
      forget: !remember && d.overridden,
      write_alloc: $("write-alloc").checked,
    });
  } catch (err) {
    setStatus("Could not start", "bad");
    $("running").hidden = true;
    $("run").disabled = false;
    addLines([{ kind: "warn", text: String(err.message || err) }]);
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
    setTimeout(() => poll(id), 800);
    return;
  }
  if (id !== state.pollId) return;
  if (data.reset) { $("activity").textContent = ""; state.since = 0; }
  state.since = data.since;
  addLines(data.lines);

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
    $("running").hidden = true;
    // Nothing survives a stop, so the page goes back to where it started
    // rather than leaving an empty results heading behind.
    $("results").hidden = true;
    $("intro").hidden = false;
    setStatus("Stopped", "stopped");
    return;
  }

  showProgress(1, "Finished", data.elapsed);
  setTimeout(() => { $("running").hidden = true; }, 900);
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

function addLines(lines) {
  if (!lines || !lines.length) return;
  const box = $("activity");
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  for (const line of lines) {
    if (!line.text.trim()) {
      box.append(el("div", "gap"));
      continue;
    }
    const row = el("div", "say " + line.kind);
    row.append(el("span", "say-text", line.text));
    box.append(row);
  }
  if (stick) box.scrollTop = box.scrollHeight;
}

function applyDetailFilter() {
  $("activity").dataset.detail = String($("show-detail").checked);
}

/* ------------------------------------------------------------------ cards */
function renderCards() {
  const box = $("cards");
  box.innerHTML = "";
  if (!state.summary.length) return;

  $("results").hidden = false;
  const d = state.detail;
  const title = $("results-title");
  title.textContent = "";
  title.append(
    document.createTextNode(d.name),
    el("small", null,
       "from " + d.pooled + (d.pooled === 1 ? " lap" : " laps")
       + " · your best was " + d.best
       + (state.unlimited ? " · no recharge limit"
                          : " · recharge " + mj(state.qualify) + "/"
                            + mj(state.race) + " MJ")));
  $("apply-open").disabled = state.summary.length < 3;

  for (const s of state.summary) box.append(card(s));
}

function card(s) {
  const node = el("article", "card");
  node.dataset.strat = s.strat;

  const head = el("div", "card-head");
  head.append(el("span", "chip", "STRAT " + s.strat),
              el("span", "card-name", s.label),
              el("span", "card-time", s.lap));
  head.append(el("span", "card-delta " + (s.delta_s < 0 ? "gain" : "loss"),
    Math.abs(s.delta_s) < 0.05
      ? "level with your best lap"
      : Math.abs(s.delta_s).toFixed(1) + "s "
        + (s.delta_s < 0 ? "quicker" : "slower") + " than your best lap"));
  node.append(head);

  const metrics = el("div", "metrics");
  const add = (label, value) => {
    const wrap = el("div");
    wrap.append(el("div", "metric-label", label),
                el("div", "metric-value", value));
    metrics.append(wrap);
  };
  add("Battery used", s.deploy_mj.toFixed(1) + " MJ");
  add("Battery recovered", s.harvest_mj.toFixed(1) + " MJ");
  add("Starts at", pct(s.store_start));
  add("Ends at", pct(s.store_end));
  add("Dips to", pct(s.store_min));
  add("Zones", String(s.map.length));
  node.append(metrics);

  node.append(strip(s), axis(s));
  const legend = el("p", "strip-legend");
  legend.append(el("span", "dep", "where you get extra power"),
                el("span", "spd", "your speed, peaking at "
                   + Math.round(s.peak_kph) + " kph"));
  if (s.map.some((m) => m.clip_kw > 0)) {
    legend.append(el("span", "har", "charging while on power"));
  }
  node.append(legend);

  if (s.notes.length) {
    const ul = el("ul", "card-notes");
    for (const n of s.notes) ul.append(el("li", null, n));
    node.append(ul);
  }

  node.append(splitTable(s));
  return node;
}

/* The map drawn against lap distance: power above the line, charging below.
 * The speed trace sits behind it because a row of bars over a bare axis says
 * nothing -- against the trace you can see the blocks land on the straights.
 * Charging while on power is rare, so that band is only drawn when the map
 * actually uses it rather than reserving a third of the height for nothing. */
function strip(s) {
  const W = 1000;
  const clips = s.map.filter((m) => m.clip_kw > 0);
  const DEPTH = clips.length ? 18 : 0;
  const MID = 64;
  const H = MID + 4 + DEPTH;
  const ns = "http://www.w3.org/2000/svg";

  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "strip");
  svg.style.height = H + "px";
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label",
    s.label + ": " + s.map.length + " deployment zones over "
    + Math.round(s.length_m) + " metres, peaking at "
    + Math.round(s.peak_kph) + " kph");

  const x = (m) => Math.max(0, Math.min(W, W * m / s.length_m));
  const rect = (a, b, y, h, fill, opacity) => {
    if (b <= a || h <= 0) return;
    const r = document.createElementNS(ns, "rect");
    r.setAttribute("x", a.toFixed(2));
    r.setAttribute("y", y.toFixed(2));
    r.setAttribute("width", Math.max(0.8, b - a).toFixed(2));
    r.setAttribute("height", h.toFixed(2));
    r.setAttribute("fill", fill);
    if (opacity) r.setAttribute("opacity", String(opacity));
    svg.append(r);
  };

  for (let i = 1; i < 4; i++) rect(W * i / 4, W * i / 4 + 1, 0, H, "var(--line-soft)");

  if (s.speed && s.speed.length > 1) {
    const top = Math.max(s.peak_kph, 1);
    const pts = s.speed.map((v, i) => {
      const px = W * i / (s.speed.length - 1);
      const py = MID - MID * 0.92 * (v / top);
      return px.toFixed(1) + "," + py.toFixed(1);
    });
    const area = document.createElementNS(ns, "polygon");
    area.setAttribute("points", "0," + MID + " " + pts.join(" ") + " " + W + "," + MID);
    area.setAttribute("fill", "var(--raise)");
    svg.append(area);
  }

  // the level the car holds for a second after every throttle application
  rect(0, W, MID - MID * FLOOR_KW / MAX_KW, 1, "var(--line)");

  for (const sp of s.map) {
    const h = MID * Math.min(1, sp.deploy_kw / MAX_KW);
    // A zone may wrap the start/finish line, so it is drawn as two pieces.
    if (sp.deploy_end >= sp.start) {
      rect(x(sp.start), x(sp.deploy_end), MID - h, h, "var(--hue)");
    } else {
      rect(x(sp.start), W, MID - h, h, "var(--hue)");
      rect(0, x(sp.deploy_end), MID - h, h, "var(--hue)");
    }
  }

  rect(0, W, MID, 1.5, "var(--line)");

  for (const sp of clips) {
    const depth = DEPTH * Math.min(1, sp.clip_kw / MAX_KW);
    if (sp.end >= sp.clip_start) {
      rect(x(sp.clip_start), x(sp.end), MID + 3, depth, "var(--dim)", 0.8);
    } else {
      rect(x(sp.clip_start), W, MID + 3, depth, "var(--dim)", 0.8);
      rect(0, x(sp.end), MID + 3, depth, "var(--dim)", 0.8);
    }
  }
  return svg;
}

/* Distance markers under the strip. The quarters are drawn as gridlines
 * above, so the numbers and the lines share one scale. */
function axis(s) {
  const row = el("div", "strip-axis");
  for (let i = 0; i <= 4; i++) {
    row.append(el("span", null,
      Math.round(s.length_m * i / 4) + (i === 4 ? " m" : "")));
  }
  return row;
}

function splitTable(s) {
  const det = el("details", "splits");
  det.append(el("summary", null,
                "The numbers that get written into your setup"));
  const scroll = el("div", "splits-scroll");
  const table = el("table", "split-table");
  const head = el("tr");
  for (const h of ["start", "deployEnd", "clipStart", "splitEnd",
                   "deploy kW", "clip kW"]) head.append(el("th", null, h));
  table.append(head);
  for (const sp of s.map) {
    const tr = el("tr");
    for (const v of [sp.start, sp.deploy_end, sp.clip_start, sp.end,
                     sp.deploy_kw, sp.clip_kw]) tr.append(el("td", null, String(v)));
    table.append(tr);
  }
  scroll.append(table);
  det.append(scroll);
  return det;
}

/* ------------------------------------------------------------------ apply */
async function openApply() {
  $("apply-panel").hidden = false;
  $("applied").hidden = true;
  state.setup = null;
  $("apply-go").disabled = true;
  $("apply-go").textContent = "Save maps";

  const list = $("setup-list");
  list.innerHTML = "";
  let setups = [];
  try {
    setups = (await get("/api/setups?circuit="
      + encodeURIComponent(state.detail.circuit))).setups;
  } catch (err) { /* fall through to the empty case */ }

  if (!setups.length) {
    $("apply-note").textContent =
      "There is no setup saved for this circuit yet. Save one from the pit "
      + "setup screen in Assetto Corsa — any name will do — then "
      + "come back. The maps go into a setup you already have; they are never "
      + "created from nothing.";
    return;
  }
  $("apply-note").textContent =
    "Only deployment maps 1 to 3 are replaced. Nothing else in your setup is "
    + "touched, and a backup copy is made first.";

  for (const s of setups) {
    const b = el("button", "setup");
    b.type = "button";
    b.setAttribute("aria-pressed", "false");
    b.append(el("span", "setup-name", s.name));
    if (s.ours) b.append(el("span", "tag", "saved by this tool"));
    b.append(el("span", "setup-meta", s.folder + "  ·  " + s.when));
    b.onclick = () => {
      state.setup = s;
      for (const other of list.children) {
        other.setAttribute("aria-pressed", String(other === b));
      }
      $("apply-go").disabled = false;
      $("apply-go").textContent = "Save into " + s.name;
    };
    list.append(b);
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
    addLines([{ kind: "warn", text: String(err.message || err) }]);
    $("apply-go").disabled = false;
    return;
  }
  setStatus("Saved", "ready");
  $("apply-panel").hidden = true;
  renderApplied(res);
}

function renderApplied(res) {
  const panel = $("applied");
  panel.hidden = false;
  panel.innerHTML = "";
  panel.append(el("h2", null, "Saved into " + state.setup.name));

  const p = el("p");
  p.append(document.createTextNode(
    "All three maps are in your setup. It will load on STRAT "
    + res.active + ", the qualifying map. To change which one you are running, "
    + "use "));
  p.append(el("code", null, "STRAT Map"));
  p.append(document.createTextNode(
    " on the pit setup screen: 1 qualifying, 2 race, 3 recharge. The PU mode "
    + "switch on the wheel does not change it."));
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
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ------------------------------------------------------------------- wire */
$("run").onclick = run;
$("stop").onclick = stop;
$("use-last").onclick = () => { state.useStale = true; load(true); };
$("ac-save").onclick = saveAcPath;
$("install-logger").onclick = installLogger;
$("ac-path").onkeydown = (e) => { if (e.key === "Enter") saveAcPath(); };
$("apply-open").onclick = openApply;
$("apply-cancel").onclick = () => { $("apply-panel").hidden = true; };
$("apply-go").onclick = applyGo;
$("show-detail").onchange = applyDetailFilter;
$("activity-toggle").onclick = () => {
  const box = $("activity");
  box.hidden = !box.hidden;
  $("activity-toggle").textContent = box.hidden ? "Show" : "Hide";
};

applyDetailFilter();

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
    $("running").hidden = false;
    $("running").dataset.stopping = String(!!data.stopping);
    $("stop").disabled = !!data.stopping;
    $("activity-wrap").hidden = false;
    $("run").disabled = true;
    state.pollId += 1;
    poll(state.pollId);
    return;
  }
  if (!data.summary || !data.summary.length) return;
  state.summary = data.summary;
  state.since = data.since;
  $("intro").hidden = true;
  $("activity-wrap").hidden = false;
  addLines(data.lines);
  renderCards();
}

checkReady().then((ok) => (ok ? load(true).then(restore) : null));
// Watch for laps as they are driven, so alt-tabbing back after a run shows
// them without anyone having to press anything.
state.watch = setInterval(() => {
  if (state.running) return;
  if (state.ready && !state.ready.ok) { checkReady(); return; }
  load(false);
}, WATCH_MS);
