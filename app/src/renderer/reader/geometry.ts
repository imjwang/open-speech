/**
 * PDF geometry (03 § Highlighting paths): engine boxes are PDF points with a top-left origin (y down),
 * relative to the page's view box. pdf.js viewports map PDF user space (bottom-left origin) to CSS
 * pixels with a 6-element transform that includes scale and rotation.
 */
import type { BBox, Block } from '@shared/api/client'

export interface ViewportLike {
  /** [a, b, c, d, e, f]: x' = a·x + c·y + e, y' = b·x + d·y + f */
  transform: number[]
  /** [xMin, yMin, xMax, yMax] in PDF user space */
  viewBox: number[]
}

export interface CssRect {
  left: number
  top: number
  width: number
  height: number
}

function apply(t: number[], x: number, y: number): [number, number] {
  const [a = 1, b = 0, c = 0, d = 1, e = 0, f = 0] = t
  return [a * x + c * y + e, b * x + d * y + f]
}

/** Top-left-origin points → PDF user space. */
export function topLeftToPdf(x: number, y: number, viewBox: number[]): [number, number] {
  const [xMin = 0, , , yMax = 0] = viewBox
  return [xMin + x, yMax - y]
}

export function bboxToCss(box: Pick<BBox, 'x0' | 'y0' | 'x1' | 'y1'>, vp: ViewportLike): CssRect {
  const [ax, ay] = topLeftToPdf(box.x0, box.y0, vp.viewBox)
  const [bx, by] = topLeftToPdf(box.x1, box.y1, vp.viewBox)
  const [x1, y1] = apply(vp.transform, ax, ay)
  const [x2, y2] = apply(vp.transform, bx, by)
  return {
    left: Math.min(x1, x2),
    top: Math.min(y1, y2),
    width: Math.abs(x2 - x1),
    height: Math.abs(y2 - y1),
  }
}

/** CSS pixel within the page → top-left-origin PDF points (for click-to-start). */
export function cssToTopLeft(px: number, py: number, vp: ViewportLike): { x: number; y: number } {
  const [a = 1, b = 0, c = 0, d = 1, e = 0, f = 0] = vp.transform
  const det = a * d - b * c
  if (det === 0) return { x: 0, y: 0 }
  const X = (d * (px - e) - c * (py - f)) / det
  const Y = (-b * (px - e) + a * (py - f)) / det
  const [xMin = 0, , , yMax = 0] = vp.viewBox
  return { x: X - xMin, y: yMax - Y }
}

function contains(b: Pick<BBox, 'x0' | 'y0' | 'x1' | 'y1'>, x: number, y: number, slop = 0): boolean {
  return x >= b.x0 - slop && x <= b.x1 + slop && y >= b.y0 - slop && y <= b.y1 + slop
}

/** Word boxes overlapping [start, end) merged into one box per line. */
export function wordRects(block: Pick<Block, 'word_boxes'>, start: number, end: number): BBox[] {
  const boxes = (block.word_boxes ?? []).filter((w) => w.end > start && w.start < end).map((w) => w.bbox)
  const lines: BBox[] = []
  for (const b of boxes) {
    const line = lines.find((l) => l.page === b.page && Math.abs((l.y0 + l.y1) / 2 - (b.y0 + b.y1) / 2) < Math.max(2, (b.y1 - b.y0) * 0.5))
    if (line) {
      line.x0 = Math.min(line.x0, b.x0)
      line.x1 = Math.max(line.x1, b.x1)
      line.y0 = Math.min(line.y0, b.y0)
      line.y1 = Math.max(line.y1, b.y1)
    } else {
      lines.push({ ...b })
    }
  }
  return lines
}

export interface AnchorLike {
  block_id?: string | null
  char_start?: number | null
  char_end?: number | null
  bbox?: BBox | null
}

export interface PageRect {
  page: number
  box: BBox
  /** exact: the anchor's own box or word boxes; block: fallback outline of the whole block. */
  precision: 'exact' | 'block'
}

/** Where to draw an anchor: its bbox, else its word boxes, else the block outline. */
export function anchorRects(anchor: AnchorLike, blocks: ReadonlyMap<string, Block>): PageRect[] {
  if (anchor.bbox) return [{ page: anchor.bbox.page, box: anchor.bbox, precision: 'exact' }]
  const block = anchor.block_id ? blocks.get(anchor.block_id) : undefined
  if (!block) return []
  if (anchor.char_start != null && anchor.char_end != null && block.word_boxes?.length) {
    const rects = wordRects(block, anchor.char_start, anchor.char_end)
    if (rects.length) return rects.map((box) => ({ page: box.page, box, precision: 'exact' }))
  }
  return block.bbox ? [{ page: block.bbox.page, box: block.bbox, precision: 'block' }] : []
}

/** Click-to-start on a PDF page: the block (and char offset of the word) under a point. */
export function hitTest(
  page: number,
  x: number,
  y: number,
  blocks: readonly Block[],
): { blockId: string; char: number } | null {
  for (const block of blocks) {
    if (!block.bbox || block.bbox.page !== page || !contains(block.bbox, x, y, 2)) continue
    const word = (block.word_boxes ?? []).find((w) => w.bbox.page === page && contains(w.bbox, x, y, 1))
    return { blockId: block.id, char: word ? word.start : 0 }
  }
  return null
}
