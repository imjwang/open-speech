import { describe, expect, it } from 'vitest'

import type { Voice } from '@shared/api/client'
import { en } from '@shared/i18n/en'
import { es } from '@shared/i18n/es'

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
  languages: [],
  ...patch,
})

const kokoro = (short: string, patch: Partial<Voice> = {}) =>
  voice(`${KOKORO}:${short}`, 'kokoro', { languages: [short.startsWith('e') ? 'es' : short.startsWith('b') ? 'en-GB' : 'en-US'], ...patch })

const ENGLISH = [kokoro('af_heart'), kokoro('af_bella'), kokoro('am_michael'), kokoro('bf_emma'), kokoro('bm_george')]
const SPANISH = [kokoro('ef_dora'), kokoro('em_alex'), kokoro('em_santa')]
const DEFAULT = `${KOKORO}:af_heart`

const ids = (vs: Voice[]) => vs.map((v) => v.id.split(':').pop())

describe('pickSampleVoices', () => {
  it('the default voice first, then contrasting accents and genders', () => {
    expect(ids(pickSampleVoices(ENGLISH, DEFAULT))).toEqual(['af_heart', 'am_michael', 'bf_emma'])
  })

  it('without a default, starts with the first voice', () => {
    expect(ids(pickSampleVoices(ENGLISH, null))[0]).toBe('af_heart')
  })

  it('offers voices that speak the interface language', () => {
    const all = [...ENGLISH, ...SPANISH]
    expect(ids(pickSampleVoices(all, DEFAULT, 'es'))).toEqual(['ef_dora', 'em_alex', 'em_santa'])
    expect(ids(pickSampleVoices(all, DEFAULT, 'en'))).toEqual(['af_heart', 'am_michael', 'bf_emma'])
  })

  it('cloud voices (no declared language) speak any language, after the local ones', () => {
    const cloud = voice('openai:gpt-4o-mini-tts:coral', 'openai')
    expect(ids(pickSampleVoices([cloud, ...SPANISH.slice(0, 2)], null, 'es'))).toEqual(['ef_dora', 'em_alex', 'coral'])
  })

  it('never offers unavailable voices, and falls back to any language when none matches', () => {
    const voices = [kokoro('af_heart', { available: false }), kokoro('am_michael'), kokoro('bf_emma')]
    expect(ids(pickSampleVoices(voices, DEFAULT))).toEqual(['am_michael', 'bf_emma'])
    expect(ids(pickSampleVoices(ENGLISH, null, 'es'))).toHaveLength(3)
  })

  it('returns at most three, without duplicates, and copes with tiny catalogs', () => {
    expect(pickSampleVoices([], null)).toEqual([])
    expect(ids(pickSampleVoices([kokoro('af_heart')], DEFAULT))).toEqual(['af_heart'])
    const picked = ids(pickSampleVoices([...ENGLISH, ...ENGLISH], DEFAULT))
    expect(picked).toHaveLength(3)
    expect(new Set(picked).size).toBe(3)
  })
})

describe('matchVoiceAnswer', () => {
  it('voice step: one/two/three, digits and ordinals, in English and Spanish', () => {
    expect(matchVoiceAnswer('voice', 'one')).toBe('voice:0')
    expect(matchVoiceAnswer('voice', 'Number 2 please')).toBe('voice:1')
    expect(matchVoiceAnswer('voice', 'three')).toBe('voice:2')
    expect(matchVoiceAnswer('voice', 'the second one')).toBe('voice:1')
    expect(matchVoiceAnswer('voice', 'la primera')).toBe('voice:0')
    expect(matchVoiceAnswer('voice', 'dos')).toBe('voice:1')
    expect(matchVoiceAnswer('voice', 'La tercera, por favor.')).toBe('voice:2')
    expect(matchVoiceAnswer('voice', 'keep this one')).toBe('keep')
    expect(matchVoiceAnswer('voice', 'hmm')).toBeNull()
  })

  it('cleanup step: yes / smart / no, in English and Spanish', () => {
    expect(matchVoiceAnswer('cleanup', 'Yes please')).toBe('yes')
    expect(matchVoiceAnswer('cleanup', 'smart')).toBe('smart')
    expect(matchVoiceAnswer('cleanup', 'no thanks')).toBe('no')
    expect(matchVoiceAnswer('cleanup', 'Sí')).toBe('yes')
    expect(matchVoiceAnswer('cleanup', 'vale')).toBe('yes')
    expect(matchVoiceAnswer('cleanup', 'inteligente')).toBe('smart')
    expect(matchVoiceAnswer('cleanup', 'No.')).toBe('no')
    expect(matchVoiceAnswer('cleanup', 'what?')).toBeNull()
  })

  it('done step: anything finishes', () => {
    expect(matchVoiceAnswer('done', 'okay')).toBe('finish')
    expect(matchVoiceAnswer('done', '  ')).toBeNull()
  })

  it('"skip setup" skips onboarding from any step, in either language', () => {
    expect(matchVoiceAnswer('voice', 'skip setup')).toBe('skip-all')
    expect(matchVoiceAnswer('cleanup', 'skip all')).toBe('skip-all')
    expect(matchVoiceAnswer('voice', 'saltar la configuración')).toBe('skip-all')
  })
})

describe('stepPrompt', () => {
  it('asks for a spoken answer only when the bar can listen', () => {
    expect(stepPrompt('voice', { canListen: true, smart: false })).toBe('onboard.voice')
    expect(stepPrompt('voice', { canListen: false, smart: false })).toBe('onboard.voice.no_mic')
    expect(stepPrompt('done', { canListen: false, smart: false })).toBe('onboard.done.no_mic')
  })

  it('names only as many numbers as there are voices to choose from', () => {
    expect(stepPrompt('voice', { canListen: true, smart: false, voices: 3 })).toBe('onboard.voice')
    expect(stepPrompt('voice', { canListen: true, smart: false, voices: 2 })).toBe('onboard.voice.two')
    expect(stepPrompt('voice', { canListen: false, smart: false, voices: 2 })).toBe('onboard.voice.no_mic')
  })

  it('offers "smart" cleanup only when the build includes it', () => {
    expect(stepPrompt('cleanup', { canListen: true, smart: true })).toBe('onboard.cleanup.smart')
    expect(stepPrompt('cleanup', { canListen: true, smart: false })).toBe('onboard.cleanup')
  })

  it('never mentions keys, accounts or sign-in, in any language', () => {
    for (const catalog of [en, es])
      for (const [key, text] of Object.entries(catalog))
        if (key.startsWith('onboard.')) expect(text).not.toMatch(/\bkeys?\b|clave|account|cuenta|sign in|iniciar sesión/i)
  })
})
