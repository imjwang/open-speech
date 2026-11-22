/**
 * Microphone capture for the command bar (07 § Voice loop details): getUserMedia with echo
 * cancellation, noise suppression and AGC → AudioWorklet → 16 kHz mono PCM16 chunks + a level for the
 * meter and the VAD. The stream is released on stop (no mic indicator while the bar is closed).
 */
import workletUrl from './pcm-capture.worklet.js?url'
import { Downsampler, rms } from './resample'

export interface MicHandlers {
  onPcm(pcm: Int16Array): void
  /** RMS level 0..1 and the chunk duration in ms. */
  onLevel(level: number, durationMs: number): void
  onError(error: Error): void
}

export interface MicSource {
  start(handlers: MicHandlers): Promise<void>
  stop(): void
  readonly active: boolean
}

export class BrowserMic implements MicSource {
  private stream: MediaStream | null = null
  private context: AudioContext | null = null
  private node: AudioWorkletNode | null = null
  private generation = 0

  get active(): boolean {
    return this.stream !== null
  }

  async start(handlers: MicHandlers): Promise<void> {
    this.stop()
    const generation = ++this.generation
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        video: false,
      })
      if (generation !== this.generation) {
        for (const t of stream.getTracks()) t.stop()
        return
      }
      const context = new AudioContext({ latencyHint: 'interactive' })
      await context.audioWorklet.addModule(workletUrl)
      if (generation !== this.generation) {
        for (const t of stream.getTracks()) t.stop()
        void context.close()
        return
      }
      const source = context.createMediaStreamSource(stream)
      const node = new AudioWorkletNode(context, 'open-speech-pcm-capture', { numberOfInputs: 1, numberOfOutputs: 0 })
      const downsampler = new Downsampler(context.sampleRate)
      node.port.onmessage = (ev: MessageEvent<Float32Array>) => {
        if (generation !== this.generation) return
        const chunk = ev.data
        handlers.onLevel(rms(chunk), (chunk.length / context.sampleRate) * 1000)
        const pcm = downsampler.process(chunk)
        if (pcm.length) handlers.onPcm(pcm)
      }
      source.connect(node)
      this.stream = stream
      this.context = context
      this.node = node
    } catch (err) {
      if (generation === this.generation) {
        const e = err as Error
        handlers.onError(
          new Error(
            e?.name === 'NotAllowedError'
              ? 'Microphone access is off. Allow it for Open Speech, or type instead.'
              : e?.name === 'NotFoundError'
                ? 'No microphone found. Type instead.'
                : e?.message || 'The microphone could not start.',
          ),
        )
      }
    }
  }

  stop(): void {
    this.generation++
    if (this.node) {
      this.node.port.onmessage = null
      this.node.disconnect()
    }
    if (this.stream) for (const t of this.stream.getTracks()) t.stop()
    if (this.context) void this.context.close().catch(() => undefined)
    this.node = null
    this.stream = null
    this.context = null
  }
}
