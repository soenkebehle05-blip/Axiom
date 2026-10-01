// Captures mic audio and posts 16-bit little-endian PCM chunks (~100 ms) to the main thread.
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = [];
    this.samples = 0;
    this.chunkSamples = Math.round(sampleRate / 10); // ~100 ms at the context's rate
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    this.buffer.push(new Float32Array(channel));
    this.samples += channel.length;
    if (this.samples >= this.chunkSamples) {
      const out = new Int16Array(this.samples);
      let offset = 0;
      for (const block of this.buffer) {
        for (let i = 0; i < block.length; i++) {
          const s = Math.max(-1, Math.min(1, block[i]));
          out[offset++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
      }
      this.port.postMessage(out.buffer, [out.buffer]);
      this.buffer = [];
      this.samples = 0;
    }
    return true;
  }
}
registerProcessor("pcm-capture", PcmCaptureProcessor);
