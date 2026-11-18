/* al work dashboard — the voice panel's capture worklet (AudioWorkletProcessor "al-capture").
 *
 * Takes the microphone at the context's rate, resamples it to 16 kHz mono (linear interpolation over
 * the averaged channels), and posts to the page:
 *   {level}               every 20 ms: RMS of the last 20 ms (0–1, not normalised)
 *   {pcm, level}          every 100 ms: an Int16Array of 1600 samples (the 100 ms just captured)
 * Nothing leaves the page from here; voice.js decides what is sent to the dashboard.
 */
"use strict";

const TARGET_RATE = 16000;
const FRAME = 1600;          // 100 ms at 16 kHz
const LEVEL_EVERY = 320;     // 20 ms at 16 kHz

class AlCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / TARGET_RATE;   // input samples per output sample
    this.pos = 0;                           // fractional read position into the carried input
    this.carry = new Float32Array(0);       // input samples not consumed yet
    this.frame = new Int16Array(FRAME);
    this.filled = 0;
    this.sumSq = 0;
    this.count = 0;
    this.lastLevel = 0;
    this.running = true;
    this.port.onmessage = (event) => { if (event.data && event.data.stop) this.running = false; };
  }

  process(inputs) {
    const input = inputs[0];
    if (!this.running) return false;
    if (!input || !input.length || !input[0].length) return true;
    const length = input[0].length;
    const mono = new Float32Array(this.carry.length + length);
    mono.set(this.carry, 0);
    for (let i = 0; i < length; i += 1) {
      let sum = 0;
      for (let c = 0; c < input.length; c += 1) sum += input[c][i];
      mono[this.carry.length + i] = sum / input.length;
    }
    let pos = this.pos;
    while (pos + 1 < mono.length) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const value = mono[i] * (1 - frac) + mono[i + 1] * frac;
      this.push(value);
      pos += this.step;
    }
    const keep = Math.floor(pos);
    this.carry = mono.slice(keep);
    this.pos = pos - keep;
    return true;
  }

  push(value) {
    const clamped = Math.max(-1, Math.min(1, value));
    this.frame[this.filled] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    this.filled += 1;
    this.sumSq += clamped * clamped;
    this.count += 1;
    if (this.count >= LEVEL_EVERY) {
      this.lastLevel = Math.sqrt(this.sumSq / this.count);
      this.sumSq = 0;
      this.count = 0;
      this.port.postMessage({ level: this.lastLevel });
    }
    if (this.filled >= FRAME) {
      const pcm = this.frame;
      this.frame = new Int16Array(FRAME);
      this.filled = 0;
      this.port.postMessage({ pcm, level: this.lastLevel }, [pcm.buffer]);
    }
  }
}

registerProcessor("al-capture", AlCapture);
