/**
 * Pure nearest-sample downsampler for the PCM AudioWorklet. The fractional
 * source position is carried across `push` calls — restarting at zero per
 * block emits ceil(blockLen/ratio) samples per block and drifts the output
 * above the target rate (≈16.19 kHz at a 44.1 kHz source).
 */

export class Resampler {
  private acc: number[] = [];
  private pos = 0;

  constructor(
    private readonly ratio: number,
    private readonly chunkSamples: number,
  ) {}

  /** Feeds a source block; returns every completed 16 kHz PCM16 chunk. */
  push(input: Float32Array): Int16Array[] {
    while (this.pos < input.length) {
      this.acc.push(input[Math.floor(this.pos)]);
      this.pos += this.ratio;
    }
    this.pos -= input.length;
    const out: Int16Array[] = [];
    while (this.acc.length >= this.chunkSamples) {
      const chunk = this.acc.splice(0, this.chunkSamples);
      const pcm = new Int16Array(chunk.length);
      for (let i = 0; i < chunk.length; i++) {
        const s = Math.max(-1, Math.min(1, chunk[i]));
        pcm[i] = Math.round(s * 32767);
      }
      out.push(pcm);
    }
    return out;
  }
}
