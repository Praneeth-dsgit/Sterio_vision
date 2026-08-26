function audioWsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/audio`;
}

class LiveAudioPlayer {
  constructor() {
    this.ws = null;
    this.ctx = null;
    this.nextTime = 0;
    this.rate = 24000;
    this.channels = 1;
    this.muted = false;
    this.connected = false;
    this.deviceLabel = "";
    this._want = true;
    this._pending = [];
    this._maxPending = 12;
  }

  setMuted(on) {
    this.muted = !!on;
    if (this.muted) this._pending = [];
  }

  async resume() {
    try {
      if (!this.ctx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        this.ctx = new AC();
      }
      if (this.ctx.state === "suspended") await this.ctx.resume();
    } catch (err) {
      console.warn("audio resume", err);
    }
  }

  connect() {
    this._want = true;
    this.disconnect(false);
    const socket = new WebSocket(audioWsUrl());
    socket.binaryType = "arraybuffer";
    this.ws = socket;
    socket.onopen = () => {
      this.connected = true;
    };
    socket.onclose = () => {
      this.connected = false;
      this.ws = null;
      if (this._want) setTimeout(() => this.connect(), 900);
    };
    socket.onerror = () => socket.close();
    socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "hello") {
            this.rate = Number(msg.rate) || 24000;
            this.channels = Number(msg.channels) || 1;
            this.deviceLabel = msg.device || "";
          }
        } catch (err) {
          /* ignore */
        }
        return;
      }
      if (this.muted || !event.data || !event.data.byteLength) return;
      this._enqueuePcm(event.data);
    };
  }

  disconnect(stopWant = true) {
    if (stopWant) this._want = false;
    if (this.ws) {
      this.ws.onclose = null;
      try {
        this.ws.close();
      } catch (err) {
        /* ignore */
      }
      this.ws = null;
    }
    this.connected = false;
    this._pending = [];
  }

  _enqueuePcm(arrayBuffer) {
    if (!this.ctx) {
      try {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        this.ctx = new AC();
      } catch (err) {
        return;
      }
    }
    if (this.ctx.state === "suspended") return;
    const samples = this.channels * Math.floor(arrayBuffer.byteLength / 2);
    if (samples < 32) return;
    const int16 = new Int16Array(arrayBuffer, 0, samples);
    const f32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) f32[i] = int16[i] / 32768;
    const frames = Math.floor(f32.length / this.channels);
    const buffer = this.ctx.createBuffer(this.channels, frames, this.rate);
    if (this.channels === 1) {
      buffer.copyToChannel(f32, 0);
    } else {
      for (let ch = 0; ch < this.channels; ch++) {
        const plane = new Float32Array(frames);
        for (let i = 0; i < frames; i++) plane[i] = f32[i * this.channels + ch];
        buffer.copyToChannel(plane, ch);
      }
    }
    this._pending.push(buffer);
    while (this._pending.length > this._maxPending) this._pending.shift();
    this._pump();
  }

  _pump() {
    if (!this.ctx || this.muted) return;
    const now = this.ctx.currentTime;
    if (this.nextTime < now + 0.04) this.nextTime = now + 0.06;
    while (this._pending.length) {
      // Keep ~250ms ahead max
      if (this.nextTime > now + 0.35) break;
      const buffer = this._pending.shift();
      const src = this.ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(this.ctx.destination);
      src.start(this.nextTime);
      this.nextTime += buffer.duration;
    }
  }
}
