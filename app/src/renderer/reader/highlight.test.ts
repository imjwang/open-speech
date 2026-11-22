import { describe, expect, it } from 'vitest'

import type { RuleHit, ScriptOut, Utterance } from '@shared/api/client'

import {
  EMPTY_MARKS,
  outlinedBlocks,
  removedMarks,
  scriptEntries,
  segmentText,
  sentenceRanges,
  utteranceRanges,
  utteranceReplacements,
  wordRange,
  type BlockMarks,
} from './highlight'

const hit = (patch: Partial<RuleHit>): RuleHit => ({
  rule_id: 'r1',
  rule_name: 'Footers',
  action: 'skip',
  block_id: 'b1',
  src_start: null,
  src_end: null,
  original: '',
  replacement: '',
  tier: 'filter',
  ...patch,
})

const utt = (id: string, patch: Partial<Utterance> = {}): Utterance => ({
  id,
  index: 0,
  text: '',
  block_ids: ['b1'],
  fidelity: 'exact',
  is_heading: false,
  paragraph_start: false,
  ...patch,
})

const seg = (block_id: string, src_start: number, src_end: number) => ({ spoken_start: 0, spoken_end: 0, block_id, src_start, src_end })

describe('sentenceRanges / wordRange', () => {
  it('keeps this block’s sentence anchors, clamped to the block', () => {
    const anchors = [
      { block_id: 'b1', char_start: 0, char_end: 12 },
      { block_id: 'b2', char_start: 0, char_end: 5 },
      { block_id: 'b1', char_start: 13, char_end: 99 },
      { block_id: 'b1', char_start: 30, char_end: 40 }, // entirely past the end
    ]
    expect(sentenceRanges('b1', anchors, 20)).toEqual([
      { start: 0, end: 12 },
      { start: 13, end: 20 },
    ])
  })

  it('a block-level anchor (no char offsets) covers the whole block', () => {
    expect(sentenceRanges('b1', [{ block_id: 'b1' }], 42)).toEqual([{ start: 0, end: 42 }])
  })

  it('wordRange maps only a char-level anchor in this block', () => {
    expect(wordRange('b1', { block_id: 'b1', char_start: 6, char_end: 11 }, 20)).toEqual({ start: 6, end: 11 })
    expect(wordRange('b1', { block_id: 'b1', char_start: 18, char_end: 25 }, 20)).toEqual({ start: 18, end: 20 })
    expect(wordRange('b1', { block_id: 'b2', char_start: 0, char_end: 3 }, 20)).toBeNull()
    expect(wordRange('b1', { block_id: 'b1' }, 20)).toBeNull()
    expect(wordRange('b1', null, 20)).toBeNull()
    expect(wordRange('b1', { block_id: 'b1', char_start: 5, char_end: 5 }, 20)).toBeNull()
  })

  it('outlinedBlocks lists blocks with block-level anchors only', () => {
    expect([...outlinedBlocks([{ block_id: 'b1' }, { block_id: 'b2', char_start: 0, char_end: 1 }, { block_id: null }])]).toEqual(['b1'])
  })
})

describe('rule marks', () => {
  it('removedMarks: whole-block drops, span skips and replacements', () => {
    const marks = removedMarks(
      'b1',
      [
        hit({ action: 'drop_block' }),
        hit({ action: 'skip', src_start: 2, src_end: 5, rule_name: 'Citations' }),
        hit({ action: 'replace', src_start: 6, src_end: 9, rule_name: 'Lexicon', replacement: 'sequel' }),
        hit({ action: 'skip', block_id: 'b2', src_start: 0, src_end: 3 }),
        hit({ action: 'skip', src_start: 50, src_end: 60 }),
      ],
      10,
    )
    expect(marks.skipped).toEqual([
      { start: 0, end: 10, rule: 'Footers' },
      { start: 2, end: 5, rule: 'Citations' },
    ])
    expect(marks.replaced).toEqual([{ start: 6, end: 9, rule: 'Lexicon', replacement: 'sequel' }])
  })

  it('utteranceReplacements reads replace hits from rule traces', () => {
    const us = [
      utt('u1', {
        rule_trace: [
          hit({ action: 'replace', src_start: 0, src_end: 3, rule_name: 'Lexicon', replacement: 'sequel' }),
          hit({ action: 'skip', src_start: 4, src_end: 6 }),
          hit({ action: 'replace', block_id: 'b2', src_start: 0, src_end: 3 }),
        ],
      }),
    ]
    expect(utteranceReplacements('b1', us, 20)).toEqual([{ start: 0, end: 3, rule: 'Lexicon', replacement: 'sequel' }])
  })

  it('utteranceRanges: segment span per utterance, whole block when unsegmented', () => {
    const us = [
      utt('u1', { segments: [seg('b1', 0, 12), seg('b1', 4, 15)] }),
      utt('u2', { segments: [seg('b2', 0, 5)] }),
      utt('u3', { block_ids: ['b1'], segments: [] }),
    ]
    expect(utteranceRanges('b1', us, 30)).toEqual([
      { start: 0, end: 15, utteranceId: 'u1' },
      { start: 0, end: 30, utteranceId: 'u3' },
    ])
  })
})

