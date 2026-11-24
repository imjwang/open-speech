/**
 * Recording a voice to clone (09 § Voices, 08 § Voice cloning): a capture path of its own, separate from the
 * conversation microphone (mic.ts).
 *
 * The conversation microphone is tuned for commands: echo cancellation and noise suppression on, downsampled to
 * 16 kHz PCM16 for speech recognition. A clone imitates everything it hears, and those filters leave artefacts
 * (pumping, smeared consonants, a "processed" timbre) that would come back in every sentence the clone says. So a
 * reference is captured as the microphone hears it: echo cancellation, noise suppression and automatic gain control
 * off, 48 kHz, kept as float, and written as a 32-bit float WAV for the engine to clean and cut (voices/reference.py).
 * The conversation microphone is closed while this one records (the controller does that).
 */
import { t } from '@shared/i18n'

import workletUrl from './pcm-capture.worklet.js?url'
import { rms } from './resample'

export const REFERENCE_RATE = 48_000

/** What a clone's recording asks of the microphone: the signal as it is. */
export const REFERENCE_CONSTRAINTS: MediaTrackConstraints = {
  echoCancellation: false,
  noiseSuppression: false,
  autoGainControl: false,
  channelCount: 1,
  sampleRate: REFERENCE_RATE,
}

/** A recording stops by itself at this length, whatever happens. */
export const REFERENCE_MAX_S = 30

export interface RecorderHandlers {
  /** RMS level 0..1 of each ~32 ms chunk, and its duration in ms (the meter and end-of-speech detection). */
  onLevel(level: number, durationMs: number): void
  onError(error: Error): void
}

export interface ReferenceTake {
  wav: Blob
  seconds: number
  sampleRate: number
}

export interface ReferenceRecorder {
  start(handlers: RecorderHandlers): Promise<void>
  /** Stop and return what was recorded (null when nothing was). */
  stop(): Promise<ReferenceTake | null>
  /** Stop and throw it away. */
  cancel(): void
  readonly recording: boolean
}

type MediaDevicesLike = Pick<MediaDevices, 'getUserMedia'>

function micError(err: unknown): Error {
  const e = err as Error | undefined
  if (e?.name === 'NotAllowedError') return new Error(t('mic.denied'))
  if (e?.name === 'NotFoundError') return new Error(t('mic.missing'))
  return new Error(e?.message || t('mic.failed'))
}

export class BrowserReferenceRecorder implements ReferenceRecorder {
  private context: AudioContext | null = null
  private stream: MediaStream | null = null
  private node: AudioWorkletNode | null = null
  private chunks: Float32Array[] = []
  private samples = 0
  private handlers: RecorderHandlers | null = null
  private generation = 0

  constructor(
    private readonly media: () => MediaDevicesLike = () => navigator.mediaDevices,
    private readonly makeContext: (sampleRate: number) => AudioContext = (sampleRate) => new AudioContext({ sampleRate, latencyHint: 'interactive' }),
  ) {}

  get recording(): boolean {
    return this.stream !== null
  }

  async start(handlers: RecorderHandlers): Promise<void> {
    this.cancel()
    const generation = ++this.generation
    this.handlers = handlers
    this.chunks = []
    this.samples = 0
    try {
      const stream = await this.media().getUserMedia({ audio: REFERENCE_CONSTRAINTS, video: false })
      if (generation !== this.generation) {
        for (const track of stream.getTracks()) track.stop()
        return
      }
      const context = this.makeContext(REFERENCE_RATE)
      await context.audioWorklet.addModule(workletUrl)
      const node = new AudioWorkletNode(context, 'open-speech-pcm-capture', { numberOfInputs: 1, numberOfOutputs: 0 })
      node.port.onmessage = (ev: MessageEvent<Float32Array>) => this.onChunk(ev.data)
      context.createMediaStreamSource(stream).connect(node)
      this.context = context
      this.node = node
      this.stream = stream
      await context.resume()
    } catch (err) {
      if (generation === this.generation) {
        this.release()
        handlers.onError(micError(err))
      }
    }
  }

  async stop(): Promise<ReferenceTake | null> {
    const sampleRate = this.context?.sampleRate ?? REFERENCE_RATE
    const chunks = this.chunks
    const total = this.samples
    this.generation++
    this.release()
    if (!total) return null
    const all = new Float32Array(total)
    let at = 0
    for (const c of chunks) {
      all.set(c, at)
      at += c.length
    }
    return { wav: encodeWav(all, sampleRate), seconds: total / sampleRate, sampleRate }
  }

  cancel(): void {
    this.generation++
    this.release()
    this.chunks = []
    this.samples = 0
  }

  private onChunk(chunk: Float32Array): void {
    const context = this.context
    if (!this.stream || !context) return
    const max = REFERENCE_MAX_S * context.sampleRate
    if (this.samples < max) {
      const keep = chunk.length <= max - this.samples ? chunk : chunk.subarray(0, max - this.samples)
      this.chunks.push(keep.slice())
      this.samples += keep.length
    }
    this.handlers?.onLevel(rms(chunk), (chunk.length / context.sampleRate) * 1000)
  }

  private release(): void {
    if (this.node) {
      this.node.port.onmessage = null
      this.node.disconnect()
    }
    if (this.stream) for (const track of this.stream.getTracks()) track.stop()
    if (this.context) void this.context.close().catch(() => undefined)
    this.node = null
    this.stream = null
    this.context = null
    this.handlers = null
  }
}

/** Mono IEEE-float WAV (format 3): the capture exactly as it was, for the engine to process. */
export function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const bytes = samples.length * 4
  const buffer = new ArrayBuffer(44 + bytes)
  const v = new DataView(buffer)
  const ascii = (at: number, s: string) => {
    for (let i = 0; i < s.length; i++) v.setUint8(at + i, s.charCodeAt(i))
  }
  ascii(0, 'RIFF')
  v.setUint32(4, 36 + bytes, true)
  ascii(8, 'WAVE')
  ascii(12, 'fmt ')
  v.setUint32(16, 16, true) // fmt chunk size
  v.setUint16(20, 3, true) // IEEE float
  v.setUint16(22, 1, true) // mono
  v.setUint32(24, sampleRate, true)
  v.setUint32(28, sampleRate * 4, true) // byte rate
  v.setUint16(32, 4, true) // block align
  v.setUint16(34, 32, true) // bits per sample
  ascii(36, 'data')
  v.setUint32(40, bytes, true)
  new Float32Array(buffer, 44, samples.length).set(samples)
  return new Blob([buffer], { type: 'audio/wav' })
}
