import { describe, expect, it } from 'vitest'

import { EnergyVad, type VadEvent } from './vad'

const dbToLevel = (db: number) => 10 ** (db / 20)

function feed(vad: EnergyVad, db: number, ms: number, chunkMs = 10): VadEvent[] {
  const events: VadEvent[] = []
  for (let t = 0; t < ms; t += chunkMs) {
    const e = vad.push(dbToLevel(db), chunkMs)
    if (e) events.push(e)
  }
  return events
}

describe('EnergyVad', () => {
  it('ends an utterance in a normal room (ambient near -30 dBFS with gain control)', () => {
    // Regression: the floor started at -60 dBFS, so a -30 dBFS room counted as speech forever.
    const vad = new EnergyVad()
    expect(feed(vad, -30, 400)).toEqual([]) // calibration + room tone: no speech
    expect(feed(vad, -12, 600)).toEqual(['speech-start'])
    expect(feed(vad, -30, 900)).toEqual(['speech-end'])
  })

  it('still works in a quiet room', () => {
    const vad = new EnergyVad()
    feed(vad, -70, 300)
    expect(feed(vad, -25, 400)).toEqual(['speech-start'])
    expect(feed(vad, -70, 900)).toEqual(['speech-end'])
  })

  it('does not take a word said right away as the room', () => {
    const vad = new EnergyVad()
    // Speech begins 100 ms into calibration; the 30th percentile still lands on the room.
    feed(vad, -32, 100)
    feed(vad, -12, 150)
    expect(feed(vad, -12, 400)).toEqual(['speech-start'])
    expect(feed(vad, -32, 900)).toEqual(['speech-end'])
  })

  it('re-measures the room after reset', () => {
    const vad = new EnergyVad()
    feed(vad, -70, 300)
    vad.reset()
    expect(feed(vad, -30, 400)).toEqual([])
    expect(feed(vad, -12, 300)).toEqual(['speech-start'])
  })

  it('keeps the room it measured for a follow-up: speech counts from the first chunk', () => {
    const vad = new EnergyVad()
    feed(vad, -32, 300)
    feed(vad, -12, 300)
    feed(vad, -32, 900)
    vad.reset({ keepFloor: true })
    // No 250 ms of calibration first: a reply right away is heard.
    expect(feed(vad, -12, 160)).toEqual(['speech-start'])
    expect(vad.threshold).toBeLessThan(-12)
  })

  it('keepFloor before the room was ever measured still calibrates', () => {
    const vad = new EnergyVad()
    vad.reset({ keepFloor: true })
    expect(feed(vad, -12, 200)).toEqual([])
  })
})
