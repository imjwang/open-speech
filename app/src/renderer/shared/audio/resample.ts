/**
 * Float32 audio at the AudioContext rate → 16 kHz mono 16-bit PCM (07 § Voice loop details). A
 * streaming box-filter decimator: each output sample averages the input samples it spans, which is a
 * cheap anti-aliasing low-pass that is adequate for speech recognition.
 */
export const TARGET_RATE = 16_000

export class Downsampler {
  private readonly ratio: number
  private pos = 0 // fractional input position of the next output sample, relative to the carry start
  private carry: Float32Array = new Float32Array(0)

  constructor(
    readonly inputRate: number,
    readonly outputRate: number = TARGET_RATE,
  ) {
    if (!(inputRate > 0) || !(outputRate > 0)) throw new Error('sample rates must be positive')
    if (outputRate > inputRate) throw new Error('Downsampler cannot upsample')
    this.ratio = inputRate / outputRate
  }

  /** Process a chunk; returns the 16-bit samples ready so far (state carries across chunks). */
  process(input: Float32Array): Int16Array {
    const data = new Float32Array(this.carry.length + input.length)
    data.set(this.carry, 0)
    data.set(input, this.carry.length)
    const out: number[] = []
    let pos = this.pos
    while (pos + this.ratio <= data.length) {
      const start = Math.floor(pos)
      const end = Math.min(data.length, Math.floor(pos + this.ratio))
      let sum = 0
      let n = 0
      for (let i = start; i < Math.max(end, start + 1); i++) {
        sum += data[i] ?? 0
        n++
      }
      out.push(floatToInt16(n ? sum / n : 0))
      pos += this.ratio
    }
    const consumed = Math.floor(pos)
    this.carry = data.slice(consumed)
    this.pos = pos - consumed
    return Int16Array.from(out)
  }

  reset(): void {
    this.pos = 0
    this.carry = new Float32Array(0)
  }
}

export function floatToInt16(x: number): number {
  const s = Math.max(-1, Math.min(1, x))
  return s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff)
}

/** Root-mean-square level of a float chunk (0..1). */
export function rms(samples: Float32Array): number {
  if (samples.length === 0) return 0
  let sum = 0
  for (const s of samples) sum += s * s
  return Math.sqrt(sum / samples.length)
}
