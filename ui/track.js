/* The Maps screen: the circuit, with one map drawn on it.
 *
 * Seven cards of bars over a distance axis said where a map deploys only to
 * someone who knows the lap in metres. Drawn on the circuit itself it needs
 * no translating: the orange is on the straights, and a car goes round at the
 * speeds the plan predicts, saying what the map is asking for where it is.
 *
 * The outline comes from the game's own racing-line file for the track
 * (`/api/track`). Where that cannot be read the lap is drawn as a plain loop,
 * which still puts each zone in the right order and proportion.
 */

"use strict";

const Maps = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const byId = (id) => document.getElementById(id);
  const MAX_KW = 350;
  const CAPACITY_KJ = 4000;
  const PAD = 26;
  //: How many times faster than life the car goes round. A lap in about
  //: twelve seconds: slow enough to follow, quick enough to see all of it.
  const PLAYBACK = 7;

  //: One colour per map, used for its row, the car and the header line.
  const TONES = { 1: "#ff5c7a", 2: "#5ec8ff", 3: "#54d6a0", 4: "#ff8a3d",
                  5: "#b08cff", 6: "#ffd23f", 7: "#7be0b4" };
  const WHAT = {
    1: "Spends the whole battery over one lap.",
    2: "Ends the lap on the charge it started with.",
    3: "Refills the battery for the next push lap.",
    4: "The race map with extra to spend, for Overtake.",
    5: "The race map, spending a little to hold position.",
    6: "The race map that spends the most.",
    7: "The race map that banks charge every lap.",
  };

  const view = {
    key: null,          // the session the outline was fetched for
    outline: null,      // {points: [[x, y, fraction]], width, height}
    plain: false,       // true when the outline is the stand-in loop
    summary: [],
    strat: null,        // which map is on show
    dist: 0,            // where the car is, as a fraction of the lap
    last: null,
    running: false,
  };

  /* ------------------------------------------------------------ geometry */
  /* A rounded loop, for a track whose racing line could not be read. */
  function plainLoop() {
    const pts = [], n = 240, w = 1000, h = 520, r = 200;
    const straight = w - 2 * r, arc = Math.PI * r;
    const total = 2 * straight + 2 * arc;
    for (let i = 0; i < n; i++) {
      let d = total * i / n, x, y;
      if (d < straight) { x = r + d; y = h; }
      else if ((d -= straight) < arc) {
        const a = Math.PI / 2 - d / r; x = w - r + r * Math.cos(a); y = h / 2 + (h / 2) * Math.sin(a);
      } else if ((d -= arc) < straight) { x = w - r - d; y = 0; }
      else { d -= straight; const a = -Math.PI / 2 - d / r; x = r + r * Math.cos(a); y = h / 2 + (h / 2) * Math.sin(a); }
      pts.push([x, y, i / n]);
    }
    return { points: pts, width: w, height: h };
  }

  function at(fraction) {
    const p = view.outline.points, n = p.length;
    let f = fraction - Math.floor(fraction);
    let lo = 0, hi = n - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (p[m][2] <= f) lo = m; else hi = m - 1; }
    const a = p[lo], b = p[(lo + 1) % n];
    const span = (lo + 1 < n ? b[2] : 1) - a[2];
    const k = span > 0 ? Math.min(1, (f - a[2]) / span) : 0;
    return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k];
  }

  /* The outline between two points of the lap, as SVG path data. A stretch
   * that runs over the start/finish line comes back as two pieces. */
  function pathBetween(from, to) {
    if (to < from) return pathBetween(from, 1) + " " + pathBetween(0, to);
    const p = view.outline.points, out = [at(from)];
    for (const q of p) if (q[2] > from && q[2] < to) out.push(q);
    out.push(at(Math.min(to, 0.99999)));
    return "M" + out.map((q) => q[0].toFixed(1) + "," + q[1].toFixed(1)).join(" L");
  }

  function wholeTrack() {
    return "M" + view.outline.points.map((q) => q[0].toFixed(1) + "," + q[1].toFixed(1)).join(" L") + " Z";
  }

  /* --------------------------------------------------------------- zones */
  /* A map's splits as stretches of the lap: what kind, how much, from where
   * to where, in metres and as fractions of the lap. */
  function zonesOf(s) {
    const L = s.length_m || 1, out = [];
    for (const sp of s.map) {
      if (sp.deploy_kw > 0 && sp.deploy_end !== sp.start) {
        out.push({ kind: "deploy", kw: sp.deploy_kw, a: sp.start, b: sp.deploy_end });
      }
      if (sp.clip_kw > 0 && sp.end !== sp.clip_start) {
        out.push({ kind: "harvest", kw: sp.clip_kw, a: sp.clip_start, b: sp.end });
      }
    }
    for (const z of out) { z.fa = Math.max(0, Math.min(1, z.a / L)); z.fb = Math.max(0, Math.min(1, z.b / L)); }
    return out;
  }

  /* Amber at low power, hot orange-red at the car's ceiling. */
  function deployColour(kw) {
    const t = Math.min(1, kw / MAX_KW);
    return "rgb(255," + Math.round(170 - 88 * t) + "," + Math.round(60 - 10 * t) + ")";
  }
  const colourOf = (z) => (z.kind === "deploy" ? deployColour(z.kw) : "#2fe0c2");

  function zoneAt(zones, f) {
    return zones.find((z) => (z.fb >= z.fa ? f >= z.fa && f < z.fb : f >= z.fa || f < z.fb));
  }

  function speedAt(s, f) {
    const v = s.speed || [];
    if (v.length < 2) return 0;
    const x = f * (v.length - 1), i = Math.floor(x);
    return v[i] + (v[Math.min(i + 1, v.length - 1)] - v[i]) * (x - i);
  }

  /* ------------------------------------------------------------- drawing */
  function frame(svg) {
    const o = view.outline;
    svg.setAttribute("viewBox", (-PAD) + " " + (-PAD) + " " + (o.width + 2 * PAD) + " " + (o.height + 2 * PAD));
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  }

  function node(tag, attrs) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }

  function current() {
    return view.summary.find((s) => s.strat === view.strat) || view.summary[0] || null;
  }

  function drawTrack() {
    const s = current(), svg = byId("track-svg");
    if (!s || !view.outline) return;
    frame(svg);
    svg.textContent = "";
    const scale = Math.max(view.outline.width, view.outline.height) / 560;
    const whole = wholeTrack();
    svg.append(node("path", { d: whole, class: "zone", stroke: "#2a3443", "stroke-width": 13 * scale }),
               node("path", { d: whole, class: "zone", stroke: "#111823", "stroke-width": 9 * scale }));
    const zones = zonesOf(s), tip = byId("tip");
    zones.forEach((z, i) => {
      const d = pathBetween(z.fa, z.fb);
      const w = (z.kind === "deploy" ? 3.5 + z.kw / MAX_KW * 4.5 : 3 + z.kw / MAX_KW * 3) * scale;
      const delay = "animation-delay:" + (i * 30) + "ms";
      // A dark casing under a crisp line, so a zone reads against the track.
      svg.append(node("path", { d, class: "zone z flat", style: delay, stroke: "#07090d",
                                "stroke-width": w + 3.5 * scale }));
      const hit = node("path", { d, class: "zone z flat hit", style: delay, stroke: colourOf(z),
                                 "stroke-width": w });
      hit.onmousemove = (e) => {
        tip.textContent = (z.kind === "deploy" ? "Deploy " : "Harvest ") + z.kw + " kW · "
          + Math.round(z.a) + "–" + Math.round(z.b) + " m";
        tip.hidden = false;
        tip.style.left = (e.clientX + 14) + "px";
        tip.style.top = (e.clientY + 12) + "px";
      };
      hit.onmouseleave = () => { tip.hidden = true; };
      svg.append(hit);
    });
    const [x, y] = at(0);
    const line = node("g", { transform: "translate(" + x + "," + y + ")" });
    line.append(node("rect", { x: -2 * scale, y: -11 * scale, width: 4 * scale, height: 22 * scale, fill: "#eaf0f8" }));
    const txt = node("text", { x: 9 * scale, y: -9 * scale, fill: "#8492a6", "font-size": 10 * scale,
                               "font-family": "JetBrains Mono, monospace" });
    txt.textContent = "S/F";
    line.append(txt);
    svg.append(line);
    const tone = TONES[s.strat] || "#ffa02e";
    for (let i = 9; i >= 0; i--) {
      svg.append(node("circle", { class: "trail", r: (4.5 - i * 0.3) * scale, fill: tone,
                                  opacity: 0.5 - i * 0.05 }));
    }
    svg.append(node("circle", { id: "car-dot", r: 6 * scale, fill: "#fff", stroke: tone,
                                "stroke-width": 3 * scale }));
    view.zones = zones;
  }

  /* The speed strip is the one the old cards drew; the playhead is new. */
  function drawStrip() {
    const s = current(), host = byId("strip-host");
    host.textContent = "";
    if (!s || typeof strip !== "function") return;
    const svg = strip(s);
    const h = svg.viewBox.baseVal.height || 68;
    const head = node("rect", { id: "strip-head", x: 0, y: 0, width: 2.5, height: h,
                                fill: TONES[s.strat] || "#ffa02e" });
    svg.append(head);
    host.append(svg);
  }

  function drawSide() {
    const s = current();
    if (!s) return;
    const tone = TONES[s.strat] || "#ffa02e";
    document.documentElement.style.setProperty("--tone", tone);
    document.documentElement.style.setProperty("--hue", tone);
    byId("map-title").textContent = s.label;
    byId("map-mode").textContent = ((s.modes || []).join(" / ") || "STRAT " + s.strat)
      + " · STRAT " + s.strat;
    byId("map-what").textContent = WHAT[s.strat] || "";
    byId("map-lap").textContent = s.lap;
    const delta = byId("map-delta"), d = s.delta_s || 0;
    delta.textContent = Math.abs(d) < 0.05 ? "level with your best lap"
      : Math.abs(d).toFixed(2) + " s " + (d < 0 ? "quicker" : "slower") + " than your best lap";
    delta.className = "delta " + (d <= 0.05 ? "gain" : "loss");

    const a = s.store_start / CAPACITY_KJ, b = s.store_end / CAPACITY_KJ;
    byId("map-batt").textContent = Math.round(a * 100) + "% → " + Math.round(b * 100) + "%";
    const cells = byId("map-cells");
    if (!cells.children.length) for (let i = 0; i < 20; i++) cells.append(document.createElement("i"));
    [...cells.children].forEach((c, i) => {
      const f = (i + 0.5) / 20, lo = Math.min(a, b), hi = Math.max(a, b);
      c.style.background = f <= lo ? "#5b6b80" : f <= hi ? (b >= a ? "#2fe0c2" : "#ff8a3d") : "";
    });
    const top = Math.max(1, ...view.summary.map((m) => Math.max(m.deploy_mj, m.harvest_mj)));
    byId("map-dep").textContent = s.deploy_mj.toFixed(2) + " MJ";
    byId("map-depbar").style.width = (100 * s.deploy_mj / top) + "%";
    byId("map-har").textContent = s.harvest_mj.toFixed(2) + " MJ";
    byId("map-harbar").style.width = (100 * s.harvest_mj / top) + "%";

    // Notes: what the optimizer has to say about this map.
    const notes = byId("map-notes");
    notes.textContent = "";
    for (const n of s.notes || []) {
      const li = document.createElement("li");
      li.textContent = n;
      notes.append(li);
    }
    byId("no-notes").hidden = (s.notes || []).length > 0;
    byId("note-count").textContent = (s.notes || []).length ? String(s.notes.length) : "";

    // Numbers: the figures behind the picture, and what gets written.
    const metrics = byId("map-metrics");
    metrics.textContent = "";
    const pct = (kj) => Math.round(100 * kj / CAPACITY_KJ) + "%";
    for (const [label, value] of [
      ["Battery used", s.deploy_mj.toFixed(1) + " MJ"],
      ["Battery recovered", s.harvest_mj.toFixed(1) + " MJ"],
      ["Starts at", pct(s.store_start)], ["Ends at", pct(s.store_end)],
      ["Dips to", pct(s.store_min)], ["Zones", String(s.map.length)]]) {
      const wrap = document.createElement("div"), l = document.createElement("div"),
            v = document.createElement("div");
      l.className = "metric-label"; l.textContent = label;
      v.className = "metric-value"; v.textContent = value;
      wrap.append(l, v);
      metrics.append(wrap);
    }
    const table = document.createElement("table");
    table.className = "split-table";
    const head = document.createElement("tr");
    for (const h of ["start", "deployEnd", "clipStart", "splitEnd", "deploy kW", "clip kW"]) {
      const th = document.createElement("th"); th.textContent = h; head.append(th);
    }
    table.append(head);
    for (const sp of s.map) {
      const tr = document.createElement("tr");
      for (const v of [sp.start, sp.deploy_end, sp.clip_start, sp.end, sp.deploy_kw, sp.clip_kw]) {
        const td = document.createElement("td"); td.className = "num"; td.textContent = String(v); tr.append(td);
      }
      table.append(tr);
    }
    const splits = byId("map-splits");
    splits.textContent = "";
    splits.append(table);

    for (const row of document.querySelectorAll("#cards .map")) {
      row.classList.toggle("is-on", Number(row.dataset.strat) === s.strat);
    }
  }

  function drawList() {
    const box = byId("cards");
    box.textContent = "";
    for (const s of view.summary) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "map";
      row.dataset.strat = s.strat;
      row.style.setProperty("--t", TONES[s.strat] || "#8492a6");
      const name = document.createElement("b"); name.textContent = s.label;
      const mode = document.createElement("em"); mode.textContent = (s.modes || []).join(" / ");
      const lap = document.createElement("span"); lap.textContent = s.lap;
      row.append(name, mode, lap);
      row.onclick = () => select(s.strat);
      box.append(row);
    }
  }

  function redraw() {
    drawSide();
    drawTrack();
    drawStrip();
  }

  function select(strat) {
    view.strat = strat;
    redraw();
  }

  /* --------------------------------------------------------------- the car */
  function tick(now) {
    const s = current();
    const shown = byId("screen-maps").classList.contains("show");
    if (s && view.outline && shown && view.last !== null) {
      const dt = Math.min(0.05, (now - view.last) / 1000);
      const v = Math.max(20, speedAt(s, view.dist));               // kph
      view.dist = (view.dist + (v / 3.6) * dt * PLAYBACK / (s.length_m || 5000)) % 1;
      const car = byId("car-dot");
      if (car) {
        const [x, y] = at(view.dist);
        car.setAttribute("cx", x); car.setAttribute("cy", y);
        const trail = byId("track-svg").querySelectorAll(".trail");
        trail.forEach((t, i) => {
          const [tx, ty] = at(view.dist - (i + 1) * 0.0026 + 1);
          t.setAttribute("cx", tx); t.setAttribute("cy", ty);
        });
        const head = byId("strip-head");
        if (head) head.setAttribute("x", (view.dist * 1000 - 1.25).toFixed(1));
        byId("car-kph").textContent = Math.round(v);
        const z = zoneAt(view.zones || [], view.dist), st = byId("car-state");
        if (!z) { st.textContent = "NOTHING COMMANDED"; st.style.color = "#566376"; }
        else {
          st.textContent = (z.kind === "deploy" ? "DEPLOY " : "HARVEST ") + z.kw + " kW";
          st.style.color = colourOf(z);
        }
      }
    }
    view.last = now;
    requestAnimationFrame(tick);
  }

  /* ----------------------------------------------------------------- api */
  async function loadOutline(key) {
    if (!key || key === view.key) return;
    view.key = key;
    let got = null;
    try {
      const res = await fetch("/api/track?key=" + encodeURIComponent(key));
      got = await res.json();
    } catch (err) { got = null; }
    if (view.key !== key) return;                 // a newer session asked since
    view.plain = !(got && got.points && got.points.length > 10);
    view.outline = view.plain ? plainLoop() : got;
    byId("no-outline").hidden = !view.plain;
    thumb();
    if (view.marked) sectors(view.marked.id, view.marked.zones, view.marked.colour);
    if (view.summary.length) redraw();
  }

  /* The small outline on the Session screen. */
  function thumb() {
    const svg = byId("session-track");
    if (!svg || !view.outline) return;
    frame(svg);
    svg.textContent = "";
    const scale = Math.max(view.outline.width, view.outline.height) / 200;
    const whole = wholeTrack();
    svg.append(node("path", { d: whole, class: "zone", stroke: "#3a475a", "stroke-width": 7 * scale }),
               node("path", { d: whole, class: "zone", stroke: "#ffa02e", "stroke-width": 2.4 * scale }));
  }

  /* A small outline with some stretches of the lap picked out: where a
   * rule applies, as [from, to] fractions of the lap. */
  function sectors(id, zones, colour) {
    const svg = byId(id);
    if (!svg) return;
    if (!view.outline) { view.outline = plainLoop(); view.plain = true; }
    view.marked = { id, zones, colour };
    frame(svg);
    svg.textContent = "";
    const scale = Math.max(view.outline.width, view.outline.height) / 200;
    svg.append(node("path", { d: wholeTrack(), class: "zone", stroke: "#3a475a", "stroke-width": 5 * scale }));
    for (const z of zones) {
      svg.append(node("path", { d: pathBetween(z[0], Math.min(z[1], 0.99999)), class: "zone",
                                stroke: colour, "stroke-width": 7 * scale }));
    }
  }

  function render(summary) {
    view.summary = summary || [];
    if (!view.outline) { view.outline = plainLoop(); view.plain = true; }
    if (!view.summary.some((s) => s.strat === view.strat)) {
      view.strat = view.summary.length ? view.summary[0].strat : null;
    }
    drawList();
    if (view.summary.length) redraw();
    if (!view.running) { view.running = true; requestAnimationFrame(tick); }
  }

  for (const tab of document.querySelectorAll(".stage-tab")) {
    tab.onclick = () => {
      for (const t of document.querySelectorAll(".stage-tab")) t.classList.toggle("is-on", t === tab);
      for (const p of document.querySelectorAll(".stage-pane")) {
        p.classList.toggle("is-on", p.dataset.pane === tab.dataset.pane);
      }
    };
  }

  return { render, select, loadOutline, thumb, sectors };
})();
