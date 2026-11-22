import { describe, expect, it } from 'vitest'

import type { Voice } from '@shared/api/client'

import { matchVoiceAnswer, pickSampleVoices, stepPrompt } from './Onboarding'

const KOKORO = 'kokoro:kokoro-82m-v1.0'

const voice = (id: string, provider: string, patch: Partial<Voice> = {}): Voice => ({
  id,
  provider,
  model: provider,
  name: id.split(':').pop() ?? id,
  styles: false,
  local: provider === 'kokoro',
  available: true,
  timestamps: true,
  ...patch,
})

const kokoro = (short: string, patch: Partial<Voice> = {}) => voice(`${KOKORO}:${short}`, 'kokoro', patch)

const LOCAL = [kokoro('af_heart'), kokoro('af_bella'), kokoro('am_michael'), kokoro('bf_emma'), kokoro('bm_george')]
const DEFAULT = `${KOKORO}:af_heart`

const ids = (vs: Voice[]) => vs.map((v) => v.id.split(':').pop())

describe('pickSampleVoices', () => {
  it('keyless: the default Kokoro voice, then contrasting accents/genders', () => {
    expect(ids(pickSampleVoices(LOCAL, DEFAULT))).toEqual(['af_heart', 'am_michael', 'bf_emma'])
  })

  it('without a default, starts with the first Kokoro voice', () => {
    expect(ids(pickSampleVoices(LOCAL, null))[0]).toBe('af_heart')
  })

  it('includes one voice per keyed cloud provider after the default', () => {
    const voices = [
      voice('openai:gpt-4o-mini-tts:alloy', 'openai'),
      voice('openai:gpt-4o-mini-tts:echo', 'openai'),
      ...LOCAL,
      voice('gemini:tts:Kore', 'gemini'),
    ]
    expect(ids(pickSampleVoices(voices, DEFAULT))).toEqual(['af_heart', 'alloy', 'Kore'])
    expect(ids(pickSampleVoices(voices.filter((v) => v.provider !== 'gemini'), DEFAULT))).toEqual(['af_heart', 'alloy', 'am_michael'])
  })

  it('never offers unavailable voices', () => {
    const voices = [
      voice('openai:gpt-4o-mini-tts:alloy', 'openai', { available: false }),
      kokoro('af_heart'),
      kokoro('am_michael', { available: false }),
      kokoro('bf_emma'),
      kokoro('bm_george'),
    ]
    const picked = pickSampleVoices(voices, DEFAULT)
    expect(ids(picked)).toEqual(['af_heart', 'bf_emma', 'bm_george'])
    expect(picked.every((v) => v.available)).toBe(true)
  })

  it('skips an unavailable default and falls back to Kokoro', () => {
    const voices = [voice('openai:gpt-4o-mini-tts:alloy', 'openai', { available: false }), ...LOCAL]
    expect(ids(pickSampleVoices(voices, 'openai:gpt-4o-mini-tts:alloy'))[0]).toBe('af_heart')
  })

  it('returns at most three, without duplicates, and copes with tiny catalogs', () => {
    const many = [...LOCAL, kokoro('af_nicole'), kokoro('am_adam'), kokoro('bf_lily')]
    const picked = pickSampleVoices(many, DEFAULT)
    expect(picked).toHaveLength(3)
    expect(new Set(picked.map((v) => v.id)).size).toBe(3)
    expect(ids(pickSampleVoices([kokoro('af_heart')], DEFAULT))).toEqual(['af_heart'])
    expect(pickSampleVoices([], null)).toEqual([])
  })
})

describe('matchVoiceAnswer', () => {
  it('keys step: skip / later / none', () => {
    expect(matchVoiceAnswer('keys', 'Skip')).toBe('skip')
    expect(matchVoiceAnswer('keys', 'maybe later')).toBe('skip')
    expect(matchVoiceAnswer('keys', 'I have no key')).toBe('skip')
    expect(matchVoiceAnswer('keys', 'hello')).toBeNull()
  })

  it('voice step: one/two/three, digits and ordinals', () => {
    expect(matchVoiceAnswer('voice', 'One.')).toBe('voice:0')
    expect(matchVoiceAnswer('voice', 'number 2')).toBe('voice:1')
    expect(matchVoiceAnswer('voice', 'three please')).toBe('voice:2')
    expect(matchVoiceAnswer('voice', 'the first')).toBe('voice:0')
    expect(matchVoiceAnswer('voice', 'third')).toBe('voice:2')
    expect(matchVoiceAnswer('voice', 'someone')).toBeNull()
  })

  // Suspected bug (Onboarding.tsx:68): "one" is checked before the ordinals, so "the second one" picks voice 1.
  it('voice step: "the second one" picks the second voice', () => {
    expect(matchVoiceAnswer('voice', 'the second one')).toBe('voice:1')
  })

  it('cleanup step: yes / smart / no', () => {
    expect(matchVoiceAnswer('cleanup', 'Yes')).toBe('yes')
    expect(matchVoiceAnswer('cleanup', 'yeah sure')).toBe('yes')
    expect(matchVoiceAnswer('cleanup', 'smart')).toBe('smart')
    expect(matchVoiceAnswer('cleanup', 'use AI')).toBe('smart')
    expect(matchVoiceAnswer('cleanup', 'no')).toBe('no')
    expect(matchVoiceAnswer('cleanup', "don't")).toBe('no')
    expect(matchVoiceAnswer('cleanup', 'maybe')).toBeNull()
  })

  it('done step: anything finishes', () => {
    expect(matchVoiceAnswer('done', 'thanks')).toBe('finish')
    expect(matchVoiceAnswer('done', '  ')).toBeNull()
  })

  it('"skip setup" skips onboarding from the later steps', () => {
    expect(matchVoiceAnswer('voice', 'skip setup')).toBe('skip-all')
    expect(matchVoiceAnswer('cleanup', 'skip all')).toBe('skip-all')
    expect(matchVoiceAnswer('voice', 'skip onboarding')).toBe('skip-all')
  })

  // Suspected bug (Onboarding.tsx:66): on the first step the plain "skip" match wins, so "skip setup" only
  // advances to the voice step instead of ending onboarding.
  it('"skip setup" on the keys step skips onboarding', () => {
    expect(matchVoiceAnswer('keys', 'skip setup')).toBe('skip-all')
  })
})

describe('stepPrompt', () => {
  const o = { canListen: true, hasClaude: false, shortcut: 'Option Space' }

  it('keys step works with no keys', () => {
    expect(stepPrompt('keys', o)).toMatch(/continue without one/i)
  })

  it('voice step asks for a spoken number only when it can listen', () => {
    expect(stepPrompt('voice', o)).toMatch(/say one, two or three/i)
    expect(stepPrompt('voice', { ...o, canListen: false })).not.toMatch(/say/i)
    expect(stepPrompt('voice', { ...o, canListen: false })).toMatch(/press play/i)
  })

  it('cleanup step offers "smart" only with an Anthropic key', () => {
    expect(stepPrompt('cleanup', { ...o, hasClaude: true })).toMatch(/smart/)
    expect(stepPrompt('cleanup', o)).not.toMatch(/smart/)
  })

  it('done step: keyless wording mentions typing and Option-Shift-R', () => {
    const keyless = stepPrompt('done', { ...o, canListen: false })
    expect(keyless).toContain('Option Space')
    expect(keyless).toMatch(/type/)
    expect(keyless).toContain('Option-Shift-R')
    expect(stepPrompt('done', o)).toMatch(/tell me what to read/)
    expect(stepPrompt('done', o)).not.toContain('Option-Shift-R')
  })
})
