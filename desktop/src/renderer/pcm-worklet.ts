/**
 * AudioWorklet that resamples the capture device's rate to 16 kHz PCM16 and
 * posts 20 ms chunks to the renderer, which forwards them to /ws/duplex as
 * `audio.frame` headers + binary payload.
 */

const TARGET_RATE = 16000;
const CHUNK_MS = 20;
const CHUNK_SAMPLES = (TARGET_RATE * CHUNK_MS) / 1000;

class PcmResampler extends AudioWorkletProcessor {
  private acc: number[] = [];
  // Fractional source position carried across process() calls — restarting at
  // zero per block emits floor(128/ratio)+1 samples per block, drifting the
  // output above 16 kHz (≈16.19 kHz at a 44.1 kHz device rate).
  private pos = 0;

  constructor() {
    super();
  }

  process(inputs: Float32Array[][]): boolean {
    const input = inputs[0]?.[0];
    if (!input || input.length === 0) return true;
    // Nearest-sample downsample; good enough for speech at these ratios and
    // the runtime's unit mapping only needs timing, not hi-fi.
    const ratio = sampleRate / TARGET_RATE;
    while (this.pos < input.length) {
      this.acc.push(input[Math.floor(this.pos)]);
      this.pos += ratio;
    }
    this.pos -= input.length;
    while (this.acc.length >= CHUNK_SAMPLES) {
      const chunk = this.acc.splice(0, CHUNK_SAMPLES);
      const pcm = new Int16Array(chunk.length);
      for (let i = 0; i < chunk.length; i++) {
        const s = Math.max(-1, Math.min(1, chunk[i]));
        pcm[i] = Math.round(s * 32767);
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}

registerProcessor("pcm-resampler", PcmResampler);
