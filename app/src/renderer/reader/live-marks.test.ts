import { describe, expect, it } from 'vitest'

import { EMPTY_MARKS, involvesBlock, liveMarks } from './highlight'

const word = (blockId: string, start: number, end: number, sentence: Array<[string, number | null, number | null]> = []) => ({
  anchor: { block_id: blockId, char_start: start, char_end: end },
  sentenceAnchors: sentence.map(([block_id, char_start, char_end]) => ({ block_id, char_start, char_end })),
})

describe('each block follows its own part of the highlight', () => {
  it('only the blocks the word or its sentence touch are involved', () => {
    const w = word('b2', 4, 9, [['b1', 30, 40], ['b2', 0, 12]])
    expect(involvesBlock(w, 'b1')).toBe(true)
    expect(involvesBlock(w, 'b2')).toBe(true)
    expect(involvesBlock(w, 'b3')).toBe(false)
    expect(involvesBlock(null, 'b1')).toBe(false)
  })

  it('an uninvolved block keeps its static marks object (so it does not re-render)', () => {
    const base = { ...EMPTY_MARKS, skipped: [{ start: 0, end: 3, rule: 'Citations' }] }
    expect(liveMarks('b3', 'Some text here.', base, word('b2', 0, 4)).marks).toBe(base)
  })

  it('adds the sentence and the word to the block they are in', () => {
    const { marks, outlined } = liveMarks('b2', 'The river bends.', EMPTY_MARKS, word('b2', 4, 9, [['b2', 0, 16]]))
    expect(marks.word).toEqual({ start: 4, end: 9 })
    expect(marks.sentence).toEqual([{ start: 0, end: 16 }])
    expect(outlined).toBe(false)
  })

  it('outlines a block that only has a block-level anchor', () => {
    const { outlined } = liveMarks('fig', 'Figure 1', EMPTY_MARKS, word('fig', 0, 0, [['fig', null, null]]))
    expect(outlined).toBe(true)
  })
})
