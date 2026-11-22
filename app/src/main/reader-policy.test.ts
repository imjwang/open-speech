import { describe, expect, it } from 'vitest'

import { LONG_TEXT_BLOCKS, toDomAnchor, worthShowing } from './reader-policy'

describe('worthShowing', () => {
  it('always shows PDFs, web pages and images', () => {
    for (const kind of ['pdf', 'web', 'image']) expect(worthShowing({ kind, block_count: 1 })).toBe(true)
  })

  it('shows text only when it is long', () => {
    expect(worthShowing({ kind: 'text', block_count: LONG_TEXT_BLOCKS })).toBe(true)
    expect(worthShowing({ kind: 'text', block_count: LONG_TEXT_BLOCKS - 1 })).toBe(false)
    expect(worthShowing({ kind: 'clipboard' })).toBe(false)
  })
})

describe('toDomAnchor', () => {
  const blocks: Record<string, { xpath: string; start_offset?: number; end_offset?: number | null }> = {
    b1: { xpath: '/html/body/p[1]/text()[1]', start_offset: 10, end_offset: 80 },
    b2: { xpath: '/html/body/p[2]' },
  }
  const lookup = (id: string) => blocks[id] ?? null

  it("passes an anchor's own dom_anchor through, with defaults", () => {
    expect(toDomAnchor({ block_id: 'b1', dom_anchor: { xpath: '/x' } }, lookup)).toEqual({ xpath: '/x', start_offset: 0, end_offset: null })
    expect(toDomAnchor({ dom_anchor: { xpath: '/x', start_offset: 3, end_offset: 7 } }, lookup)).toEqual({ xpath: '/x', start_offset: 3, end_offset: 7 })
  })

  it("offsets word chars by the block's start offset", () => {
    expect(toDomAnchor({ block_id: 'b1', char_start: 2, char_end: 6 }, lookup)).toEqual({ xpath: blocks.b1!.xpath, start_offset: 12, end_offset: 16 })
    expect(toDomAnchor({ block_id: 'b2', char_start: 2, char_end: 6 }, lookup)).toEqual({ xpath: '/html/body/p[2]', start_offset: 2, end_offset: 6 })
    expect(toDomAnchor({ block_id: 'b1', char_start: 2, char_end: null }, lookup)).toEqual({ xpath: blocks.b1!.xpath, start_offset: 12, end_offset: null })
  })

  it('a block-level anchor maps to the whole block', () => {
    expect(toDomAnchor({ block_id: 'b1' }, lookup)).toEqual({ xpath: blocks.b1!.xpath, start_offset: 10, end_offset: 80 })
    expect(toDomAnchor({ block_id: 'b2' }, lookup)).toEqual({ xpath: '/html/body/p[2]', start_offset: 0, end_offset: null })
  })

  it('returns null without a known block', () => {
    expect(toDomAnchor({ block_id: 'missing', char_start: 0, char_end: 1 }, lookup)).toBeNull()
    expect(toDomAnchor({ char_start: 0, char_end: 1 }, lookup)).toBeNull()
  })
})
