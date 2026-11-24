/**
 * Microphone capture for the command bar (07 § Voice loop details): getUserMedia with echo cancellation and
 * noise suppression → AudioWorklet → 16 kHz mono PCM16 chunks + a level for the meter and the VAD.
 *
 * Two steps, so the first word after the hotkey is never lost to setup:
 * - `prepare()` builds the audio graph (AudioContext + the capture worklet) when the page loads. No microphone is
 *   touched and no indicator shows; the context is suspended until it is needed.
 * - `open()` only asks for the microphone stream and plugs it into the prepared graph (the context resumes in
 *   parallel). The stream stays open for a whole conversation (`close()` at the end), so follow-ups start at once.
 */
import { t } from '@shared/i18n'

import workletUrl from './pcm-capture.worklet.js?url'
import { Downsampler, rms } from './resample'

export interface MicHandlers {
  onPcm(pcm: Int16Array): void
  /** RMS level 0..1 and the chunk duration in ms. */
  onLevel(level: number, durationMs: number): void
  onError(error: Error): void
}

export interface MicSource {
  /** Build the audio graph ahead of time (no microphone). Safe to call more than once. */
  prepare(): Promise<void>
  /** Open the microphone stream (getUserMedia only, when prepared). A no-op while it is already open. */
  open(handlers: MicHandlers): Promise<void>
  /** Release the microphone stream (the indicator goes away); the graph stays prepared. */
  close(): void
  /** The microphone stream is open (or opening). */
  readonly live: boolean
}

function micError(err: unknown): Error {
  const e = err as Error | undefined
  if (e?.name === 'NotAllowedError') return new Error(t('mic.denied'))
  if (e?.name === 'NotFoundError') return new Error(t('mic.missing'))
  return new Error(e?.message || t('mic.failed'))
}

export class BrowserMic implements MicSource {
  private context: AudioContext | null = null
  private node: AudioWorkletNode | null = null
  private preparing: Promise<void> | null = null
  private stream: MediaStream | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private handlers: MicHandlers | null = null
  private downsampler: Downsampler | null = null
  private opening = false
  private generation = 0

  get live(): boolean {
    return this.opening || this.stream !== null
  }

  prepare(): Promise<void> {
    this.preparing ??= (async () => {
      const context = new AudioContext({ latencyHint: 'interactive' })
      await context.audioWorklet.addModule(workletUrl)
      const node = new AudioWorkletNode(context, 'open-speech-pcm-capture', { numberOfInputs: 1, numberOfOutputs: 0 })
      node.port.onmessage = (ev: MessageEvent<Float32Array>) => this.onChunk(ev.data)
      this.context = context
      this.node = node
      // Nothing to capture yet: no audio thread running for nothing. `open()` resumes it.
      await context.suspend().catch(() => undefined)
    })().catch((err: unknown) => {
      this.preparing = null // try again on the next open
      throw err
    })
    return this.preparing
  }

  async open(handlers: MicHandlers): Promise<void> {
    this.handlers = handlers
    if (this.live) return
    const generation = ++this.generation
    this.opening = true
    try {
      // The graph and the stream in parallel: whichever is slower decides when the first chunk arrives.
      const [stream] = await Promise.all([
        navigator.mediaDevices.getUserMedia({
          // No automatic gain control: on macOS it changes the system-wide input volume (it moved a test Mac
          // from 50 to 92) and raises room noise. The VAD calibrates to the room instead.
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false, channelCount: 1 },
          video: false,
        }),
        this.prepare(),
      ])
      if (generation !== this.generation) {
        for (const track of stream.getTracks()) track.stop()
        return
      }
      const context = this.context!
      this.downsampler = new Downsampler(context.sampleRate)
      this.source = context.createMediaStreamSource(stream)
      this.source.connect(this.node!)
      this.stream = stream
      await context.resume()
    } catch (err) {
      if (generation === this.generation) {
        this.release()
        handlers.onError(micError(err))
      }
    } finally {
      if (generation === this.generation) this.opening = false
    }
  }

  close(): void {
    this.generation++
    this.opening = false
    this.release()
  }

  /** One-step use (the rules editor's dictation): prepare and open. */
  async start(handlers: MicHandlers): Promise<void> {
    await this.open(handlers)
  }

  /** One-step use: close and drop the graph too. */
  stop(): void {
    this.close()
    if (this.node) {
      this.node.port.onmessage = null
      this.node.disconnect()
    }
    if (this.context) void this.context.close().catch(() => undefined)
    this.node = null
    this.context = null
    this.preparing = null
  }

  private release(): void {
    if (this.source) this.source.disconnect()
    if (this.stream) for (const track of this.stream.getTracks()) track.stop()
    this.source = null
    this.stream = null
    this.downsampler = null
    if (this.context && this.context.state === 'running') void this.context.suspend().catch(() => undefined)
  }

  private onChunk(chunk: Float32Array): void {
    const handlers = this.handlers
    const context = this.context
    if (!this.stream || !handlers || !context || !this.downsampler) return
    handlers.onLevel(rms(chunk), (chunk.length / context.sampleRate) * 1000)
    const pcm = this.downsampler.process(chunk)
    if (pcm.length) handlers.onPcm(pcm)
  }
}
