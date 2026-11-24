import { afterEach, describe, expect, it } from 'vitest'

import { en } from './en'
import { es } from './es'
import { getLanguage, onLanguageChange, setLanguage, t, tMaybe, translate } from './index'

afterEach(() => setLanguage('en'))

const placeholders = (text: string) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort()

describe('interface text', () => {
  it('Spanish has every message English has, with the same placeholders', () => {
    expect(Object.keys(es).sort()).toEqual(Object.keys(en).sort())
    for (const key of Object.keys(en) as Array<keyof typeof en>) expect(placeholders(es[key]), key).toEqual(placeholders(en[key]))
  })

  it('no message is empty, and none asks for a key or an account', () => {
    for (const catalog of [en, es])
      for (const [key, text] of Object.entries(catalog)) {
        expect(text.trim(), key).not.toBe('')
        expect(text, key).not.toMatch(/api key|paste a key|clave de api/i)
      }
  })

  it('translates with parameters and follows the current language', () => {
    expect(t('heard', { text: 'pause' })).toBe('You said: pause')
    setLanguage('es')
    expect(getLanguage()).toBe('es')
    expect(t('heard', { text: 'pausa' })).toBe('Has dicho: pausa')
    expect(translate('en', 'row.queue_more', { n: 2 })).toBe('2 more queued')
  })

  it('an unknown language falls back to English, and listeners hear real changes only', () => {
    let changes = 0
    const off = onLanguageChange(() => changes++)
    setLanguage('fr')
    expect(getLanguage()).toBe('en')
    expect(changes).toBe(0)
    setLanguage('es')
    setLanguage('es')
    expect(changes).toBe(1)
    off()
  })

  it('tMaybe falls back for keys built at run time', () => {
    expect(tMaybe('theme.lavender', 'x')).toBe('Lavender')
    expect(tMaybe('theme.something new', 'Something new')).toBe('Something new')
  })
})
