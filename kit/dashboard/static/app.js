/* al work dashboard — vanilla JS, no dependencies, no build step.
 *
 * Modules (in order): util · store · live (SSE) · router · components (graph, timeline, transcript,
 * log) · views (now, runs, run, layers, layer, repos, job) · keyboard · boot.
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

  /** Markdown → DOM nodes, built with h() and text nodes only, so raw HTML in a document stays text.
   * Blocks: fenced code, ATX headings, rules, blockquotes, one level of - / * / + and 1. lists,
   * paragraphs; lines starting with | (tables) are kept as one preformatted block. */
  function renderMarkdown(text) {
    const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
    const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
    const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*$/;
    const FENCE = /^\s{0,3}(```+|~~~+)/;
    const QUOTE = /^\s{0,3}>\s?(.*)$/;
    const BULLET = /^\s{0,3}[-*+]\s+(.*)$/;
    const NUMBER = /^\s{0,3}(\d{1,9})[.)]\s+(.*)$/;
    const TABLE = /^\s{0,3}\|/;
    const starts = (line) => FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line)
      || BULLET.test(line) || NUMBER.test(line) || TABLE.test(line);
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      let m;
      if (!line.trim()) { i += 1; continue; }
      if ((m = FENCE.exec(line))) {
        const fence = m[1];
        const code = [];
        i += 1;
        while (i < lines.length && !lines[i].trimStart().startsWith(fence)) code.push(lines[i++]);
        i += 1;
        out.push(h("pre", {}, h("code", { text: code.join("\n") })));
      } else if ((m = HEADING.exec(line))) {
        out.push(h(`h${m[1].length}`, {}, inlineMarkdown(m[2].replace(/\s+#+$/, ""))));
        i += 1;
      } else if (RULE.test(line)) {
        out.push(h("hr"));
        i += 1;
      } else if (QUOTE.test(line)) {
        const quoted = [];
        while (i < lines.length && (m = QUOTE.exec(lines[i]))) { quoted.push(m[1]); i += 1; }
        out.push(h("blockquote", {}, inlineMarkdown(quoted.join("\n").trim())));
      } else if (BULLET.test(line) || NUMBER.test(line)) {
        const ordered = !BULLET.test(line);
        const marker = ordered ? NUMBER : BULLET;
        const items = [];
        while (i < lines.length) {
          const current = lines[i];
          if ((m = marker.exec(current)) && !RULE.test(current)) { items.push(ordered ? m[2] : m[1]); i += 1; }
          else if (items.length && current.trim() && /^\s+/.test(current) && !starts(current.trimStart())) { items[items.length - 1] += ` ${current.trim()}`; i += 1; }
          else break;
        }
        const first = ordered ? Number(NUMBER.exec(line)[1]) : 1;
        out.push(h(ordered ? "ol" : "ul", ordered && first !== 1 ? { start: first } : {}, items.map((item) => h("li", {}, inlineMarkdown(item)))));
      } else if (TABLE.test(line)) {
        const rows = [];
        while (i < lines.length && TABLE.test(lines[i])) rows.push(lines[i++]);
        out.push(h("pre", { class: "md-table", text: rows.join("\n") }));
      } else {
        const para = [];
        while (i < lines.length && lines[i].trim() && (!para.length || !starts(lines[i]))) para.push(lines[i++].trim());
        out.push(h("p", {}, inlineMarkdown(para.join("\n"))));
      }
    }
    return out;
  }

  /** Inline Markdown → nodes: `code`, **strong** / __strong__, *em* / _em_, [label](url). Links go
   * through safeHref (javascript: and other schemes become "#") and open in a new tab without referrer. */
  function inlineMarkdown(text) {
    const out = [];
    const pattern = /`([^`\n]+)`|\*\*(.+?)\*\*|__(.+?)__|\*([^*\s](?:[^*]*?[^*\s])?)\*|(?<![A-Za-z0-9_])_([^_\s](?:[^_]*?[^_\s])?)_(?![A-Za-z0-9_])|\[([^\]\n]+)\]\(\s*([^)\s]*)(?:\s+"[^"\n]*")?\s*\)/g;
    let last = 0;
    let m;
    while ((m = pattern.exec(text))) {
      if (m.index > last) out.push(document.createTextNode(text.slice(last, m.index)));
      if (m[1] !== undefined) out.push(h("code", { text: m[1] }));
      else if (m[2] !== undefined || m[3] !== undefined) out.push(h("strong", {}, inlineMarkdown(m[2] ?? m[3])));
      else if (m[4] !== undefined || m[5] !== undefined) out.push(h("em", {}, inlineMarkdown(m[4] ?? m[5])));
      else out.push(h("a", { href: safeHref(m[7]), target: "_blank", rel: "noopener noreferrer" }, inlineMarkdown(m[6])));
      last = pattern.lastIndex;
    }
    if (last < text.length) out.push(document.createTextNode(text.slice(last)));
    return out;
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

  /** A run a spending limit stopped: marked apart from other stops, with the limit that did it. */
  function budgetBadge(run) {
    const stop = run.budget && run.budget.stopped;
    if (!stop) return null;
    return h("span", { class: "badge budget", title: stop.reason || "", text: `budget stop: ${stop.limit}` });
  }

  /** "$1.20 of $5.00" against the run limit, else just the cost. */
  function costOfLimit(run) {
    const b = run.budget || {};
    return b.run !== null && b.run !== undefined ? `${fmt.cost(run.cost)} of ${fmt.cost(b.run)}` : fmt.cost(run.cost);
  }

  /** The agent CLI's name of a usage window, in words: five_hour → 5-hour, seven_day → weekly. */
  function windowLabel(name) {
    return { five_hour: "5-hour", seven_day: "weekly" }[name] || String(name || "").replace(/_/g, " ");
  }

  /** "API-equivalent" for a run whose agent calls ran on a subscription: its dollars are an estimate, not a bill. */
  function costLabel(run) {
    return run && (run.billing === "subscription" || run.billing === "mixed") ? "API-equivalent" : "";
  }

  /** A run a subscription usage limit stopped, with the window that did it. */
  function limitBadge(run) {
    const stop = run.usage && run.usage.stopped;
    if (!stop) return null;
    return h("span", { class: "badge limit", title: stop.reason || "", text: `limit stop: ${windowLabel(stop.window)}` });
  }

  /** One layer's block of the "Subscription usage" panel: what the agent CLI last reported. */
  function usagePanel(layer) {
    const usage = layer.usage || { state: "none", windows: [] };
    const el = h("div", { class: "usage-layer", dataset: { layer: layer.key } },
      h("div", { class: "name" }, h("a", { href: `#/layer/${enc(layer.key)}`, text: layer.name || layer.key })));
    const threshold = usage.threshold;
    if (usage.state === "api-key") {
      el.appendChild(h("div", { class: "empty", text: "API key: not applicable" }));
    } else if (usage.state !== "known") {
      el.appendChild(h("div", { class: "empty", text: "no data yet" }));
    } else {
      for (const w of usage.windows || []) {
        const known = w.used !== null && w.used !== undefined;
        let share;
        if (known) {
          const fill = h("i", { class: threshold && w.used * 100 >= threshold.percent ? "over" : "" });
          fill.style.width = `${Math.round(w.used * 100)}%`;
          share = [h("span", { class: "usage-bar" }, fill), h("b", { class: "num", text: fmt.pct(w.used) })];
        } else {
          share = h("span", { class: "faint", text: "share used: not reported by the CLI" });
        }
        const reset = w.expired ? "reset time passed; no newer report" : (w.resets_at ? `resets ${fmt.time(w.resets_at)}` : "reset time: not reported");
        el.appendChild(h("div", { class: "usage-window", dataset: { window: w.window } },
          h("span", { class: "label", text: w.label || windowLabel(w.window) }), share,
          w.status ? h("span", { class: `badge${w.status === "rejected" ? " limit" : (w.status === "allowed_warning" ? " warn" : "")}`, text: w.status }) : null,
          h("span", { class: "reset", text: reset }),
          h("span", { class: "faint" }, w.at ? `as of ${fmt.time(w.at)} (${fmt.ago(w.at)})` : "as of: unknown",
            w.run ? [" · ", h("a", { href: `#/run/${enc(layer.key)}/${enc(w.run)}`, text: w.run })] : null)));
      }
    }
    if (threshold) {
      el.appendChild(h("div", { class: "faint", text: `no new agent step at ${threshold.percent}% (then: ${threshold.on_limit === "wait"
        ? `wait for the reset, at most ${threshold.wait_minutes} min` : "hand over"})` }));
    }
    return el;
  }

  /** A promotion run's badge: which stage goes where, and whether it waits for people. */
  function promotionBadge(run) {
    const p = run && run.promotion;
    if (!p) return null;
    const what = `promote ${p.from} → ${p.to}`;
    const state = p.dry_run ? "dry run" : (p.state === "waiting for approval" ? "waiting for approval" : p.state);
    return h("span", { class: `badge promote${run.status === "waiting" ? " waiting" : ""}`, title: p.why || what,
      text: `${what} · ${p.changes} change${p.changes === 1 ? "" : "s"}${p.refused ? `, ${p.refused} refused` : ""} · ${state}` });
  }

  function outcomeBadge(run) {
    const o = run.outcome || {};
    if (run.usage && run.usage.stopped) return limitBadge(run);
    if (run.budget && run.budget.stopped) return budgetBadge(run);
    if (run.status === "handed-over") {
      return h("span", { class: "badge handed-over", title: (o.handover || []).join(", "), text: "handed to a person" });
    }
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

  /** The dashboard's writes (POST /api/models, POST /api/start): JSON with the X-AL-Dashboard header;
   * throws the server's error, with its status and answer on the error. */
  api.post = async function post(path, body) {
    const response = await fetch(path, { method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-AL-Dashboard": "1" }, body: JSON.stringify(body) });
    let data = null;
    try { data = await response.json(); } catch (_err) { data = null; }
    if (!response.ok) {
      const error = new Error((data && data.error) || `${response.status} ${path}`);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    data = data || {};
    data.httpStatus = response.status;
    return data;
  };

  const modelNotes = new Map();   // layer key -> {text, error}: the last answer, kept when the block is rebuilt

  /** One layer's block of the "Models" panel: which model each tier means, why, and a switch. */
  function modelsPanel(layer) {
    const m = layer.models || { tiers: [], known: [], writable: false, why: "", error: "" };
    const last = modelNotes.get(layer.key);
    const note = h("div", { class: `model-note faint${last && last.error ? " error" : ""}`, text: last ? last.text : "" });
    const el = h("div", { class: "models-panel", dataset: { layer: layer.key } },
      h("div", { class: "name" }, h("a", { href: `#/layer/${enc(layer.key)}`, text: layer.name || layer.key })));
    // the live update can rebuild this block while the POST is in flight: write the answer to every
    // note of this layer in the document (the rebuilt one), not only to this block's own
    const show = (text, error) => {
      modelNotes.set(layer.key, { text, error });
      const live = [...document.querySelectorAll(".models-panel .model-note")]
        .filter((n) => n.closest(".models-panel").dataset.layer === layer.key);
      for (const n of new Set([note, ...live])) { n.textContent = text; n.classList.toggle("error", error); }
    };
    const send = async (body, button) => {
      button.disabled = true;
      note.textContent = "";
      note.classList.remove("error");
      try {
        const answer = await api.post("/api/models", { layer: layer.key, ...body });
        show(answer.note || "saved; applies to runs started from now on", false);
      } catch (err) {
        show(String(err.message || err), true);
      } finally {
        button.disabled = false;
      }
    };
    for (const t of m.tiers || []) {
      const known = m.known || [];
      const custom = !known.includes(t.model);
      const select = h("select", { "aria-label": `model of tier ${t.tier}`, disabled: !m.writable },
        known.map((id) => h("option", { value: id, text: id, selected: !custom && id === t.model })),
        h("option", { value: "", text: "custom…", selected: custom }));
      const input = h("input", { type: "text", class: "mono", placeholder: "model id", "aria-label": `custom model id of tier ${t.tier}`,
        value: custom ? t.model || "" : "", disabled: !m.writable, hidden: !custom });
      select.addEventListener("change", () => { input.hidden = select.value !== ""; if (!input.hidden) input.focus(); });
      const apply = h("button", { class: "btn apply", text: "Apply", disabled: !m.writable });
      apply.addEventListener("click", () => send({ tier: t.tier, model: select.value || input.value.trim() }, apply));
      const reset = t.local ? h("button", { class: "btn reset", text: "Reset", disabled: !m.writable,
        title: "drop the local setting of this tier" }) : null;
      if (reset) reset.addEventListener("click", () => send({ tier: t.tier, reset: true }, reset));
      el.appendChild(h("div", { class: "tier-row", dataset: { tier: t.tier } },
        h("span", { class: "tier", text: t.tier }),
        h("span", { class: "current mono", text: t.model || "?" }),
        h("span", { class: "source", text: t.source || "unknown" }),
        h("span", { class: "controls" }, select, input, apply, reset)));
    }
    if (m.error) el.appendChild(h("div", { class: "why error", text: m.error }));
    if (!m.writable && m.why) el.appendChild(h("div", { class: "why", text: m.why }));
    el.appendChild(note);
    el.appendChild(h("div", { class: "faint foot",
      text: "Applies to runs started from now on; runs in flight keep their models. --model and AL_MODELS still win." }));
    return el;
  }

  /** "heavy claude-fable-5-2 (local setting) · base claude-opus-5-5 (built-in default)" for a run's state.models. */
  function runModelsText(models) {
    return Object.entries(models || {}).map(([tier, spec]) => `${tier} ${(spec && spec.model) || "?"} (${(spec && spec.source) || "?"})`).join(" · ");
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
    workspace: { enabled: false, roots: [], repos: [] }, al: { found: false, looked: [] }, actions: { enabled: false, why: "" },
    jobs: new Map(),
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
        store.workspace = snap.workspace || { enabled: false, roots: [], repos: [] };
        store.al = snap.al || { found: false, looked: [] };
        store.actions = snap.actions || { enabled: false, why: "" };
        store.jobs = new Map((snap.jobs || []).map((job) => [job.id, job]));
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
      source.addEventListener("workspace", (event) => {
        store.workspace = JSON.parse(event.data);
        store.emit("workspace");
      });
      source.addEventListener("job", (event) => {
        const job = JSON.parse(event.data);
        store.jobs.set(job.id, job);
        store.emit("job", job);
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
      link.classList.toggle("active", link.dataset.view === route.view || (route.view === "run" && link.dataset.view === "runs") || (route.view === "layer" && link.dataset.view === "layers")
        || ((route.view === "repo" || route.view === "job") && link.dataset.view === "repos"));
    }
    const group = route.view === "repo" ? "repos" : route.view;   // the Repos view keeps its list while a repository is selected
    if (current && current.name === group && current.accepts && current.accepts(route)) {
      current.route(route);
      return;
    }
    if (current && current.destroy) current.destroy();
    const main = clear(document.getElementById("main"));
    delete main.dataset.rendered;
    const crumbs = clear(document.getElementById("crumbs"));
    const make = { now: NowView, runs: RunsView, run: RunView, layers: LayersView, layer: LayerView, repos: ReposView, repo: ReposView,
      job: JobView }[route.view] || NowView;
    current = make(main, crumbs, route);
    current.name = group in { now: 1, runs: 1, run: 1, layers: 1, layer: 1, repos: 1, job: 1 } ? group : "now";
    main.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }

  // =========================================================================== components: workflow graph
  const GLYPH = { passed: "✓", failed: "✗", running: "●", pending: "○", skipped: "–", done: "•", unknown: "?", died: "⊘", waiting: "⏸", rejected: "⊗", "handed-over": "↪", "not-affected": "∅", reused: "↺" };
  // a check that was not run, in the runtime's own words (affected.summary_text)
  function checkResultText(r) {
    if (r.outcome === "not-affected") return `not affected (${r.why || "no covered path changed"}; ${r.paths_total ?? (r.paths || []).length} paths compared)`;
    if (r.outcome === "reused") return `reused from ${(r.from && r.from.step) || r.from_step || "an earlier run"}, same tree ${String(r.tree || "").slice(0, 8)}`;
    if (r.outcome === "failed") return r.exit === null || r.exit === undefined ? "failed" : `failed (exit ${r.exit})`;
    return r.outcome || "";
  }

  /** "claude-opus-5-5" → "opus-5-5": the part of a model name that tells models apart on a small node. */
  function shortModel(agent) {
    if (!agent) return "";
    return String(agent.model || agent.kind || "").replace(/^claude-/, "");
  }
  let graphSerial = 0;

  function renderGraph(graph, opts = {}) {
    graphSerial += 1;
    const marker = `arrow-${graphSerial}`;
    const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
    const svg = s("svg", { viewBox: `0 0 ${graph.width} ${graph.height}`, class: opts.static ? "g-static" : "",
      role: "img", "aria-label": "workflow graph" });
    svg.style.minWidth = `${Math.round(graph.width * 0.72)}px`;
    svg.style.maxWidth = `${graph.width}px`;
    svg.appendChild(s("defs", {}, s("marker", { id: marker, viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
      s("path", { d: "M0,0 L8,4 L0,8 z", class: "g-arrow" }))));
    const live = (st) => ["passed", "failed", "running", "done", "died", "waiting", "rejected", "handed-over"].includes(st);
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
        const depth = Math.max(y1, y2) + 34;
        d = `M${x1},${y1} C${x1},${depth} ${x2},${depth} ${x2},${y2}`;
        lx = (x1 + x2) / 2; ly = depth + 6;
      } else if (b.x < a.x && b.y > a.y + a.h) {
        // the workflow wrapped onto the next row: down from this node, back left in the gap between the rows, into the next
        const x1 = a.x + a.w / 2, y1 = a.y + a.h, x2 = b.x, y2 = b.y + b.h / 2, gap = (b.band ?? b.y) - 15, xl = Math.max(4, x2 - 14);
        d = `M${x1},${y1} V${gap} H${xl} V${y2 - 8} Q${xl},${y2} ${xl + 8},${y2} H${x2}`;
        lx = xl; ly = gap - 4;
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
      if (node.type === "script") classes.push("t-script-node");
      if (opts.selected && opts.selected === node.step && node.type !== "candidate") classes.push("selected");
      if (node.picked) classes.push("picked");
      const meta = [];
      if (!opts.static) {
        if (status === "skipped") meta.push("skipped");
        else if (status === "not-affected") meta.push("not affected");
        else if (node.duration !== null && node.duration !== undefined) meta.push(fmt.dur(node.duration));
        if (node.cost) meta.push(fmt.cost(node.cost));
        if (node.type === "check" && node.attempts > 1) meta.push(`${node.attempts} tries`);
        if (node.type === "check" && node.skipped) meta.push(`${node.skipped} skipped`);
        if (node.type === "check" && node.outcome === "reused") meta.push("reused");
        if (node.resolves) meta.push(`${node.resolves} fix`);
      } else if (node.when && node.when !== "always") {
        meta.push(`when ${node.when.replace("class-", "")}`);
      }
      const agentLine = node.agent ? `agent ${node.agent.id || "?"} · ${node.agent.kind}${node.agent.model ? ` · ${node.agent.model}` : ""}${node.agent.tier ? ` · tier ${node.agent.tier}${node.agent.tier_from ? ` (${node.agent.tier_from})` : ""}` : ""}` : "";
      const title = [`${node.label} (${node.type})`, node.sub, agentLine, status !== "pending" ? status : "", node.detail || ""].filter(Boolean).join("\n");
      const subText = node.agent && !opts.static ? `${node.sub ? `${String(node.sub).split(" @")[0]} · ` : ""}${shortModel(node.agent)}` : node.sub;
      const g = s("g", { class: classes.join(" "), tabindex: opts.onNode ? 0 : null, "data-step": node.step, "data-node": node.id,
        onclick: opts.onNode ? () => opts.onNode(node) : null,
        onkeydown: opts.onNode ? (event) => { if (event.key === "Enter") opts.onNode(node); } : null },
        s("title", { text: title }),
        s("rect", { class: "box", x: node.x, y: node.y, width: node.w, height: node.h, rx: 6 }),
        s("rect", { class: `type t-${node.type}`, x: node.x, y: node.y + 6, width: 3, height: node.h - 12, rx: 1.5 }),
        s("text", { class: "label", x: node.x + 11, y: node.y + 18, text: clip(node.label, 17) }),
        s("text", { class: "sub", x: node.x + 11, y: node.y + 31, text: clip(subText, 24) }),
        s("text", { class: "meta", x: node.x + 11, y: node.y + 45, text: meta.join(" · ") }),
        opts.static ? null : s("text", { class: "icon", x: node.x + node.w - 9, y: node.y + 17, "text-anchor": "end", text: GLYPH[status] || "" }));
      svg.appendChild(g);
    }
    return svg;
  }

  /** What ran an agent call: the model when known, else the agent kind (claude-code, codex, custom, mock). */
  function agentLabel(a) {
    if (a.model) return a.agent_kind && a.agent_kind !== "claude-code" ? `${a.model} (${a.agent_kind})` : a.model;
    if (a.agent_kind) return a.agent_kind;
    return a.kind === "claude" ? "claude" : a.kind;
  }

  function clip(text, n) {
    text = String(text || "");
    return text.length > n ? `${text.slice(0, n - 1)}…` : text;
  }

  function graphLegend() {
    const item = (cls, label) => h("span", {}, h("i", { class: cls }), label);
    const legend = h("div", { class: "legend" });
    const colours = { agent: "var(--agent)", check: "var(--check)", script: "var(--script)", review: "var(--review)", ship: "var(--ship)", "fan-out": "var(--fan)" };
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
    const W = Math.max(420, opts.width || 1000), labelW = Math.min(190, Math.round(W * 0.26)), right = 70, rowH = 18, top = 20;
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
      const x = labelW + (bar.start - t0) * k, w = Math.max(bar.kind === "limit" ? 3 : 2, (end - bar.start) * k);
      const duration = end - bar.start;
      const label = bar.level === 0 ? bar.label : `  ${bar.label}`;
      const tip = `${bar.label} · ${bar.kind}${bar.role && bar.role !== "main" ? ` (${bar.role})` : ""} · ${bar.status}\n${fmt.dur(duration)}${bar.cost ? ` · ${fmt.cost(bar.cost)}` : ""}${bar.exit !== undefined && bar.exit !== null ? ` · exit ${bar.exit}` : ""}`;
      const row = s("g", { class: "tl-row", onclick: opts.onBar ? () => opts.onBar(bar) : null },
        s("title", { text: tip }),
        s("rect", { class: "tl-bg", x: 0, y, width: W, height: rowH }),
        s("text", { class: `tl-label l${bar.level}`, x: bar.level ? 14 : 4, y: y + 12, text: clip(label, 28) }),
        bar.status === "skipped" || bar.status === "not-affected" || bar.status === "reused"
          ? s("text", { class: "tl-tick", x: x + 3, y: y + 12, text: bar.status === "not-affected" ? "not affected" : bar.status })
          : s("rect", { class: `tl-bar k-${bar.kind} l${bar.level} st-${bar.status}`, x, y: y + 3, width: w, height: rowH - 6, rx: 2 }),
        (bar.level === 1 || bar.kind === "check") && !bar.outcome && bar.status !== "not-affected" ? s("text", { class: "tl-tick", x: Math.min(x + w + 4, W - right + 4), y: y + 12,
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
      this.source = new EventSource(o.job ? `/api/tail?job=${enc(o.job)}&what=job`
        : `/api/tail?layer=${enc(o.layer)}&run=${enc(o.run)}&${what}`);
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

    /** True when this view streams the log `opts` asks for. */
    shows(opts) { return ["layer", "run", "what", "file"].every((key) => (this.opts[key] || "") === (opts[key] || "")); }

    /** Move the streaming log, with its text and position, into a freshly drawn panel. */
    mount(container) {
      const top = this.pre.scrollTop;
      container.appendChild(this.pre);
      this.pre.scrollTop = this.follow ? this.pre.scrollHeight : top;
      return this;
    }

    toggleFollow() { this.follow = !this.follow; if (this.follow) this.pre.scrollTop = this.pre.scrollHeight; }
    destroy() { this.source.close(); }
  }

  // =========================================================================== components: a job step's activity
  const ACTIVITY_WINDOW = 300;   // journal rows shown at first, and added by "show earlier"
  const TEXT_WINDOW = 500;       // log lines shown at first, and added by "show earlier"

  /** The toolbar both activity views share: a search box, an optional toggle and a count. */
  function activityTools(view, extra) {
    view.search = h("input", { type: "search", class: "act-search", placeholder: "search this step", "aria-label": "search this step's activity", "data-role": "search" });
    view.search.addEventListener("input", debounce(() => { view.query = view.search.value.trim(); view.render(); }, 150));
    view.count = h("span", { class: "count" });
    view.earlier = h("button", { type: "button", class: "toggle", "data-role": "earlier", text: "show earlier", hidden: true });
    return h("div", { class: "toolbar act-tools" }, view.search, extra, view.count);
  }

  /** Keep a scroll box at its bottom when it was there (or on the first draw), else where the reader left it. */
  function keepScroll(box, draw, first) {
    const atBottom = first || box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    const top = box.scrollTop;
    draw();
    box.scrollTop = atBottom ? box.scrollHeight : top;
  }

  /** An agent step's journal (/api/job/journal, condensed by the job runner): its messages as Markdown,
   * each tool call as one line (tool, target, result size, duration), errors highlighted, the final
   * report and the totals. Hook, system and bookkeeping events stay hidden until "show everything". The
   * latest rows only, "show earlier" for more, and a search over every loaded row. */
  class ActivityView {
    constructor(container, opts) {
      this.opts = opts;
      this.events = [];
      this.next = 0;
      this.limit = ACTIVITY_WINDOW;
      this.showAll = false;
      this.query = "";
      this.drawn = false;
      this.gone = false;
      this.all = h("button", { type: "button", class: "toggle", "data-role": "show-all", "aria-pressed": "false", text: "show everything",
        title: "also show hooks, start-up, rate limits and other bookkeeping" });
      this.all.addEventListener("click", () => {
        this.showAll = !this.showAll;
        this.all.setAttribute("aria-pressed", String(this.showAll));
        this.all.classList.toggle("on", this.showAll);
        this.render();
      });
      const tools = activityTools(this, this.all);
      this.earlier.addEventListener("click", () => { this.limit += ACTIVITY_WINDOW; this.render(true); });
      this.list = h("div", { class: "act-list", tabindex: 0, "aria-label": `activity of ${opts.step}` });
      this.el = h("div", { class: "activity", "data-role": "activity", dataset: { step: opts.step, rows: "0", kind: "journal" } },
        tools, this.earlier, this.list);
      container.appendChild(this.el);
      this.refresh();
    }

    url() { return `/api/job/journal?id=${enc(this.opts.job)}&step=${enc(this.opts.step)}&from=${this.next}`; }

    /** Fetch the events after those already here (all of them after a retry started the journal afresh). */
    async refresh() {
      if (this.loading) { this.again = true; return; }
      this.loading = true;
      try {
        for (let page = 0; page < 100; page += 1) {
          const data = await api(this.url());
          if (this.gone) return;
          if (data.total < this.next) { this.events = []; this.next = 0; continue; }
          this.events.push(...data.events);
          const more = data.next_from > this.next && data.next_from < data.total;
          this.next = data.next_from;
          if (!more) break;
        }
        this.render();
      } catch (_error) {
        if (!this.drawn) this.render();   // no journal yet: the step has not started; the next job event asks again
      } finally {
        this.loading = false;
        if (this.again && !this.gone) { this.again = false; this.refresh(); }
      }
    }

    /** Events → rows: a tool call and its result are one row. */
    rows() {
      const calls = new Map();
      const rows = [];
      for (const event of this.events) {
        if (event.kind === "tool") { const row = { ...event, done: null }; calls.set(event.id, row); rows.push(row); }
        else if (event.kind === "tool.done") { const call = calls.get(event.id); if (call) call.done = event; }
        else rows.push(event);
      }
      return rows;
    }

    static rowText(row) {
      return [row.kind, row.what, row.tool, row.target, row.text].filter(Boolean).join(" ");
    }

    render(keepTop) {
      const q = this.query.toLowerCase();
      let rows = this.rows().filter((row) => this.showAll || row.kind !== "hidden");
      if (q) rows = rows.filter((row) => ActivityView.rowText(row).toLowerCase().includes(q));
      const shown = rows.slice(Math.max(0, rows.length - this.limit));
      const draw = () => replace(this.list, shown.length ? shown.map((row) => this.row(row, q))
        : h("div", { class: "faint act-empty", text: q ? `nothing matches “${this.query}”` : this.events.length ? "only bookkeeping so far (show everything)" : "nothing yet" }));
      if (keepTop) { const top = this.list.scrollTop; draw(); this.list.scrollTop = top; } else keepScroll(this.list, draw, !this.drawn);
      this.drawn = true;
      const hidden = rows.length - shown.length;
      this.earlier.hidden = hidden <= 0;
      this.earlier.textContent = `show earlier (${hidden} more)`;
      this.count.textContent = q ? `${rows.length} matching` : `${shown.length} of ${rows.length}`;
      this.el.dataset.rows = String(shown.length);
    }

    row(row, q) {
      const marked = (text, cls) => { const el = h("span", { class: cls || null }); highlight(el, text || "", q); return el; };
      const at = row.t ? fmt.clock(row.t) : "";
      const time = h("span", { class: "act-time faint", text: at });
      switch (row.kind) {
        case "message":
          return h("div", { class: "act-row act-msg" }, time, q ? h("div", { class: "act-text" }, marked(row.text)) : h("div", { class: "act-text md" }, renderMarkdown(row.text)));
        case "tool": {
          const done = row.done;
          return h("div", { class: `act-row act-tool${done && done.error ? " act-err" : ""}${done ? "" : " act-open"}`, title: row.text || "" },
            time, h("span", { class: "act-name", text: row.tool }), marked(row.target || row.text, "act-target mono"),
            h("span", { class: "act-size num dim", text: done ? `${fmt.bytes(done.bytes)}` : "…" }),
            h("span", { class: "act-dur num dim", text: done ? fmt.dur((done.ms || 0) / 1000) : "" }));
        }
        case "error":
          return h("div", { class: "act-row act-err" }, time, h("span", { class: "act-mark", text: "!" }), marked(row.text, "act-text"));
        case "report":
          return h("div", { class: "act-row act-report" }, h("div", { class: "act-report-head", text: "final report" }),
            q ? marked(row.text, "act-text") : h("div", { class: "act-text md" }, renderMarkdown(row.text)));
        case "result":
          return h("div", { class: `act-row act-result${row.error ? " act-err" : ""}` }, time,
            h("span", { text: [`${row.turns ?? "?"} turns`, fmt.dur((row.ms || 0) / 1000), fmt.cost(row.cost), row.error ? "ended with an error" : "finished"].join(" · ") }));
        case "hidden":
          return h("div", { class: "act-row act-hidden" }, time, h("span", { class: "act-name", text: row.what || "meta" }), marked(row.text, "act-text"));
        default:
          return h("div", { class: "act-row" }, time, marked(row.text, "act-text"));
      }
    }

    destroy() { this.gone = true; }
  }

  /** A plain command step's log (/api/job/log: ANSI stripped, the home directory as ~): the latest lines,
   * "show earlier" for more, and a search that keeps the matching lines and marks the matches. */
  class TextWindow {
    constructor(container, opts) {
      this.opts = opts;
      this.lines = [];
      this.first = 0;
      this.next = null;
      this.query = "";
      this.drawn = false;
      this.gone = false;
      const tools = activityTools(this, null);
      this.earlier.addEventListener("click", () => this.showEarlier());
      this.pre = h("div", { class: "text-window mono", tabindex: 0, "aria-label": `output of ${opts.step}` });
      this.el = h("div", { class: "activity", "data-role": "activity", dataset: { step: opts.step, rows: "0", kind: "log" } },
        tools, this.earlier, this.pre);
      container.appendChild(this.el);
      this.refresh();
    }

    url(params) { return `/api/job/log?id=${enc(this.opts.job)}&step=${enc(this.opts.step)}&${params}`; }

    async refresh() {
      if (this.loading) { this.again = true; return; }
      this.loading = true;
      try {
        for (let page = 0; page < 100; page += 1) {
          if (this.next === null) {
            const data = await api(this.url(`tail=${TEXT_WINDOW}`));
            if (this.gone) return;
            Object.assign(this, { lines: data.lines, first: data.from, next: data.next_from });
            break;
          }
          const data = await api(this.url(`from=${this.next}`));
          if (this.gone) return;
          if (data.total < this.next) { this.next = null; continue; }   // a retry started the log afresh
          this.lines.push(...data.lines);
          const more = data.next_from > this.next && data.next_from < data.total;
          this.next = data.next_from;
          if (!more) break;
        }
        this.render();
      } catch (_error) {
        if (!this.drawn) this.render();
      } finally {
        this.loading = false;
        if (this.again && !this.gone) { this.again = false; this.refresh(); }
      }
    }

    async showEarlier() {
      const from = Math.max(0, this.first - TEXT_WINDOW);
      try {
        const data = await api(this.url(`from=${from}`));
        this.lines = data.lines.slice(0, this.first - from).concat(this.lines);
        this.first = from;
        this.render(true);
      } catch (_error) { /* kept as it is */ }
    }

    render(keepTop) {
      const q = this.query.toLowerCase();
      const shown = q ? this.lines.filter((line) => line.toLowerCase().includes(q)) : this.lines;
      const draw = () => replace(this.pre, shown.length ? shown.map((line) => { const el = h("div", { class: "tw-line" }); highlight(el, line || " ", q); return el; })
        : h("div", { class: "faint", text: q ? `nothing matches “${this.query}”` : "no output yet" }));
      if (keepTop) { const top = this.pre.scrollTop; draw(); this.pre.scrollTop = top; } else keepScroll(this.pre, draw, !this.drawn);
      this.drawn = true;
      this.earlier.hidden = this.first <= 0;
      this.earlier.textContent = `show earlier (${this.first} more lines)`;
      this.count.textContent = q ? `${shown.length} matching lines` : `lines ${this.first + 1}–${this.first + this.lines.length}`;
      this.el.dataset.rows = String(shown.length);
    }

    destroy() { this.gone = true; }
  }

  // =========================================================================== views: Now
  function NowView(main, crumbs) {
    crumbs.textContent = "what the agents are doing now";
    const strip = h("div", { class: "strip" });
    const grid = h("div", { class: "now-grid" });
    const pools = h("div", { class: "pools" });
    const usage = h("div", { class: "usage" });
    const modelsBox = h("div", { class: "models" });
    const recent = h("div", { class: "table-wrap" });
    const containerNote = h("span", { class: "faint" });
    main.append(strip,
      h("div", { class: "card-head" }, h("h2", { text: "In flight" }), h("span", { class: "spacer" }), containerNote), grid,
      h("div", { class: "card-head" }, h("h2", { text: "Subscription usage" }), h("span", { class: "spacer" }),
        h("span", { class: "faint", text: "what the agent CLI last reported; not a live meter" })), usage,
      h("div", { class: "card-head" }, h("h2", { text: "Models" }), h("span", { class: "spacer" }),
        h("span", { class: "faint", text: "which model each tier uses; a local setting of this machine" })), modelsBox,
      h("div", { class: "card-head", style: null }, h("h2", { text: "Slots and ports" })), pools,
      h("div", { class: "card-head" }, h("h2", { text: "Recently finished" })), recent);
    grid.style.marginBottom = "18px";
    pools.style.marginBottom = "18px";
    usage.style.marginBottom = "18px";
    modelsBox.style.marginBottom = "18px";
    const modelBlocks = new Map();   // layer key -> {sig, el}: a block is rebuilt only when its data changes
    const cards = new Map();
    let selected = -1;

    function stat(label, value, small) {
      return h("div", { class: "stat" }, h("div", { class: "label", text: label }), h("div", { class: "value" }, value, small ? h("small", { text: ` ${small}` }) : null));
    }

    /** A layer's spending limits and how much of today's limit its runs have used. */
    function budgetStat(layer) {
      const b = layer.budget;
      const limits = [b.run !== null ? `${fmt.cost(b.run)}/run` : "", b.step !== null ? `${fmt.cost(b.step)}/call` : ""].filter(Boolean).join(" · ");
      const value = b.day !== null ? `${fmt.cost(b.today)} of ${fmt.cost(b.day)}` : fmt.cost(b.today);
      const el = stat(`budget today · ${layer.name}`, value, [limits, b.stops_today ? `${b.stops_today} budget stop${b.stops_today > 1 ? "s" : ""}` : ""].filter(Boolean).join(" · "));
      el.classList.add("budget-stat");
      el.dataset.layer = layer.key;
      if (b.day !== null && b.today >= b.day) el.classList.add("over");
      return el;
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
          run.dry_run ? h("span", { class: "badge", text: "dry run" }) : null, promotionBadge(run), h("span", { class: "spacer" }), progress(run.steps)),
        h("div", { class: "title", text: run.title || "(no request text)" }),
        h("div", { class: "doing" }, h("span", { class: "step", text: current.step ? `▶ ${current.step}` : "▶" }),
          h("span", { class: "action", text: run.waiting ? `waiting for a free ${run.waiting} slot` : (current.action || "working") })),
        h("div", { class: "facts" },
          h("span", {}, "elapsed ", h("b", { class: "num", "data-since": run.started || "", text: fmt.dur(now() - (run.started || now())) })),
          h("span", {}, "cost so far ", h("b", { class: "num", text: costOfLimit(run) }),
            costLabel(run) ? h("span", { class: "faint", text: " API-equivalent" }) : null, run.cost_partial ? h("span", { class: "faint", text: " +running step" }) : null),
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
      const finishedDay = day.filter((r) => r.status === "passed" || r.status === "failed" || r.status === "handed-over");
      const handedDay = finishedDay.filter((r) => r.status === "handed-over").length;
      const costDay = day.reduce((sum, r) => sum + (r.cost || 0), 0);
      replace(strip,
        stat("running", running.length, running.filter((r) => r.waiting).length ? `${running.filter((r) => r.waiting).length} waiting` : ""),
        stat("runs, 24 h", day.length, finishedDay.length ? `${fmt.pct(finishedDay.filter((r) => r.status === "passed").length / finishedDay.length)} passed`
          + (handedDay ? ` · ${handedDay} handed over` : "") : ""),
        stat("cost, 24 h", fmt.cost(costDay), day.some((r) => costLabel(r)) ? "API-equivalent" : ""),
        ...store.layers.filter((l) => l.budget).map((l) => budgetStat(l)),
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
      renderUsage();
      renderModels();
      renderPools();
      renderRecent(runs);
    }

    function renderUsage() {
      replace(usage, store.layers.length ? store.layers.map((layer) => usagePanel(layer)) : h("div", { class: "empty", text: "no layers yet" }));
    }

    function renderModels() {
      if (!store.layers.length) { modelBlocks.clear(); replace(modelsBox, h("div", { class: "empty", text: "no layers yet" })); return; }
      const blocks = store.layers.map((layer) => {
        const sig = JSON.stringify([layer.name, layer.models]);
        const known = modelBlocks.get(layer.key);
        if (known && known.sig === sig) return known.el;
        const el = modelsPanel(layer);
        modelBlocks.set(layer.key, { sig, el });
        return el;
      });
      for (const key of [...modelBlocks.keys()]) if (!store.layers.some((l) => l.key === key)) modelBlocks.delete(key);
      if (blocks.length !== modelsBox.children.length || blocks.some((el, i) => modelsBox.children[i] !== el)) replace(modelsBox, blocks);
    }

    function renderPools() {
      clear(pools);
      const idle = [];
      for (const layer of store.layers) {
        const data = store.slots[layer.key];
        if (!data) continue;
        const busy = Object.values(data.pools).some((p) => p.used) || (data.ports || []).some((p) => p.alive !== false)
          || store.runList().some((r) => r.layer === layer.key && r.status === "running");
        if (!busy) { idle.push(layer.key); continue; }
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
      if (idle.length) {
        pools.appendChild(h("div", { class: "pool" }, h("div", { class: "name" }, h("span", { text: `${idle.length} idle layer${idle.length === 1 ? "" : "s"}` }),
          h("span", { class: "faint", text: "no slot held, no port leased" })),
          h("div", { class: "ports" }, idle.map((key, i) => [i ? " · " : "", h("a", { href: `#/layer/${enc(key)}`, text: key })]))));
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
      if (!done.length) body.appendChild(h("tr", {}, h("td", { class: "dim", text: store.loaded ? "no finished runs yet" : "loading…" })));
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
        select("status", "statuses", ["running", "passed", "handed-over", "failed", "died", "waiting", "unknown"]), select("klass", "classes", uniq("klass")),
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
      const finished = runs.filter((r) => r.status === "passed" || r.status === "failed" || r.status === "handed-over");
      const passed = finished.filter((r) => r.status === "passed").length;
      const handedOver = finished.filter((r) => r.status === "handed-over").length;
      const costs = runs.map((r) => r.cost).filter((c) => c !== null && c !== undefined);
      const durations = finished.map((r) => r.duration).filter((d) => d !== null && d !== undefined);
      const stat = (label, value, small) => h("div", { class: "stat" }, h("div", { class: "label", text: label }), h("div", { class: "value" }, value, small ? h("small", { text: ` ${small}` }) : null));
      replace(totals, stat("runs", runs.length, `${finished.length} finished`), stat("pass rate", fmt.pct(finished.length ? passed / finished.length : null), `${passed}/${finished.length}`),
        stat("handed over", handedOver),
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
          h("td", {}, outcomeBadge(run), promotionBadge(run), run.dry_run ? h("span", { class: "badge", text: "dry" }) : null));
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
  const promotionForm = new Map();   // run key -> {by, comment, picked}: kept while the card is rebuilt

  /** The promotion card of a promotion run: the changes with their acceptance criteria and the proof of
   * each, the decisions so far, the pack, and Accept / Refuse (revert | patch) / Approve all / Continue,
   * which POST /api/promotion (the layer's own adws/promote.py records the decision or resumes the run). */
  function promotionCard(detail, layerKey, reload) {
    const p = detail.promotion, sm = detail.summary;
    const key = `${layerKey}/${p.id}`;
    const form = promotionForm.get(key) || { by: localStorage.getItem("al.promotion.by") || "", comment: "", picked: new Set() };
    promotionForm.set(key, form);
    const note = h("div", { class: "promotion-note", role: "status" });
    const status = p.approval_status || {};
    const fileLink = (rel, label) => h("a", { href: `/files/${enc(layerKey)}/${rel}`, target: "_blank", rel: "noopener", text: label });
    const send = async (body, done) => {
      form.by = by.value.trim();
      form.comment = comment.value;
      if (body.action === "decide" && !form.by) { replace(note, h("span", { class: "badge warn", text: "your name goes with every decision" })); by.focus(); return; }
      localStorage.setItem("al.promotion.by", form.by);
      try {
        const answer = await api.post("/api/promotion", { layer: layerKey, from: p.from, to: p.to, ...body,
          ...(body.action === "decide" ? { by: form.by, comment: form.comment } : {}) });
        replace(note, h("span", { class: "badge ok", text: done(answer) }));
        if (body.action === "decide") { form.comment = ""; form.picked.clear(); }
        reload();
      } catch (error) {
        replace(note, h("span", { class: "badge warn", text: error.message }));
      }
    };
    const proofItem = (item) => {
      const mark = item.ok ? "✓" : "✗";
      if ((item.kind === "image" || item.kind === "video") && item.path) {
        return h("li", { class: item.ok ? "ok" : "bad" }, `${mark} ${item.kind} `, fileLink(item.path, item.label));
      }
      return h("li", { class: item.ok ? "ok" : "bad", text: `${mark} ${item.label}` });
    };
    const decided = (change) => change.status === "refused"
      ? h("span", { class: "badge warn", text: `refused by ${change.by}${change.excluded ? ` · ${change.excluded}` : ""}` })
      : change.status === "accepted" ? h("span", { class: "badge ok", text: `accepted by ${change.by}` })
        : h("span", { class: "badge", text: "undecided" });
    const rows = p.changes.map((change) => {
      const pick = h("input", { type: "checkbox", "aria-label": `select change ${change.id}`, disabled: !p.can_decide || !!change.excluded,
        onchange: (e) => { if (e.target.checked) form.picked.add(change.id); else form.picked.delete(change.id); } });
      pick.checked = form.picked.has(change.id);
      return h("div", { class: `promotion-change s-${change.status}`, "data-change": change.id },
        h("div", { class: "card-head" }, pick, h("h3", { text: change.title || change.id }), h("span", { class: "mono dim", text: change.id }),
          h("span", { class: "badge", text: change.klass }), h("span", { class: "spacer" }), decided(change),
          change.patch_run && change.patch_run.run_id ? h("a", { class: "badge info", href: `#/run/${enc(layerKey)}/${enc(change.patch_run.run_id)}`, text: `patch run ${change.patch_run.run_id}` }) : null),
        change.comment ? h("div", { class: "why", text: `“${change.comment}”` }) : null,
        h("ul", { class: "criteria" }, change.criteria.map((c) => h("li", { class: c.uncovered ? "uncovered" : "" },
          h("span", { text: c.criterion }), c.uncovered ? h("span", { class: "badge warn", text: "no proof" }) : null,
          h("ul", { class: "proof" }, c.proof.map(proofItem))))),
        change.files.length ? h("div", { class: "faint mono", text: change.files.join("  ") }) : null);
    });
    const by = h("input", { type: "text", class: "promotion-by", placeholder: "your name", maxlength: 80, value: form.by, "aria-label": "your name" });
    const comment = h("textarea", { class: "promotion-comment", placeholder: "comment (why)", maxlength: 2000, rows: 2, "aria-label": "comment" });
    comment.value = form.comment;
    const picked = () => [...form.picked];
    const needPick = (verb) => { if (!picked().length) { replace(note, h("span", { class: "badge warn", text: `select the changes to ${verb}` })); return false; } return true; };
    const button = (text, cls, onclick, enabled = true) => h("button", { type: "button", class: `toggle ${cls}`, text, disabled: !enabled, onclick });
    const can = p.can_decide && sm.status !== "running";
    const actions = h("div", { class: "promotion-actions" }, by, comment,
      h("div", { class: "buttons" },
        button("Accept selected", "accept", () => needPick("accept") && send({ action: "decide", decision: "accept", changes: picked() }, () => `accepted ${picked().join(", ")}`), can),
        button("Refuse · revert", "refuse", () => needPick("refuse") && send({ action: "decide", decision: "refuse", then: "revert", changes: picked() }, () => "refused: it is reverted when the promotion continues"), can),
        button("Refuse · patch run", "refuse", () => needPick("refuse") && send({ action: "decide", decision: "refuse", then: "patch", changes: picked() }, () => "refused: reverted, and a patch run starts with your comment"), can),
        button("Approve all", "approve", () => send({ action: "decide", decision: "approve", changes: [] }, () => "approved"), can),
        button("Continue", "continue", () => send({ action: "continue" }, (a) => `continuing promotion ${a.run_id}`), p.open && sm.status !== "running")),
      note);
    const decisions = (p.decisions || []).map((d) => h("li", {}, h("b", { text: d.by }), ` ${d.decision} ${(d.scope || []).join(", ")}`,
      d.then ? ` (then ${d.then})` : "", d.comment ? ` — “${d.comment}”` : "", h("span", { class: "faint", text: ` ${d.at}${d.kind !== "human" ? ` · ${d.kind}` : ""}` })));
    const stopped = p.stopped ? h("div", { class: "badge warn", text: `stopped at ${p.stopped.step}: ${p.stopped.reason}` }) : null;
    return h("div", { class: "card promotion-card" },
      h("div", { class: "card-head" }, h("h2", { text: `Promote ${p.from_branch} → ${p.to_branch}` }),
        h("span", { class: "badge", text: `${p.method} · ${p.approval}${p.approval !== "auto" ? ` × ${p.approvers}` : ""}` }),
        p.open ? h("span", { class: "badge waiting", text: status.why || "open" }) : h("span", { class: "badge", text: p.closed_at ? "closed" : "not open" }),
        h("span", { class: "spacer" }), p.pack ? fileLink(p.pack, "open the presentation pack") : null,
        ...Object.entries(p.records || {}).map(([name, rel]) => fileLink(rel, name))),
      stopped,
      (p.notes || []).length ? h("ul", { class: "issues" }, p.notes.map((n) => h("li", { text: n }))) : null,
      h("div", { class: "promotion-changes" }, rows),
      p.can_decide ? actions : null,
      decisions.length ? h("details", { class: "promotion-decisions", open: true }, h("summary", { text: `decisions (${decisions.length})` }), h("ul", {}, decisions)) : null);
  }

  function RunView(main, crumbs, route) {
    let [, layerKey, runId] = route.parts;
    let detail = null;
    let shown = "";            // the JSON text of the detail last rendered
    let loads = 0;             // completed loads, published as main.dataset.rendered
    let gone = false;          // set by destroy(); a late load() then touches nothing
    let sub = null;            // the open step panel / log viewer
    let state = { tab: "overview", step: "", params: {} };
    let timelineDraw = null;
    const onResize = debounce(() => { if (timelineDraw && state.tab === "overview") timelineDraw(); }, 200);
    window.addEventListener("resize", onResize);
    const head = h("div", { class: "run-head" });
    const tabs = h("div", { class: "tabs" });
    const graphCard = h("div", { class: "card" });
    const body = h("div");
    main.append(head, tabs, body);
    replace(crumbs, h("a", { href: "#/runs", text: "runs" }), " / ", h("a", { href: `#/layer/${enc(layerKey)}`, text: layerKey }), " / ", h("span", { class: "mono", text: runId }));

    function link(extra, params) { return routeHash(["run", layerKey, runId, ...extra], params); }

    async function load() {
      let fresh;
      try {
        fresh = await api(`/api/run?layer=${enc(layerKey)}&run=${enc(runId)}`);
      } catch (error) {
        if (gone) return;
        replace(body, h("div", { class: "empty", text: `This run is not (or no longer) available: ${error.message}` }));
        main.dataset.rendered = String(++loads);
        return;
      }
      if (gone) return;
      const text = JSON.stringify(fresh);
      if (text !== shown) {        // an unchanged detail keeps its nodes (hover, focus, selection)
        detail = fresh;
        shown = text;
        renderHead();
        renderTabs();
        renderGraph_();
        if (state.tab === "overview" && !state.step) renderOverview();
        else if (state.tab === "step") refreshStepHead();
        else if (state.tab === "artefacts") renderArtefacts();
        else if (state.tab === "documents") renderDocuments();
      }
      main.dataset.rendered = String(++loads);
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
            sm.status === "handed-over" && (sm.outcome.handover || []).length
              ? h("span", { class: "badge handed-over", title: "why a person decides", text: `↪ ${sm.outcome.handover.join(", ")}` }) : null,
            promotionBadge(sm),
            sm.fanout_parent ? h("a", { class: "badge info", href: `#/run/${enc(layerKey)}/${enc(sm.fanout_parent)}`,
              text: sm.fanout && sm.fanout.index ? `candidate ${sm.fanout.index} of ${sm.fanout_parent}${sm.fanout.step ? ` (${sm.fanout.step})` : ""}` : `candidate of ${sm.fanout_parent}` }) : null,
            sm.status === "died" ? h("span", { class: "badge died", title: "state.json still says running, but its heartbeat stopped and its process is gone",
              text: `process died · last seen ${fmt.ago(sm.last_seen)} (${fmt.time(sm.last_seen)})` }) : null,
            sm.waiting ? h("span", { class: "badge warn", text: `waiting for a ${sm.waiting} slot` }) : null,
            sm.status === "waiting" ? h("span", { class: "badge waiting", title: "a script step exited with a code it declares as waiting; the worktree is kept",
              text: `⏸ waiting${sm.waiting_step ? ` at ${sm.waiting_step.name}` : ""} · resume: ${sm.resume || ""}` }) : null,
            sm.current ? h("span", { class: "badge info", text: `▶ ${sm.current.step}: ${sm.current.action}` }) : null,
            h("button", { class: "toggle", type: "button", text: "copy link", title: "copy a deep link to this view", onclick: (e) => copyLink(e.target) })),
          prompt,
          sm.problem ? h("div", { class: "badge warn", text: sm.problem }) : null),
        h("div", { class: "figures" }, fig("time", elapsed), fig(costLabel(sm) ? "cost (API-equivalent)" : "cost", h("b", { text: fmt.cost(sm.cost_children ? (sm.cost || 0) + sm.cost_children : sm.cost) },
          sm.cost_children ? h("small", { text: ` incl. ${fmt.cost(sm.cost_children)} candidates` }) : null,
          sm.budget && sm.budget.run !== null && sm.budget.run !== undefined ? h("small", { class: "budget-of", text: ` of ${fmt.cost(sm.budget.run)} run limit` }) : null)),
          fig("tokens out", h("b", { text: fmt.tokens(sm.tokens.out) })), fig("turns", h("b", { text: sm.turns || "—" }))));
    }

    function renderTabs() {
      const tab = (name, label, hash) => h("a", { href: hash, class: state.tab === name || (name === "overview" && state.tab === "step") ? "active" : "", text: label });
      replace(tabs, tab("overview", "Workflow", link([])),
        tab("documents", `Documents (${(detail.documents || []).length})`, link(["documents"])), tab("artefacts", `Artefacts (${detail.artefacts.length})`, link(["artefacts"])),
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
      if (detail.promotion) body.appendChild(promotionCard(detail, layerKey, load));
      const grid = h("div", { class: "grid-2" }, h("div", { class: "stack" }, timelineCard(), costsCard()), h("div", { class: "stack" }, outcomeCard()));
      body.appendChild(grid);
    }

    function timelineCard() {
      const t = detail.timeline.totals;
      const sw = (colour) => { const i = h("i"); i.style.background = colour; return i; };
      const wrap = h("div", { class: "tl-wrap" });
      const draw = () => replace(wrap, renderTimeline(detail.timeline, { width: wrap.clientWidth || 800, onBar: (bar) => {
        if (bar.kind === "wait" || bar.kind === "limit" || bar.kind === "setup") return;
        if (bar.kind === "agent") go(link(["step", bar.step || bar.ref], { agent: bar.ref }));
        else if (bar.kind === "check" || bar.kind === "gate") go(link(["step", bar.step || bar.check], { attempt: bar.ref }));
        else go(link(["step", bar.step]));
      } }));
      requestAnimationFrame(draw);
      timelineDraw = draw;
      return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Timeline" }),
        h("span", { class: "spacer" }), h("span", { class: "faint", text: "click a bar to open it" })), wrap,
        h("div", { class: "totals" }, h("span", {}, "run ", h("b", { text: fmt.dur(t.run) })),
          h("span", {}, sw("var(--agent)"), "agents ", h("b", { text: fmt.dur(t.agent) })),
          h("span", {}, sw("var(--check)"), "checks ", h("b", { text: fmt.dur(t.check) })),
          t.wait ? h("span", {}, sw("var(--waiting)"), "waiting for a slot ", h("b", { text: fmt.dur(t.wait) })) : null,
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
          h("td", { text: a.step || "—" }), h("td", { class: "mono", text: a.dir }),
          h("td", { class: "dim", title: [a.agent_kind ? `agent ${a.agent_id || "?"} (${a.agent_kind})` : "", a.tier ? `tier ${a.tier}${a.tier_from ? `, chosen by: ${a.tier_from}` : ""}${a.model_from === "override" ? "; model overridden for this run" : ""}` : ""].filter(Boolean).join("\n"),
            text: `${agentLabel(a)}${a.tier ? ` · ${a.tier}` : ""}` }),
          h("td", { class: "right num", text: a.turns ?? "—" }), h("td", { class: "right num", text: fmt.tokens(a.tokens.in) }),
          h("td", { class: "right num", text: fmt.tokens(a.tokens.out) }), h("td", { class: "right num dim", text: `${fmt.tokens(a.tokens.cache_read)} / ${fmt.tokens(a.tokens.cache_write)}` }),
          h("td", { class: "right num", text: a.start ? fmt.dur((a.end || now()) - a.start) : "—" }), h("td", { class: "right num", text: fmt.cost(a.cost) }), h("td", {}, bar));
      });
      return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Cost and tokens" }), h("span", { class: "spacer" }),
        h("span", { class: "faint", text: costLabel(detail.summary)
          ? "from each agent's final result event; API-equivalent: these calls ran on a subscription; cache = read / write"
          : "from each agent's final result event; cache = read / write" })),
        h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["step", "agent", "model", "turns", "in", "out", "cache", "time", "cost", "share"].map((label, i) => h("th", { class: i >= 3 && i <= 8 ? "right" : "", text: label })))),
          h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { colspan: 10, class: "dim", text: "no agent steps yet" }))),
          h("tfoot", {}, h("tr", { class: "sum-row" }, h("td", { text: "total" }), h("td", { text: `${agents.length} agents` }), h("td"),
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
          ["ports", (detail.state.ports || []).join(", ")],
          ["models", runModelsText(detail.state.models)]])));
      if (detail.budget) {
        const b = detail.budget, stop = b.stopped;
        const limit = (v) => (v === null || v === undefined ? "none" : fmt.cost(v));
        blocks.push(h("div", { class: "outcome-block budget-block" }, h("div", { class: "card-head" }, h("h3", { text: "Spending" }),
          stop ? h("span", { class: "badge budget", text: `stopped by the ${stop.limit} limit` }) : null),
          kv([["spent", `${fmt.cost(b.spent)}${b.run !== null && b.run !== undefined ? ` of ${fmt.cost(b.run)} run limit` : ""}${b.run_from && b.run_from !== "layer" ? ` (${b.run_from})` : ""}`],
            ["limits", `run ${limit(b.run)} · per agent call ${limit(b.step)} · per day ${limit(b.day)}`],
            ["why it stopped", stop ? stop.reason : ""],
            ["no cost reported", (b.unreported || []).length ? `${b.unreported.join(", ")} (counted as $0)` : ""]])));
      }
      if (detail.usage && detail.usage.stopped) {
        const stop = detail.usage.stopped;
        blocks.push(h("div", { class: "outcome-block limit-block" }, h("div", { class: "card-head" }, h("h3", { text: "Subscription limit" }),
          h("span", { class: "badge limit", text: `stopped by the ${windowLabel(stop.window)} limit` })),
          kv([["window", windowLabel(stop.window)], ["share used", stop.used === null || stop.used === undefined ? "not reported by the CLI" : fmt.pct(stop.used)],
            ["resets", stop.resets_at ? fmt.time(stop.resets_at) : "not reported"], ["why", stop.reason]])));
      }
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
          gate.action ? h("span", { class: `badge ${gate.ship ? "good" : (gate.reasons || []).every((r) => /^review-(human|agent):/.test(r)) ? "info" : "warn"}`,
            text: gate.ship ? `gate: ${gate.action}` : "gate: a human decides" }) : null),
          kv([["action", ship.action], ["shipped", ship.shipped === undefined ? "" : String(ship.shipped)], ["target", ship.target],
            ["pull request", /^https?:\/\//.test(url) ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: url }) : ""],
            ["commit", ship.commit ? h("span", { class: "mono", text: String(ship.commit).slice(0, 12) }) : ""],
            ["gate reasons", gate.reasons && gate.reasons.length ? h("ul", { class: "issues" }, gate.reasons.map((r) => h("li", { class: /^review-(human|agent):/.test(r) ? "" : "sev-blocker", text: r }))) : (gate.action ? "none: every rule held" : "")],
            ["diff", gate.diff_lines !== undefined ? `${gate.diff_lines} lines in ${(gate.changed_files || []).length} files` : ""],
            ["changed", gate.changed_files && gate.changed_files.length ? h("span", { class: "mono", text: gate.changed_files.join("  ") }) : ""],
            ["proof", ship.proof ? `${ship.proof.attached || 0} file(s) attached${ship.proof.reason ? ` (${ship.proof.reason})` : ""}` : ""],
            ["note", ship.note]])));
      }
      if ((detail.proof || []).length) {
        // what each check proved: ran (passed / failed), not affected (why) or reused; text only
        const cls = { passed: "good", failed: "bad", "not-affected": "not-affected", reused: "reused" };
        const evidence = (r) => {
          if (r.outcome === "not-affected") {
            const paths = r.paths || [], total = r.paths_total ?? paths.length;
            return `${total} path${total === 1 ? "" : "s"} compared${paths.length ? `: ${paths.slice(0, 5).join(", ")}${total > Math.min(paths.length, 5) ? ` … and ${total - Math.min(paths.length, 5)} more` : ""}` : ""}`;
          }
          if (r.outcome === "reused") return `tree ${String(r.tree || "").slice(0, 8)}${r.from && r.from.log ? ` · log ${r.from.log}` : ""}`;
          return [r.tree ? `tree ${String(r.tree).slice(0, 8)}` : "", r.log ? `log ${r.log}` : ""].filter(Boolean).join(" · ");
        };
        blocks.push(h("div", { class: "outcome-block", "data-role": "proof" }, h("div", { class: "card-head" }, h("h3", { text: "What the checks proved" })),
          h("div", { class: "table-wrap" }, h("table", { class: "proof" },
            h("thead", {}, h("tr", {}, ["Check", "Where", "Result", "Evidence"].map((label) => h("th", { text: label })))),
            h("tbody", {}, detail.proof.map((r) => h("tr", { "data-outcome": r.outcome },
              h("td", { class: "mono", text: r.check }), h("td", { text: r.where }),
              h("td", {}, h("span", { class: `badge ${cls[r.outcome] || ""}`, text: checkResultText(r) })),
              h("td", { class: "dim", text: evidence(r) }))))))));
      }
      const review = detail.review;
      if (review && (review.rounds.length || review.mode)) {
        const block = h("div", { class: "outcome-block" }, h("div", { class: "card-head" }, h("h3", { text: "Review" }),
          review.mode ? h("span", { class: "badge", text: review.mode }) : null,
          review.success === true ? h("span", { class: "badge good", text: "approved" }) : review.success === false ? h("span", { class: "badge bad", text: "rejected" }) : null));
        if (!review.rounds.length && review.summary) block.appendChild(h("div", { class: "dim", text: review.summary }));
        block.append(...reviewRounds(review));
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
          if (at.outcome) {   // a decision: the check was not run (not affected, or its pass on the same tree reused)
            sources.push({ key: `${name}.decision.${at.attempt}`, kind: "check", label: `${name} ${at.outcome === "reused" ? "reused" : "not affected"}${at.phase ? " (re-check)" : ""}`,
              status: at.outcome, start: at.start || at.end, attempt: at, check: name });
            continue;
          }
          const status = at.running ? "running" : at.exit === 0 ? "passed" : at.exit === null || at.exit === undefined ? "done" : "failed";
          sources.push({ key: at.file, kind: "check", label: `${name} #${at.attempt}${at.phase ? " (re-check)" : ""}${at.running ? " (running)" : ""}`, status, start: at.start || at.end, attempt: at, check: name });
        }
      }
      const script = type === "script" && detail.scripts ? detail.scripts[step] : null;
      if (script) {
        const waits = !!script.waiting;
        sources.push({ key: script.log, kind: "script", label: `${step} (exit ${script.exit}${waits ? ", waiting" : ""})`,
          status: script.exit === 0 ? "passed" : (waits ? "waiting" : "failed"),
          start: (detail.steps_detail[step] || {}).started, script });
      }
      if (type === "ship") {
        for (const bar of detail.timeline.bars.filter((b) => b.kind === "gate")) {
          sources.push({ key: bar.ref, kind: "check", label: bar.label, status: bar.status, start: bar.start, check: bar.check,
            attempt: { file: bar.ref, exit: bar.exit, outcome: bar.outcome, why: bar.why, paths: bar.paths, paths_total: bar.paths_total, tree: bar.tree, from_step: bar.from_step } });
        }
      }
      const live = detail.timeline.bars.find((b) => b.level === 1 && b.kind === "check" && b.step === step && b.status === "running");
      if (live && !sources.some((src) => src.key === live.ref)) sources.push({ key: live.ref, kind: "check", label: `${live.label} (running)`, status: "running", start: live.start, attempt: { file: live.ref }, check: live.check, pending: true });
      sources.sort((a, b) => (a.start || 0) - (b.start || 0));
      return { node, type, sources };
    }

    function openStep() {
      // A log that is already streaming is kept when the panel is drawn again for the same file
      // (a running check that settles): emptying it and fetching it afresh would blank it meanwhile.
      const previous = sub;
      sub = null;
      try { renderStep(previous); } finally { if (previous && previous !== sub) previous.destroy(); }
    }

    // the inner steps of a script step that follows its own journal (script.event lines), with their documents and usage
    function innerSteps(step, sc) {
      const docs = (detail.documents || []).filter((d) => d.kind === "inner" && d.step === step);
      const usage = (u) => (u ? `${fmt.tokens(u.in)} in · ${fmt.tokens(u.out)} out${u.cost !== null && u.cost !== undefined ? ` · ${fmt.cost(u.cost)}` : ""}` : "");
      const subjectDocs = (subject) => docs.filter((d) => subject && d.name.split("/")[0] === subject);
      return h("details", { class: "section inner-steps", open: "" },
        h("summary", { text: `inner steps (${sc.inner.length})${sc.usage ? ` · ${usage(sc.usage)}` : ""}` }),
        h("table", { class: "inner" }, h("tbody", {}, sc.inner.map((e) => h("tr", {},
          h("td", { class: "num dim", text: fmt.clock(e.at) }), h("td", { class: "mono", text: e.kind || "—" }),
          h("td", { text: e.subject || "" }), h("td", { class: "num", text: usage(e.usage) }),
          h("td", {}, subjectDocs(e.fields && e.fields.step ? e.fields.step : e.subject).map((d) =>
            h("a", { class: "toggle", href: link(["documents"], { path: d.path }), text: d.name }))))))),
        docs.length ? h("div", { class: "inner-docs" }, h("span", { class: "dim", text: "documents: " }),
          docs.map((d) => h("a", { class: "toggle", href: link(["documents"], { path: d.path }), text: d.name }))) : null);
    }

    /** Each review round's verdicts, every reviewer's issues and the patch between rounds. */
    function reviewRounds(review) {
      const out = [];
      for (const round of review.rounds) {
        out.push(h("div", { class: "dim", text: `round ${round.round}: ${round.success ? "approved" : "not approved"}` }));
        for (const reviewer of round.reviewers) {
          const issues = [...reviewer.issues].sort((a, b) => ["blocker", "tech_debt", "skippable"].indexOf(a.severity) - ["blocker", "tech_debt", "skippable"].indexOf(b.severity));
          out.push(h("div", { class: "reviewer" },
            h("span", { class: reviewer.success ? "s-passed" : "s-failed", text: reviewer.success ? "✓" : "✗" }),
            h("div", {}, h("a", { href: link(["step", "review"], { agent: reviewer.dir }), class: "mono", text: reviewer.dir }), " ", h("span", { text: reviewer.summary }),
              issues.length ? h("ul", { class: "issues" }, issues.map((issue) => h("li", { class: `sev-${issue.severity}` },
                h("span", { class: `badge sev-${issue.severity}`, text: issue.severity }), " ", h("span", { class: "desc", text: issue.description }),
                issue.resolution ? h("div", { class: "res", text: `→ ${issue.resolution}` }) : null))) : null)));
        }
        if (round.patch) {
          out.push(h("div", { class: "reviewer" }, h("span", { class: "s-running", text: "↺" }),
            h("div", {}, "patch: ", h("a", { href: link(["step", "review"], { agent: round.patch.dirs[0] }), class: "mono", text: round.patch.dirs[0] }), " → ",
              h("a", { href: link(["step", "review"], { agent: round.patch.dirs[1] }), class: "mono", text: round.patch.dirs[1] }),
              round.patch.rerun.length ? ` → re-checked ${round.patch.rerun.join(", ")}` : "")));
        }
      }
      return out;
    }

    function renderStep(previous) {
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
        record.detail && !record.detail.trimStart().startsWith("{") ? h("span", { class: "dim title-cell", text: clip(record.detail, 160) }) : null);
      for (const doc of (detail.documents || []).filter((d) => d.step === step)) {
        headEl.appendChild(h("a", { class: "toggle", href: link(["documents"], { path: doc.path }), text: `document: ${doc.name}` }));
      }
      panel.appendChild(headEl);
      if (record.discipline) {
        const ok = record.discipline.ok;
        panel.appendChild(h("div", { class: `discipline ${ok ? "good" : "warn"}`, text: record.discipline.text }));
      }
      if (type === "review" && detail.review && detail.review.rounds.length) {
        panel.appendChild(h("div", { class: "step-rounds" }, reviewRounds(detail.review)));
      }
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
        const figures = h("span", { class: "dim num" }, `${agentLabel(agent)} · ${fmt.cost(agent.cost)} · ${agent.turns ?? "—"} turns · `,
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
      } else if (source.kind === "script") {
        const sc = source.script;
        content.appendChild(h("div", { class: "dim" }, "a script step: it runs the codebase's own command; nothing fixes it when it fails, and the gate does not count it as a check"));
        content.appendChild(h("dl", { class: "kv" }, [h("dt", { text: "command" }), h("dd", { class: "mono", text: sc.cmd || "" }),
          h("dt", { text: "arguments" }), h("dd", { class: "mono", text: (sc.args || []).join(" ") || "(none)" }),
          h("dt", { text: "exit" }), h("dd", { class: "num", text: String(sc.exit) + (sc.waiting ? " (waiting: --resume runs it again)" : "") }),
          sc.journal && sc.journal.path ? h("dt", { text: "journal" }) : null,
          sc.journal && sc.journal.path ? h("dd", { class: "mono", text: `${sc.journal.path} · ${sc.journal.found ? `${sc.journal.lines} line(s)` : "not found"}`
            + `${sc.journal.malformed ? ` · ${sc.journal.malformed} malformed` : ""}${sc.journal.refused ? ` · refused: ${sc.journal.refused}` : ""}` }) : null]));
        if ((sc.inner || []).length) content.appendChild(innerSteps(step, sc));
        const block = h("div", { class: "textblock script-log" }, h("pre", { text: "loading…" }));
        content.appendChild(block);
        fetch(`/files/${enc(layerKey)}/${sc.log.split("/").map(enc).join("/")}`, { credentials: "same-origin" })
          .then((r) => (r.ok ? r.text() : `(log not available: ${r.status})`)).then((text) => replace(block, h("pre", { text })));
      } else {
        const at = source.attempt || {};
        const cmd = detail.check_cmds[source.check] || "";
        const resolveAfter = detail.agents.find((a) => a.dir === `${source.check}-resolve-${at.attempt}`);
        content.appendChild(h("div", { class: "toolbar" },
          h("span", { class: "mono", text: cmd ? `$ ${cmd}` : source.label }),
          at.exit !== undefined && at.exit !== null ? h("span", { class: `badge ${at.exit === 0 ? "good" : "bad"}`, text: at.exit === "fail" ? "exit ≠ 0" : `exit ${at.exit}` }) : null,
          at.outcome ? h("span", { class: `badge ${at.outcome}`, "data-role": "decision-badge", text: at.outcome === "reused" ? "reused" : "not affected" }) : null,
          at.skipped ? h("span", { class: "badge warn", "data-role": "skips", text: `${at.skipped} skipped`,
            title: (at.skip_reasons || []).map(([reason, count]) => `${reason} ×${count}`).join("\n") }) : null,
          at.env ? h("span", { class: "badge", text: `in ${at.env}` }) : null,
          source.pending ? h("span", { class: "badge info" }, "running for ", h("span", { class: "num", "data-since": source.start, text: fmt.dur(now() - source.start) }),
            " · this run's runtime writes the log when the command finishes") : null,
          at.running ? h("span", { class: "badge info", "data-role": "live-check" }, "running for ", h("span", { class: "num", "data-since": source.start, text: fmt.dur(now() - source.start) }),
            " · following the log as it is written") : null,
          resolveAfter ? h("a", { href: link(["step", step], { agent: resolveAfter.dir }), class: "toggle", text: `→ resolved by ${resolveAfter.dir}` }) : null,
          h("span", { class: "spacer" }),
          !source.pending && at.file ? h("a", { class: "toggle", href: `/files/${enc(layerKey)}/agents/${enc(runId)}/checks/${enc(at.file)}?download=1`, text: "download log" }) : null));
        if (at.outcome) {
          // why the command was not run: text only, with the paths that were compared
          const paths = at.paths || [], total = at.paths_total ?? paths.length;
          content.appendChild(h("div", { class: "decision", "data-role": "decision" },
            h("div", { class: "decision-what", text: at.outcome === "reused"
              ? `reused from ${at.from_step || "an earlier run"}, tree ${String(at.tree || "").slice(0, 8)}: the command was not run again`
              : `not affected: ${at.why || "no covered path changed"}` }),
            at.outcome !== "reused" || paths.length ? h("div", { class: "dim", text: `${total} path${total === 1 ? "" : "s"} compared${paths.length ? ":" : ""}` }) : null,
            paths.length ? h("pre", { class: "decision-paths", text: paths.join("\n") + (total > paths.length ? `\n… and ${total - paths.length} more` : "") }) : null,
            at.outcome === "reused" && at.file ? h("div", { class: "dim", text: "the log below is the log of the run that passed on this tree" }) : null));
        }
        if (!source.pending && at.file) {
          const opts = { layer: layerKey, run: runId, what: "check", file: at.file, running: !!at.running, label: source.label };
          sub = previous instanceof LogView && previous.shows(opts) ? previous.mount(content) : new LogView(content, opts);
        }
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
      const rank = { image: 0, video: 1, audio: 2, html: 3, junit: 4, coverage: 5, trace: 6, log: 8 };
      const ordered = [...groups].sort((a, b) => (rank[a[1][0].type] ?? 7) - (rank[b[1][0].type] ?? 7));
      for (const [id, items] of ordered) {
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

    // ------------------------------------------------------------------ documents tab
    let docCache = null;   // { path, mtime, data }: the open document, kept across reloads while its file is unchanged

    function docQuery() { return `layer=${enc(layerKey)}&run=${enc(runId)}`; }

    function renderDocuments() {
      for (const child of [...body.children]) child.remove();
      const docs = detail.documents || [];
      const steps = new Set(detail.graph.nodes.map((n) => n.step));
      const card = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Documents" }), h("span", { class: "spacer" }),
        docs.length ? h("a", { class: "toggle", href: `/api/doc/zip?${docQuery()}`, download: "", text: "Download all (.zip)" }) : null));
      body.appendChild(card);
      if (!docs.length) {
        card.appendChild(h("div", { class: "empty", text: "This run has produced no documents yet." }));
      } else {
        card.appendChild(h("table", { class: "docs" },
          h("thead", {}, h("tr", {}, ["name", "step", "size", "time", ""].map((label) => h("th", { text: label })))),
          h("tbody", {}, docs.map((d) => h("tr", { class: d.path === state.params.path ? "selected" : "", dataset: { path: d.path } },
            h("td", {}, h("div", { class: "doc-name", text: d.name }), h("div", { class: "faint mono", text: d.path })),
            h("td", {}, d.step && steps.has(d.step) ? h("a", { href: link(["step", d.step]), text: d.step }) : h("span", { class: "dim", text: d.step || "—" })),
            h("td", { class: "num", text: fmt.bytes(d.bytes) }),
            h("td", { class: "num", text: fmt.time(d.mtime) }),
            h("td", { class: "actions" },
              h("button", { type: "button", class: "toggle", "data-role": "view", text: "View", onclick: () => go(link(["documents"], { path: d.path }), true) }),
              h("a", { class: "toggle", "data-role": "download", href: `/api/doc/download?${docQuery()}&path=${enc(d.path)}`, download: d.filename, text: "Download" })))))));
      }
      const path = state.params.path;
      if (!path) return;
      const entry = docs.find((d) => d.path === path);
      const view = h("div", { class: "card doc-view" });
      body.appendChild(view);
      if (!entry) { view.appendChild(h("div", { class: "empty", text: `${path} is not a document of this run (yet).` })); return; }
      if (docCache && docCache.path === path && docCache.mtime === entry.mtime) { showDocument(view, entry, docCache.data); return; }
      view.appendChild(h("div", { class: "dim", text: "loading…" }));
      api(`/api/doc?${docQuery()}&path=${enc(path)}`).then((data) => {
        docCache = { path, mtime: data.mtime, data };
        if (view.isConnected) showDocument(view, entry, data);
      }, (error) => replace(view, h("div", { class: "empty", text: `Could not load ${path}: ${error.message}` })));
    }

    function showDocument(view, entry, data) {
      const raw = !!state.params.raw;
      const markdown = data.type === "markdown" && data.viewable;
      let content;
      if (data.too_large) content = h("div", { class: "empty", text: `too large to show here (${fmt.bytes(data.bytes)}); download it instead` });
      else if (!data.viewable) content = h("div", { class: "empty", text: "download only" });
      else if (markdown && !raw) content = h("div", { class: "md" }, renderMarkdown(data.text));
      else if (data.type === "json") {
        let pretty = data.text;
        try { pretty = JSON.stringify(JSON.parse(data.text), null, 2); } catch (_error) { /* shown as written */ }
        content = h("pre", { text: pretty });
      } else content = h("pre", { class: "doc-raw", text: data.text });
      replace(view,
        h("div", { class: "card-head" }, h("h2", { text: entry.name }), h("span", { class: "badge", text: entry.kind }),
          entry.step ? h("span", { class: "dim", text: `step ${entry.step}` }) : null,
          h("span", { class: "dim num", text: `${fmt.bytes(entry.bytes)} · ${fmt.time(entry.mtime)}` }), h("span", { class: "spacer" }),
          markdown ? h("button", { type: "button", class: "toggle", "data-role": "raw-toggle", text: raw ? "rendered" : "raw",
            onclick: () => go(link(["documents"], { path: entry.path, raw: raw ? "" : "1" }), true) }) : null,
          h("a", { class: "toggle", href: `/api/doc/download?${docQuery()}&path=${enc(entry.path)}`, download: entry.filename, text: "Download" }),
          h("a", { class: "toggle", href: link(["documents"]), text: "close" })),
        content);
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
      const tabChanged = tab !== state.tab && !(tab === "step" && state.tab === "overview") && !(tab === "overview" && state.tab === "step");
      state = { ...state, tab, step: tab === "step" ? parts[4] : "", params: route.params };
      if (sameStep) return true;
      if (tabChanged) window.scrollTo(0, 0);
      if (sub) { sub.destroy(); sub = null; }
      if (!detail) return true;
      renderTabs();
      renderGraph_();
      if (tab === "overview") renderOverview();
      else if (tab === "step") { state.sourceSig = sourcesFor(state.step).sources.map((src) => `${src.key}:${src.status}`).join("|"); openStep(); }
      else if (tab === "artefacts") renderArtefacts();
      else if (tab === "documents") renderDocuments();
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
    load().then(() => {
      if (state.tab === "step") { state.sourceSig = sourcesFor(state.step).sources.map((src) => `${src.key}:${src.status}`).join("|"); openStep(); }
      else if (state.tab === "log" && !sub) renderLog();
    });

    return {
      accepts: (r) => r.parts[1] === layerKey && r.parts[2] === runId,
      route: (r) => apply(r),
      destroy() { gone = true; off(); clearInterval(timer); window.removeEventListener("resize", onResize); if (sub) sub.destroy(); },
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

  // =========================================================================== components: start work
  /** A request key: the same key never starts a second run (a double click, a retried request). */
  function requestKey() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  /** Ask before something happens that a person should agree to; resolves true for "start anyway". */
  function confirmDialog(title, message, action) {
    return new Promise((resolve) => {
      const overlay = document.getElementById("overlay");
      let done = false;
      let watch = null;
      const finish = (answer) => {
        if (done) return;
        done = true;
        if (watch) watch.disconnect();
        overlay.hidden = true;
        clear(overlay);
        overlay.onclick = null;
        resolve(answer);
      };
      const go = h("button", { class: "btn primary confirm-go", type: "button", text: action, onclick: () => finish(true) });
      const cancel = h("button", { class: "btn confirm-cancel", type: "button", text: "Cancel", onclick: () => finish(false) });
      replace(overlay, h("div", { class: "panel start-confirm", role: "dialog", "aria-modal": "true", "aria-label": title,
        onclick: (e) => e.stopPropagation() },
        h("h3", { text: title }), h("p", { class: "confirm-message", text: message }), h("div", { class: "confirm-buttons" }, cancel, go)));
      overlay.hidden = false;
      overlay.onclick = () => finish(false);
      // Esc (the page's own key handler) or anything else that hides the overlay is a "Cancel"
      watch = new MutationObserver(() => { if (overlay.hidden) finish(false); });
      watch.observe(overlay, { attributes: true, attributeFilter: ["hidden"] });
      go.focus();
    });
  }

  /** The "Start work" panel of one layer: a prompt, any workflow or command of the layer (also as
   * `/name` with completion), class and tiers, optionally an issue; then the new run. */
  function startPanel(layer) {
    const status = layer.start || { enabled: false, why: "" };
    const el = h("div", { class: "start-panel card", dataset: { layer: layer.key } },
      h("div", { class: "card-head" }, h("h3", { text: "Start work" }), h("span", { class: "dim", text: layer.name || layer.key })));
    if (!status.enabled) {
      el.appendChild(h("div", { class: "why start-off", text: status.why || "starting work is off" }));
      return el;
    }
    const body = h("div", { class: "start-body" }, h("div", { class: "empty", text: "loading…" }));
    el.appendChild(body);
    let key = requestKey();
    api(`/api/startable?layer=${enc(layer.key)}`).then((catalog) => build(catalog)).catch((err) => {
      replace(body, h("div", { class: "why", text: `cannot load what this layer can start: ${err.message || err}` }));
    });

    function build(catalog) {
      const entries = [
        ...catalog.workflows.map((w) => ({ kind: "workflow", name: w.name, summary: w.summary, description: w.description, item: w })),
        ...catalog.commands.map((c) => ({ kind: "command", name: c.name, summary: c.summary, description: c.description, item: c })),
      ];
      const valueOf = (e) => `${e.kind}:${e.name}`;
      const byValue = new Map(entries.map((e) => [valueOf(e), e]));
      const prompt = h("textarea", { class: "start-prompt", rows: 4, "aria-label": "what to do",
        placeholder: "Describe the work. Type / to pick a workflow or a single command, e.g. /plan-chore tidy the readme" });
      const select = h("select", { class: "start-target", "aria-label": "workflow or command" },
        h("optgroup", { label: "Workflows" }, catalog.workflows.map((w) => h("option", { value: `workflow:${w.name}`, text: `/${w.name}` }))),
        catalog.commands.length ? h("optgroup", { label: "Single command (one step, never ships)" },
          catalog.commands.map((c) => h("option", { value: `command:${c.name}`,
            text: `/${catalog.collisions.includes(c.name) ? "command:" : ""}${c.name} ${c.argument_hint || ""}`.trim() }))) : null);
      const list = h("ul", { class: "start-complete", role: "listbox", hidden: true, "aria-label": "workflows and commands" });
      const summary = h("div", { class: "start-summary" });
      const klass = h("select", { class: "start-class", "aria-label": "class" },
        h("option", { value: "", text: "let the workflow classify" }), (catalog.classes || ["chore", "bug", "feature"]).map((c) => h("option", { value: c, text: c })));
      const tier = h("select", { class: "start-tier", "aria-label": "tier for every step" },
        h("option", { value: "", text: "each step's own tier" }), (catalog.tiers || []).map((t) => h("option", { value: t, text: `every step on ${t}` })));
      const modelInputs = (catalog.tiers || []).map((t) => h("input", { type: "text", class: "mono start-model", dataset: { tier: t },
        placeholder: `${t}: model id (optional)`, "aria-label": `model id for tier ${t}` }));
      const issue = h("select", { class: "start-issue", "aria-label": "log as an issue" });
      const issueRow = h("label", { class: "start-row", hidden: !catalog.issue.available }, h("span", { text: "issue" }), issue);
      const classRow = h("label", { class: "start-row" }, h("span", { text: "class" }), klass);
      const unattended = h("div", { class: "start-unattended" });
      const note = h("div", { class: "start-note faint" });
      const go = h("button", { class: "btn primary start-go", type: "button", text: "Start" });
      let active = -1;
      let shown = [];

      const chosen = () => byValue.get(select.value) || entries[0];
      const typed = () => {
        const m = /^\s*\/(\S*)/.exec(prompt.value);
        return m ? m[1] : null;
      };
      function describe() {
        const e = chosen();
        if (!e) return;
        summary.textContent = e.summary + (e.description ? ` — ${e.description}` : "");
        classRow.hidden = e.kind !== "workflow";
        const options = [["none", "do not log it as an issue"], ["start", "log it as an issue and start now"]];
        if (e.kind === "workflow" && catalog.issue.trigger.includes(e.name)) options.push(["trigger", "create the issue and let the trigger start it"]);
        const previous = issue.value;
        replace(issue, options.map(([value, text]) => h("option", { value, text })));
        if (options.some(([value]) => value === previous)) issue.value = previous;
        const ships = e.kind === "workflow" ? e.item.ships_unattended : "";
        unattended.className = `start-unattended${ships ? " warn" : ""}`;
        unattended.textContent = e.kind === "command"
          ? "One step in the layer's normal environment. It never merges, deploys or opens a pull request."
          : ships ? `Without a person, this workflow ${ships === "merge" ? "merges into the main branch" : "deploys"} when every gate passes; you will be asked to confirm.`
            : "Nothing is merged or deployed without a person: the work ends in a review for you.";
      }
      function closeList() { list.hidden = true; active = -1; shown = []; prompt.setAttribute("aria-expanded", "false"); }
      function pick(e) {
        const label = e.kind === "command" && catalog.collisions.includes(e.name) ? `command:${e.name}` : e.name;
        prompt.value = prompt.value.replace(/^\s*\/\S*\s*/, `/${label} `);
        select.value = valueOf(e);
        closeList();
        describe();
        prompt.focus();
        prompt.setSelectionRange(prompt.value.length, prompt.value.length);
      }
      function complete() {
        const word = typed();
        const caret = prompt.selectionStart || 0;
        if (word === null || caret > prompt.value.indexOf("/") + 1 + word.length) return closeList();
        const forced = word.startsWith("command:");
        const stem = forced ? word.slice(8) : word;
        shown = entries.filter((e) => e.name.startsWith(stem) && (!forced || e.kind === "command")).slice(0, 12);
        if (!shown.length) return closeList();
        active = Math.min(Math.max(active, 0), shown.length - 1);
        replace(list, shown.map((e, i) => h("li", { role: "option", class: i === active ? "active" : "", "aria-selected": i === active ? "true" : "false",
          dataset: { value: valueOf(e) }, onmousedown: (ev) => { ev.preventDefault(); pick(e); } },
          h("span", { class: "mono", text: `/${e.name}` }), h("span", { class: "dim", text: e.kind === "workflow" ? " workflow" : ` ${e.item.kind}` }),
          h("span", { class: "faint", text: ` — ${e.summary}` }))));
        list.hidden = false;
        prompt.setAttribute("aria-expanded", "true");
      }
      function syncFromText() {
        const word = typed();
        if (word === null) return;
        const forced = word.startsWith("command:");
        const name = forced ? word.slice(8) : word;
        const match = (!forced && byValue.get(`workflow:${name}`)) || byValue.get(`command:${name}`);
        if (match && select.value !== valueOf(match)) { select.value = valueOf(match); describe(); }
      }
      prompt.addEventListener("input", () => { active = 0; complete(); syncFromText(); });
      prompt.addEventListener("blur", () => setTimeout(closeList, 100));
      prompt.addEventListener("keydown", (ev) => {
        if (list.hidden) {
          if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); submit(); }
          return;
        }
        if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
          ev.preventDefault();
          active = (active + (ev.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length;
          complete();
        } else if (ev.key === "Enter" || ev.key === "Tab") {
          ev.preventDefault();
          if (shown[active]) pick(shown[active]);
        } else if (ev.key === "Escape") {
          ev.preventDefault();
          ev.stopPropagation();
          closeList();
        }
      });
      select.addEventListener("change", describe);

      function request(extra) {
        const e = chosen();
        const models = {};
        for (const input of modelInputs) if (input.value.trim()) models[input.dataset.tier] = input.value.trim();
        const out = { layer: layer.key, request_key: key, issue: issue.value || "none", model_set: tier.value || null, models, ...extra };
        if (e.kind === "workflow" && klass.value) out.class = klass.value;
        if (typed() !== null) out.text = prompt.value;
        else { out.target = { kind: e.kind, name: e.name }; out.prompt = prompt.value; }
        return out;
      }
      async function submit(extra = {}) {
        go.disabled = true;
        note.textContent = "";
        note.classList.remove("error");
        try {
          const answer = await api.post("/api/start", request(extra));
          key = requestKey();   // a new request from now on; the same one never starts twice
          if (answer.run_id) {
            note.textContent = `run ${answer.run_id} started${answer.queued ? `; ${answer.queued}` : ""}`;
            location.hash = answer.url;
            return;
          }
          const issueUrl = answer.issue && answer.issue.url;
          note.textContent = `${answer.message || "issue created"}${issueUrl ? `: ${issueUrl}` : ""}`;
        } catch (err) {
          const data = err.data || {};
          if (err.status === 409 && data.confirm) {
            const title = data.confirm === "ships" ? "This workflow ships without a person" : "Start the same request again?";
            const yes = await confirmDialog(title, data.message || err.message, data.confirm === "ships" ? "Start anyway" : "Start again");
            if (yes) return submit({ ...extra, [`confirm_${data.confirm}`]: true });
            note.textContent = "not started";
          } else {
            note.textContent = String(err.message || err);
            note.classList.add("error");
          }
        } finally {
          go.disabled = false;
        }
      }
      go.addEventListener("click", () => submit());
      replace(body,
        h("div", { class: "start-input" }, prompt, list),
        h("div", { class: "start-controls" },
          h("label", { class: "start-row" }, h("span", { text: "run" }), select),
          classRow,
          h("label", { class: "start-row" }, h("span", { text: "tier" }), tier),
          issueRow),
        modelInputs.length ? h("div", { class: "start-models" }, modelInputs) : null,
        summary, unattended,
        h("div", { class: "start-actions" }, go, h("span", { class: "faint", text: "Ctrl+Enter" }), note));
      describe();
    }
    return el;
  }

  // =========================================================================== views: Layers
  function LayersView(main, crumbs) {
    crumbs.textContent = "watched layers";
    const startHost = h("div", { class: "start-host" });
    const grid = h("div", { class: "layer-grid" });
    main.append(startHost, grid);
    let startShown = "";
    let startChoice = "";
    /** The panel is rebuilt only when its layer or whether it may start changes: typing is kept. */
    function renderStart() {
      if (!store.layers.length) { clear(startHost); startShown = ""; return; }
      if (!store.layers.some((l) => l.key === startChoice)) startChoice = store.layers[0].key;
      const layer = store.layer(startChoice);
      const sig = JSON.stringify([store.layers.map((l) => l.key), startChoice, layer.start]);
      if (sig === startShown) return;
      startShown = sig;
      const chooser = store.layers.length > 1 ? h("label", { class: "start-row start-layer" }, h("span", { text: "layer" }),
        h("select", { "aria-label": "layer to start work in", onchange: (e) => { startChoice = e.target.value; renderStart(); } },
          store.layers.map((l) => h("option", { value: l.key, text: l.name || l.key, selected: l.key === startChoice })))) : null;
      replace(startHost, chooser, startPanel(layer));
    }
    function render() {
      renderStart();
      clear(grid);
      for (const layer of store.layers) {
        const runs = store.runList().filter((r) => r.layer === layer.key);
        const finished = runs.filter((r) => r.status === "passed" || r.status === "failed" || r.status === "handed-over");
        const passed = finished.filter((r) => r.status === "passed").length;
        const handedOver = finished.filter((r) => r.status === "handed-over").length;
        const cost = runs.reduce((sum, r) => sum + (r.cost || 0), 0);
        grid.appendChild(h("div", { class: "card layer-card", tabindex: 0, onclick: () => go(`#/layer/${enc(layer.key)}`) },
          h("div", { class: "card-head" }, h("h3", { text: layer.name }), layer.key !== layer.name ? h("span", { class: "dim mono", text: layer.key }) : null,
            h("span", { class: "spacer" }), layer.running ? h("span", { class: "badge info", text: `${layer.running} running` }) : null),
          h("div", { class: "dim mono", text: layer.dir }), layer.description ? h("div", { text: layer.description }) : null,
          h("div", { class: "adw-stats" }, h("span", {}, h("b", { text: runs.length }), " runs"), h("span", {}, h("b", { text: fmt.pct(finished.length ? passed / finished.length : null) }), " passed"),
            handedOver ? h("span", {}, h("b", { text: handedOver }), " handed over") : null,
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
        (data.promotions || []).length ? h("div", { class: "card promotions-section" },
          h("div", { class: "card-head" }, h("h3", { text: "Promotions" }), h("span", { class: "dim", text: "a stage moves into the next once people accept it" }),
            h("span", { class: "spacer" }), h("span", { class: "promotion-note", role: "status" })),
          data.promotions.map((p) => {
            const note = h("span", { class: "faint" });
            const start = async (dry) => {
              try {
                const answer = await api.post("/api/promotion", { layer: key, action: p.open ? "continue" : "start", from: p.from, to: p.to, dry_run: dry });
                go(answer.url);
              } catch (error) { replace(note, h("span", { class: "badge warn", text: error.message })); }
            };
            const enabled = data.promote && data.promote.available && data.promote.start.enabled;
            const why = !data.promote || !data.promote.available ? "run `al gen` to add adws/promote.py" : data.promote.start.why;
            return h("div", { class: "promotion-row" },
              h("b", { text: `${p.from} → ${p.to}` }), h("span", { class: "badge", text: `${p.approval}${p.approval !== "auto" ? ` × ${p.approvers}` : ""} · ${p.method}` }),
              p.hotfix ? h("span", { class: "badge warn", text: "hotfix" }) : null, p.tag ? h("span", { class: "badge", text: `tag ${p.tag}` }) : null,
              p.open ? h("a", { class: "badge waiting", href: `#/run/${enc(key)}/${enc(p.open.id)}`, text: `open: ${p.open.id}` }) : null,
              h("span", { class: "spacer" }),
              h("button", { type: "button", class: "toggle", disabled: !enabled, title: enabled ? "" : why, text: p.open ? `Continue ${p.from} → ${p.to}` : `Promote ${p.from} → ${p.to}`, onclick: () => start(false) }),
              h("button", { type: "button", class: "toggle", disabled: !enabled, title: enabled ? "build the pack and say what would happen; changes nothing" : why, text: "Dry run", onclick: () => start(true) }),
              note);
          })) : null,
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

  // =========================================================================== views: Repos (the workspace) and Job
  const STATE_TITLE = { layer: "has an al layer (in the repository or a sibling NAME.al)",
    setup: "has an agent setup al can adopt (rule files, prompts, workflow scripts or a hand-built suite)",
    none: "no layer and no agent setup found" };

  function stateBadge(state) {
    return h("span", { class: `badge badge-${state || "none"}`, title: STATE_TITLE[state] || "", text: state || "none" });
  }

  /** A unified diff as one div per line, classed by its kind; every line is text. */
  function renderDiff(text) {
    return h("div", { class: "diff mono" }, String(text || "").split("\n").map((line) => {
      const kind = line.startsWith("+++") || line.startsWith("---") ? "diff-file" : line.startsWith("@@") ? "diff-hunk"
        : line.startsWith("+") ? "diff-add" : line.startsWith("-") ? "diff-del" : "";
      return h("div", { class: kind || null, text: line || " " });
    }));
  }

  /** A job's document as the run documents are shown: Markdown rendered as text nodes, JSON pretty, a diff by line. */
  function jobDocument(name, data) {
    if (data.too_large) return h("div", { class: "empty", text: `too large to show here (${fmt.bytes(data.bytes)})` });
    if (name.endsWith(".diff.txt")) return renderDiff(data.text);
    if (name.endsWith(".md")) return h("div", { class: "md" }, renderMarkdown(data.text));
    if (name.endsWith(".json")) {
      let pretty = data.text;
      try { pretty = JSON.stringify(JSON.parse(data.text), null, 2); } catch (_error) { /* shown as written */ }
      return h("pre", { text: pretty });
    }
    return h("pre", { class: "doc-raw", text: data.text });
  }

  async function waitForJob(id, until, timeoutMs = 120000) {
    const end = Date.now() + timeoutMs;
    for (;;) {
      const job = await api(`/api/job?id=${enc(id)}`);
      if (until(job) || Date.now() > end) return job;
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
  }

  const CHOICE_LABEL = { forge: "forge", host: "forge host", repo: "repository", input: "work comes from", trigger: "started by",
    environment: "environment", review: "review", delivery: "delivery preset" };

  /** The detected choices of a create or adopt (choices.json of the preview job): one row per choice with
   * its value (a select, or a text field for host and repository) and the fact that supports it. A row the
   * person changes is marked "changed"; only changed choices are sent with the confirmation. */
  function choicesForm(choices) {
    const rows = [];
    const form = h("div", { class: "choices" });
    for (const [key, choice] of Object.entries(choices || {})) {
      const options = choice.options || [];
      const given = String(choice.value || "");
      // a list in the options' order, as the checkboxes report it, so an untouched one is unchanged
      const detected = choice.multi ? options.filter((o) => given.split(",").includes(o)).join(",") : given;
      const mark = h("span", { class: "badge warn changed-mark", text: "changed", hidden: true });
      let field;
      if (!options.length) {
        field = h("input", { type: "text", class: "mono", value: detected, placeholder: key === "host" ? "https://forge.example.org" : "owner/name",
          "aria-label": CHOICE_LABEL[key] || key, dataset: { choice: key } });
      } else if (choice.multi) {   // a list: one checkbox per option
        const chosen = new Set(detected.split(","));
        field = h("span", { class: "multi", role: "group", "aria-label": CHOICE_LABEL[key] || key, dataset: { choice: key } },
          options.map((value) => h("label", {}, h("input", { type: "checkbox", value, checked: chosen.has(value) }), ` ${value}`)));
      } else {
        field = h("select", { "aria-label": CHOICE_LABEL[key] || key, dataset: { choice: key } },
          options.map((value) => h("option", { value, text: value, selected: value === detected })));
      }
      const current = () => (choice.multi && options.length
        ? Array.from(field.querySelectorAll("input:checked")).map((box) => box.value).join(",") : field.value.trim());
      const row = h("div", { class: "choice", dataset: { key } },
        h("label", { class: "choice-key", text: CHOICE_LABEL[key] || key }), field, mark,
        h("div", { class: "fact", text: choice.fact || "" }));
      const update = () => {
        const changed = current() !== detected;
        row.classList.toggle("changed", changed);
        mark.hidden = !changed;
      };
      field.addEventListener("change", update);
      field.addEventListener("input", update);
      rows.push({ key, detected, current });
      form.appendChild(row);
    }
    return { el: form, changed: () => Object.fromEntries(rows.filter((r) => r.current() !== r.detected).map((r) => [r.key, r.current()])) };
  }

  /** The confirmation of a writing action: its command lines, what it writes, (adopt) the dry run's diff and
   * (create, adopt) the detected choices, each editable. */
  function confirmAction(repo, action, preview, done) {
    const overlay = document.getElementById("overlay");
    const close = () => { overlay.hidden = true; clear(overlay); };
    const status = h("div", { class: "dim", "data-role": "preview-status" });
    const diff = h("div", { class: "preview-diff" });
    const diffPart = h("div", { hidden: true }, h("h4", { text: "What the dry run would write" }), diff);
    const choicesSlot = h("div", { class: "choices-slot" });
    const choicesPart = h("div", { hidden: true }, h("h4", { text: "Detected choices" }),
      h("div", { class: "dim", text: "what al author will declare, each with the fact that supports it; change any before you confirm" }), choicesSlot);
    let form = null;
    const confirm = h("button", { type: "button", class: "btn primary", "data-role": "confirm", text: "Confirm", disabled: !!preview.job });
    const cancel = h("button", { type: "button", class: "btn", "data-role": "cancel", text: "Cancel", onclick: close });
    confirm.addEventListener("click", async () => {
      confirm.disabled = true;
      try {
        const body = { repo: repo.key, action: action.name, confirm: preview.nonce };
        const changed = form ? form.changed() : {};
        if (Object.keys(changed).length) body.choices = changed;
        const answer = await api.post("/api/action", body);
        close();
        done(answer.job);
      } catch (error) {
        status.className = "why error";
        status.textContent = error.message;
        confirm.disabled = false;
      }
    });
    replace(overlay, h("div", { class: "panel action-confirm", onclick: (e) => e.stopPropagation() },
      h("div", { class: "card-head" }, h("h3", { text: `${action.name} · ${repo.name}` }), h("span", { class: "spacer" }), cancel),
      h("div", { class: "dim", text: action.description }),
      h("h4", { text: "It runs" }), h("div", { class: "commands" }, preview.commands.map((line) => h("code", { class: "cmd", text: line }))),
      h("h4", { text: "It writes" }), h("ul", { class: "writes" }, (preview.writes.length ? preview.writes : ["nothing outside the jobs directory"]).map((path) => h("li", { class: "mono", text: path }))),
      choicesPart, diffPart,
      status, h("div", { class: "actions-row" }, confirm, cancel)));
    overlay.hidden = false;
    overlay.onclick = close;
    if (preview.job) {
      status.textContent = action.name === "adopt" ? "running the dry run and detecting choices…" : "detecting choices…";
      waitForJob(preview.job, (job) => job.status !== "running").then(async (job) => {
        if (job.status !== "passed") {
          status.className = "why error";
          status.textContent = `the preview ${job.status} (exit ${job.exit}); see job ${job.id}`;
          return;
        }
        const names = job.steps.map((step) => step.name);
        if (names.includes("adopt-dry-run")) {
          const doc = await api(`/api/job/doc?id=${enc(job.id)}&name=preview.diff.txt`);
          replace(diff, renderDiff(doc.text));
          diffPart.hidden = false;
        }
        if (names.includes("choices")) {
          const doc = await api(`/api/job/doc?id=${enc(job.id)}&name=choices.json`);
          form = choicesForm(JSON.parse(doc.text));
          replace(choicesSlot, form.el);
          choicesPart.hidden = false;
        }
        status.textContent = "";
        confirm.disabled = false;
      }).catch((error) => { status.className = "why error"; status.textContent = error.message; });
    }
  }

  /** The rows of a create or adopt job's result card (result.json of its `result` step, read by the server
   * and shown inside the passed outcome card): [label, data-role, value] for the forge, the workflows, what
   * was adopted and left behind, the commands that failed or did not run, and the next steps. Its paths
   * are already shown as the job shows them (the server shortens them). */
  function resultRows(card) {
    const forge = card.forge || {};
    const adopted = card.adopted || {};
    const workflows = card.workflows || [];
    const unverified = card.unverified || [];
    return {
      forge: ["forge", "forge", h("code", { class: "mono", text: forge.clause || forge.kind || "(forge none)" })],
      workflows: ["workflows", "workflows", workflows.length ? workflows.map((name) => h("span", { class: "badge", text: name })) : "none"],
      adopted: ["adopted", "adopted", `${(adopted.rules || []).length} rule file(s), ${(adopted.commands || []).length} command(s), ${(adopted.suite || []).length} suite workflow(s)`],
      leftBehind: ["left behind", "left-behind", (card.left_behind || []).length ? card.left_behind.join(", ") : "nothing"],
      commands: ["unverified commands", "unverified-commands", unverified.length
        ? h("ul", {}, unverified.map((u) => h("li", {}, h("span", { class: "mono", text: u.name }), ` ${u.result}`))) : "none"],
      next: ["next", "next", h("ol", { class: "next" }, (card.next || []).map((step) => h("li", { class: "mono", text: step })))],
    };
  }

  function ReposView(main, crumbs, route) {
    const filter = h("input", { type: "search", class: "repo-filter", placeholder: "filter repositories  /", "aria-label": "filter repositories" });
    const list = h("div", { class: "repo-list", role: "listbox", "aria-label": "repositories" });
    const panel = h("div", { class: "repo-panel" });
    const refresh = h("button", { type: "button", class: "toggle", "data-role": "refresh", text: "refresh", title: "rescan the workspace" });
    main.append(h("div", { class: "repos" }, h("div", { class: "repo-side" }, h("div", { class: "repo-tools" }, filter, refresh), list), panel));
    let selected = route.view === "repo" ? route.parts[1] || "" : "";
    let loaded = null;

    function setCrumbs() {
      const roots = (store.workspace.roots || []).join(", ");
      replace(crumbs, h("a", { href: "#/repos", text: "repos" }), roots ? h("span", { class: "dim", text: ` in ${roots}` }) : null,
        selected ? [" / ", h("span", { text: selected })] : null);
    }

    function renderList() {
      clear(list);
      const ws = store.workspace;
      if (!ws.enabled) {
        list.appendChild(h("div", { class: "empty", text: store.loaded ? "No workspace: start with --workspace DIR, or with --layer for a layer whose repository sits in a folder of repositories." : "connecting…" }));
        return;
      }
      const q = filter.value.trim().toLowerCase();
      const repos = ws.repos.filter((r) => !q || r.name.toLowerCase().includes(q) || r.state.includes(q));
      for (const repo of repos) {
        list.appendChild(h("a", { class: `repo-row${repo.key === selected ? " selected" : ""}`, href: `#/repo/${enc(repo.key)}`, role: "option",
          dataset: { repo: repo.key }, "aria-selected": repo.key === selected ? "true" : "false" },
          h("span", { class: "repo-name", text: repo.name }), stateBadge(repo.state),
          repo.running_job ? h("span", { class: "pill s-running", text: "running" }) : null,
          h("span", { class: "spacer" }),
          h("span", { class: "mono dim", text: repo.branch || "" }), repo.dirty ? h("span", { class: "dirty", title: "uncommitted changes", text: "●" }) : null,
          h("span", { class: "faint", text: fmt.ago(repo.last_activity) })));
      }
      if (!repos.length) list.appendChild(h("div", { class: "empty", text: q ? `no repository matches “${q}”` : "no git repositories in the workspace" }));
    }

    async function loadPanel() {
      if (!selected) {
        replace(panel, h("div", { class: "empty", text: store.workspace.enabled ? "Choose a repository." : "" }));
        return;
      }
      let data;
      try { data = await api(`/api/repo?repo=${enc(selected)}`); } catch (_error) {
        replace(panel, h("div", { class: "empty", text: `No repository ${selected} in the workspace.` }));
        return;
      }
      loaded = data;
      renderPanel(data);
    }

    function runAction(repo, action, button) {
      return async () => {
        button.disabled = true;
        try {
          const answer = await api.post("/api/action", { repo: repo.key, action: action.name });
          if (answer.preview) confirmAction(repo, action, answer, (job) => go(`#/job/${enc(job)}`));
          else go(`#/job/${enc(answer.job)}`);
        } catch (error) {
          const note = panel.querySelector(`[data-action="${action.name}"] .why`);
          if (note) { note.className = "why error"; note.textContent = error.message; }
        } finally { button.disabled = !action.available; }
      };
    }

    function renderPanel(data) {
      const repo = data.repo;
      const layer = repo.layer_key ? store.layer(repo.layer_key) : null;
      const actions = data.actions.map((action) => {
        const button = h("button", { type: "button", class: `btn${action.writes ? " writes" : ""}`, text: action.name, disabled: !action.available,
          title: action.available ? action.description : action.why });
        button.addEventListener("click", runAction(repo, action, button));
        return h("div", { class: `action${action.available ? "" : " off"}`, dataset: { action: action.name } },
          h("div", { class: "action-head" }, button, h("span", { class: "dim", text: action.description }),
            action.writes ? h("span", { class: "badge warn", text: "writes · asks first" }) : null),
          h("div", { class: "commands" }, action.commands.map((line) => h("code", { class: "cmd", text: line }))),
          h("div", { class: "why", text: action.available ? "" : action.why }));
      });
      replace(panel,
        h("div", { class: "card" },
          h("div", { class: "card-head" }, h("h2", { text: repo.name }), stateBadge(repo.state), h("span", { class: "mono dim", text: repo.branch }),
            repo.dirty ? h("span", { class: "badge warn", text: "uncommitted changes" }) : null, h("span", { class: "spacer" }),
            h("span", { class: "dim mono", text: repo.dir })),
          repo.error ? h("div", { class: "why error", text: repo.error }) : null,
          repo.truncated ? h("div", { class: "faint", text: `classified from the first ${repo.files} tracked files` }) : null,
          repo.layer ? h("div", { class: "repo-layer" }, "layer: ", h("span", { class: "mono", text: repo.layer }),
            layer ? [" · ", h("a", { href: `#/layer/${enc(layer.key)}`, text: "workflows and runs" }), " ",
              h("span", { class: "piter" }, layer.adws.map((name) => h("span", { class: "badge", text: name })))]
              : h("span", { class: "faint", text: repo.layer_generated ? " (not watched yet)" : " (not generated yet: run gen)" })) : null),
        h("div", { class: "card al-panel" }, h("div", { class: "card-head" }, h("h3", { text: "al" }),
          h("span", { class: "dim", text: "each button runs exactly the command line shown under it" })),
          data.al.found ? null : h("div", { class: "why", text: `The al tool was not found; the actions need it. Looked for: ${data.al.looked.join(", ")}` }),
          toolsTable(data.al.tools || []),
          actions),
        data.jobs.length ? h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", { text: "Jobs" })),
          h("div", { class: "job-list" }, data.jobs.map((job) => h("a", { class: "job-row", href: `#/job/${enc(job.id)}` },
            pill(job.status), h("span", { class: "mono", text: job.id }), h("span", { text: job.action + (job.preview ? " (dry run)" : "") }),
            h("span", { class: "faint", text: fmt.ago(job.created) }))))) : null,
        data.summary ? h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", { text: "Existing agent setup" })),
          h("div", { class: "md" }, renderMarkdown(data.summary))) : null);
    }

    /** What al doctor sees on this machine: each tool, where it was found (or why not), its version. */
    function toolsTable(tools) {
      if (!tools.length) return null;
      return h("table", { class: "tools" },
        h("thead", {}, h("tr", {}, ["tool", "found where", "version"].map((text) => h("th", { text })))),
        h("tbody", {}, tools.map((tool) => h("tr", { class: tool.found ? "" : "missing", dataset: { tool: tool.tool } },
          h("td", { class: "mono", text: tool.tool }),
          h("td", {}, tool.found ? [h("span", { class: "mono", text: tool.path }), " ", h("span", { class: "faint", text: `(${tool.source})` })]
            : h("div", { class: "why", text: tool.sentence })),
          h("td", { class: "dim", text: tool.version || "" })))));
    }

    filter.addEventListener("input", renderList);
    refresh.addEventListener("click", async () => {
      refresh.disabled = true;
      try { store.workspace = await api("/api/workspace?refresh=1"); renderList(); loadPanel(); } finally { refresh.disabled = false; }
    });
    const reload = throttle(() => { if (selected) loadPanel(); }, 1000);
    const off = store.on((kind, data) => {
      if (kind === "snapshot" || kind === "workspace") { renderList(); setCrumbs(); reload(); }
      if (kind === "job" && data && data.repo === selected) reload();
      if (kind === "layers" && loaded) renderPanel(loaded);
    });
    setCrumbs();
    renderList();
    loadPanel();
    return {
      destroy: off,
      searchInput: filter,
      accepts: (r) => r.view === "repos" || r.view === "repo",
      route(r) {
        selected = r.view === "repo" ? r.parts[1] || "" : "";
        setCrumbs();
        renderList();
        loadPanel();
      },
      back() { if (selected) go("#/repos"); },
    };
  }

  /** A workspace action's job, in the order a person reads it: the header (status, duration, how many
   * items need a person), the outcome once it ended (what was created, with a passed create's or adopt's
   * result card inside it, or which step failed, why, and "Retry from STEP"), the steps row (what the
   * running step is doing right now), the selected step's activity (an agent step's condensed journal, the
   * last agent step by default; a plain step's log), the author's Decisions and Left out,
   * the changed files and documents, and last the full output (collapsed; open for a job from before
   * the steps/ files existed). */
  function JobView(main, crumbs, route) {
    const id = route.parts[1] || "";
    let chosen = route.params.step || "";   // the step a person picked (kept in the route as ?step=)
    let current = null;                      // the job as last loaded
    let view = null, viewKey = "";           // the selected step's ActivityView or TextWindow
    let log = null;                          // the full output's LogView, made when it is first opened
    let outputSet = false;
    let shownDoc = "";
    const fileUrl = (name) => `/api/job/file?id=${enc(id)}&name=${enc(name)}`;
    const head = h("div", { class: "job-head" });
    const outcome = h("div", { class: "job-outcome-slot" });
    const steps = h("div", { class: "card job-steps" });
    const activityHead = h("div", { class: "card-head" });
    const activityBody = h("div", { class: "activity-slot" });
    const activityCard = h("div", { class: "card job-activity" }, activityHead, activityBody);
    const reports = h("div", { class: "job-reports" });
    const changed = h("div", { class: "card job-changed" });
    const docs = h("div", { class: "card job-docs" });
    const docView = h("div", { class: "doc-view-slot" });
    const outputBody = h("div", { class: "job-output-body" });
    const output = h("details", { class: "card job-output", "data-role": "full-output" },
      h("summary", {}, h("h3", { text: "Full output" }), h("span", { class: "dim", text: "every step, as it ran (an agent step condensed)" })), outputBody);
    output.addEventListener("toggle", () => { if (output.open && !log) log = new LogView(outputBody, { job: id, running: true, label: "job output" }); });
    main.append(head, outcome, steps, activityCard, reports, changed, docs, docView, output);

    /** The step whose activity shows: the one picked, else the running one, else the failed one, else the last
     * agent step (author rather than scout), else the last step with activity. */
    function selected(job) {
      if (chosen && job.steps.some((s) => s.name === chosen)) return chosen;
      const failed = job.outcome && job.outcome.step;
      const pick = job.steps.find((s) => s.status === "running") || job.steps.find((s) => failed && s.name === failed && s.activity)
        || [...job.steps].reverse().find((s) => s.stream === "agent" && s.activity) || [...job.steps].reverse().find((s) => s.activity);
      return pick ? pick.name : "";
    }

    function choose(name) {
      chosen = name;
      go(routeHash(["job", id], { step: name }), true);
    }

    function render(job) {
      current = job;
      replace(crumbs, h("a", { href: "#/repos", text: "repos" }), " / ", h("a", { href: `#/repo/${enc(job.repo)}`, text: job.repo_name || job.repo }),
        " / ", h("span", { text: `job ${job.id}` }));
      const duration = job.started ? (job.ended || now()) - job.started : null;
      replace(head, h("div", { class: "run-head" }, h("div", {},
        h("div", { class: "line1" }, h("h1", { text: `${job.action}${job.preview ? " (dry run)" : ""} · ${job.repo_name}` }), pill(job.status),
          job.exit !== null && job.exit !== undefined ? h("span", { class: "badge", text: `exit ${job.exit}` }) : null,
          job.needs_person > 0 ? h("span", { class: "badge warn", "data-role": "needs-person", title: "items of the author's report left for a person (Left out and why)",
            text: `needs a person: ${job.needs_person}` }) : null),
        h("div", { class: "dim" }, h("span", { class: "mono", text: job.repo_dir }), job.layer ? [" · layer ", h("span", { class: "mono", text: job.layer })] : null,
          ` · started ${fmt.time(job.started || job.created)} · `,
          job.status === "running" && job.started ? h("span", { dataset: { since: job.started }, text: fmt.dur(duration) }) : fmt.dur(duration),
          job.output_bytes ? [" · ", h("a", { href: fileUrl("output.log"), download: true, "data-role": "download-output", text: "Download full output" })] : null),
        job.reason ? h("div", { class: `why${job.outcome ? " brief" : ""}`, text: job.reason }) : null)));
      replace(outcome, outcomeCard(job));
      const sel = selected(job);
      replace(steps, h("div", { class: "card-head" }, h("h3", { text: "Steps" }), h("span", { class: "dim", text: "run in order; the first that fails stops the job" })),
        h("div", { class: "job-steprow", "data-role": "steps" }, job.steps.map((step) => stepChip(step, step.name === sel))));
      renderActivity(job, sel);
      const report = job.report || { decisions: [], left_out: [] };
      replace(reports, report.decisions.length ? reportCard("Decisions", "decisions", report.decisions) : null,
        report.left_out.length ? reportCard("Left out and why", "left-out", report.left_out) : null);
      replace(changed, h("div", { class: "card-head" }, h("h3", { text: "Changed files" })),
        job.status === "running" ? h("div", { class: "faint", text: "listed when the job ends" })
          : [job.changed.length ? h("ul", { class: "changed" }, job.changed.map((c) => h("li", {}, h("span", { class: `badge change-${c.change}`, text: c.change }), " ",
            h("span", { class: "mono", text: c.path })))) : h("div", { class: "faint", text: "no file of the layer changed" }),
          h("div", { class: job.repo_unchanged === false ? "why" : "faint", text: job.repo_unchanged === false
            ? `the repository's working tree changed: ${job.repo_changes.join(", ")}` : "the repository itself is unchanged" })]);
      replace(docs, h("div", { class: "card-head" }, h("h3", { text: "Documents" })),
        (job.docs || []).length ? h("table", { class: "docs" }, h("tbody", {}, job.docs.map((doc) => h("tr", { dataset: { doc: doc.name } },
          h("td", {}, h("span", { class: "doc-name", text: doc.name })), h("td", { class: "num", text: fmt.bytes(doc.bytes) }),
          h("td", { class: "actions" }, h("button", { type: "button", class: "toggle", "data-role": "view", text: "View", onclick: () => showDoc(doc.name) }))))))
          : h("div", { class: "faint", text: "none" }));
      if (!outputSet) {   // a job from before the steps/ files: its combined log is all there is, open
        outputSet = true;
        if (!job.has_steps_dir) output.open = true;
      }
      if (shownDoc) showDoc(shownDoc);
    }

    function outcomeCard(job) {
      const o = job.outcome;
      if (!o) return null;
      if (o.status === "passed") {
        const c = o.created || {};
        const counts = [c.added ? `${c.added} added` : "", c.modified ? `${c.modified} changed` : "", c.removed ? `${c.removed} removed` : ""].filter(Boolean).join(", ");
        const made = job.action === "create" || job.action === "adopt";
        const card = o.card ? resultRows(o.card) : null;
        const rows = [
          c.layer ? [made ? "created" : "layer", "created", [h("span", { class: "mono", text: c.layer }), counts ? h("span", { class: "dim", text: ` · ${counts}` }) : null]] : null,
          card ? card.forge : null,
          card ? card.workflows : null,
          card ? card.adopted : o.adopted ? ["adopted", "adopted", `${o.adopted} file${o.adopted === 1 ? "" : "s"} under adopted/`] : null,
          card ? card.leftBehind : null,
          job.report ? ["unverified", "unverified", o.unverified.length
            ? h("ul", { class: "plain-list" }, o.unverified.map((item) => h("li", {}, inlineMarkdown(item)))) : h("span", { class: "faint", text: "nothing" })] : null,
          card ? card.commands : null,
          card ? card.next : o.next ? ["next", "next", inlineMarkdown(o.next)] : null,
        ].filter(Boolean);
        // with a result card, its rows (.result-card .card-row); else the plain key/value list
        const body = card
          ? rows.map(([label, role, value]) => h("div", { class: "card-row", "data-role": role },
            h("span", { class: "card-key", text: label }), h("span", { class: "card-value" }, value)))
          : h("dl", { class: "kv" }, rows.map(([label, role, value]) => [h("dt", { text: label }), h("dd", { "data-role": role }, value)]));
        return h("div", { class: `card job-outcome${card ? " result-card" : ""} s-passed`, "data-role": "outcome" },
          h("div", { class: "outcome-title" }, pill("passed"), h("span", { text: made ? " the layer is written" : " done" })), body);
      }
      return h("div", { class: `card job-outcome s-${o.status}`, "data-role": "outcome" },
        h("div", { class: "outcome-title" }, pill(o.status), h("span", { text: " at step " }), h("strong", { class: "mono", "data-role": "outcome-step", text: o.step || "?" })),
        h("div", { class: "outcome-reason", "data-role": "reason", text: o.reason }),
        h("div", { class: "outcome-actions" },
          job.retry_from && store.actions.enabled ? h("div", { class: "job-retry" }, retryButton(job)) : null,
          o.log ? h("a", { class: "btn", href: fileUrl(`steps/${o.log}`), download: true, "data-role": "download-log", text: "Download log" }) : null),
        o.lines.length ? h("pre", { class: "outcome-lines", "data-role": "lines", text: o.lines.join("\n") }) : null);
    }

    function stepChip(step, isSelected) {
      const chip = h("div", { class: `job-step step-chip s-${step.status}${isSelected ? " selected" : ""}`, role: "button", tabindex: 0,
        "aria-pressed": isSelected ? "true" : "false", dataset: { step: step.name } },
        h("div", { class: "chip-top" }, pill(step.status), h("span", { class: "step-name", text: step.name }), h("span", { class: "spacer" }),
          h("span", { class: "num dim", text: step.exit !== null && step.exit !== undefined ? `exit ${step.exit}` : "" }),
          h("span", { class: "num dim", text: step.started ? fmt.dur((step.ended || now()) - step.started) : "" })),
        h("code", { class: "cmd", text: step.display }),
        step.doing ? h("div", { class: "doing", "data-role": "doing", text: step.doing }) : null,
        step.reason ? h("div", { class: "why", text: step.reason }) : null);
      chip.addEventListener("click", () => choose(step.name));
      chip.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(step.name); } });
      return chip;
    }

    function renderActivity(job, name) {
      const step = job.steps.find((s) => s.name === name);
      activityCard.hidden = !job.has_steps_dir;
      if (!job.has_steps_dir || !step) {
        if (view) view.destroy();
        view = null;
        viewKey = "";
        clear(activityBody);
        return;
      }
      const file = `steps/${step.name}.log`;
      replace(activityHead, h("h3", { text: `Activity · ${step.name}` }), step.doing ? h("span", { class: "dim doing", text: step.doing }) : null,
        h("span", { class: "spacer" }),
        h("span", { class: "downloads" },
          step.raw ? h("a", { href: fileUrl(file), download: true, "data-role": "raw", title: "the agent's whole event stream", text: `raw events (${fmt.bytes(step.log_bytes)})` })
            : step.activity ? h("a", { href: fileUrl(file), download: true, "data-role": "log", text: `log (${fmt.bytes(step.log_bytes)})` }) : null,
          step.journal ? h("a", { href: fileUrl(`steps/${step.name}.journal.jsonl`), download: true, "data-role": "journal", text: "journal" }) : null));
      const key = `${step.name}:${step.activity}`;
      if (key !== viewKey) {
        if (view) view.destroy();
        clear(activityBody);
        viewKey = key;
        const opts = { job: id, step: step.name, running: step.status === "running" };
        view = step.activity === "journal" ? new ActivityView(activityBody, opts) : step.activity === "log" ? new TextWindow(activityBody, opts) : null;
        if (!view) activityBody.appendChild(h("div", { class: "faint", text: step.status === "pending" ? "not started yet"
          : step.status === "skipped" ? "skipped: an earlier step did not pass" : "no output was recorded" }));
      } else if (view) view.refresh();
    }

    function reportCard(title, role, items) {
      return h("div", { class: "card job-report", "data-role": role },
        h("div", { class: "card-head" }, h("h3", { text: title }), h("span", { class: "badge", text: String(items.length) })),
        h("ul", { class: "md report-items" }, items.map((item) => h("li", {}, renderMarkdown(item)))));
    }

    /** "Retry from STEP": the steps that did not pass run again (a retried author accepts the agentic.rkt
     * the earlier attempt left, when it compiles); the passed ones and the output stay. */
    function retryButton(job) {
      const from = job.retry_from;
      const names = job.steps.map((step) => step.name);
      const again = names.slice(names.indexOf(from));
      const note = h("span", { class: "dim" });
      const button = h("button", { type: "button", class: "btn writes", "data-role": "retry", text: `Retry from ${from}` });
      button.addEventListener("click", async () => {
        if (!window.confirm(`Run ${again.join(", ")} again for ${job.repo_name}? The steps that passed are kept.`)) return;
        button.disabled = true;
        try {
          await api.post("/api/job/retry", { job: job.id, from });
          await load();
        } catch (error) {
          note.className = "why error";
          note.textContent = error.message;
          button.disabled = false;
        }
      });
      return [button, " ", note];
    }

    async function showDoc(name) {
      shownDoc = name;
      try {
        const data = await api(`/api/job/doc?id=${enc(id)}&name=${enc(name)}`);
        replace(docView, h("div", { class: "card doc-view" }, h("div", { class: "card-head" }, h("h2", { text: name }),
          h("span", { class: "dim num", text: fmt.bytes(data.bytes) }), h("span", { class: "spacer" }),
          h("button", { type: "button", class: "toggle", text: "close", onclick: () => { shownDoc = ""; clear(docView); } })), jobDocument(name, data)));
      } catch (error) { replace(docView, h("div", { class: "empty", text: `Could not load ${name}: ${error.message}` })); }
    }

    async function load() {
      try { render(await api(`/api/job?id=${enc(id)}`)); } catch (_error) {
        replace(head, h("div", { class: "empty", text: `No such job: ${id}` }));
      }
      main.dataset.rendered = String(Number(main.dataset.rendered || 0) + 1);
    }
    const reload = throttle(load, 500);
    const off = store.on((kind, data) => { if (kind === "job" && data && data.id === id) reload(); if (kind === "snapshot") reload(); });
    load();
    return {
      destroy() { off(); if (view) view.destroy(); if (log) log.destroy(); },
      accepts: (r) => r.view === "job" && r.parts[1] === id,
      route(r) { chosen = r.params.step || ""; if (current) render(current); },
      get searchInput() { return view && view.search; },
      toggleFollow: () => { if (log) log.toggleFollow(); },
      back: () => history.back(),
    };
  }

  // =========================================================================== keyboard
  function help() {
    const overlay = document.getElementById("overlay");
    const keys = [["1 · 2 · 3 · 4", "Now · Runs · Layers · Repos"], ["j / k", "next / previous run, step or transcript event"], ["Enter", "open the selection / expand an event"],
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
    if (event.key === "4") return go("#/repos");
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
