/**
 * SwasthyaSync — microphone capture for Gemini Live.
 * Converts the mic stream (any sample rate) to 16 kHz mono 16-bit PCM and posts ~100 ms chunks
 * while capturing is switched on (push-to-talk). Also posts a rough input level for the UI.
 */
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.active = false;
    this.pos = 0;
    this.acc = 0;
    this.count = 0;
    this.out = [];
    this.port.onmessage = (e) => {
      if (e.data === 'start') {
        this.active = true;
        this.out = [];
        this.pos = 0; this.acc = 0; this.count = 0;
      } else if (e.data === 'stop') {
        this.flush();
        this.active = false;
        this.port.postMessage({ done: true });
      }
    };
  }

  flush() {
    if (!this.out.length) return;
    const pcm = new Int16Array(this.out.length);
    let sum = 0;
    for (let i = 0; i < this.out.length; i++) {
      const s = Math.max(-1, Math.min(1, this.out[i]));
      sum += s * s;
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    const level = Math.sqrt(sum / this.out.length);
    this.port.postMessage({ pcm: pcm.buffer, level }, [pcm.buffer]);
    this.out = [];
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!this.active || !ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.acc += ch[i];
      this.count += 1;
      this.pos += 1;
      if (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        this.out.push(this.acc / this.count);
        this.acc = 0;
        this.count = 0;
      }
    }
    if (this.out.length >= 1600) this.flush(); // 100 ms at 16 kHz
    return true;
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor);
