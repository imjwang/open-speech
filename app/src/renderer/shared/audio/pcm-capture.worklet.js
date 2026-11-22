// AudioWorklet: forwards mono Float32 input in ~32 ms batches to the main thread, which downsamples
// to 16 kHz PCM16 (see resample.ts). Plain JS: loaded via `audioWorklet.addModule()` as a Vite asset.
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this.batch = Math.max(128, Math.round(sampleRate * 0.032))
    this.buffer = new Float32Array(this.batch)
    this.filled = 0
  }

  process(inputs) {
    const input = inputs[0]
    if (!input || input.length === 0) return true
    const channels = input.length
    const frames = input[0].length
    for (let i = 0; i < frames; i++) {
      let s = 0
      for (let c = 0; c < channels; c++) s += input[c][i]
      this.buffer[this.filled++] = s / channels
      if (this.filled === this.batch) {
        this.port.postMessage(this.buffer, [this.buffer.buffer])
        this.buffer = new Float32Array(this.batch)
        this.filled = 0
      }
    }
    return true
  }
}

registerProcessor('open-speech-pcm-capture', PcmCaptureProcessor)
