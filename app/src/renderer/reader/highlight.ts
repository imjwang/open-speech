/**
 * Highlight mapping for the text reader (03 § Highlighting paths): turn `playback.word` anchors
 * (`{block_id, char_start, char_end}`, offsets in code points) and the script's rule hits into styled
 * segments of each block's text. Pure functions, unit-tested.
 */
import type { Anchor } from '@shared/api/ws.gen'
import type { RuleHit, ScriptOut, Utterance } from '@shared/api/client'
import { codePointIndexTable, codePointLength, cpToUtf16 } from '@shared/text'

export interface Range {
  start: number
  end: number
}

export interface Segment {
  /** Code point offsets into the block text. */
  start: number
  end: number
  text: string
  sentence: boolean
  word: boolean
  /** Rule that skipped this span (Script view / FR-RULE-5). */
  skippedBy: string | null
  /** A replacement (lexicon or annotate) of this span. */
  replaced: { rule: string; replacement: string } | null
  utteranceId: string | null
}

export interface BlockMarks {
  sentence: Range[]
  word: Range | null
  skipped: Array<Range & { rule: string }>
  replaced: Array<Range & { rule: string; replacement: string }>
  utterances: Array<Range & { utteranceId: string }>
}

export const EMPTY_MARKS: BlockMarks = { sentence: [], word: null, skipped: [], replaced: [], utterances: [] }

function clampRange(r: Range, len: number): Range | null {
  const start = Math.max(0, Math.min(r.start, len))
  const end = Math.max(start, Math.min(r.end, len))
  return end > start ? { start, end } : null
}

/** Sentence ranges in one block from `sentence_anchors`. A block-level anchor (no chars) covers the block. */
export function sentenceRanges(blockId: string, anchors: readonly Anchor[], blockLength: number): Range[] {
  const out: Range[] = []
  for (const a of anchors) {
    if (a.block_id !== blockId) continue
    const r =
      a.char_start == null || a.char_end == null
        ? { start: 0, end: blockLength }
        : clampRange({ start: a.char_start, end: a.char_end }, blockLength)
    if (r) out.push(r)
  }
  return out
}

export function wordRange(blockId: string, anchor: Anchor | null | undefined, blockLength: number): Range | null {
  if (!anchor || anchor.block_id !== blockId || anchor.char_start == null || anchor.char_end == null) return null
  return clampRange({ start: anchor.char_start, end: anchor.char_end }, blockLength)
}

/** Blocks that only have block-level anchors (fidelity block/section): outline them instead. */
export function outlinedBlocks(anchors: readonly Anchor[]): Set<string> {
  const ids = new Set<string>()
  for (const a of anchors) if (a.block_id && a.char_start == null) ids.add(a.block_id)
  return ids
}

export function removedMarks(blockId: string, removed: readonly RuleHit[], blockLength: number): Pick<BlockMarks, 'skipped' | 'replaced'> {
  const skipped: BlockMarks['skipped'] = []
  const replaced: BlockMarks['replaced'] = []
  for (const hit of removed) {
    if (hit.block_id !== blockId) continue
    const whole = hit.action === 'drop_block' || hit.action === 'keep_only' || hit.src_start == null || hit.src_end == null
    const r = whole ? { start: 0, end: blockLength } : clampRange({ start: hit.src_start ?? 0, end: hit.src_end ?? 0 }, blockLength)
    if (!r) continue
    if (hit.action === 'replace') replaced.push({ ...r, rule: hit.rule_name, replacement: hit.replacement })
    else skipped.push({ ...r, rule: hit.rule_name })
  }
  return { skipped, replaced }
}

