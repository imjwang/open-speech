/**
 * The capture path for a voice to clone: the microphone as it is (no echo cancellation, noise suppression or gain
 * control, 48 kHz), kept as float and written as a WAV (the upload itself: shared/api/client.test.ts).
 */
import { describe, expect, it, vi } from 'vitest'

import { BrowserReferenceRecorder, encodeWav, REFERENCE_CONSTRAINTS, REFERENCE_RATE } from './reference'

vi.mock('./pcm-capture.worklet.js?url', () => ({ default: 'worklet.js' }))

class FakeNode {
  port: { onmessage: ((ev: MessageEvent<Float32Array>) => void) | null } = { onmessage: null }
  disconnect = vi.fn()
}

function fakeAudio() {
  const nodes: FakeNode[] = []
  const contexts: Array<{ sampleRate: number; closed: boolean }> = []
  ;(globalThis as unknown as { AudioWorkletNode: unknown }).AudioWorkletNode = class extends FakeNode {
    constructor() {
      super()
      nodes.push(this)
    }
  }
  const makeContext = (sampleRate: number) => {
    const ctx = {
      sampleRate,
      closed: false,
      audioWorklet: { addModule: vi.fn(async () => undefined) },
      createMediaStreamSource: () => ({ connect: vi.fn() }),
      resume: vi.fn(async () => undefined),
      close: vi.fn(async () => {
        ctx.closed = true
      }),
    }
    contexts.push(ctx)
    return ctx as unknown as AudioContext
  }
  const track = { stop: vi.fn() }
  const getUserMedia = vi.fn(async () => ({ getTracks: () => [track] }) as unknown as MediaStream)
  return { nodes, contexts, makeContext, getUserMedia, track }
}

describe('the reference capture', () => {
  it('asks for the microphone with echo cancellation, noise suppression and gain control off, at 48 kHz', async () => {
    const a = fakeAudio()
    const rec = new BrowserReferenceRecorder(() => ({ getUserMedia: a.getUserMedia }), a.makeContext)
    await rec.start({ onLevel: vi.fn(), onError: vi.fn() })
    expect(a.getUserMedia).toHaveBeenCalledWith({ audio: REFERENCE_CONSTRAINTS, video: false })
    expect(REFERENCE_CONSTRAINTS).toMatchObject({ echoCancellation: false, noiseSuppression: false, autoGainControl: false, sampleRate: 48000 })
    expect(a.contexts[0]?.sampleRate).toBe(REFERENCE_RATE)
    expect(rec.recording).toBe(true)
  })

  it('keeps every sample as float, reports the level, and stops into a WAV', async () => {
    const a = fakeAudio()
    const levels: number[] = []
    const rec = new BrowserReferenceRecorder(() => ({ getUserMedia: a.getUserMedia }), a.makeContext)
    await rec.start({ onLevel: (l) => levels.push(l), onError: vi.fn() })
    const node = a.nodes[0]!
    const chunk = new Float32Array(1536).fill(0.25)
    node.port.onmessage?.({ data: chunk } as MessageEvent<Float32Array>)
    node.port.onmessage?.({ data: chunk } as MessageEvent<Float32Array>)
    expect(levels[0]).toBeCloseTo(0.25, 5)
    const take = await rec.stop()
    expect(take?.seconds).toBeCloseTo(3072 / 48000, 6)
    expect(a.track.stop).toHaveBeenCalled()
    expect(a.contexts[0]?.closed).toBe(true)
    expect(rec.recording).toBe(false)
    const bytes = new DataView(await take!.wav.arrayBuffer())
    expect(bytes.getUint16(20, true)).toBe(3) // IEEE float
    expect(bytes.getUint32(24, true)).toBe(48000)
    expect(bytes.getUint32(40, true)).toBe(3072 * 4)
    expect(bytes.getFloat32(44, true)).toBeCloseTo(0.25, 6)
  })

  it('a denied microphone is reported, and nothing stays open', async () => {
    const a = fakeAudio()
    const denied = Object.assign(new Error('denied'), { name: 'NotAllowedError' })
    const onError = vi.fn()
    const rec = new BrowserReferenceRecorder(() => ({ getUserMedia: vi.fn(async () => Promise.reject(denied)) }), a.makeContext)
    await rec.start({ onLevel: vi.fn(), onError })
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: expect.stringContaining('Microphone access is off') }))
    expect(rec.recording).toBe(false)
  })

  it('encodes a valid RIFF header', async () => {
    const wav = encodeWav(new Float32Array([0, 0.5, -0.5]), 48000)
    const v = new DataView(await wav.arrayBuffer())
    const text = (at: number) => String.fromCharCode(v.getUint8(at), v.getUint8(at + 1), v.getUint8(at + 2), v.getUint8(at + 3))
    expect([text(0), text(8), text(12), text(36)]).toEqual(['RIFF', 'WAVE', 'fmt ', 'data'])
    expect(v.getUint32(4, true)).toBe(36 + 12)
    expect(v.getFloat32(48, true)).toBeCloseTo(0.5, 6)
  })
})
