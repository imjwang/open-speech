/**
 * Energy voice-activity detector for end of utterance (07: "800 ms of silence (energy VAD in v1)").
 * Tracks an adaptive noise floor; speech starts after `minSpeechMs` above floor + margin and ends after
 * `endOfSpeechMs` (settings `voice_input.end_of_speech_ms`) below it. It fires `speech-end` at most once
 * per utterance; `reset()` arms it again.
 */
export type VadEvent = 'speech-start' | 'speech-end'

export interface VadOptions {
  endOfSpeechMs?: number
  minSpeechMs?: number
  /** dB above the noise floor that counts as speech. */
  marginDb?: number
  /** Absolute floor for the speech threshold (dBFS). */
  minThresholdDb?: number
}

export function toDb(level: number): number {
  return 20 * Math.log10(Math.max(level, 1e-8))
}

export class EnergyVad {
  endOfSpeechMs: number
  private readonly minSpeechMs: number
  private readonly marginDb: number
  private readonly minThresholdDb: number
  private floorDb = -60
  private speechMs = 0
  private silenceMs = 0
  private speaking = false
  private ended = false
  private heardSpeech = false

  constructor(options: VadOptions = {}) {
    this.endOfSpeechMs = options.endOfSpeechMs ?? 800
    this.minSpeechMs = options.minSpeechMs ?? 150
    this.marginDb = options.marginDb ?? 12
    this.minThresholdDb = options.minThresholdDb ?? -50
  }

  get isSpeaking(): boolean {
    return this.speaking
  }

  /** True once any speech was detected since the last reset. */
  get hasSpeech(): boolean {
    return this.heardSpeech
  }

  get threshold(): number {
    return Math.max(this.floorDb + this.marginDb, this.minThresholdDb)
  }

  /** Feed one chunk's RMS level (0..1) and its duration. */
  push(level: number, durationMs: number): VadEvent | null {
    if (this.ended) return null
    const db = toDb(level)
    const loud = db > this.threshold
    if (!this.speaking) {
      // Noise floor: follow drops quickly, rises slowly (never learns speech as noise).
      if (!loud) this.floorDb = db < this.floorDb ? db : this.floorDb + (db - this.floorDb) * 0.05
      if (loud) {
        this.speechMs += durationMs
        if (this.speechMs >= this.minSpeechMs) {
          this.speaking = true
          this.heardSpeech = true
          this.silenceMs = 0
          return 'speech-start'
        }
      } else {
        this.speechMs = Math.max(0, this.speechMs - durationMs)
      }
      return null
    }
    if (loud) {
      this.silenceMs = 0
      return null
    }
    this.silenceMs += durationMs
    if (this.silenceMs >= this.endOfSpeechMs) {
      this.speaking = false
      this.ended = true
      return 'speech-end'
    }
    return null
  }

  reset(): void {
    this.speechMs = 0
    this.silenceMs = 0
    this.speaking = false
    this.ended = false
    this.heardSpeech = false
  }
}
