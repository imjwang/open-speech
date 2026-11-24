import { afterEach, describe, expect, it } from 'vitest'

import { setLanguage } from '../shared/i18n'
import { trayTooltip, trayUpdates, trayView, trayVisual, type TrayInputs } from './tray-state'

const none: TrayInputs = { listening: false, speaking: false, playing: false, attention: false }
const opts = { detail: null, pulseOn: false, spendLine: null, title: '' }

afterEach(() => setLanguage('en'))

describe('tray state', () => {
  it('shows the most important thing: attention, listening, speaking, reading, idle', () => {
    expect(trayVisual(none)).toBe('idle')
    expect(trayVisual({ ...none, playing: true })).toBe('playing')
    expect(trayVisual({ ...none, playing: true, speaking: true })).toBe('speaking')
    expect(trayVisual({ ...none, speaking: true, listening: true })).toBe('listening')
    expect(trayVisual({ ...none, listening: true, attention: true })).toBe('attention')
  })

  it('says it in the interface language, with the month on its own line', () => {
    expect(trayTooltip('speaking', null)).toBe('Open Speech: speaking')
    expect(trayTooltip('playing', null, 'This month: $0.12 · estimated')).toBe('Open Speech: reading\nThis month: $0.12 · estimated')
    setLanguage('es')
    expect(trayTooltip('attention', 'el motor no está en marcha')).toBe('Open Speech: necesita atención · el motor no está en marcha')
  })

  it('pulses only while listening', () => {
    expect(trayView({ ...none, listening: true }, { ...opts, pulseOn: true }).image).toBe('listening-pulse')
    expect(trayView({ ...none, playing: true }, { ...opts, pulseOn: true }).image).toBe('playing')
  })
})

describe('trayUpdates: set only what changed', () => {
  it('sets everything the first time', () => {
    expect(trayUpdates(null, trayView(none, opts))).toEqual({ image: 'idle', tooltip: 'Open Speech', title: '' })
  })

  it('sets nothing for a playback update that changes nothing on screen', () => {
    const reading = trayView({ ...none, playing: true }, opts)
    // Reading sends a state update every second: the tray must not be touched for those.
    expect(trayUpdates(reading, trayView({ ...none, playing: true }, opts))).toEqual({})
  })

  it("doesn't reset the listening pulse when playback updates arrive", () => {
    const pulsing = trayView({ ...none, listening: true, playing: true }, { ...opts, pulseOn: true })
    const afterPlaybackUpdate = trayView({ ...none, listening: true, playing: true }, { ...opts, pulseOn: true })
    expect(trayUpdates(pulsing, afterPlaybackUpdate)).toEqual({})
    // The pulse timer itself flips the image, and only the image.
    expect(trayUpdates(pulsing, trayView({ ...none, listening: true, playing: true }, opts))).toEqual({ image: 'listening' })
  })

  it('changes the tooltip alone when only the month changes, and the title when it is shown', () => {
    const a = trayView(none, { ...opts, spendLine: 'This month: $0.12 · estimated' })
    const b = trayView(none, { ...opts, spendLine: 'This month: $0.13 · estimated' })
    expect(trayUpdates(a, b)).toEqual({ tooltip: 'Open Speech\nThis month: $0.13 · estimated' })
    expect(trayUpdates(b, { ...b, title: '$0.13' })).toEqual({ title: '$0.13' })
  })
})