describe('segmentText', () => {
  it('splits at every mark boundary and flags sentence/word membership', () => {
    const text = 'Hello world. Bye.'
    const marks: BlockMarks = { ...EMPTY_MARKS, sentence: [{ start: 0, end: 12 }], word: { start: 6, end: 11 } }
    const segs = segmentText(text, marks)
    expect(segs.map((s) => [s.text, s.sentence, s.word])).toEqual([
      ['Hello ', true, false],
      ['world', true, true],
      ['.', true, false],
      [' Bye.', false, false],
    ])
    expect(segs.map((s) => s.text).join('')).toBe(text)
  })

  it('returns the whole text as one plain segment without marks', () => {
    expect(segmentText('abc', EMPTY_MARKS)).toEqual([
      { start: 0, end: 3, text: 'abc', sentence: false, word: false, skippedBy: null, replaced: null, utteranceId: null },
    ])
  })

  it('uses code-point offsets, so astral characters slice correctly', () => {
    const text = '😀 hi 🎉 there'
    // code points: 😀=0 ' '=1 h=2 i=3 ' '=4 🎉=5 ' '=6 t=7..e=11
    const segs = segmentText(text, { ...EMPTY_MARKS, word: { start: 7, end: 12 }, sentence: [{ start: 2, end: 4 }] })
    expect(segs.find((s) => s.word)?.text).toBe('there')
    expect(segs.find((s) => s.sentence)?.text).toBe('hi')
    expect(segs.map((s) => s.text).join('')).toBe(text)
  })

  it('carries skipped, replaced and utterance info onto the covered segments', () => {
    const marks: BlockMarks = {
      ...EMPTY_MARKS,
      skipped: [{ start: 0, end: 4, rule: 'Citations' }],
      replaced: [{ start: 5, end: 8, rule: 'Lexicon', replacement: 'sequel' }],
      utterances: [{ start: 4, end: 12, utteranceId: 'u7' }],
    }
    const segs = segmentText('[12] SQL rocks', marks)
    expect(segs.map((s) => [s.text, s.skippedBy, s.replaced?.replacement ?? null, s.utteranceId])).toEqual([
      ['[12]', 'Citations', null, null],
      [' ', null, null, 'u7'],
      ['SQL', null, 'sequel', 'u7'],
      [' roc', null, null, 'u7'],
      ['ks', null, null, null],
    ])
  })
})

describe('scriptEntries', () => {
  it('interleaves utterances and removed spans in reading order, skipping replacements', () => {
    const script: ScriptOut = {
      document_id: 'd',
      mode: 'classic',
      utterances: [
        utt('u2', { block_ids: ['b2'], segments: [seg('b2', 0, 10)] }),
        utt('u1', { block_ids: ['b1'], segments: [seg('b1', 5, 20)] }),
      ],
      removed: [
        hit({ block_id: 'b2', action: 'drop_block' }),
        hit({ block_id: 'b1', action: 'skip', src_start: 0, src_end: 5 }),
        hit({ block_id: 'b1', action: 'replace', src_start: 6, src_end: 9 }),
      ],
    }
    const order = new Map([
      ['b1', 0],
      ['b2', 1],
    ])
    expect(scriptEntries(script, order).map((e) => e.key)).toEqual(['removed:1', 'u1', 'removed:0', 'u2'])
  })
})
