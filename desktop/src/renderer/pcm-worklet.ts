/**
 * AudioWorklet that resamples the capture device's rate to 16 kHz PCM16 and
 * posts 20 ms chunks to the renderer, which forwards them to /ws/duplex as
 * `audio.frame` headers + binary payload.
 */

import { Resampler } from "../shared/resample.js";

const TARGET_RATE = 16000;
const CHUNK_MS = 20;
const CHUNK_SAMPLES = (TARGET_RATE * CHUNK_MS) / 1000;

class PcmResampler extends AudioWorkletProcessor {
  private resampler = new Resampler(sampleRate / TARGET_RATE, CHUNK_SAMPLES);

  process(inputs: Float32Array[][]): boolean {
    const input = inputs[0]?.[0];
    if (!input || input.length === 0) return true;
    // Nearest-sample downsample; good enough for speech at these ratios and
    // the runtime's unit mapping only needs timing, not hi-fi.
    for (const pcm of this.resampler.push(input)) {
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}

registerProcessor("pcm-resampler", PcmResampler);
