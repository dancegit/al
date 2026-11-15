/* al work dashboard — vanilla JS, no dependencies, no build step.
 *
 * Modules (in order): util · store · live (SSE) · router · components (graph, timeline, transcript,
 * log) · views (now, runs, run, layers, layer) · keyboard · boot.
 *
 * Safety rule for this file: agent output is untrusted. Text only ever reaches the page through
 * textContent / createTextNode / SVG attributes; nothing is parsed as HTML (a test enforces it).
 */
(() => {
  "use strict";

  // =========================================================================== util
  const SVG_NS = "http://www.w3.org/2000/svg";

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    applyAttrs(el, attrs);
    appendAll(el, children);
    return el;
  }

  function s(tag, attrs, ...children) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "text") el.textContent = String(value);
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, String(value));
    }
    appendAll(el, children);
    return el;
  }

  function applyAttrs(el, attrs) {
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "text") el.textContent = String(value);
      else if (key === "class") el.className = value;
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else if (key === "href") el.setAttribute("href", safeHref(value));
      else el.setAttribute(key, value === true ? "" : String(value));
    }
  }

  function appendAll(el, children) {
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }

  function safeHref(value) {
    const text = String(value);
    if (text.startsWith("#") || text.startsWith("/")) return text;
    if (/^https?:\/\//i.test(text)) return text;
    return "#";
  }

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
  function replace(el, ...children) { clear(el); appendAll(el, children); return el; }
  const enc = encodeURIComponent;

  const fmt = {
    dur(sec) {
      if (sec === null || sec === undefined || !isFinite(sec)) return "—";
      sec = Math.max(0, sec);
      if (sec < 10) return `${sec.toFixed(1)}s`;
      if (sec < 60) return `${Math.round(sec)}s`;
      const m = Math.floor(sec / 60), r = Math.floor(sec % 60);
      if (m < 60) return `${m}m ${String(r).padStart(2, "0")}s`;
      return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
    },
    cost(c) {
      if (c === null || c === undefined) return "—";
      if (c === 0) return "$0";
      return c < 0.1 ? `$${c.toFixed(3)}` : `$${c.toFixed(2)}`;
    },
    tokens(n) {
      if (!n) return "0";
      if (n < 1000) return String(n);
      if (n < 1e6) return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`;
      return `${(n / 1e6).toFixed(1)}M`;
    },
    bytes(n) {
      if (n === null || n === undefined) return "";
      if (n < 1024) return `${n} B`;
      if (n < 1048576) return `${(n / 1024).toFixed(1)} kB`;
      return `${(n / 1048576).toFixed(1)} MB`;
    },
    time(epoch) {
      if (!epoch) return "—";
      const d = new Date(epoch * 1000);
      const pad = (v) => String(v).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    },
    clock(epoch) {
      if (!epoch) return "";
      const d = new Date(epoch * 1000);
      return d.toTimeString().slice(0, 8);
    },
    ago(epoch) {
      if (!epoch) return "—";
      const sec = Date.now() / 1000 - epoch;
      if (sec < 60) return "just now";
      if (sec < 3600) return `${Math.floor(sec / 60)} min ago`;
      if (sec < 86400) return `${Math.floor(sec / 3600)} h ago`;
      return `${Math.floor(sec / 86400)} d ago`;
    },
    pct(v) { return v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`; },
  };

  const now = () => Date.now() / 1000;

  function pill(status, label) {
    return h("span", { class: `pill s-${status || "unknown"}`, text: label || status || "unknown" });
  }

  function progress(steps) {
    return h("span", { class: "progress", title: steps.map((st) => `${st.name}: ${st.status}`).join("\n") },
      steps.map((st) => h("i", { class: `s-${st.status}` })));
  }

  function outcomeBadge(run) {
    const o = run.outcome || {};
    if (o.ship === "merge" && o.shipped) return h("span", { class: "badge good", text: "merged" });
    if (o.ship === "deploy" && o.shipped) return h("span", { class: "badge good", text: "deployed" });
    if (o.ship === "handoff") return h("span", { class: "badge warn", text: "needs human" });
    if (o.ship === "pr") return h("span", { class: "badge info", text: "PR" });
    if (o.ship) return h("span", { class: "badge", text: o.ship });
    return null;
  }

  async function api(path) {
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error(`${response.status} ${path}`);
    return response.json();
  }

  function debounce(fn, ms) {
    let timer = null;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
  }

  function throttle(fn, ms) {
    let last = 0, timer = null;
    return (...args) => {
      const wait = ms - (Date.now() - last);
      if (wait <= 0) { last = Date.now(); fn(...args); return; }
      clearTimeout(timer);
      timer = setTimeout(() => { last = Date.now(); fn(...args); }, wait);
    };
  }

  /** Put `text` into `el`, wrapping case-insensitive matches of `query` in <mark>, as text nodes only. */
  function highlight(el, text, query) {
    clear(el);
    text = text || "";
    if (!query) { el.textContent = text; return 0; }
    const lower = text.toLowerCase(), q = query.toLowerCase();
    let from = 0, count = 0;
    for (;;) {
      const at = lower.indexOf(q, from);
      if (at < 0) break;
      if (at > from) el.appendChild(document.createTextNode(text.slice(from, at)));
      el.appendChild(h("mark", { text: text.slice(at, at + q.length) }));
      from = at + q.length;
      count += 1;
    }
    if (from < text.length) el.appendChild(document.createTextNode(text.slice(from)));
    return count;
  }

  // =========================================================================== store
  const store = {
    layers: [], runs: new Map(), slots: {}, containers: {}, inContainer: false, loaded: false,
    listeners: new Set(),
    on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); },
    emit(kind, data) { for (const fn of this.listeners) fn(kind, data); },
    runList() { return [...this.runs.values()]; },
    layer(key) { return this.layers.find((l) => l.key === key); },
  };

  function decorate(run) {
    // a candidate of a fan-out knows its parent
    return run;
  }

  // =========================================================================== live (SSE)
  const live = {
    source: null,
    connect() {
      const conn = document.getElementById("conn");
      const source = new EventSource("/api/events");
      this.source = source;
      const status = (cls, text) => { conn.className = `conn ${cls}`; conn.textContent = text; };
      source.addEventListener("open", () => status("conn-live", "live"));
      source.addEventListener("error", () => status("conn-wait", "reconnecting"));
      source.addEventListener("snapshot", (event) => {
        const snap = JSON.parse(event.data);
        store.layers = snap.layers;
        store.runs = new Map(snap.runs.map((run) => [run.key, decorate(run)]));
        store.slots = snap.slots || {};
        store.containers = snap.containers || {};
        store.inContainer = !!snap.in_container;
        store.loaded = true;
        status("conn-live", "live");
        store.emit("snapshot");
      });
      source.addEventListener("run", (event) => {
        const run = JSON.parse(event.data);
        store.runs.set(run.key, decorate(run));
        store.emit("run", run);
      });
      source.addEventListener("run-removed", (event) => {
        const { key } = JSON.parse(event.data);
        store.runs.delete(key);
        store.emit("run-removed", key);
      });
      source.addEventListener("slots", (event) => {
        const data = JSON.parse(event.data);
        store.slots[data.layer] = data;
        store.emit("slots", data);
      });
      source.addEventListener("layers", (event) => {
        store.layers = JSON.parse(event.data);
        store.emit("layers");
      });
      source.addEventListener("containers", (event) => {
        store.containers = JSON.parse(event.data);
        store.emit("containers");
      });
    },
  };

  // =========================================================================== router
  function parseRoute() {
    const raw = location.hash.replace(/^#\/?/, "");
    const [path, query] = raw.split("?");
    const parts = (path || "now").split("/").filter(Boolean).map(decodeURIComponent);
    const params = Object.fromEntries(new URLSearchParams(query || ""));
    return { parts, params, view: parts[0] || "now" };
  }

  function go(hash, replaceState = false) {
    if (replaceState) history.replaceState(null, "", hash);
    else location.hash = hash;
    if (replaceState) router();
  }

  function routeHash(parts, params) {
    const path = parts.map((part) => enc(part)).join("/");
    const query = new URLSearchParams(Object.entries(params || {}).filter(([, v]) => v !== "" && v !== null && v !== undefined && v !== false)).toString();
    return `#/${path}${query ? `?${query}` : ""}`;
  }

  let current = null;

  function router() {
    const route = parseRoute();
    for (const link of document.querySelectorAll(".views a")) {
      link.classList.toggle("active", link.dataset.view === route.view || (route.view === "run" && link.dataset.view === "runs") || (route.view === "layer" && link.dataset.view === "layers"));
    }
    if (current && current.name === route.view && current.accepts && current.accepts(route)) {
      current.route(route);
      return;
    }
    if (current && current.destroy) current.destroy();
    const main = clear(document.getElementById("main"));
    const crumbs = clear(document.getElementById("crumbs"));
    const make = { now: NowView, runs: RunsView, run: RunView, layers: LayersView, layer: LayerView }[route.view] || NowView;
    current = make(main, crumbs, route);
    current.name = route.view in { now: 1, runs: 1, run: 1, layers: 1, layer: 1 } ? route.view : "now";
    main.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }

  // =========================================================================== components: workflow graph
  const GLYPH = { passed: "✓", failed: "✗", running: "●", pending: "○", skipped: "–", done: "•", unknown: "?" };
  let graphSerial = 0;

  function renderGraph(graph, opts = {}) {
    graphSerial += 1;
    const marker = `arrow-${graphSerial}`;
    const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
    const svg = s("svg", { viewBox: `0 0 ${graph.width} ${graph.height}`, class: opts.static ? "g-static" : "",
      role: "img", "aria-label": "workflow graph" });
    svg.style.minWidth = `${Math.round(graph.width * 0.72)}px`;
    svg.appendChild(s("defs", {}, s("marker", { id: marker, viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
      s("path", { d: "M0,0 L8,4 L0,8 z", class: "g-arrow" }))));
    const live = (st) => ["passed", "failed", "running", "done"].includes(st);
    const edgesLayer = s("g", {});
    const labelsLayer = s("g", {});
    for (const edge of graph.edges) {
      const a = nodes.get(edge.from), b = nodes.get(edge.to);
      if (!a || !b) continue;
      const taken = !opts.static && live(a.status) && live(b.status);
      let d, lx, ly;
      if (edge.kind === "resolve") {
        const x1 = a.x + a.w * 0.28, x2 = a.x + a.w * 0.72, y = a.y;
        d = `M${x1},${y} C${x1},${y - 30} ${x2},${y - 30} ${x2},${y}`;
        lx = a.x + a.w / 2; ly = y - 26;
      } else if (edge.kind === "patch") {
        const x1 = a.x + a.w / 2, x2 = b.x + b.w / 2, y1 = a.y + a.h, y2 = b.y + b.h;
        const depth = Math.max(y1, y2) + 38;
        d = `M${x1},${y1} C${x1},${depth} ${x2},${depth} ${x2},${y2}`;
        lx = (x1 + x2) / 2; ly = depth - 4;
      } else {
        const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2, dx = (x2 - x1) / 2;
        d = `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
        lx = x1 + dx; ly = (y1 + y2) / 2 - 4;
      }
      edgesLayer.appendChild(s("path", { d, class: `g-edge ${edge.kind}${taken ? " taken" : ""}`, "marker-end": `url(#${marker})` }));
      if (edge.label && (edge.kind === "resolve" || edge.kind === "patch")) {
        labelsLayer.appendChild(s("text", { x: lx, y: ly, "text-anchor": "middle", class: `g-edge-label ${edge.kind}`,
          text: edge.kind === "resolve" ? `↻ ${edge.label}` : `↺ ${edge.label}` }));
      } else if (edge.label && edge.kind === "branch") {
        labelsLayer.appendChild(s("text", { x: b.x - 6, y: b.y + b.h / 2 - 5, "text-anchor": "end", class: "g-edge-label", text: edge.label }));
      }
    }
    svg.appendChild(edgesLayer);
    svg.appendChild(labelsLayer);
    for (const node of graph.nodes) {
      const status = opts.static ? "pending" : (node.status || "pending");
      const classes = ["g-node", `st-${status}`];
      if (opts.selected && opts.selected === node.step && node.type !== "candidate") classes.push("selected");
      if (node.picked) classes.push("picked");
      const meta = [];
      if (!opts.static) {
        if (node.duration !== null && node.duration !== undefined) meta.push(fmt.dur(node.duration));
        if (node.cost) meta.push(fmt.cost(node.cost));
        if (node.type === "check" && node.attempts > 1) meta.push(`${node.attempts} tries`);
        if (node.resolves) meta.push(`${node.resolves} fix`);
      } else if (node.when && node.when !== "always") {
        meta.push(`when ${node.when.replace("class-", "")}`);
      }
      const title = [`${node.label} (${node.type})`, node.sub, status !== "pending" ? status : "", node.detail || ""].filter(Boolean).join("\n");
      const g = s("g", { class: classes.join(" "), tabindex: opts.onNode ? 0 : null, "data-step": node.step, "data-node": node.id,
        onclick: opts.onNode ? () => opts.onNode(node) : null,
        onkeydown: opts.onNode ? (event) => { if (event.key === "Enter") opts.onNode(node); } : null },
        s("title", { text: title }),
        s("rect", { class: "box", x: node.x, y: node.y, width: node.w, height: node.h, rx: 6 }),
        s("rect", { class: `type t-${node.type}`, x: node.x, y: node.y + 6, width: 3, height: node.h - 12, rx: 1.5 }),
        s("text", { class: "label", x: node.x + 11, y: node.y + 18, text: clip(node.label, 17) }),
        s("text", { class: "sub", x: node.x + 11, y: node.y + 31, text: clip(node.sub, 22) }),
        s("text", { class: "meta", x: node.x + 11, y: node.y + 45, text: meta.join(" · ") }),
        opts.static ? null : s("text", { class: "icon", x: node.x + node.w - 9, y: node.y + 17, "text-anchor": "end", text: GLYPH[status] || "" }));
      svg.appendChild(g);
    }
    return svg;
  }

  function clip(text, n) {
    text = String(text || "");
    return text.length > n ? `${text.slice(0, n - 1)}…` : text;
  }

  function graphLegend() {
    const item = (cls, label) => h("span", {}, h("i", { class: cls }), label);
    const legend = h("div", { class: "legend" });
    const colours = { agent: "var(--agent)", check: "var(--check)", review: "var(--review)", ship: "var(--ship)", "fan-out": "var(--fan)" };
    for (const [name, colour] of Object.entries(colours)) {
      const i = h("i");
      i.style.background = colour;
      legend.appendChild(h("span", {}, i, name));
    }
    legend.appendChild(h("span", { class: "faint", text: "↻ resolve loop · ↺ patch loop · dashed: by class" }));
    return legend;
  }

  // =========================================================================== components: timeline
  function niceStep(span) {
    const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
    return steps.find((v) => span / v <= 8) || 14400;
  }

  function renderTimeline(timeline, opts = {}) {
    const bars = timeline.bars.filter((bar) => bar.start);
    const t0 = timeline.start || Math.min(...bars.map((b) => b.start));
    const tEnd = Math.max(timeline.end || 0, ...bars.map((b) => b.end || now()), t0 + 1);
    const W = 1000, labelW = 190, right = 64, rowH = 17, top = 20;
    const span = tEnd - t0, k = (W - labelW - right) / span;
    const height = top + bars.length * rowH + 6;
    const svg = s("svg", { viewBox: `0 0 ${W} ${height}`, role: "img", "aria-label": "timeline" });
    const step = niceStep(span);
    for (let t = 0; t <= span + 0.001; t += step) {
      const x = labelW + t * k;
      svg.appendChild(s("line", { x1: x, x2: x, y1: top - 4, y2: height, class: "tl-axis" }));
      svg.appendChild(s("text", { x, y: 11, "text-anchor": "middle", class: "tl-tick", text: fmt.dur(t) }));
    }
    bars.forEach((bar, index) => {
      const y = top + index * rowH;
      const end = bar.end || now();
      const x = labelW + (bar.start - t0) * k, w = Math.max(2, (end - bar.start) * k);
      const duration = end - bar.start;
      const label = bar.level === 0 ? bar.label : `  ${bar.label}`;
      const tip = `${bar.label} · ${bar.kind}${bar.role && bar.role !== "main" ? ` (${bar.role})` : ""} · ${bar.status}\n${fmt.dur(duration)}${bar.cost ? ` · ${fmt.cost(bar.cost)}` : ""}${bar.exit !== undefined && bar.exit !== null ? ` · exit ${bar.exit}` : ""}`;
      const row = s("g", { class: "tl-row", onclick: opts.onBar ? () => opts.onBar(bar) : null },
        s("title", { text: tip }),
        s("rect", { class: "tl-bg", x: 0, y, width: W, height: rowH }),
        s("text", { class: `tl-label l${bar.level}`, x: bar.level ? 14 : 4, y: y + 12, text: clip(label, 28) }),
        bar.status === "skipped"
          ? s("text", { class: "tl-tick", x: x + 3, y: y + 12, text: "skipped" })
          : s("rect", { class: `tl-bar k-${bar.kind} l${bar.level} st-${bar.status}`, x, y: y + 3, width: w, height: rowH - 6, rx: 2 }),
        bar.level === 1 || bar.kind === "check" ? s("text", { class: "tl-tick", x: Math.min(x + w + 4, W - right + 4), y: y + 12,
          text: `${fmt.dur(duration)}${bar.cost ? ` ${fmt.cost(bar.cost)}` : ""}` }) : null);
      svg.appendChild(row);
    });
    return svg;
  }

  // =========================================================================== components: transcript
  const KIND_LABEL = { init: "start", text: "says", thinking: "thinks", result: "result", final: "result", task: "task",
    meta: "info", user: "user", plain: "output", corrupt: "corrupt", mock: "mock" };
  const CATEGORY = { text: "say", user: "say", plain: "say", mock: "say", tool: "tools", result: "tools", thinking: "think",
    init: "system", meta: "system", task: "system", final: "final", corrupt: "final" };

  class TranscriptView {
    constructor(container, opts) {
      this.opts = opts;
      this.items = [];
      this.byTool = new Map();
      this.selected = -1;
      this.tools = new Set();
      this.filters = { say: true, tools: true, think: true, system: false, errors: opts.params.errors === "1",
        tool: opts.params.tool || "", q: opts.params.q || "" };
      for (const name of (opts.params.hide || "").split(",").filter(Boolean)) this.filters[name] = false;
      for (const name of (opts.params.show || "").split(",").filter(Boolean)) this.filters[name] = true;
      this.follow = opts.running;
      this.build(container);
      this.connect();
    }

    build(container) {
      const chip = (key, label) => {
        const button = h("button", { class: `toggle${this.filters[key] ? " on" : ""}`, type: "button", text: label, "data-filter": key,
          onclick: () => { this.filters[key] = !this.filters[key]; button.classList.toggle("on", this.filters[key]); this.applyFilters(); this.saveParams(); } });
        return button;
      };
      this.toolSelect = h("select", { "aria-label": "tool", onchange: () => { this.filters.tool = this.toolSelect.value; this.applyFilters(); this.saveParams(); } },
        h("option", { value: "", text: "all tools" }));
      this.search = h("input", { type: "search", placeholder: "search transcript  /", value: this.filters.q, "aria-label": "search transcript",
        oninput: debounce(() => { this.filters.q = this.search.value.trim(); this.applyFilters(); this.saveParams(); }, 150) });
      this.followButton = h("button", { class: `toggle${this.follow ? " on" : ""}`, type: "button", text: "follow  f", "data-role": "follow",
        onclick: () => this.toggleFollow() });
      this.count = h("span", { class: "count" });
      this.cost = h("span", { class: "count" });
      const download = h("a", { href: `${this.opts.rawUrl}?download=1`, class: "toggle", text: "raw .jsonl", title: "download the raw transcript" });
      this.toolbar = h("div", { class: "toolbar" }, chip("say", "says"), chip("tools", "tools"), chip("think", "thinking"), chip("system", "system"),
        chip("errors", "errors only"), this.toolSelect, this.search, this.followButton, download, this.count);
      this.feed = h("div", { class: "feed", tabindex: 0, "aria-label": "transcript" });
      this.feed.addEventListener("scroll", () => {
        const atBottom = this.feed.scrollHeight - this.feed.scrollTop - this.feed.clientHeight < 40;
        if (!atBottom && this.follow && !this.autoScrolling) this.setFollow(false);
      });
      this.empty = h("div", { class: "empty", text: "waiting for the agent's first event…" });
      this.feed.appendChild(this.empty);
      container.appendChild(this.toolbar);
      container.appendChild(this.feed);
    }

    saveParams() {
      const hide = ["say", "tools", "think"].filter((k) => !this.filters[k]);
      const show = this.filters.system ? ["system"] : [];
      this.opts.onParams({ q: this.filters.q, tool: this.filters.tool, errors: this.filters.errors ? "1" : "",
        hide: hide.join(","), show: show.join(",") });
    }

    connect() {
      const o = this.opts;
      this.source = new EventSource(`/api/tail?layer=${enc(o.layer)}&run=${enc(o.run)}&dir=${enc(o.dir)}&what=transcript`);
      this.source.addEventListener("rows", (event) => this.addRows(JSON.parse(event.data)));
      this.source.addEventListener("reset", () => this.reset());
      this.source.addEventListener("meta", (event) => { this.meta = JSON.parse(event.data); this.updateCount(); });
    }

    destroy() { if (this.source) this.source.close(); }

    reset() {
      this.items = []; this.byTool.clear(); this.selected = -1;
      clear(this.feed).appendChild(this.empty);
      this.updateCount();
    }

    addRows(rows) {
      if (this.empty.parentNode) this.empty.remove();
      const fragment = document.createDocumentFragment();
      for (const row of rows) {
        if (row.k === "result" && row.tid && this.byTool.has(row.tid)) {
          this.byTool.get(row.tid).attach(row);
          continue;
        }
        const item = this.makeItem(row);
        this.items.push(item);
        if (row.k === "tool" && row.tid) this.byTool.set(row.tid, item);
        if (row.tool && row.k === "tool" && !this.tools.has(row.tool)) {
          this.tools.add(row.tool);
          this.toolSelect.appendChild(h("option", { value: row.tool, text: row.tool, selected: this.filters.tool === row.tool }));
        }
        item.el.classList.toggle("hidden", !this.visible(item));
        fragment.appendChild(item.el);
      }
      this.feed.appendChild(fragment);
      if (this.filters.q) this.applyHighlight();
      this.updateCount();
      if (this.follow) this.scrollBottom();
    }

    makeItem(row) {
      const view = this;
      const body = h("div", { class: "body" });
      const el = h("div", { class: `ev k-${row.k}${row.err ? " err" : ""}`, "data-n": row.n },
        h("span", { class: "when", text: this.when(row) }),
        h("span", { class: "kind", text: row.k === "tool" ? row.tool : (KIND_LABEL[row.k] || row.k) }),
        body,
        h("button", { class: "raw-button", type: "button", title: "show the raw JSON of this event", text: "{}", onclick: () => item.toggleRaw() }));
      const searchable = [];
      const add = (node, text) => { searchable.push({ node, text }); return node; };
      const item = { row, el, body, searchable, results: [],
        text() { return [row.title, row.text, row.cmd, row.note, (row.diff || []).join("\n"), ...this.results.map((r) => r.text + (r.stderr || ""))].join("\n"); },
        attach(result) { this.results.push(result); if (result.err) el.classList.add("err"); body.appendChild(view.resultBlock(result, this)); },
        toggleRaw() {
          if (this.raw) { this.raw.remove(); this.raw = null; return; }
          this.raw = h("pre", { class: "rawjson", text: "loading…" });
          body.appendChild(this.raw);
          const o = view.opts;
          fetch(`/api/rawline?layer=${enc(o.layer)}&run=${enc(o.run)}&dir=${enc(o.dir)}&line=${row.n}`, { credentials: "same-origin" })
            .then((r) => r.text()).then((text) => { if (this.raw) this.raw.textContent = text; });
        },
        toggleOpen() { const block = body.querySelector(".result"); if (block) block.classList.toggle("open"); } };
      switch (row.k) {
        case "thinking":
          body.appendChild(h("span", { text: `thinking${row.chars ? ` (${row.chars} characters)` : ""}${row.ms ? ` · ${fmt.dur(row.ms / 1000)}` : ""}` }));
          break;
        case "tool":
          body.appendChild(add(h("div", { class: "title" }), row.title));
          if (row.note) body.appendChild(add(h("div", { class: "note" }), row.note));
          if (row.diff && row.diff.length) body.appendChild(this.diffBlock(row.diff));
          break;
        case "result":
          body.appendChild(this.resultBlock(row, item, true));
          break;
        case "final":
          body.appendChild(add(h("div", { class: "title" }), row.title));
          body.appendChild(h("div", { class: "final-figures" },
            h("span", {}, "cost ", h("b", { text: fmt.cost(row.cost) })), h("span", {}, "turns ", h("b", { text: row.turns ?? "—" })),
            h("span", {}, "time ", h("b", { text: fmt.dur((row.duration_ms || 0) / 1000) })),
            row.tokens ? h("span", {}, "tokens ", h("b", { text: `${fmt.tokens(row.tokens.in)} in · ${fmt.tokens(row.tokens.out)} out · ${fmt.tokens(row.tokens.cache_read)} cache read · ${fmt.tokens(row.tokens.cache_write)} cache write` })) : null));
          if (row.text) body.appendChild(add(h("div", { class: "text" }), row.text));
          break;
        case "text": case "user": case "plain": case "mock":
          if (row.title) body.appendChild(add(h("div", { class: "title" }), row.title));
          body.appendChild(add(h("div", { class: "text" }), row.text));
          break;
        default:
          body.appendChild(add(h("div", { class: "title" }), row.title || row.k));
          if (row.text) body.appendChild(add(h("div", { class: "text" }), row.text));
      }
      for (const entry of searchable) entry.node.textContent = entry.text || "";
      return item;
    }

    when(row) {
      if (row.ts && this.opts.start) return `+${fmt.dur(row.ts - this.opts.start)}`;
      return `#${row.n}`;
    }

    diffBlock(lines) {
      const block = h("div", { class: "diff" });
      const render = (list) => {
        clear(block);
        for (const line of list) {
          const cls = line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
          block.appendChild(h("div", { class: cls, text: line }));
        }
      };
      if (lines.length > 14) {
        render(lines.slice(0, 12));
        block.appendChild(h("button", { class: "more-button", type: "button", text: `show all ${lines.length} lines`, onclick: () => render(lines) }));
      } else {
        render(lines);
      }
      return block;
    }

    resultBlock(result, item, standalone) {
      const pre = h("pre");
      const searchableText = (result.text || "") + (result.stderr ? `\n--- stderr ---\n${result.stderr}` : "");
      item.searchable.push({ node: pre, text: searchableText });
      pre.textContent = searchableText || "(empty)";
      const label = `${standalone && result.tool ? `${result.tool} ` : ""}result · ${fmt.bytes(result.size)} · ${result.lines} line${result.lines === 1 ? "" : "s"}${result.err ? " · error" : ""}`;
      const block = h("div", { class: `result${result.err ? " err" : ""}` },
        h("div", { class: "result-head", onclick: () => block.classList.toggle("open") }, h("span", { class: "caret", text: "▸" }), h("span", { text: label }),
          result.more ? h("span", { class: "faint", text: "(preview; { } shows all)" }) : null),
        pre);
      if (result.diff && result.diff.length && !(item.row.diff && item.row.diff.length)) block.appendChild(this.diffBlock(result.diff));
      if (result.err) block.classList.add("open");
      return block;
    }

    visible(item) {
      const f = this.filters, row = item.row;
      const category = CATEGORY[row.k] || "system";
      if (category !== "final" && !f[category]) return false;
      if (f.errors && !(row.err || item.results.some((r) => r.err) || row.k === "corrupt")) return false;
      if (f.tool && !(row.k === "tool" && row.tool === f.tool)) return false;
      if (f.q && !item.text().toLowerCase().includes(f.q.toLowerCase())) return false;
      return true;
    }

    applyFilters() {
      for (const item of this.items) item.el.classList.toggle("hidden", !this.visible(item));
      this.applyHighlight();
      this.updateCount();
      if (this.follow) this.scrollBottom();
    }

    applyHighlight() {
      let total = 0;
      for (const item of this.items) {
        if (item.el.classList.contains("hidden") && !item.highlighted) continue;
        for (const entry of item.searchable) total += highlight(entry.node, entry.text, this.filters.q);
        item.highlighted = !!this.filters.q;
        if (this.filters.q && item.searchable.some((e) => e.node.tagName === "PRE" && e.node.querySelector("mark"))) {
          const block = item.body.querySelector(".result");
          if (block) block.classList.add("open");
        }
      }
      this.matches = total;
    }

    updateCount() {
      const shown = this.items.filter((i) => !i.el.classList.contains("hidden")).length;
      const m = this.meta || {};
      const parts = [`${shown} / ${this.items.length} events`];
      if (this.filters.q) parts.push(`${this.matches || 0} matches`);
      if (m.cost !== null && m.cost !== undefined) parts.push(fmt.cost(m.cost));
      if (m.turns) parts.push(`${m.turns} turns`);
      this.count.textContent = parts.join(" · ");
    }

    scrollBottom() {
      this.autoScrolling = true;
      this.feed.scrollTop = this.feed.scrollHeight;
      requestAnimationFrame(() => { this.autoScrolling = false; });
    }

    setFollow(on) {
      this.follow = on;
      this.followButton.classList.toggle("on", on);
      if (on) this.scrollBottom();
    }

    toggleFollow() { this.setFollow(!this.follow); }

    move(delta) {
      const visible = this.items.filter((i) => !i.el.classList.contains("hidden"));
      if (!visible.length) return;
      let index = visible.findIndex((i) => i.el.classList.contains("selected"));
      if (index >= 0) visible[index].el.classList.remove("selected");
      index = Math.max(0, Math.min(visible.length - 1, index + delta));
      visible[index].el.classList.add("selected");
      visible[index].el.scrollIntoView({ block: "nearest" });
      if (this.follow) this.setFollow(false);
    }

    open() {
      const item = this.items.find((i) => i.el.classList.contains("selected"));
      if (item) item.toggleOpen();
    }

    focusSearch() { this.search.focus(); this.search.select(); }
  }

  // =========================================================================== components: logs (ANSI segments)
  class LogView {
    constructor(container, opts) {
      this.opts = opts;
      this.follow = opts.running !== false;
      this.pre = h("div", { class: "log", tabindex: 0, "aria-label": opts.label || "log" });
      this.pre.addEventListener("scroll", () => {
        const atBottom = this.pre.scrollHeight - this.pre.scrollTop - this.pre.clientHeight < 40;
        if (!atBottom && this.follow && !this.autoScrolling) this.follow = false;
        if (atBottom) this.follow = true;
      });
      container.appendChild(this.pre);
      const o = opts;
      const what = o.what === "runlog" ? "what=runlog" : `what=check&file=${enc(o.file)}`;
      this.source = new EventSource(`/api/tail?layer=${enc(o.layer)}&run=${enc(o.run)}&${what}`);
      this.source.addEventListener("text", (event) => this.add(JSON.parse(event.data).segments));
      this.source.addEventListener("reset", () => clear(this.pre));
    }

    add(segments) {
      const fragment = document.createDocumentFragment();
      for (const [text, fg, bg, flags] of segments) {
        if (!fg && !bg && !flags) { fragment.appendChild(document.createTextNode(text)); continue; }
        const span = h("span", { class: (flags || "").split("").join(" "), text });
        if (fg) span.style.color = fg;
        if (bg) span.style.backgroundColor = bg;
        fragment.appendChild(span);
      }
      this.pre.appendChild(fragment);
      if (this.follow) { this.autoScrolling = true; this.pre.scrollTop = this.pre.scrollHeight; requestAnimationFrame(() => { this.autoScrolling = false; }); }
    }

    toggleFollow() { this.follow = !this.follow; if (this.follow) this.pre.scrollTop = this.pre.scrollHeight; }
    destroy() { this.source.close(); }
  }

  // =========================================================================== views: Now
  function NowView(main, crumbs) {
    crumbs.textContent = "what the agents are doing now";
    const strip = h("div", { class: "strip" });
    const grid = h("div", { class: "now-grid" });
    const pools = h("div", { class: "pools" });
    const recent = h("div", { class: "table-wrap" });
    const containerNote = h("span", { class: "faint" });
    main.append(strip,
      h("div", { class: "card-head" }, h("h2", { text: "In flight" }), h("span", { class: "spacer" }), containerNote), grid,
      h("div", { class: "card-head", style: null }, h("h2", { text: "Slots and ports" })), pools,
      h("div", { class: "card-head" }, h("h2", { text: "Recently finished" })), recent);
    grid.style.marginBottom = "18px";
    pools.style.marginBottom = "18px";
    const cards = new Map();
    let selected = -1;

    function stat(label, value, small) {
      return h("div", { class: "stat" }, h("div", { class: "label", text: label }), h("div", { class: "value" }, value, small ? h("small", { text: ` ${small}` }) : null));
    }

    function card(run) {
      const el = cards.get(run.key) || h("div", { class: "run-card", tabindex: 0, onclick: () => go(`#/run/${enc(run.layer)}/${enc(run.id)}`) });
      el.className = `run-card${run.waiting ? " waiting" : ""}${el.classList.contains("selected") ? " selected" : ""}`;
      el.dataset.key = run.key;
      const current = run.current || {};
      const container = run.container;
      replace(el,
        h("div", { class: "head" }, h("span", { class: `dot s-${run.waiting ? "waiting" : "running"}` }),
          h("span", { class: "where", text: `${run.layer} · ${run.adw}` }), h("span", { class: "mono faint", text: run.id }),
          run.dry_run ? h("span", { class: "badge", text: "dry run" }) : null, h("span", { class: "spacer" }), progress(run.steps)),
        h("div", { class: "title", text: run.title || "(no request text)" }),
        h("div", { class: "doing" }, h("span", { class: "step", text: current.step ? `▶ ${current.step}` : "▶" }),
          h("span", { class: "action", text: run.waiting ? `waiting for a free ${run.waiting} slot` : (current.action || "working") })),
        h("div", { class: "facts" },
          h("span", {}, "elapsed ", h("b", { class: "num", "data-since": run.started || "", text: fmt.dur(now() - (run.started || now())) })),
          h("span", {}, "cost so far ", h("b", { class: "num", text: fmt.cost(run.cost) }), run.cost_partial ? h("span", { class: "faint", text: " +running step" }) : null),
          current.since ? h("span", {}, "step ", h("b", { class: "num", "data-since": current.since, text: fmt.dur(now() - current.since) })) : null,
          run.waiting ? h("span", { class: "badge warn", text: `waiting: ${run.waiting} slot` }) : null,
          run.ports && run.ports.length ? h("span", { class: "mono faint", text: `ports ${run.ports.join(",")}` }) : null),
        h("div", { class: "container", title: "container" }, container
          ? `⬡ ${container.name} · ${container.state || "?"}${container.cpu ? ` · CPU ${container.cpu}` : ""}${container.mem ? ` · ${container.mem}` : ""}${container.source === "run.log" ? " (from run.log)" : ""}`
          : "⬡ no container recorded"));
      cards.set(run.key, el);
      return el;
    }

    function render() {
      const runs = store.runList();
      const running = runs.filter((r) => r.status === "running").sort((a, b) => (a.started || 0) - (b.started || 0));
      const day = runs.filter((r) => r.started && r.started > now() - 86400);
      const finishedDay = day.filter((r) => r.status === "passed" || r.status === "failed");
      const costDay = day.reduce((sum, r) => sum + (r.cost || 0), 0);
      replace(strip,
        stat("running", running.length, running.filter((r) => r.waiting).length ? `${running.filter((r) => r.waiting).length} waiting` : ""),
        stat("runs, 24 h", day.length, finishedDay.length ? `${fmt.pct(finishedDay.filter((r) => r.status === "passed").length / finishedDay.length)} passed` : ""),
        stat("cost, 24 h", fmt.cost(costDay)),
        stat("layers watched", store.layers.length),
        stat("all runs", runs.length));
      const keep = new Set(running.map((r) => r.key));
      for (const key of [...cards.keys()]) if (!keep.has(key)) { cards.get(key).remove(); cards.delete(key); }
      if (!running.length) {
        replace(grid, h("div", { class: "empty", text: store.loaded ? "Nothing is running right now. Start a workflow and it appears here within a second." : "connecting…" }));
      } else {
        const empty = grid.querySelector(".empty");
        if (empty) empty.remove();
        running.forEach((run, index) => {
          const el = card(run);
          if (grid.children[index] !== el) grid.insertBefore(el, grid.children[index] || null);
        });
      }
      const info = store.containers || {};
      containerNote.textContent = info.available ? `containers: ${info.note}` : (info.note || "");
      renderPools();
      renderRecent(runs);
    }

    function renderPools() {
      clear(pools);
      for (const layer of store.layers) {
        const data = store.slots[layer.key];
        if (!data) continue;
        const rows = Object.entries(data.pools).map(([pool, p]) => {
          const meter = h("span", { class: "meter" });
          for (let i = 0; i < p.total; i += 1) meter.appendChild(h("i", { class: i < p.used ? "on" : "" }));
          return h("div", { class: "pool-row", title: p.holders.map((x) => x.who).join("\n") || "free" },
            h("span", { text: pool }), meter, h("span", { class: "num", text: `${p.used}/${p.total}` }));
        });
        const ports = (data.ports || []).filter((p) => p.alive !== false).map((p) => `${p.run}: ${p.ports.join(",")}`).join("  ");
        pools.appendChild(h("div", { class: "pool" },
          h("div", { class: "name" }, h("a", { href: `#/layer/${enc(layer.key)}`, text: layer.key }),
            h("span", { class: "faint", text: data.source === "locks" ? "from lock files" : "inferred from run state" }),
            data.waiting && data.waiting.length ? h("span", { class: "badge warn", text: `${data.waiting.length} waiting` }) : null),
          rows, h("div", { class: "ports", text: ports ? `ports ${ports}` : "no leased ports" })));
      }
      if (!pools.children.length) pools.appendChild(h("div", { class: "empty", text: "no layers yet" }));
    }

    function renderRecent(runs) {
      const done = runs.filter((r) => r.status !== "running").sort((a, b) => (b.ended || b.started || 0) - (a.ended || a.started || 0)).slice(0, 8);
      const body = h("tbody");
      for (const run of done) {
        body.appendChild(h("tr", { onclick: () => go(`#/run/${enc(run.layer)}/${enc(run.id)}`) },
          h("td", {}, pill(run.status)), h("td", { class: "num dim", text: fmt.ago(run.ended || run.started) }),
          h("td", { text: `${run.layer} · ${run.adw}` }), h("td", { class: "title-cell", text: run.title }),
          h("td", { class: "right num", text: fmt.dur(run.duration) }), h("td", { class: "right num", text: fmt.cost(run.cost) }),
          h("td", {}, outcomeBadge(run))));
      }
      replace(recent, h("table", {}, body));
    }

    const off = store.on(throttle(render, 300));
    render();
    return {
      destroy: off,
      move(delta) {
        const list = [...grid.querySelectorAll(".run-card")];
        if (!list.length) return;
        list.forEach((el) => el.classList.remove("selected"));
        selected = Math.max(0, Math.min(list.length - 1, selected + delta));
        list[selected].classList.add("selected");
        list[selected].focus();
      },
      open() { const el = grid.querySelector(".run-card.selected"); if (el) el.click(); },
    };
  }

  // =========================================================================== views: Runs
  function RunsView(main, crumbs, route) {
    crumbs.textContent = "run history";
    const params = { layer: "", adw: "", status: "", klass: "", from: "", to: "", q: "", sort: "time", dir: "desc", ...route.params };
    const selects = {};
    const select = (name, label, values) => {
      const el = h("select", { "aria-label": label, onchange: () => { params[name] = el.value; update(); } },
        h("option", { value: "", text: `all ${label}` }), values.map((v) => h("option", { value: v, text: v, selected: params[name] === v })));
      selects[name] = el;
      return el;
    };
    const search = h("input", { type: "search", placeholder: "search requests, ids, errors  /", value: params.q, "aria-label": "search runs",
      oninput: debounce(() => { params.q = search.value.trim(); update(); }, 150) });
    const from = h("input", { type: "date", value: params.from, "aria-label": "from date", onchange: () => { params.from = from.value; update(); } });
    const to = h("input", { type: "date", value: params.to, "aria-label": "to date", onchange: () => { params.to = to.value; update(); } });
    const filters = h("div", { class: "filters" });
    const totals = h("div", { class: "strip" });
    const table = h("table");
    main.append(filters, totals, h("div", { class: "table-wrap" }, table));
    let selectedKey = null;

    function buildFilters() {
      const runs = store.runList();
      const uniq = (key) => [...new Set(runs.map((r) => r[key]).filter(Boolean))].sort();
      replace(filters, select("layer", "layers", store.layers.map((l) => l.key)), select("adw", "workflows", uniq("adw")),
        select("status", "statuses", ["running", "passed", "failed", "unknown"]), select("klass", "classes", uniq("klass")),
        h("label", {}, "from", from), h("label", {}, "to", to), search);
    }

    function filtered() {
      const q = params.q.toLowerCase();
      const fromT = params.from ? new Date(`${params.from}T00:00:00`).getTime() / 1000 : null;
      const toT = params.to ? new Date(`${params.to}T23:59:59`).getTime() / 1000 : null;
      let runs = store.runList().filter((r) => (!params.layer || r.layer === params.layer) && (!params.adw || r.adw === params.adw)
        && (!params.status || r.status === params.status) && (!params.klass || r.klass === params.klass)
        && (!fromT || (r.started || 0) >= fromT) && (!toT || (r.started || 0) <= toT)
        && (!q || `${r.id} ${r.title} ${r.prompt} ${r.error} ${r.adw} ${r.layer}`.toLowerCase().includes(q)));
      const key = { time: (r) => r.started || 0, cost: (r) => r.cost ?? -1, duration: (r) => (r.duration ?? (r.status === "running" ? now() - (r.started || now()) : -1)) }[params.sort] || ((r) => r.started || 0);
      runs.sort((a, b) => (key(a) - key(b)) * (params.dir === "asc" ? 1 : -1));
      return runs;
    }

    function median(values) {
      if (!values.length) return null;
      const sorted = [...values].sort((a, b) => a - b), mid = Math.floor(sorted.length / 2);
      return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
    }

    function header(label, sortKey, right) {
      const active = params.sort === sortKey;
      return h("th", { class: `${right ? "right " : ""}${sortKey ? "sortable" : ""}${active ? " sorted" : ""}${active && params.dir === "asc" ? " asc" : ""}`, text: label,
        onclick: sortKey ? () => { params.dir = active && params.dir === "desc" ? "asc" : "desc"; params.sort = sortKey; update(); } : null });
    }

    function render() {
      const runs = filtered();
      const finished = runs.filter((r) => r.status === "passed" || r.status === "failed");
      const passed = finished.filter((r) => r.status === "passed").length;
      const costs = runs.map((r) => r.cost).filter((c) => c !== null && c !== undefined);
      const durations = finished.map((r) => r.duration).filter((d) => d !== null && d !== undefined);
      const stat = (label, value, small) => h("div", { class: "stat" }, h("div", { class: "label", text: label }), h("div", { class: "value" }, value, small ? h("small", { text: ` ${small}` }) : null));
      replace(totals, stat("runs", runs.length, `${finished.length} finished`), stat("pass rate", fmt.pct(finished.length ? passed / finished.length : null), `${passed}/${finished.length}`),
        stat("total cost", fmt.cost(costs.reduce((a, b) => a + b, 0))), stat("median cost", fmt.cost(median(costs))),
        stat("median time", fmt.dur(median(durations))), stat("total time", fmt.dur(durations.reduce((a, b) => a + b, 0))));
      const body = h("tbody");
      for (const run of runs) {
        const tr = h("tr", { "data-key": run.key, class: run.key === selectedKey ? "selected" : "", onclick: () => go(`#/run/${enc(run.layer)}/${enc(run.id)}`) },
          h("td", {}, pill(run.status)), h("td", { class: "num", text: fmt.time(run.started) }), h("td", { text: run.layer }), h("td", { text: run.adw }),
          h("td", { class: "mono dim", text: run.id }), h("td", { text: run.klass }),
          h("td", { class: "title-cell", title: run.prompt, text: run.title || run.problem || "" }), h("td", {}, progress(run.steps)),
          h("td", { class: "right num", ...(run.status === "running" ? { "data-since": run.started } : {}), text: run.status === "running" ? fmt.dur(now() - (run.started || now())) : fmt.dur(run.duration) }),
          h("td", { class: "right num", text: fmt.cost(run.cost) }), h("td", { class: "right num dim", text: fmt.tokens((run.tokens || {}).out) }),
          h("td", {}, outcomeBadge(run), run.dry_run ? h("span", { class: "badge", text: "dry" }) : null));
        body.appendChild(tr);
      }
      if (!runs.length) body.appendChild(h("tr", {}, h("td", { colspan: 12, class: "dim", text: store.loaded ? "no runs match" : "loading…" })));
      replace(table, h("thead", {}, h("tr", {}, header("status"), header("started", "time"), header("layer"), header("workflow"), header("id"), header("class"),
        header("request"), header("steps"), header("time", "duration", true), header("cost", "cost", true), header("out tok", null, true), header("outcome"))), body);
    }

    function update() {
      history.replaceState(null, "", routeHash(["runs"], params));
      render();
    }

    buildFilters();
    render();
    const off = store.on(throttle((kind) => { if (kind === "snapshot" || kind === "layers") buildFilters(); render(); }, 400));
    return {
      destroy: off,
      accepts: () => false,
      searchInput: search,
      move(delta) {
        const rows = [...table.querySelectorAll("tbody tr[data-key]")];
        if (!rows.length) return;
        let index = rows.findIndex((r) => r.dataset.key === selectedKey);
        index = Math.max(0, Math.min(rows.length - 1, index + delta));
        rows.forEach((r) => r.classList.remove("selected"));
        rows[index].classList.add("selected");
        rows[index].scrollIntoView({ block: "nearest" });
        selectedKey = rows[index].dataset.key;
      },
      open() { const row = table.querySelector("tbody tr.selected"); if (row) row.click(); },
    };
  }

  // =========================================================================== views: Run detail
  function RunView(main, crumbs, route) {
    let [, layerKey, runId] = route.parts;
    let detail = null;
    let sub = null;            // the open step panel / log viewer
    let state = { tab: "overview", step: "", params: {} };
    const head = h("div", { class: "run-head" });
    const tabs = h("div", { class: "tabs" });
    const graphCard = h("div", { class: "card" });
    const body = h("div");
    main.append(head, tabs, body);
    replace(crumbs, h("a", { href: "#/runs", text: "runs" }), " / ", h("a", { href: `#/layer/${enc(layerKey)}`, text: layerKey }), " / ", h("span", { class: "mono", text: runId }));

    function link(extra, params) { return routeHash(["run", layerKey, runId, ...extra], params); }

    async function load() {
      try {
        detail = await api(`/api/run?layer=${enc(layerKey)}&run=${enc(runId)}`);
      } catch (error) {
        replace(body, h("div", { class: "empty", text: `This run is not (or no longer) available: ${error.message}` }));
        return;
      }
      renderHead();
      renderTabs();
      renderGraph_();
      if (state.tab === "overview" && !state.step) renderOverview();
      else if (state.tab === "step") refreshStepHead();
      else if (state.tab === "artefacts") renderArtefacts();
    }

    const reload = debounce(load, 350);

    function renderHead() {
      const sm = detail.summary;
      const prompt = h("div", { class: "prompt", title: "click to expand", text: detail.state.prompt || sm.prompt || "", onclick: () => prompt.classList.toggle("open") });
      const elapsed = sm.status === "running" ? h("b", { class: "num", "data-since": sm.started, text: fmt.dur(now() - sm.started) }) : h("b", { class: "num", text: fmt.dur(sm.duration) });
      const fig = (label, value) => h("div", { class: "stat" }, h("div", { class: "label", text: label }), h("div", { class: "value" }, value));
      replace(head,
        h("div", {},
          h("div", { class: "line1" }, pill(sm.status), h("h1", { text: `${sm.adw}` }), h("span", { class: "mono dim", text: sm.id }),
            h("span", { class: "badge", text: sm.layer }), sm.klass ? h("span", { class: "badge", text: `class ${sm.klass}` }) : null,
            sm.dry_run ? h("span", { class: "badge", text: "dry run (mock agent)" }) : null, outcomeBadge(sm),
            sm.waiting ? h("span", { class: "badge warn", text: `waiting for a ${sm.waiting} slot` }) : null,
            sm.current ? h("span", { class: "badge info", text: `▶ ${sm.current.step}: ${sm.current.action}` }) : null,
            h("button", { class: "toggle", type: "button", text: "copy link", title: "copy a deep link to this view", onclick: (e) => copyLink(e.target) })),
          prompt,
          sm.problem ? h("div", { class: "badge warn", text: sm.problem }) : null),
        h("div", { class: "figures" }, fig("time", elapsed), fig("cost", h("b", { text: fmt.cost(sm.cost) })),
          fig("tokens out", h("b", { text: fmt.tokens(sm.tokens.out) })), fig("turns", h("b", { text: sm.turns || "—" }))));
    }

    function renderTabs() {
      const tab = (name, label, hash) => h("a", { href: hash, class: state.tab === name || (name === "overview" && state.tab === "step") ? "active" : "", text: label });
      replace(tabs, tab("overview", "Workflow", link([])), tab("artefacts", `Artefacts (${detail.artefacts.length})`, link(["artefacts"])),
        tab("log", "Run log", link(["log"])));
    }

    function renderGraph_() {
      const legend = graphLegend();
      const wrap = h("div", { class: "graph-wrap" }, renderGraph(detail.graph, {
        selected: state.step,
        onNode: (node) => {
          if (node.type === "candidate" && node.run) go(`#/run/${enc(layerKey)}/${enc(node.run)}`);
          else go(link(["step", node.step]));
        } }));
      replace(graphCard, h("div", { class: "card-head" }, h("h2", { text: "Workflow" }), h("span", { class: "spacer" }), legend), wrap);
      if (graphCard.parentNode !== body) body.prepend(graphCard);
    }

    function renderOverview() {
      for (const child of [...body.children]) if (child !== graphCard) child.remove();
      const grid = h("div", { class: "grid-2" }, h("div", { class: "stack" }, timelineCard(), costsCard()), h("div", { class: "stack" }, outcomeCard()));
      body.appendChild(grid);
    }

    function timelineCard() {
      const t = detail.timeline.totals;
      const sw = (colour) => { const i = h("i"); i.style.background = colour; return i; };
      return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Timeline" })),
        h("div", { class: "tl-wrap" }, renderTimeline(detail.timeline, { onBar: (bar) => {
          if (bar.kind === "agent") go(link(["step", bar.step || bar.ref], { agent: bar.ref }));
          else if (bar.kind === "check" || bar.kind === "gate") go(link(["step", bar.step || bar.check], { attempt: bar.ref }));
          else go(link(["step", bar.step]));
        } })),
        h("div", { class: "totals" }, h("span", {}, "run ", h("b", { text: fmt.dur(t.run) })),
          h("span", {}, sw("var(--agent)"), "agents ", h("b", { text: fmt.dur(t.agent) })),
          h("span", {}, sw("var(--check)"), "checks ", h("b", { text: fmt.dur(t.check) })),
          h("span", {}, sw("var(--panel-3)"), "other (setup, slots, git, gate bookkeeping) ", h("b", { text: fmt.dur(t.other) }))));
    }

    function costsCard() {
      const agents = [...detail.agents].sort((a, b) => (a.start || 0) - (b.start || 0));
      const total = agents.reduce((sum, a) => sum + (a.cost || 0), 0);
      const sumKey = (key) => agents.reduce((sum, a) => sum + ((a.tokens || {})[key] || 0), 0);
      const rows = agents.map((a) => {
        const share = total ? (a.cost || 0) / total : 0;
        const bar = h("span", { class: "meter" });
        const fill = h("i", { class: "on" });
        fill.style.maxWidth = "none";
        fill.style.flex = `0 0 ${Math.round(share * 100)}%`;
        bar.style.width = "80px";
        bar.appendChild(fill);
        return h("tr", { onclick: () => go(link(["step", a.step || a.dir], { agent: a.dir })) },
          h("td", { text: a.step || "—" }), h("td", { class: "mono", text: a.dir }), h("td", { class: "dim", text: a.kind === "claude" ? (a.model || "claude") : a.kind }),
          h("td", { class: "right num", text: a.turns ?? "—" }), h("td", { class: "right num", text: fmt.tokens(a.tokens.in) }),
          h("td", { class: "right num", text: fmt.tokens(a.tokens.out) }), h("td", { class: "right num dim", text: `${fmt.tokens(a.tokens.cache_read)} / ${fmt.tokens(a.tokens.cache_write)}` }),
          h("td", { class: "right num", text: a.start ? fmt.dur((a.end || now()) - a.start) : "—" }), h("td", { class: "right num", text: fmt.cost(a.cost) }), h("td", {}, bar));
      });
      return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Cost and tokens" }), h("span", { class: "spacer" }),
        h("span", { class: "faint", text: "from each agent's final result event; cache = read / write" })),
        h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["step", "agent", "model", "turns", "in", "out", "cache", "time", "cost", "share"].map((label, i) => h("th", { class: i >= 3 && i <= 8 ? "right" : "", text: label })))),
          h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { colspan: 10, class: "dim", text: "no agent steps yet" }))),
          h("tfoot", {}, h("tr", { class: "totals" }, h("td", { text: "total" }), h("td", { text: `${agents.length} agents` }), h("td"),
            h("td", { class: "right num", text: agents.reduce((s2, a) => s2 + (a.turns || 0), 0) }), h("td", { class: "right num", text: fmt.tokens(sumKey("in")) }),
            h("td", { class: "right num", text: fmt.tokens(sumKey("out")) }), h("td", { class: "right num", text: `${fmt.tokens(sumKey("cache_read"))} / ${fmt.tokens(sumKey("cache_write"))}` }),
            h("td"), h("td", { class: "right num", text: fmt.cost(total) }), h("td"))))));
    }

    function outcomeCard() {
      const sm = detail.summary, blocks = [];
      const kv = (pairs) => h("dl", { class: "kv" }, pairs.filter(([, v]) => v !== null && v !== undefined && v !== "").map(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)]));
      blocks.push(h("div", { class: "outcome-block" }, h("div", { class: "card-head" }, h("h3", { text: "Status" }), pill(sm.status)),
        kv([["failed step", sm.failed_step], ["why", sm.error], ["branch", detail.state.branch ? h("span", { class: "mono", text: detail.state.branch }) : ""],
          ["plan", detail.state.plan ? h("span", { class: "mono", text: detail.state.plan }) : ""], ["source", detail.state.source],
          ["ports", (detail.state.ports || []).join(", ")]])));
      if (detail.handoff) {
        const ho = detail.handoff;
        const target = ho.file ? h("a", { href: `/files/${enc(layerKey)}/agents/${enc(runId)}/PR.md`, text: "PR.md" })
          : /^https?:\/\//.test(ho.target || "") ? h("a", { href: ho.target, target: "_blank", rel: "noopener noreferrer", text: ho.target }) : ho.target;
        blocks.push(h("div", { class: "outcome-block" }, h("div", { class: "card-head" }, h("h3", { text: "Why this stopped" }), h("span", { class: "badge warn", text: "handed to a human" })),
          ho.why ? h("div", { class: "why", text: ho.why }) : null,
          kv([["stopped at", ho.stopped_at], ["what the agent asks", ho.asks ? h("pre", { text: ho.asks }) : ""], ["target", target],
            ["reasons", ho.reasons && ho.reasons.length ? h("ul", { class: "issues" }, ho.reasons.map((r) => h("li", { text: r }))) : ""],
            ["handoff error", ho.error], ["no handoff", ho.skipped]])));
      }
      if (detail.gate || (detail.ship && detail.ship.action)) {
        const gate = detail.gate || {}, ship = detail.ship || {};
        const url = ship.url || (ship.pr && ship.pr.url) || "";
        blocks.push(h("div", { class: "outcome-block" }, h("div", { class: "card-head" }, h("h3", { text: "Gate and ship" }),
          gate.action ? h("span", { class: `badge ${gate.ship ? "good" : "warn"}`, text: gate.ship ? `gate: ${gate.action}` : "gate: to a human" }) : null),
          kv([["action", ship.action], ["shipped", ship.shipped === undefined ? "" : String(ship.shipped)], ["target", ship.target],
            ["pull request", /^https?:\/\//.test(url) ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: url }) : ""],
            ["commit", ship.commit ? h("span", { class: "mono", text: String(ship.commit).slice(0, 12) }) : ""],
            ["gate reasons", gate.reasons && gate.reasons.length ? h("ul", { class: "issues" }, gate.reasons.map((r) => h("li", { class: "sev-blocker", text: r }))) : (gate.action ? "none: every rule held" : "")],
            ["diff", gate.diff_lines !== undefined ? `${gate.diff_lines} lines in ${(gate.changed_files || []).length} files` : ""],
            ["changed", gate.changed_files && gate.changed_files.length ? h("span", { class: "mono", text: gate.changed_files.join("  ") }) : ""],
            ["proof", ship.proof ? `${ship.proof.attached || 0} file(s) attached${ship.proof.reason ? ` (${ship.proof.reason})` : ""}` : ""],
            ["note", ship.note]])));
      }
      const review = detail.review;
      if (review && (review.rounds.length || review.mode)) {
        const block = h("div", { class: "outcome-block" }, h("div", { class: "card-head" }, h("h3", { text: "Review" }),
          review.mode ? h("span", { class: "badge", text: review.mode }) : null,
          review.success === true ? h("span", { class: "badge good", text: "approved" }) : review.success === false ? h("span", { class: "badge bad", text: "rejected" }) : null));
        if (!review.rounds.length && review.summary) block.appendChild(h("div", { class: "dim", text: review.summary }));
        for (const round of review.rounds) {
          block.appendChild(h("div", { class: "dim", text: `round ${round.round}: ${round.success ? "approved" : "not approved"}` }));
          for (const reviewer of round.reviewers) {
            const issues = [...reviewer.issues].sort((a, b) => ["blocker", "tech_debt", "skippable"].indexOf(a.severity) - ["blocker", "tech_debt", "skippable"].indexOf(b.severity));
            block.appendChild(h("div", { class: "reviewer" },
              h("span", { class: reviewer.success ? "s-passed" : "s-failed", text: reviewer.success ? "✓" : "✗" }),
              h("div", {}, h("a", { href: link(["step", "review"], { agent: reviewer.dir }), class: "mono", text: reviewer.dir }), " ", h("span", { text: reviewer.summary }),
                issues.length ? h("ul", { class: "issues" }, issues.map((issue) => h("li", { class: `sev-${issue.severity}` },
                  h("span", { class: `badge sev-${issue.severity}`, text: issue.severity }), " ", h("span", { class: "desc", text: issue.description }),
                  issue.resolution ? h("div", { class: "res", text: `→ ${issue.resolution}` }) : null))) : null)));
          }
          if (round.patch) {
            block.appendChild(h("div", { class: "reviewer" }, h("span", { class: "s-running", text: "↺" }),
              h("div", {}, "patch: ", h("a", { href: link(["step", "review"], { agent: round.patch.dirs[0] }), class: "mono", text: round.patch.dirs[0] }), " → ",
                h("a", { href: link(["step", "review"], { agent: round.patch.dirs[1] }), class: "mono", text: round.patch.dirs[1] }),
                round.patch.rerun.length ? ` → re-checked ${round.patch.rerun.join(", ")}` : "")));
          }
        }
        blocks.push(block);
      }
      if (Object.keys(detail.fanout || {}).length) {
        for (const [name, fan] of Object.entries(detail.fanout)) {
          blocks.push(h("div", { class: "outcome-block" }, h("div", { class: "card-head" }, h("h3", { text: `Fan-out ${name}` }), h("span", { class: "badge", text: `pick ${fan.pick}` })),
            h("table", {}, h("tbody", {}, fan.candidates.map((c) => h("tr", { onclick: () => go(`#/run/${enc(layerKey)}/${enc(c.id)}`) },
              h("td", {}, pill(c.status)), h("td", { class: "mono", text: c.id }), h("td", { text: fan.picked.includes(c.id) ? "picked" : "" }),
              h("td", { class: "num", text: fmt.dur(c.seconds) }), h("td", { class: "num dim", text: `${c.diff_lines || 0} lines` }), h("td", { class: "dim title-cell", text: c.reason || "" })))))));
        }
      }
      return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Outcome" })), blocks);
    }

    // ------------------------------------------------------------------ step panel
    function sourcesFor(step) {
      const node = detail.graph.nodes.find((n) => n.step === step && n.type !== "candidate");
      const type = node ? node.type : "agent";
      const sources = [];
      for (const agent of detail.agents.filter((a) => a.step === step || (!a.step && a.dir === step))) {
        sources.push({ key: agent.dir, kind: "agent", label: agent.dir, status: agent.status, start: agent.start, agent });
      }
      const checkName = type === "check" ? step : null;
      for (const [name, attempts] of Object.entries(detail.checks)) {
        for (const at of attempts) {
          const inStep = name === checkName || (type === "review" && at.phase === step);
          if (!inStep) continue;
          sources.push({ key: at.file, kind: "check", label: `${name} #${at.attempt}${at.phase ? " (re-check)" : ""}`, status: at.exit === 0 ? "passed" : at.exit === null || at.exit === undefined ? "done" : "failed", start: at.start || at.end, attempt: at, check: name });
        }
      }
      if (type === "ship") {
        for (const bar of detail.timeline.bars.filter((b) => b.kind === "gate")) {
          sources.push({ key: bar.ref, kind: "check", label: bar.label, status: bar.status, start: bar.start, attempt: { file: bar.ref, exit: bar.exit }, check: bar.check });
        }
      }
      const live = detail.timeline.bars.find((b) => b.kind === "check" && b.step === step && b.status === "running");
      if (live && !sources.some((src) => src.key === live.ref)) sources.push({ key: live.ref, kind: "check", label: `${live.label} (running)`, status: "running", start: live.start, attempt: { file: live.ref }, check: live.check, pending: true });
      sources.sort((a, b) => (a.start || 0) - (b.start || 0));
      return { node, type, sources };
    }

    function openStep() {
      if (sub) { sub.destroy(); sub = null; }
      for (const child of [...body.children]) if (child !== graphCard) child.remove();
      const step = state.step;
      const { node, type, sources } = sourcesFor(step);
      const panel = h("div", { class: "card step-panel" });
      body.appendChild(panel);
      const wanted = state.params.agent || state.params.attempt;
      let source = sources.find((src) => src.key === wanted) || sources.find((src) => src.status === "running") || sources[sources.length - 1];
      const record = detail.steps_detail[step] || {};
      const chips = h("div", { class: "chips" }, sources.map((src) => h("button", { type: "button", class: `chip${src === source ? " active" : ""}`, "data-source": src.key,
        onclick: () => go(link(["step", step], src.kind === "agent" ? { agent: src.key } : { attempt: src.key }), true) },
        h("span", { class: `dot s-${src.status}` }), src.label)));
      const headEl = h("div", { class: "step-head" },
        h("button", { class: "toggle", type: "button", text: "← workflow  esc", onclick: () => go(link([])) }),
        h("h3", { text: step }), h("span", { class: "badge", text: type }), pill(record.status || (node && node.status) || "pending"),
        record.started ? h("span", { class: "num dim", ...(record.status === "running" ? { "data-since": record.started } : {}),
          text: fmt.dur((record.ended || now()) - record.started) }) : null,
        node && node.cost ? h("span", { class: "num", text: fmt.cost(node.cost) }) : null,
        record.detail ? h("span", { class: "dim title-cell", text: clip(record.detail, 160) }) : null);
      panel.appendChild(headEl);
      panel.refreshHead = () => {};
      if (sources.length > 1 || type === "check" || type === "review") panel.appendChild(chips);
      if (type === "fan-out") {
        panel.appendChild(h("div", { class: "dim", text: "Each candidate is its own run; open one from the graph lanes or the outcome table." }));
      }
      if (!source) {
        panel.appendChild(h("div", { class: "empty", text: record.status === "skipped" ? `Skipped: ${record.detail || ""}` : "Nothing recorded for this step yet." }));
        return;
      }
      const content = h("div");
      panel.appendChild(content);
      if (source.kind === "agent") {
        const agent = source.agent;
        const tabName = state.params.tab || "transcript";
        const tabBar = h("div", { class: "tabs" });
        const tab = (name, label) => h("button", { type: "button", class: tabName === name ? "active" : "", text: label,
          onclick: () => go(link(["step", step], { ...state.params, agent: agent.dir, tab: name === "transcript" ? "" : name }), true) });
        const figures = h("span", { class: "dim num" }, `${agent.kind === "claude" ? agent.model || "claude" : agent.kind} · ${fmt.cost(agent.cost)} · ${agent.turns ?? "—"} turns · `,
          `${fmt.tokens(agent.tokens.in)} in / ${fmt.tokens(agent.tokens.out)} out · `, agent.status === "running" ? h("span", { "data-since": agent.start, text: fmt.dur(now() - agent.start) }) : fmt.dur(agent.start ? (agent.end || now()) - agent.start : null));
        tabBar.append(tab("transcript", "Transcript"), tab("prompt", "Prompt"), tab("output", "Output"), h("span", { class: "spacer" }), figures);
        tabBar.style.alignItems = "center";
        content.appendChild(tabBar);
        if (tabName === "transcript") {
          sub = new TranscriptView(content, { layer: layerKey, run: runId, dir: agent.dir, start: agent.start, running: agent.status === "running",
            rawUrl: `/files/${enc(layerKey)}/agents/${enc(runId)}/${enc(agent.dir)}/raw.jsonl`, params: state.params,
            onParams: (p) => history.replaceState(null, "", link(["step", step], { agent: agent.dir, ...p })) });
        } else {
          const block = h("div", { class: "textblock" }, h("pre", { text: "loading…" }));
          content.appendChild(block);
          api(`/api/step?layer=${enc(layerKey)}&run=${enc(runId)}&dir=${enc(agent.dir)}`).then((data) => {
            if (tabName === "prompt") replace(block, markdownSections(data.prompt));
            else replace(block, h("pre", { text: data.output || "(no output yet)" }), data.stderr ? h("details", { class: "section" }, h("summary", { text: "stderr" }), h("pre", { text: data.stderr })) : null);
          });
        }
      } else {
        const at = source.attempt || {};
        const cmd = detail.check_cmds[source.check] || "";
        const resolveAfter = detail.agents.find((a) => a.dir === `${source.check}-resolve-${at.attempt}`);
        content.appendChild(h("div", { class: "toolbar" },
          h("span", { class: "mono", text: cmd ? `$ ${cmd}` : source.label }),
          at.exit !== undefined && at.exit !== null ? h("span", { class: `badge ${at.exit === 0 ? "good" : "bad"}`, text: at.exit === "fail" ? "exit ≠ 0" : `exit ${at.exit}` }) : null,
          source.pending ? h("span", { class: "badge info", text: "running: the log is written when the command finishes" }) : null,
          resolveAfter ? h("a", { href: link(["step", step], { agent: resolveAfter.dir }), class: "toggle", text: `→ resolved by ${resolveAfter.dir}` }) : null,
          h("span", { class: "spacer" }),
          !source.pending ? h("a", { class: "toggle", href: `/files/${enc(layerKey)}/agents/${enc(runId)}/checks/${enc(at.file)}?download=1`, text: "download log" }) : null));
        if (!source.pending) sub = new LogView(content, { layer: layerKey, run: runId, what: "check", file: at.file, running: false, label: source.label });
      }
    }

    function refreshStepHead() {
      // the graph shows live status; re-open the panel only when its set of sources changed
      const { sources } = sourcesFor(state.step);
      const signature = sources.map((src) => `${src.key}:${src.status}`).join("|");
      if (signature !== state.sourceSig) { state.sourceSig = signature; if (!(sub && sub instanceof TranscriptView && sources.some((src) => src.status === "running"))) openStep(); }
    }

    function markdownSections(text) {
      // the prompt rendered as text, split at headings into collapsible sections
      const out = [];
      const parts = text.split(/\n(?=#{1,3} )/);
      for (const part of parts) {
        const first = part.split("\n", 1)[0];
        if (/^#{1,3} /.test(first)) {
          out.push(h("details", { class: "section", open: true }, h("summary", { text: first.replace(/^#+ /, "") }), h("pre", { text: part.slice(first.length + 1) })));
        } else if (part.trim()) {
          out.push(h("pre", { text: part }));
        }
      }
      return out;
    }

    // ------------------------------------------------------------------ artefacts and log tabs
    function renderArtefacts() {
      for (const child of [...body.children]) child.remove();
      const card = h("div", { class: "card" });
      body.appendChild(card);
      if (!detail.artefacts.length) { card.appendChild(h("div", { class: "empty", text: "This run collected no artefacts." })); return; }
      const file = (path) => `/files/${enc(layerKey)}/${path.split("/").map(enc).join("/")}`;
      const groups = new Map();
      for (const art of detail.artefacts) {
        const key = `${art.id}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(art);
      }
      for (const [id, items] of groups) {
        const type = items[0].type;
        const group = h("div", { class: "art-group" }, h("div", { class: "card-head" }, h("h3", { text: id }), h("span", { class: "badge", text: type }),
          h("span", { class: "dim", text: `collected by step ${items[0].step}` }), h("span", { class: "dim", text: `${items.length} file${items.length === 1 ? "" : "s"}` })));
        if (type === "image") {
          group.appendChild(h("div", { class: "thumbs" }, items.map((art) => h("div", { class: "thumb", onclick: () => lightbox(file(art.path)) },
            h("img", { src: file(art.path), alt: art.name, loading: "lazy" }), h("div", { class: "cap", text: `${art.name} · ${fmt.bytes(art.bytes)}` })))));
        } else if (type === "video") {
          for (const art of items) {
            group.appendChild(h("video", { src: file(art.path), controls: true, preload: "metadata" }));
            group.appendChild(h("div", { class: "dim", text: `${art.name} · ${fmt.bytes(art.bytes)}${art.warning ? ` · ${art.warning}` : ""}` }));
            if (art.keyframes.length) {
              group.appendChild(h("div", { class: "frames" }, art.keyframes.map((frame) => h("img", { src: file(frame), alt: "keyframe", loading: "lazy", onclick: () => lightbox(file(frame)) }))));
            }
          }
        } else {
          group.appendChild(h("ul", { class: "issues" }, items.map((art) => h("li", {},
            h("a", { href: file(art.path), target: "_blank", rel: "noopener", text: art.name }), h("span", { class: "dim", text: ` · ${fmt.bytes(art.bytes)}` }),
            art.tail ? h("pre", { class: "dim", text: art.tail.split("\n").slice(-6).join("\n") }) : null))));
        }
        card.appendChild(group);
      }
    }

    function renderLog() {
      for (const child of [...body.children]) child.remove();
      const card = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "run.log" }), h("span", { class: "spacer" }),
        h("a", { class: "toggle", href: `/files/${enc(layerKey)}/agents/${enc(runId)}/run.log?download=1`, text: "download" })));
      body.appendChild(card);
      sub = new LogView(card, { layer: layerKey, run: runId, what: "runlog", running: true, label: "run log" });
    }

    function apply(route) {
      const parts = route.parts;
      const tab = parts[3] === "step" ? "step" : parts[3] || "overview";
      const changedRun = parts[1] !== layerKey || parts[2] !== runId;
      if (changedRun) return false;
      const sameStep = tab === "step" && state.tab === "step" && state.step === parts[4]
        && (state.params.agent || "") === (route.params.agent || "") && (state.params.attempt || "") === (route.params.attempt || "")
        && (state.params.tab || "") === (route.params.tab || "");
      state = { ...state, tab, step: tab === "step" ? parts[4] : "", params: route.params };
      if (sameStep) return true;
      if (sub) { sub.destroy(); sub = null; }
      if (!detail) return true;
      renderTabs();
      renderGraph_();
      if (tab === "overview") renderOverview();
      else if (tab === "step") { state.sourceSig = sourcesFor(state.step).sources.map((src) => `${src.key}:${src.status}`).join("|"); openStep(); }
      else if (tab === "artefacts") renderArtefacts();
      else if (tab === "log") renderLog();
      return true;
    }

    const off = store.on((kind, data) => {
      if (kind === "run" && data.key === `${layerKey}/${runId}`) reload();
      if (kind === "snapshot") reload();
    });
    const timer = setInterval(() => {
      if (detail && detail.summary.status === "running" && (state.tab === "overview" || state.tab === "step")) reload();
    }, 4000);

    apply(route);
    load().then(() => apply(parseRoute()) || null).then(() => {
      if (state.tab === "step") { state.sourceSig = sourcesFor(state.step).sources.map((src) => `${src.key}:${src.status}`).join("|"); openStep(); }
      else if (state.tab === "log" && !sub) renderLog();
    });

    return {
      accepts: (r) => r.parts[1] === layerKey && r.parts[2] === runId,
      route: (r) => apply(r),
      destroy() { off(); clearInterval(timer); if (sub) sub.destroy(); },
      get searchInput() { return sub && sub.search; },
      move(delta) {
        if (sub && sub.move) return sub.move(delta);
        // step through the graph nodes
        const nodes = detail ? detail.graph.nodes.filter((n) => n.type !== "candidate") : [];
        if (!nodes.length) return;
        let index = nodes.findIndex((n) => n.step === state.step);
        index = Math.max(0, Math.min(nodes.length - 1, index + delta));
        go(link(["step", nodes[index].step]));
      },
      open() { if (sub && sub.open) sub.open(); },
      toggleFollow() { if (sub && sub.toggleFollow) sub.toggleFollow(); },
      back() { if (state.tab !== "overview") go(link([])); else go("#/runs"); },
    };
  }

  function copyLink(button) {
    const text = location.href;
    const done = () => { button.textContent = "copied"; setTimeout(() => { button.textContent = "copy link"; }, 1200); };
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, () => prompt("Copy this link", text));
    else prompt("Copy this link", text);
  }

  function lightbox(src) {
    const overlay = document.getElementById("overlay");
    replace(overlay, h("img", { src, alt: "artefact" }));
    overlay.hidden = false;
    overlay.onclick = () => { overlay.hidden = true; clear(overlay); };
  }

  // =========================================================================== views: Layers
  function LayersView(main, crumbs) {
    crumbs.textContent = "watched layers";
    const grid = h("div", { class: "layer-grid" });
    main.append(grid);
    function render() {
      clear(grid);
      for (const layer of store.layers) {
        const runs = store.runList().filter((r) => r.layer === layer.key);
        const finished = runs.filter((r) => r.status === "passed" || r.status === "failed");
        const passed = finished.filter((r) => r.status === "passed").length;
        const cost = runs.reduce((sum, r) => sum + (r.cost || 0), 0);
        grid.appendChild(h("div", { class: "card layer-card", tabindex: 0, onclick: () => go(`#/layer/${enc(layer.key)}`) },
          h("div", { class: "card-head" }, h("h3", { text: layer.name }), layer.key !== layer.name ? h("span", { class: "dim mono", text: layer.key }) : null,
            h("span", { class: "spacer" }), layer.running ? h("span", { class: "badge info", text: `${layer.running} running` }) : null),
          h("div", { class: "dim mono", text: layer.dir }), layer.description ? h("div", { text: layer.description }) : null,
          h("div", { class: "adw-stats" }, h("span", {}, h("b", { text: runs.length }), " runs"), h("span", {}, h("b", { text: fmt.pct(finished.length ? passed / finished.length : null) }), " passed"),
            h("span", {}, h("b", { text: fmt.cost(cost) }), " total"), h("span", {}, h("b", { text: layer.adws.length }), " workflows")),
          h("div", { class: "piter" }, layer.adws.map((name) => h("span", { class: "badge", text: name })))));
      }
      if (!store.layers.length) grid.appendChild(h("div", { class: "empty", text: store.loaded ? "No layers found. Start with --layer DIR or --scan DIR." : "connecting…" }));
    }
    const off = store.on(throttle(render, 500));
    render();
    return { destroy: off };
  }

  function LayerView(main, crumbs, route) {
    const key = route.parts[1];
    replace(crumbs, h("a", { href: "#/layers", text: "layers" }), " / ", h("span", { text: key }));
    const content = h("div", {}, h("div", { class: "empty", text: "loading…" }));
    main.append(content);
    const load = async () => {
      let data;
      try { data = await api(`/api/layer?layer=${enc(key)}`); } catch (error) {
        replace(content, h("div", { class: "empty", text: `No such layer: ${key}` }));
        return;
      }
      const layer = data.layer, st = data.stats;
      const explain = data.explain ? h("button", { class: "toggle", type: "button", text: "what this layer does, in plain words", onclick: () => {
        const overlay = document.getElementById("overlay");
        replace(overlay, h("div", { class: "panel", onclick: (e) => e.stopPropagation() }, h("div", { class: "card-head" }, h("h3", { text: data.explain }), h("span", { class: "spacer" }),
          h("button", { class: "toggle", type: "button", text: "close  esc", onclick: () => { overlay.hidden = true; } })), h("pre", { text: data.explain_text })));
        overlay.hidden = false;
        overlay.onclick = () => { overlay.hidden = true; };
      } }) : h("span", { class: "faint", text: "no plain-language explanation file (EXPLAIN.md) in this layer" });
      replace(content,
        h("div", { class: "run-head" }, h("div", {}, h("div", { class: "line1" }, h("h1", { text: layer.name }), h("span", { class: "dim mono", text: layer.dir })),
          h("div", { class: "prompt open", text: layer.description || "" }), h("div", { class: "piter" }, explain,
            h("a", { class: "toggle", href: `/files/${enc(key)}/agentic-layer.json`, target: "_blank", rel: "noopener", text: "agentic-layer.json" }),
            h("a", { class: "toggle", href: routeHash(["runs"], { layer: key }), text: "all runs" }))),
          h("div", { class: "figures" }, [["runs", st.count], ["pass rate", fmt.pct(st.pass_rate)], ["total cost", fmt.cost(st.cost_total)], ["median time", fmt.dur(st.duration_median)]]
            .map(([label, value]) => h("div", { class: "stat" }, h("div", { class: "label", text: label }), h("div", { class: "value", text: value }))))),
        data.adws.map((adw) => h("div", { class: "card adw-section" },
          h("div", { class: "card-head" }, h("h3", { text: adw.name }), h("span", { class: "dim", text: adw.description }), h("span", { class: "spacer" }),
            h("a", { class: "toggle", href: routeHash(["runs"], { layer: key, adw: adw.name }), text: `${adw.stats.count} runs` })),
          h("div", { class: "piter" }, h("span", { class: "badge", text: `input: ${(adw.input || []).join(", ") || "cli"}` }), h("span", { class: "badge", text: `trigger: ${(adw.trigger || []).join(", ") || "manual"}` }),
            h("span", { class: "badge", text: `environment: ${(adw.environment || []).join(" → ") || "worktree"}` }), h("span", { class: "badge", text: `review: ${adw.review}` })),
          h("div", { class: "adw-stats" }, h("span", {}, h("b", { text: fmt.pct(adw.stats.pass_rate) }), " passed"), h("span", {}, "average time ", h("b", { text: fmt.dur(adw.stats.duration_mean) })),
            h("span", {}, "average cost ", h("b", { text: fmt.cost(adw.stats.cost_mean) })), h("span", {}, "total ", h("b", { text: fmt.cost(adw.stats.cost_total) }))),
          h("div", { class: "graph-wrap" }, renderGraph(adw.graph, { static: true })),
          h("div", { class: "mini-runs" }, adw.recent.map((run) => h("a", { href: `#/run/${enc(key)}/${enc(run.id)}`, title: run.title },
            h("span", { class: `dot s-${run.status}` }), run.id, h("span", { class: "dim", text: fmt.cost(run.cost) })))))));
    };
    load();
    const off = store.on(throttle((kind) => { if (kind !== "slots" && kind !== "containers") load(); }, 3000));
    return { destroy: off };
  }

  // =========================================================================== keyboard
  function help() {
    const overlay = document.getElementById("overlay");
    const keys = [["1 · 2 · 3", "Now · Runs · Layers"], ["j / k", "next / previous run, step or transcript event"], ["Enter", "open the selection / expand an event"],
      ["/", "search (runs or transcript)"], ["f", "follow the running step's transcript or log"], ["Esc", "back / close"], ["?", "this help"]];
    replace(overlay, h("div", { class: "panel", onclick: (e) => e.stopPropagation() }, h("h3", { text: "Keyboard" }),
      h("div", { class: "shortcuts" }, keys.map(([k, v]) => [h("span", {}, h("kbd", { text: k })), h("span", { text: v })])),
      h("p", { class: "dim", text: "Every view is a link: copy the address bar to share a run, a step or a filtered transcript." })));
    overlay.hidden = false;
    overlay.onclick = () => { overlay.hidden = true; };
  }

  document.addEventListener("keydown", (event) => {
    const overlay = document.getElementById("overlay");
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement && document.activeElement.tagName);
    if (event.key === "Escape") {
      if (!overlay.hidden) { overlay.hidden = true; clear(overlay); return; }
      if (typing) { document.activeElement.blur(); return; }
      if (current && current.back) current.back();
      return;
    }
    if (typing || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "/") { const input = current && current.searchInput; if (input) { event.preventDefault(); input.focus(); input.select && input.select(); } return; }
    if (event.key === "1") return go("#/now");
    if (event.key === "2") return go("#/runs");
    if (event.key === "3") return go("#/layers");
    if (event.key === "?") return help();
    if (event.key === "j" && current && current.move) { event.preventDefault(); return current.move(1); }
    if (event.key === "k" && current && current.move) { event.preventDefault(); return current.move(-1); }
    if (event.key === "Enter" && current && current.open && !(document.activeElement && document.activeElement.closest && document.activeElement.closest(".g-node"))) { return current.open(); }
    if (event.key === "f" && current && current.toggleFollow) return current.toggleFollow();
  });

  // =========================================================================== boot
  setInterval(() => {
    const t = now();
    for (const el of document.querySelectorAll("[data-since]")) {
      const since = parseFloat(el.dataset.since);
      if (since) el.textContent = fmt.dur(t - since);
    }
  }, 1000);
  document.getElementById("help-button").addEventListener("click", help);
  window.addEventListener("hashchange", router);
  if (!location.hash) history.replaceState(null, "", "#/now");
  live.connect();
  router();
})();
