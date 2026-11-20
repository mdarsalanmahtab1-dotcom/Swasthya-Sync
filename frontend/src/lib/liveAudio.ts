/**
 * SwasthyaSync — audio plumbing for the Gemini Live voice path.
 *
 * livePlayer: plays Live's streamed answer (16-bit PCM, 24 kHz) gap-free as chunks arrive.
 * liveMic:    push-to-talk microphone capture → 16 kHz 16-bit PCM chunks (via an AudioWorklet).
 *
 * Both are singletons so the WebSocket hook and the conversation screen share them.
 */

type Listener = () => void;

function b64ToInt16(b64: string): Int16Array {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Int16Array(bytes.buffer);
}

export function arrayBufferToB64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let bin = '';
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    bin += String.fromCharCode(...bytes.subarray(i, i + step));
  }
  return btoa(bin);
}

class LivePlayer {
  private ctx: AudioContext | null = null;
  private nextTime = 0;
  private sources = new Set<AudioBufferSourceNode>();
  private listeners = new Set<Listener>();
  playing = false;

  private ensure(): AudioContext {
    if (!this.ctx) this.ctx = new AudioContext();
    if (this.ctx.state === 'suspended') void this.ctx.resume();
    return this.ctx;
  }

  /** Call from a user gesture (e.g. first tap) so the browser allows audio playback. */
  unlock() {
    this.ensure();
  }

  play(b64: string, rate = 24000) {
    const ctx = this.ensure();
    const pcm = b64ToInt16(b64);
    if (!pcm.length) return;
    const buffer = ctx.createBuffer(1, pcm.length, rate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) data[i] = pcm[i] / 0x8000;
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
    const startAt = Math.max(ctx.currentTime + 0.04, this.nextTime);
    src.start(startAt);
    this.nextTime = startAt + buffer.duration;
    this.sources.add(src);
    src.onended = () => {
      this.sources.delete(src);
      if (this.sources.size === 0) this.setPlaying(false);
    };
    this.setPlaying(true);
  }

  stop() {
    this.sources.forEach((s) => {
      try { s.stop(); } catch { /* already stopped */ }
    });
    this.sources.clear();
    this.nextTime = 0;
    this.setPlaying(false);
  }

  private setPlaying(v: boolean) {
    if (this.playing === v) return;
    this.playing = v;
    this.listeners.forEach((l) => l());
  }

  subscribe(l: Listener): () => void {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }
}

class LiveMic {
  private ctx: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private stream: MediaStream | null = null;
  private onChunk: ((b64: string) => void) | null = null;
  private listeners = new Set<Listener>();
  recording = false;
  level = 0;

  private async ensure() {
    if (this.node && this.ctx) {
      if (this.ctx.state === 'suspended') await this.ctx.resume();
      return;
    }
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    this.ctx = new AudioContext();
    await this.ctx.audioWorklet.addModule('/pcm-capture-worklet.js');
    const source = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, 'pcm-capture');
    const mute = this.ctx.createGain();
    mute.gain.value = 0; // keep the graph running without playing the mic back
    source.connect(this.node);
    this.node.connect(mute).connect(this.ctx.destination);
    this.node.port.onmessage = (e) => {
      const msg = e.data as { pcm?: ArrayBuffer; level?: number; done?: boolean };
      if (msg.pcm && this.onChunk) {
        this.onChunk(arrayBufferToB64(msg.pcm));
        this.level = msg.level ?? 0;
        this.notify();
      }
      if (msg.done && this.pendingStop) {
        this.pendingStop();
        this.pendingStop = null;
      }
    };
  }

  private pendingStop: (() => void) | null = null;

  async start(onChunk: (b64: string) => void) {
    await this.ensure();
    this.onChunk = onChunk;
    this.node!.port.postMessage('start');
    this.recording = true;
    this.notify();
  }

  /** Stops capture and resolves after the last buffered chunk was delivered. */
  stop(): Promise<void> {
    if (!this.node || !this.recording) return Promise.resolve();
    return new Promise((resolve) => {
      const timer = window.setTimeout(() => { this.pendingStop = null; resolve(); }, 300);
      this.pendingStop = () => { window.clearTimeout(timer); resolve(); };
      this.node!.port.postMessage('stop');
      this.recording = false;
      this.level = 0;
      this.notify();
    });
  }

  private notify() {
    this.listeners.forEach((l) => l());
  }

  subscribe(l: Listener): () => void {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }
}

export const livePlayer = new LivePlayer();
export const liveMic = new LiveMic();