/** Replacements recorded on utterances (e.g. lexicon "SQL" → "sequel"). */
export function utteranceReplacements(blockId: string, utterances: readonly Utterance[], blockLength: number): BlockMarks['replaced'] {
  const out: BlockMarks['replaced'] = []
  for (const u of utterances) {
    for (const hit of u.rule_trace ?? []) {
      if (hit.block_id !== blockId || hit.action !== 'replace' || hit.src_start == null || hit.src_end == null) continue
      const r = clampRange({ start: hit.src_start, end: hit.src_end }, blockLength)
      if (r) out.push({ ...r, rule: hit.rule_name, replacement: hit.replacement })
    }
  }
  return out
}

/** Which utterance covers which source range of a block (click a sentence to start there). */
export function utteranceRanges(blockId: string, utterances: readonly Utterance[], blockLength: number): BlockMarks['utterances'] {
  const out: BlockMarks['utterances'] = []
  for (const u of utterances) {
    let start = Infinity
    let end = -Infinity
    for (const seg of u.segments ?? []) {
      if (seg.block_id !== blockId) continue
      start = Math.min(start, seg.src_start)
      end = Math.max(end, seg.src_end)
    }
    if (start === Infinity && u.block_ids.includes(blockId) && (u.segments ?? []).length === 0) {
      start = 0
      end = blockLength
    }
    const r = start < end ? clampRange({ start, end }, blockLength) : null
    if (r) out.push({ ...r, utteranceId: u.id })
  }
  return out
}

/** Split a block's text at every mark boundary. Offsets are code points; slicing handles astral chars. */
export function segmentText(text: string, marks: BlockMarks): Segment[] {
  const length = codePointLength(text)
  const table = codePointIndexTable(text)
  const cuts = new Set<number>([0, length])
  const add = (r: Range) => {
    cuts.add(Math.max(0, Math.min(r.start, length)))
    cuts.add(Math.max(0, Math.min(r.end, length)))
  }
  marks.sentence.forEach(add)
  if (marks.word) add(marks.word)
  marks.skipped.forEach(add)
  marks.replaced.forEach(add)
  marks.utterances.forEach(add)
  const points = [...cuts].sort((a, b) => a - b)
  const within = (r: Range, s: number, e: number) => r.start <= s && e <= r.end
  const segments: Segment[] = []
  for (let i = 0; i < points.length - 1; i++) {
    const start = points[i] ?? 0
    const end = points[i + 1] ?? 0
    if (end <= start) continue
    const skip = marks.skipped.find((r) => within(r, start, end))
    const rep = marks.replaced.find((r) => within(r, start, end))
    segments.push({
      start,
      end,
      text: text.slice(cpToUtf16(text, start, table), cpToUtf16(text, end, table)),
      sentence: marks.sentence.some((r) => within(r, start, end)),
      word: !!marks.word && within(marks.word, start, end),
      skippedBy: skip ? skip.rule : null,
      replaced: rep ? { rule: rep.rule, replacement: rep.replacement } : null,
      utteranceId: marks.utterances.find((r) => within(r, start, end))?.utteranceId ?? null,
    })
  }
  return segments
}

export interface ScriptEntry {
  kind: 'utterance' | 'removed'
  key: string
  order: number
  utterance?: Utterance
  hit?: RuleHit
}

/** Script view: utterances (what will be spoken) interleaved with removed spans, in reading order. */
export function scriptEntries(script: ScriptOut, blockOrder: ReadonlyMap<string, number>): ScriptEntry[] {
  const entries: ScriptEntry[] = []
  for (const u of script.utterances) {
    const first = u.block_ids[0]
    const seg = (u.segments ?? [])[0]
    const order = (first !== undefined ? (blockOrder.get(first) ?? 0) : 0) * 1e6 + (seg?.src_start ?? 0)
    entries.push({ kind: 'utterance', key: u.id, order, utterance: u })
  }
  script.removed.forEach((hit, i) => {
    if (hit.action === 'replace') return
    const order = (blockOrder.get(hit.block_id) ?? 0) * 1e6 + (hit.src_start ?? 0) - 0.5
    entries.push({ kind: 'removed', key: `removed:${i}`, order, hit })
  })
  return entries.sort((a, b) => a.order - b.order)
}
