import { describe, expect, it } from 'vitest'

import type { BBox, Block } from '@shared/api/client'

import { anchorRects, bboxToCss, cssToTopLeft, hitTest, topLeftToPdf, wordRects, type ViewportLike } from './geometry'

/** What pdf.js gives for a US Letter page (612×792 pt) at `scale`, no rotation. */
const letter = (scale = 1, viewBox = [0, 0, 612, 792]): ViewportLike => ({
  transform: [scale, 0, 0, -scale, -viewBox[0]! * scale, viewBox[3]! * scale],
  viewBox,
})

const box = (page: number, x0: number, y0: number, x1: number, y1: number): BBox => ({ page, x0, y0, x1, y1 })

const block = (id: string, patch: Partial<Block> = {}): Block => ({ id, type: 'paragraph', text: '', order: 0, ...patch })

describe('bboxToCss', () => {
  it('maps top-left-origin points to CSS pixels at scale 1', () => {
    expect(bboxToCss(box(1, 72, 100, 172, 120), letter())).toEqual({ left: 72, top: 100, width: 100, height: 20 })
  })

  it('applies the viewport scale', () => {
    expect(bboxToCss(box(1, 72, 100, 172, 120), letter(2))).toEqual({ left: 144, top: 200, width: 200, height: 40 })
  })

  it('handles a view box with a non-zero origin (cropped page)', () => {
    const vp = letter(1, [10, 20, 622, 812])
    expect(topLeftToPdf(0, 0, vp.viewBox)).toEqual([10, 812])
    expect(bboxToCss(box(1, 0, 0, 50, 10), vp)).toEqual({ left: 0, top: 0, width: 50, height: 10 })
  })

  it('handles a rotated viewport (90°) with positive sizes', () => {
    // pdf.js rotation 90 at scale 1 for a 612×792 page: x' = y, y' = x
    const vp: ViewportLike = { transform: [0, 1, 1, 0, 0, 0], viewBox: [0, 0, 612, 792] }
    const css = bboxToCss(box(1, 72, 100, 172, 120), vp)
    expect(css).toEqual({ left: 672, top: 72, width: 20, height: 100 })
  })
})

describe('cssToTopLeft', () => {
  it('inverts bboxToCss', () => {
    for (const vp of [letter(), letter(1.5), letter(1, [10, 20, 622, 812]), { transform: [0, 1, 1, 0, 0, 0], viewBox: [0, 0, 612, 792] }]) {
      const css = bboxToCss(box(1, 72, 100, 172, 120), vp)
      const corner = vp.transform[1] === 0 ? cssToTopLeft(css.left, css.top, vp) : cssToTopLeft(css.left + css.width, css.top, vp)
      expect(corner.x).toBeCloseTo(72)
      expect(corner.y).toBeCloseTo(100)
    }
  })

  it('returns the origin for a degenerate transform', () => {
    expect(cssToTopLeft(10, 10, { transform: [0, 0, 0, 0, 0, 0], viewBox: [0, 0, 1, 1] })).toEqual({ x: 0, y: 0 })
  })
})

describe('wordRects', () => {
  const words = {
    word_boxes: [
      { start: 0, end: 5, bbox: box(1, 10, 100, 40, 110) },
      { start: 6, end: 11, bbox: box(1, 45, 100, 80, 111) },
      { start: 12, end: 16, bbox: box(1, 10, 115, 35, 125) },
      { start: 17, end: 20, bbox: box(2, 10, 100, 30, 110) },
    ],
  }

  it('merges overlapping words into one box per line', () => {
    expect(wordRects(words, 0, 16)).toEqual([box(1, 10, 100, 80, 111), box(1, 10, 115, 35, 125)])
  })

  it('selects words overlapping [start, end)', () => {
    expect(wordRects(words, 5, 6)).toEqual([])
    expect(wordRects(words, 4, 7)).toEqual([box(1, 10, 100, 80, 111)])
  })

  it('keeps pages apart', () => {
    expect(wordRects(words, 12, 20).map((b) => b.page)).toEqual([1, 2])
  })

  it('does not mutate the source boxes', () => {
    wordRects(words, 0, 16)
    expect(words.word_boxes[0]!.bbox).toEqual(box(1, 10, 100, 40, 110))
  })
})

describe('anchorRects', () => {
  const blocks = new Map<string, Block>([
    ['b1', block('b1', { bbox: box(3, 0, 0, 100, 50), word_boxes: [{ start: 0, end: 4, bbox: box(3, 5, 5, 20, 15) }] })],
    ['b2', block('b2', { bbox: box(4, 0, 0, 100, 50) })],
    ['b3', block('b3')],
  ])

  it("uses the anchor's own bbox first", () => {
    const own = box(9, 1, 2, 3, 4)
    expect(anchorRects({ block_id: 'b1', bbox: own }, blocks)).toEqual([{ page: 9, box: own, precision: 'exact' }])
  })

  it('then word boxes for the char range', () => {
    expect(anchorRects({ block_id: 'b1', char_start: 0, char_end: 4 }, blocks)).toEqual([
      { page: 3, box: box(3, 5, 5, 20, 15), precision: 'exact' },
    ])
  })

  it('then the block outline', () => {
    expect(anchorRects({ block_id: 'b1', char_start: 10, char_end: 12 }, blocks)).toEqual([
      { page: 3, box: box(3, 0, 0, 100, 50), precision: 'block' },
    ])
    expect(anchorRects({ block_id: 'b2', char_start: 0, char_end: 4 }, blocks)[0]?.precision).toBe('block')
  })

  it('returns nothing for unknown blocks or blocks without geometry', () => {
    expect(anchorRects({ block_id: 'nope' }, blocks)).toEqual([])
    expect(anchorRects({ block_id: 'b3' }, blocks)).toEqual([])
    expect(anchorRects({}, blocks)).toEqual([])
  })
})

describe('hitTest', () => {
  const blocks = [
    block('b1', { bbox: box(1, 10, 10, 200, 40), word_boxes: [{ start: 7, end: 12, bbox: box(1, 50, 12, 90, 25) }] }),
    block('b2', { bbox: box(2, 10, 10, 200, 40) }),
  ]

  it('finds the block and the word start under the point', () => {
    expect(hitTest(1, 60, 20, blocks)).toEqual({ blockId: 'b1', char: 7 })
  })

  it('falls back to the block start between words, with a little slop at the edges', () => {
    expect(hitTest(1, 20, 20, blocks)).toEqual({ blockId: 'b1', char: 0 })
    expect(hitTest(1, 9, 20, blocks)).toEqual({ blockId: 'b1', char: 0 })
  })

  it('respects the page and misses outside blocks', () => {
    expect(hitTest(2, 60, 20, blocks)).toEqual({ blockId: 'b2', char: 0 })
    expect(hitTest(1, 300, 300, blocks)).toBeNull()
    expect(hitTest(3, 60, 20, blocks)).toBeNull()
  })
})
