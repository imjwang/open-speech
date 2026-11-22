import { describe, expect, it } from 'vitest'

import { codePointIndexTable, codePointLength, cpSlice, cpToUtf16 } from './text'

describe('code point helpers', () => {
  it('takes the fast path for BMP-only text', () => {
    expect(codePointIndexTable('héllo — ok')).toBeNull()
    expect(codePointLength('héllo')).toBe(5)
    expect(cpToUtf16('hello', 3)).toBe(3)
    expect(cpToUtf16('hello', 99)).toBe(5)
    expect(cpToUtf16('hello', -2)).toBe(0)
  })

  it('maps code points to UTF-16 indices around astral characters', () => {
    const text = 'a😀b🎉c'
    expect(text.length).toBe(7)
    expect(codePointLength(text)).toBe(5)
    expect(codePointIndexTable(text)).toEqual([0, 1, 3, 4, 6, 7])
    expect(cpToUtf16(text, 2)).toBe(3)
    expect(cpToUtf16(text, 5)).toBe(7)
    expect(cpToUtf16(text, 50)).toBe(7)
  })

  it('cpSlice slices by code points', () => {
    const text = 'a😀b🎉c'
    expect(cpSlice(text, 1, 3)).toBe('😀b')
    expect(cpSlice(text, 3)).toBe('🎉c')
    expect(cpSlice('plain', 1, 3)).toBe('la')
  })
})
