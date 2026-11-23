/* al work dashboard — the voice helper panel (window.alVoice.panel).
 *
 * Talk with the layer's helper about its runs; it answers aloud and in text and proposes work as a
 * card. Nothing starts from speech: the person answers yes, then clicks Approve on the card, and the
 * server checks both. Speech stays on the machine: the dashboard proxies to the layer's own voice
 * service (kit/runtime/voice/serve.py) on loopback.
 *
 * Parts: helpers (WAV, frames) · panel (DOM) · capture (worklet, hold to talk, always listen) ·
 * playback (ALV1 frames, queue, interrupt) · synthesizer (canvas or, with reduced motion, a level
 * meter) · conversation (turns, transcript, cards) · keyboard.
 *
 * Safety rule for this file, as for app.js: everything the helper, the recogniser or the server says is
 * untrusted text. It reaches the page only through textContent / createTextNode (window.alUi.h); nothing
 * is parsed as HTML (a test enforces it).
 */
(() => {
  "use strict";

  const MODE_KEY = "al.voice.mode";
  const RATE = 16000;
  const INTERRUPTED_MS = 600;
  const RESUME_WINDOW_MS = 10000;

  // =========================================================================== helpers
  /** 16-bit mono PCM → a WAV file (RIFF, little endian). */
  function wavFromInt16(samples, rate = RATE) {
    const bytes = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(bytes);
    const ascii = (offset, text) => { for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i)); };
    ascii(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    ascii(8, "WAVE");
    ascii(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);          // PCM
    view.setUint16(22, 1, true);          // mono
    view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    ascii(36, "data");
    view.setUint32(40, samples.length * 2, true);
    new Int16Array(bytes, 44).set(samples);
    return new Uint8Array(bytes);
  }

  function concatInt16(chunks) {
    const total = chunks.reduce((n, c) => n + c.length, 0);
    const out = new Int16Array(total);
    let at = 0;
    for (const chunk of chunks) { out.set(chunk, at); at += chunk.length; }
    return out;
  }

  function concatBytes(a, b) {
    if (!a.length) return b;
    const out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }

  /** A frame length: the service writes it in network order; a little-endian writer is read too. */
  function readLength(view, offset) {
    const big = view.getUint32(offset, false);
    return big <= 0x1000000 ? big : view.getUint32(offset, true);
  }

  /** ALV1 frames (`ALV1` + u32 header length + header JSON + u32 audio length + WAV) from the bytes
   * received so far → {frames: [{header, wav}], rest}: the bytes of an incomplete frame stay in rest. */
  function parseFrames(buffer) {
    const frames = [];
    let at = 0;
    const view = new DataView(buffer.buffer, buffer.byteOffset, buffer.byteLength);
    for (;;) {
      if (buffer.length - at < 8) break;
      if (buffer[at] !== 0x41 || buffer[at + 1] !== 0x4c || buffer[at + 2] !== 0x56 || buffer[at + 3] !== 0x31) {
        throw new Error("the speech stream is not ALV1 frames");
      }
      const headerLength = readLength(view, at + 4);
      if (buffer.length - at < 8 + headerLength + 4) break;
      const header = JSON.parse(new TextDecoder().decode(buffer.subarray(at + 8, at + 8 + headerLength)));
      const audioLength = readLength(view, at + 8 + headerLength);
      const end = at + 8 + headerLength + 4 + audioLength;
      if (buffer.length < end) break;
      const wav = audioLength ? buffer.slice(at + 12 + headerLength, end) : null;
      frames.push({ header, wav });
      at = end;
    }
    return { frames, rest: buffer.slice(at) };
  }

  function randomId() {
    const bytes = new Uint8Array(8);
    window.crypto.getRandomValues(bytes);
    return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function shortRun(id) { return String(id || "").slice(0, 8); }

  const STATE_TEXT = {
    idle: "Ready.",
    listening: "Listening…",
    thinking: "Thinking…",
    speaking: "Speaking.",
    interrupted: "Stopped speaking.",
    loading: "Loading the speech models…",
    off: "Voice is off.",
  };
  // the speech engines the service may report (/health tts_engine, each frame's `engine`)
  const ENGINE_NAMES = { kokoro: "Kokoro", espeak: "espeak-ng", fake: "the test engine" };

  // =========================================================================== panel
  /** Mounts the voice helper panel for a layer (and, on a repository's panel, that repository) into
   * container. Returns {el, destroy}. */
  function panel(container, opts) {
    const { h, api } = window.alUi;
    const layer = opts.layer;
    const repo = opts.repo || "";
    const enc = encodeURIComponent;
    const query = `layer=${enc(layer)}${repo ? `&repo=${enc(repo)}` : ""}`;
    const uid = randomId().slice(0, 6);

    const st = {
      status: null, ready: false, setUp: true, destroyed: false, mode: "hold", interrupt: true, maxUtterance: 30,
      ctx: null, gain: null, playAnalyser: null, micAnalyser: null, mic: null, micSource: null, worklet: null, sink: null,
      workletLoaded: false, opening: null,
      holdWanted: false, holding: false, holdFrames: [], holdSamples: 0,
      listening: false, streamId: "", queue: [], inFlight: false, hearing: false,
      speaking: false, playback: null, gen: 0, nextTime: 0, resumable: null,
      thinking: 0, interruptedUntil: 0, loading: false, state: "", level: 0, micLevel: 0,
      cards: new Map(), raf: 0, timers: [], service: null,
    };

    // ---- DOM
    const radios = ["hold", "always"].map((value) => h("input", { type: "radio", name: `voice-mode-${uid}`, value, class: `voice-mode-${value}` }));
    const modeGroup = h("div", { class: "voice-modes", role: "radiogroup", "aria-label": "Listening mode" },
      h("label", {}, radios[0], " Hold to talk"), h("label", {}, radios[1], " Always listen"));
    const keep = h("input", { type: "checkbox", class: "voice-keep" });
    const keepLabel = h("label", { class: "voice-keep-label" }, keep, " Keep recordings for this conversation");
    const motion = reducedMotion();
    const canvas = motion ? null : h("canvas", { class: "voice-synth", role: "img", "aria-label": "voice helper: idle", width: 320, height: 140 });
    const bar = h("span", { class: "voice-level-bar" });
    const meter = motion ? h("div", { class: "voice-level", role: "meter", "aria-label": "sound level", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": "0" }, bar) : null;
    const levelEl = canvas || meter;
    const stateLine = h("div", { class: "voice-state", "aria-live": "polite" });
    const engineEl = h("p", { class: "voice-engine dim", "aria-live": "polite" });
    const talk = h("button", { type: "button", class: "btn primary voice-talk", "aria-pressed": "false", text: "Hold to talk" });
    const stopSpeaking = h("button", { type: "button", class: "btn voice-stop", text: "Stop speaking", disabled: true });
    const closeMicButton = h("button", { type: "button", class: "btn voice-close-mic", text: "Close microphone" });
    const micOpen = h("div", { class: "voice-mic-open", role: "status", hidden: true },
      h("span", { class: "voice-mic-dot", "aria-hidden": "true" }), h("span", { text: "Microphone open" }), closeMicButton);
    const note = h("div", { class: "voice-note", role: "status" });
    const personCaption = h("p", { class: "voice-caption voice-caption-person" });
    const helperCaption = h("p", { class: "voice-caption voice-caption-helper" });
    const captions = h("div", { class: "voice-captions", "aria-live": "polite" }, personCaption, helperCaption);
    const cardsEl = h("div", { class: "voice-cards" });
    const transcript = h("ol", { class: "voice-transcript", "aria-label": "Conversation" });
    const text = h("input", { type: "text", class: "voice-text", "aria-label": "Message to the helper", placeholder: "or type here", maxlength: "2000", autocomplete: "off" });
    const send = h("button", { type: "submit", class: "btn voice-send", text: "Send" });
    const form = h("form", { class: "voice-form" }, text, send);
    const controls = h("div", { class: "voice-controls" }, talk, stopSpeaking, micOpen);
    const body = h("div", { class: "voice-body" },
      h("div", { class: "voice-stage" }, levelEl, stateLine, engineEl, controls),
      h("div", { class: "voice-talk-side" }, captions, cardsEl, transcript, form));
    const root = h("section", { class: "card voice-panel", "aria-label": "Voice helper", dataset: { state: "idle", layer, mic: "closed" } },
      h("div", { class: "card-head voice-head" }, h("h3", { text: "Voice helper" }),
        h("span", { class: "dim", text: "talk about the runs; nothing starts until you click Approve" }), h("span", { class: "spacer" }),
        modeGroup, keepLabel),
      note, body);
    container.appendChild(root);

    function setNote(message, kind) {
      note.className = `voice-note${kind ? ` ${kind}` : ""}`;
      note.textContent = message || "";
    }

    // which engine speaks: "Spoken by Kokoro", "Spoken by espeak-ng (Kokoro is not available)", "Speech is off";
    // the reason Kokoro was not used is the line's title
    function showEngine(engine, why) {
      if (!engine) return;
      root.dataset.engine = engine;
      engineEl.title = why || "";
      const line = engine === "off" ? "Speech is off: answers are text only"
        : `Spoken by ${ENGINE_NAMES[engine] || engine}${engine === "espeak" && why ? " (Kokoro is not available)" : ""}`;
      if (engineEl.textContent !== line) engineEl.textContent = line;
    }

    function showNotSetUp(why) {
      st.setUp = false;
      const command = (st.status && st.status.setup) || `al voice ${layer} setup`;
      const head = String(why || "voice is not set up").split(":")[0] || "voice is not set up";
      note.className = "voice-note warn voice-not-set-up";
      while (note.firstChild) note.removeChild(note.firstChild);
      note.append(h("span", { text: `${head}: run ` }), h("code", { class: "voice-setup", text: command }),
        h("span", { text: " once (it downloads the speech models). You can type meanwhile." }));
      talk.disabled = true;
      update();
    }

    // ---- state
    function computeState() {
      const t = performance.now();
      if (t < st.interruptedUntil) return "interrupted";
      if (st.speaking) return "speaking";
      if (st.thinking > 0) return "thinking";
      if (st.holding || (st.listening && st.mic)) return "listening";
      if (st.loading) return "loading";
      if (st.status && !st.status.available) return "off";
      return "idle";
    }

    function stateText(state) {
      if (state === "idle") {
        if (!st.setUp || !secure()) return "Type a message below.";
        return st.mode === "always" ? "Ready. Start listening, or type below." : "Ready. Hold the button (or Space) and speak, or type below.";
      }
      if (state === "listening") return st.holding ? "Listening… release to send." : "Listening. Speak; I answer when you pause.";
      if (state === "speaking") return st.interrupt && st.mode === "always" ? "Speaking. Speak or press Escape to interrupt." : "Speaking. Press Escape to stop.";
      if (state === "off") return (st.status && st.status.why) || STATE_TEXT.off;
      return STATE_TEXT[state] || state;
    }

    function update() {
      if (st.destroyed) return;
      const state = computeState();
      if (state !== st.state) {
        st.state = state;
        root.dataset.state = state;
        if (state === "interrupted" && window.performance && performance.mark) performance.mark("al-voice-interrupted");
        if (canvas) canvas.setAttribute("aria-label", `voice helper: ${state}`);
      }
      const line = stateText(state);
      if (stateLine.textContent !== line) stateLine.textContent = line;
      stopSpeaking.disabled = !st.speaking;
      talk.setAttribute("aria-pressed", st.holding || st.listening ? "true" : "false");
      const label = st.mode === "always" ? (st.listening ? "Stop listening" : "Start listening") : "Hold to talk";
      if (talk.textContent !== label) talk.textContent = label;
      micOpen.hidden = !st.mic;
      root.dataset.mic = st.mic ? "open" : "closed";
    }

    function later(fn, ms) {
      const id = setTimeout(() => { st.timers = st.timers.filter((t) => t !== id); if (!st.destroyed) fn(); }, ms);
      st.timers.push(id);
    }

    function secure() { return !!window.isSecureContext; }

    // ---- requests
    async function postBytes(path, bytes, type) {
      const response = await fetch(path, { method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": type, "X-AL-Dashboard": "1" }, body: bytes });
      let data = null;
      try { data = await response.json(); } catch (_err) { data = null; }
      if (!response.ok) {
        const error = new Error((data && data.error) || `${response.status} ${path}`);
        error.status = response.status;
        error.data = data;
        throw error;
      }
      return data || {};
    }

    async function ensureService() {
      if (st.ready) return true;
      if (st.service) return st.service;
      st.service = (async () => {
        st.loading = true;
        update();
        try {
          await api.post("/api/voice/service", { layer, action: "start" });
          const end = Date.now() + 120000;
          while (!st.destroyed && Date.now() < end) {
            let health = null;
            try { health = await api(`/api/voice/health?layer=${enc(layer)}`); } catch (_error) { health = null; }
            if (health && health.missing && health.missing.length) { showNotSetUp("voice is not set up"); return false; }
            if (health && health.ready) { st.ready = true; showEngine(health.tts_engine, health.tts_why); return true; }
            await new Promise((resolve) => setTimeout(resolve, 500));
          }
          setNote("The speech service did not become ready; you can type.", "warn");
          return false;
        } catch (error) {
          if ((error.data && error.data.missing && error.data.missing.length) || /not set up/i.test(error.message)) showNotSetUp("voice is not set up");
          else setNote(`The speech service is not available: ${error.message}`, "warn");
          return false;
        } finally {
          st.loading = false;
          st.service = null;
          update();
        }
      })();
      return st.service;
    }

    // ---- audio graph
    function ensureAudio() {
      if (!st.ctx) {
        const Context = window.AudioContext || window.webkitAudioContext;
        st.ctx = new Context();
        st.gain = st.ctx.createGain();
        st.playAnalyser = st.ctx.createAnalyser();
        st.playAnalyser.fftSize = 1024;
        st.gain.connect(st.playAnalyser);
        st.playAnalyser.connect(st.ctx.destination);
      }
      if (st.ctx.state === "suspended") st.ctx.resume().catch(() => {});
      return st.ctx;
    }

    // ---- capture
    async function openMic() {
      if (st.mic) return true;
      if (st.opening) return st.opening;
      if (!secure() || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        setNote("The microphone works only on loopback or https; you can type instead.", "warn");
        return false;
      }
      st.opening = (async () => {
        try {
          const ctx = ensureAudio();
          const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
          if (st.destroyed) { stream.getTracks().forEach((t) => t.stop()); return false; }
          if (!st.workletLoaded) { await ctx.audioWorklet.addModule("/static/voice-worklet.js"); st.workletLoaded = true; }
          const source = ctx.createMediaStreamSource(stream);
          const node = new AudioWorkletNode(ctx, "al-capture", { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
          const analyser = ctx.createAnalyser();
          analyser.fftSize = 1024;
          const sink = ctx.createGain();
          sink.gain.value = 0;             // keeps the worklet pulled by the graph; nothing is heard
          source.connect(node);
          source.connect(analyser);
          node.connect(sink);
          sink.connect(ctx.destination);
          node.port.onmessage = (event) => onCapture(event.data);
          for (const track of stream.getAudioTracks()) track.addEventListener("ended", () => closeMic());
          Object.assign(st, { mic: stream, micSource: source, worklet: node, micAnalyser: analyser, sink });
          setNote("");
          return true;
        } catch (error) {
          setNote(`The microphone could not be opened (${error.name || error.message}); you can type instead.`, "warn");
          return false;
        } finally {
          st.opening = null;
          update();
        }
      })();
      return st.opening;
    }

    function closeMic() {
      st.holdWanted = false;
      st.holding = false;
      st.holdFrames = [];
      stopListening();
      if (st.mic) {
        for (const track of st.mic.getTracks()) track.stop();
        try { st.worklet.port.postMessage({ stop: true }); } catch (_error) { /* gone */ }
        for (const node of [st.micSource, st.worklet, st.micAnalyser, st.sink]) { try { node.disconnect(); } catch (_error) { /* gone */ } }
      }
      Object.assign(st, { mic: null, micSource: null, worklet: null, micAnalyser: null, sink: null, micLevel: 0 });
      update();
    }

    function onCapture(data) {
      if (typeof data.level === "number") st.micLevel = data.level;
      if (!data.pcm) return;
      if (st.holding) {
        st.holdFrames.push(data.pcm);
        st.holdSamples += data.pcm.length;
        if (st.holdSamples >= st.maxUtterance * RATE) {
          setNote(`Stopped at ${st.maxUtterance} seconds, the longest utterance this layer takes.`);
          endHold();
        }
      } else if (st.listening) {
        st.queue.push(data.pcm);
        pump();
      }
    }

    // hold to talk
    async function beginHold() {
      if (st.holdWanted || st.mode !== "hold" || talk.disabled) return;
      st.holdWanted = true;
      if (st.speaking) interrupt("hold");
      ensureService();
      const ok = await openMic();
      if (!ok || !st.holdWanted) { st.holdWanted = false; return; }
      st.holding = true;
      st.holdFrames = [];
      st.holdSamples = 0;
      personCaption.className = "voice-caption voice-caption-person partial";
      update();
    }

    async function endHold() {
      st.holdWanted = false;
      if (!st.holding) return;
      st.holding = false;
      const samples = concatInt16(st.holdFrames);
      st.holdFrames = [];
      update();
      if (samples.length < RATE * 0.3) { setNote("Too short: hold the button while you speak."); return; }
      st.thinking += 1;
      update();
      try {
        if (!(await ensureService())) return;
        const heard = await sendUtterance(wavFromInt16(samples));
        const said = String((heard && heard.heard) || "").trim();
        if (!said) { setNote("I heard nothing I could recognise; try again."); return; }
        showPerson(said);
        await turn(said, "voice");
      } catch (error) {
        setNote(error.message, "error");
      } finally {
        st.thinking -= 1;
        update();
      }
    }

    async function sendUtterance(wav) {
      for (let attempt = 0; ; attempt += 1) {
        try {
          return await postBytes(`/api/voice/utterance?layer=${enc(layer)}`, wav, "audio/wav");
        } catch (error) {
          if (error.status === 503 && attempt < 60 && !st.destroyed) {
            st.loading = true;
            update();
            await new Promise((resolve) => setTimeout(resolve, 1000));
            st.loading = false;
            continue;
          }
          throw error;
        }
      }
    }

    // always listen
    async function startListening() {
      if (st.listening || talk.disabled) return;
      ensureService();
      const ok = await openMic();
      if (!ok) return;
      st.listening = true;
      st.streamId = randomId();
      st.queue = [];
      update();
    }

    function stopListening() {
      st.listening = false;
      st.queue = [];
      st.hearing = false;
      update();
    }

    async function pump() {
      if (st.inFlight || !st.queue.length || !st.listening) return;
      const chunk = concatInt16(st.queue);
      st.queue = [];
      st.inFlight = true;
      try {
        const bytes = new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength);
        const answer = await postBytes(`/api/voice/listen?layer=${enc(layer)}&stream=${enc(st.streamId)}&speaking=${st.speaking ? 1 : 0}`,
          bytes, "audio/L16;rate=16000");
        if (st.listening) handleListen(answer);
      } catch (error) {
        if (error.status !== 503) setNote(error.message, "error");
        await new Promise((resolve) => setTimeout(resolve, error.status === 503 ? 1000 : 500));
      } finally {
        st.inFlight = false;
      }
      if (st.queue.length) pump();
    }

    function handleListen(answer) {
      if (answer.barge_in && st.speaking && st.interrupt) {
        if (performance.mark) performance.mark("al-voice-barge-in");
        interrupt("barge-in");
      }
      if (answer.state === "speech") {
        st.hearing = true;
        if (answer.partial) showPerson(answer.partial, true);
      } else if (answer.state === "end") {
        st.hearing = false;
        if (answer.echo) {
          if (st.resumable && performance.now() < st.resumable.until) resume();
          return;
        }
        st.resumable = null;
        const said = String(answer.text || "").trim();
        if (!said) return;
        showPerson(said);
        st.thinking += 1;
        update();
        turn(said, "voice").finally(() => { st.thinking -= 1; update(); });
      } else {
        st.hearing = false;
      }
    }

    // ---- playback
    async function speak(reply) {
      if (!reply || !reply.id || !st.setUp) return;
      if (!(await ensureService())) return;
      stopPlayback();
      const ctx = ensureAudio();
      const gen = ++st.gen;
      const playback = { id: reply.id, items: [], done: false, gen, reader: null };
      st.playback = playback;
      st.speaking = true;
      st.nextTime = 0;
      st.resumable = null;
      update();
      let response;
      try {
        response = await fetch("/api/voice/speak", { method: "POST", credentials: "same-origin", cache: "no-store",
          headers: { "Content-Type": "application/json", "X-AL-Dashboard": "1" }, body: JSON.stringify({ layer, reply: reply.id }) });
      } catch (error) {
        finishPlayback(playback);
        setNote(`Could not speak: ${error.message}`, "warn");
        return;
      }
      if (!response.ok || !response.body) {
        let message = `${response.status}`;
        try { message = (await response.json()).error || message; } catch (_error) { /* not JSON */ }
        finishPlayback(playback);
        setNote(`Could not speak: ${message}`, "warn");
        return;
      }
      const reader = response.body.getReader();
      playback.reader = reader;
      let buffer = new Uint8Array(0);
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (playback.gen !== st.gen) break;
          if (value) buffer = concatBytes(buffer, value);
          const parsed = parseFrames(buffer);
          buffer = parsed.rest;
          for (const frame of parsed.frames) {
            if (frame.header.cancelled) { playback.done = true; break; }
            showEngine(frame.header.engine, frame.header.why);
            if (frame.wav) {
              const audio = await ctx.decodeAudioData(frame.wav.buffer.slice(frame.wav.byteOffset, frame.wav.byteOffset + frame.wav.byteLength));
              if (playback.gen !== st.gen) break;
              schedule(playback, audio, frame.header);
            }
            if (frame.header.last) playback.done = true;
          }
          if (done || playback.done || playback.gen !== st.gen) break;
        }
      } catch (error) {
        if (playback.gen === st.gen) setNote(`Speech stopped: ${error.message}`, "warn");
      }
      playback.done = true;
      if (playback.gen === st.gen) checkFinished(playback);
    }

    function schedule(playback, audio, header) {
      const ctx = st.ctx;
      const source = ctx.createBufferSource();
      source.buffer = audio;
      source.connect(st.gain);
      const start = Math.max(ctx.currentTime + 0.03, st.nextTime);
      source.start(start);
      st.nextTime = start + audio.duration;
      const item = { source, audio, start, end: start + audio.duration, text: String(header.text || ""), ended: false };
      source.onended = () => { item.ended = true; checkFinished(playback); };
      playback.items.push(item);
      const trace = (window.alVoice.trace = window.alVoice.trace || []);
      trace.push({ reply: playback.id, seq: header.seq, text: item.text, at: performance.now() + (start - ctx.currentTime) * 1000, start,
        duration: audio.duration, envelope: header.envelope || [] });
      if (trace.length > 50) trace.shift();
      root.dataset.queued = String(playback.items.filter((i) => !i.ended).length);
    }

    function checkFinished(playback) {
      if (playback !== st.playback) return;
      root.dataset.queued = String(playback.items.filter((i) => !i.ended).length);
      if (playback.done && playback.items.every((i) => i.ended)) finishPlayback(playback);
    }

    function finishPlayback(playback) {
      if (playback !== st.playback) return;
      st.playback = null;
      st.speaking = false;
      root.dataset.queued = "0";
      update();
    }

    /** Stops what is playing without telling anyone (a new reply replaces it). */
    function stopPlayback() {
      const playback = st.playback;
      if (!playback) return [];
      st.gen += 1;
      const now = st.ctx ? st.ctx.currentTime : 0;
      const notStarted = [];
      for (const item of playback.items) {
        if (item.ended) continue;
        if (item.start > now) notStarted.push(item);
        item.source.onended = null;
        try { item.source.stop(); } catch (_error) { /* already stopped */ }
        item.ended = true;
      }
      if (playback.reader) playback.reader.cancel().catch(() => {});
      const unfinished = !playback.done;
      st.playback = null;
      st.speaking = false;
      root.dataset.queued = "0";
      return { playback, notStarted, unfinished };
    }

    /** Barge-in, Escape, Stop speaking or a new hold: stop every scheduled sentence, drop the queue,
     * cancel the service's stream, and say on the caption what was not spoken. */
    function interrupt(reason) {
      if (!st.speaking || !st.playback) return;
      const { playback, notStarted, unfinished } = stopPlayback();
      st.interruptedUntil = performance.now() + INTERRUPTED_MS;
      update();
      later(update, INTERRUPTED_MS + 30);
      api.post("/api/voice/cancel", { layer, reply: playback.id }).catch(() => {});
      st.resumable = reason === "barge-in" && notStarted.length
        ? { audio: notStarted.map((i) => ({ audio: i.audio, text: i.text })), until: performance.now() + RESUME_WINDOW_MS, id: playback.id }
        : null;
      const dropped = notStarted.map((i) => i.text).filter(Boolean).join(" ");
      helperCaption.querySelectorAll(".voice-dropped").forEach((el) => el.remove());
      helperCaption.appendChild(h("span", { class: "voice-dropped",
        text: dropped ? ` — not spoken: ${dropped}${unfinished ? " …" : ""}` : " — the rest was not spoken" }));
    }

    /** The recogniser heard the helper's own voice: play the sentences the barge-in dropped. */
    function resume() {
      const pending = st.resumable;
      st.resumable = null;
      if (!pending || !st.ctx) return;
      helperCaption.querySelectorAll(".voice-dropped").forEach((el) => el.remove());
      const gen = ++st.gen;
      const playback = { id: pending.id, items: [], done: true, gen, reader: null };
      st.playback = playback;
      st.speaking = true;
      st.nextTime = 0;
      for (const piece of pending.audio) schedule(playback, piece.audio, { text: piece.text, seq: -1 });
      update();
    }

    // ---- synthesizer
    const timeData = new Float32Array(1024);
    const freqData = new Uint8Array(512);
    const bands = [0, 0, 0, 0, 0];

    function measure() {
      const analyser = st.speaking ? st.playAnalyser : (st.holding || st.listening) && st.mic ? st.micAnalyser : null;
      let target = 0;
      if (analyser) {
        analyser.getFloatTimeDomainData(timeData);
        let sum = 0;
        for (let i = 0; i < timeData.length; i += 1) sum += timeData[i] * timeData[i];
        target = Math.min(1, Math.sqrt(sum / timeData.length) * 4);
        analyser.getByteFrequencyData(freqData);
        const edges = [1, 4, 12, 32, 90, 256];
        for (let b = 0; b < 5; b += 1) {
          let total = 0;
          for (let i = edges[b]; i < edges[b + 1]; i += 1) total += freqData[i];
          bands[b] = Math.min(1, total / (edges[b + 1] - edges[b]) / 200);
        }
      } else {
        for (let b = 0; b < 5; b += 1) bands[b] *= 0.8;
      }
      st.level = target >= st.level ? target : Math.max(target, st.level * 0.7);
      if (st.level < 0.005) st.level = 0;
    }

    function draw(t) {
      const ctx2 = canvas.getContext("2d");
      const w = canvas.width, hgt = canvas.height, cx = w / 2, cy = hgt / 2;
      const base = Math.min(w, hgt) * 0.22;
      ctx2.clearRect(0, 0, w, hgt);
      const state = st.state;
      const level = st.level;
      const color = state === "listening" ? "63, 185, 80" : state === "interrupted" ? "242, 85, 90" : "110, 168, 254";
      if (state === "thinking") {
        const angle = t / 1200;
        ctx2.lineWidth = 3;
        for (let i = 0; i < 3; i += 1) {
          ctx2.strokeStyle = `rgba(${color}, ${0.25 + 0.1 * i})`;
          ctx2.beginPath();
          ctx2.arc(cx, cy, base * 1.2, angle + (i * 2 * Math.PI) / 3, angle + (i * 2 * Math.PI) / 3 + 0.9);
          ctx2.stroke();
        }
        return;
      }
      if (state === "interrupted") {
        const left = Math.max(0, (st.interruptedUntil - performance.now()) / INTERRUPTED_MS);
        ctx2.strokeStyle = `rgba(${color}, ${0.3 + 0.7 * left})`;
        ctx2.lineWidth = 2 + 8 * left;
        ctx2.beginPath();
        ctx2.arc(cx, cy, base * 1.3, 0, 2 * Math.PI);
        ctx2.stroke();
        return;
      }
      if (level < 0.02) {           // idle, or a pause: flat
        ctx2.strokeStyle = "rgba(125, 135, 150, 0.35)";
        ctx2.lineWidth = 1;
        ctx2.beginPath();
        ctx2.arc(cx, cy, base, 0, 2 * Math.PI);
        ctx2.stroke();
        return;
      }
      ctx2.fillStyle = `rgba(${color}, ${0.15 + 0.45 * level})`;
      ctx2.beginPath();
      ctx2.arc(cx, cy, base * (1 + 0.5 * level), 0, 2 * Math.PI);
      ctx2.fill();
      ctx2.strokeStyle = `rgba(${color}, ${0.5 + 0.5 * level})`;
      ctx2.lineWidth = 2 + 10 * level;
      ctx2.stroke();
      const gap = 0.18;
      for (let b = 0; b < 5; b += 1) {
        const band = bands[b];
        if (band < 0.02) continue;
        const from = -Math.PI / 2 + (b * 2 * Math.PI) / 5 + gap / 2;
        ctx2.strokeStyle = `rgba(${color}, ${0.3 + 0.7 * band})`;
        ctx2.lineWidth = 3 + 7 * band;
        ctx2.beginPath();
        ctx2.arc(cx, cy, base * (1.45 + 0.45 * band), from, from + (2 * Math.PI) / 5 - gap);
        ctx2.stroke();
      }
    }

    function frame(t) {
      if (st.destroyed) return;
      update();
      measure();
      const level = st.level.toFixed(2);
      if (canvas) {
        draw(t);
        canvas.dataset.level = level;
      } else {
        bar.style.width = `${Math.round(st.level * 100)}%`;
        meter.dataset.level = level;
        meter.setAttribute("aria-valuenow", String(Math.round(st.level * 100)));
      }
      st.raf = requestAnimationFrame(frame);
    }

    // ---- conversation
    function showPerson(said, partial = false) {
      personCaption.className = `voice-caption voice-caption-person${partial ? " partial" : ""}`;
      personCaption.textContent = said;
    }

    function showHelper(said) {
      helperCaption.textContent = said || "";
    }

    async function turn(said, via) {
      st.thinking += 1;
      update();
      try {
        const answer = await api.post("/api/voice/turn", { layer, text: said, via });
        const reply = answer.reply || null;
        if (answer.card) renderCard(answer.card);
        if (reply) showHelper(reply.text);
        await loadTranscript();
        st.thinking -= 1;
        update();
        if (reply && reply.speak && st.status && st.status.available) speak(reply);
        return answer;
      } catch (error) {
        st.thinking -= 1;
        update();
        setNote(error.message, "error");
        return null;
      }
    }

    function renderEntries(entries) {
      while (transcript.firstChild) transcript.removeChild(transcript.firstChild);
      for (const entry of entries || []) {
        const who = entry.who === "person" ? "You" : entry.who === "helper" ? "Helper" : "note";
        transcript.appendChild(h("li", { class: `voice-entry voice-${entry.who || "system"}${entry.echo ? " echo" : ""}`, dataset: { who: entry.who || "system" } },
          h("span", { class: "voice-who", text: who }), h("span", { class: "voice-said", text: entry.text || "" }),
          entry.via === "voice" ? h("span", { class: "faint voice-via", text: " (spoken)" }) : null));
      }
      transcript.scrollTop = transcript.scrollHeight;
    }

    async function loadTranscript() {
      try {
        const data = await api(`/api/voice/transcript?layer=${enc(layer)}`);
        renderEntries(data.entries);
        for (const card of data.cards || []) renderCard(card);
      } catch (_error) { /* the transcript is a convenience; the captions carry the turn */ }
    }

    // ---- cards
    const KIND_TITLE = { start: "Start", retry: "Retry", job: "Run al", resume: "Resume", answer: "Answer", stop: "Stop" };

    function cardRows(card) {
      const rows = [];
      const add = (label, value) => { if (value !== null && value !== undefined && value !== "") rows.push([label, String(value)]); };
      add(card.command ? "Command" : "Workflow", card.workflow || card.command || card.target || "");
      if (card.kind === "job") add("Action", `al ${card.job || ""}`);
      if (card.run) add("Run", card.run);
      add("Request", card.request);
      const starts = card.kind === "start" || card.kind === "retry";
      add("Class", card.class || (starts ? "decided by the run" : ""));
      add("Tier", card.tier || card.model_set || (starts ? "each step's own tier" : ""));
      add("Repository", card.repo || card.repository);
      add("What will happen", card.what_happens);
      return rows;
    }

    function renderCard(card) {
      if (!card || !card.id) return;
      const known = st.cards.get(card.id);
      const merged = known ? { ...known.card, ...card } : card;
      const node = buildCard(merged);
      if (known && known.node.parentNode) known.node.parentNode.replaceChild(node, known.node);
      else cardsEl.prepend(node);
      st.cards.set(card.id, { card: merged, node });
    }

    function buildCard(card) {
      const status = h("div", { class: "voice-card-note", role: "status" });
      const kind = KIND_TITLE[card.kind] || card.kind || "Proposal";
      let action = null;
      if (card.state === "approved") {
        const run = card.run_id || (card.result && card.result.run_id);
        action = h("div", { class: "voice-started" }, h("b", { text: "Started" }),
          run ? [" ", h("a", { href: card.url || `#/run/${encodeURIComponent(layer)}/${encodeURIComponent(run)}`, class: "voice-run-link", text: `run ${shortRun(run)}` })] : null);
      } else if (card.state === "proposed" || card.state === "confirmed") {
        if (card.approvable) {
          const approve = h("button", { type: "button", class: "btn primary voice-approve", text: "Approve", disabled: card.state !== "confirmed",
            title: card.state === "confirmed" ? "start it now" : "answer yes first (say it or type it)" });
          approve.addEventListener("click", () => approveCard(card, approve, status));
          action = h("div", { class: "voice-card-actions" }, approve,
            card.state === "confirmed" && card.expires_at ? h("span", { class: "voice-countdown faint", dataset: { expires: String(card.expires_at) } }) : null,
            card.state === "proposed" ? h("span", { class: "faint", text: "Say or type yes to confirm it, then click Approve." }) : null);
        } else {
          action = h("div", { class: "voice-why-not why", text: card.why_not || "this dashboard cannot start it" });
        }
      }
      const node = h("article", { class: `voice-card st-${card.state || "proposed"}`, dataset: { proposal: card.id, state: card.state || "proposed" }, "aria-label": `Proposal: ${kind}` },
        h("div", { class: "voice-card-head" }, h("b", { text: kind }), h("span", { class: `badge voice-card-state`, text: card.state || "proposed" })),
        card.summary ? h("p", { class: "voice-card-summary", text: card.summary }) : null,
        h("dl", {}, cardRows(card).map(([label, value]) => [h("dt", { text: label }), h("dd", { text: value })])),
        action, status);
      tickCountdowns(node);
      return node;
    }

    async function approveCard(card, button, status) {
      button.disabled = true;
      status.className = "voice-card-note";
      status.textContent = "starting…";
      try {
        const answer = await api.post("/api/voice/approve", { layer, proposal: card.id, digest: card.digest });
        renderCard({ ...card, ...(answer.card || {}), id: card.id, state: "approved", run_id: answer.run_id || (answer.card && answer.card.run_id), url: answer.url || card.url });
        loadTranscript();
      } catch (error) {
        status.className = "voice-card-note why error";
        status.textContent = error.message;
        if (error.status === 410) renderCard({ ...card, state: "expired" });
        else button.disabled = false;
      }
    }

    function tickCountdowns(scope) {
      const nowS = Date.now() / 1000;
      for (const el of (scope || cardsEl).querySelectorAll(".voice-countdown")) {
        const left = Math.round(parseFloat(el.dataset.expires) - nowS);
        el.textContent = left > 0 ? `approve within ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "expired: ask again";
      }
    }

    // ---- controls
    function setMode(mode, remember) {
      st.mode = mode === "always" ? "always" : "hold";
      radios[0].checked = st.mode === "hold";
      radios[1].checked = st.mode === "always";
      if (remember) { try { localStorage.setItem(MODE_KEY, st.mode); } catch (_error) { /* private mode */ } }
      if (st.mode === "hold") stopListening();
      update();
    }

    for (const radio of radios) {
      radio.addEventListener("change", () => {
        if (!radio.checked) return;
        setMode(radio.value, true);
        if (st.mode === "always") startListening();
      });
    }

    talk.addEventListener("pointerdown", (event) => {
      if (st.mode !== "hold" || event.button !== 0) return;
      event.preventDefault();
      try { talk.setPointerCapture(event.pointerId); } catch (_error) { /* not capturable */ }
      beginHold();
    });
    const release = () => { if (st.mode === "hold") endHold(); };
    talk.addEventListener("pointerup", release);
    talk.addEventListener("pointercancel", release);
    talk.addEventListener("click", (event) => {
      if (st.mode === "always") { if (st.listening) stopListening(); else startListening(); return; }
      if (event.detail === 0) { if (st.holding || st.holdWanted) endHold(); else beginHold(); }   // Enter on the button
    });
    talk.addEventListener("contextmenu", (event) => event.preventDefault());
    stopSpeaking.addEventListener("click", () => interrupt("button"));
    closeMicButton.addEventListener("click", () => closeMic());
    keep.addEventListener("change", async () => {
      const wanted = keep.checked;
      try {
        await api.post("/api/voice/recordings", { layer, keep: wanted });
        setNote(wanted ? "Recordings of this conversation are kept under the layer's agents/voice/." : "Recordings are no longer kept.");
      } catch (error) {
        keep.checked = !wanted;
        setNote(error.message, "warn");
      }
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const said = text.value.trim();
      if (!said) return;
      if (st.speaking) interrupt("text");
      ensureAudio();               // a user gesture: the reply may be spoken
      text.value = "";
      showPerson(said);
      send.disabled = true;
      try { await turn(said, "text"); } finally { send.disabled = false; }
    });

    function onKeyDown(event) {
      const target = event.target;
      const inField = target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName);
      if (event.key === "Escape") {
        if (st.speaking) { event.preventDefault(); event.stopPropagation(); interrupt("escape"); }
        return;
      }
      if (inField || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === " " || event.code === "Space") {
        if (st.mode !== "hold") return;
        event.preventDefault();
        event.stopPropagation();
        if (!event.repeat) beginHold();
        return;
      }
      if (event.key === "m" || event.key === "M") { event.stopPropagation(); closeMic(); }
    }

    function onKeyUp(event) {
      const target = event.target;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      if (event.key === " " || event.code === "Space") {
        if (st.mode !== "hold") return;
        event.preventDefault();
        event.stopPropagation();
        endHold();
      }
    }

    root.addEventListener("keydown", onKeyDown);
    root.addEventListener("keyup", onKeyUp);
    const countdown = setInterval(() => tickCountdowns(), 1000);

    // ---- start
    async function init() {
      let status;
      try { status = await api(`/api/voice/status?${query}`); } catch (error) {
        setNote(`The voice helper is not available: ${error.message}`, "warn");
        st.status = { available: false, why: "not available" };
        talk.disabled = true;
        update();
        return;
      }
      if (st.destroyed) return;
      st.status = status;
      st.interrupt = status.interrupt !== false;
      st.maxUtterance = status.max_utterance || 30;
      let stored = null;
      try { stored = localStorage.getItem(MODE_KEY); } catch (_error) { stored = null; }
      setMode(stored || status.mode || "hold", false);
      if (!status.available) {
        const why = status.why || "voice is off";
        if (status.not_set_up || /not set up/i.test(why)) showNotSetUp(why);
        else setNote(why, "warn");
        talk.disabled = true;
        keep.disabled = true;
        for (const radio of radios) radio.disabled = true;
        update();
        if (!(status.helper && status.helper.enabled)) { text.disabled = true; send.disabled = true; return; }
      }
      if (!secure()) {
        setNote(status.secure_context_note || "The microphone works only on loopback or https; you can type instead.", "warn");
        talk.disabled = true;
        for (const radio of radios) radio.disabled = true;
      }
      if (status.helper && status.helper.enabled === false) {
        setNote(status.helper.why || "the helper cannot answer on this layer", "warn");
      }
      try {
        const session = await api.post("/api/voice/session", { layer });
        keep.checked = !!session.keep_recordings;
      } catch (error) {
        setNote(`No voice session: ${error.message}`, "error");
        return;
      }
      await loadTranscript();
      root.dataset.session = "ready";     // the conversation is open (tests wait for it)
      update();
    }

    update();
    st.raf = requestAnimationFrame(frame);
    // the playback clock (AudioContext seconds) the trace's `start` is on: tests map a moment to a sentence's envelope
    window.alVoice.audioTime = () => (st.ctx ? st.ctx.currentTime : 0);
    init();

    function destroy() {
      if (st.destroyed) return;
      stopPlayback();
      closeMic();
      st.destroyed = true;
      cancelAnimationFrame(st.raf);
      clearInterval(countdown);
      for (const id of st.timers) clearTimeout(id);
      if (st.ctx) st.ctx.close().catch(() => {});
      root.removeEventListener("keydown", onKeyDown);
      root.removeEventListener("keyup", onKeyUp);
      if (root.parentNode) root.parentNode.removeChild(root);
    }

    return { el: root, destroy, interrupt, closeMic };
  }

  window.alVoice = { panel, parseFrames, wavFromInt16, trace: [] };
})();
