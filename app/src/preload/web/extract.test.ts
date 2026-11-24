// @vitest-environment jsdom
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { Readability } from '@mozilla/readability'
import { describe, expect, it } from 'vitest'

import type { HtmlBlocksMessage } from '../../shared/ipc'
import { buildTextMap, rangeForAnchor, resolveXPath } from './dom-text'
import { blockTypeFor, extractBlocks, type ReadabilityCtor } from './extract'

// One saved page serves both sides: the engine tests ingest the payload this extractor produces for it.
const FIXTURES = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../engine/tests/fixtures/pipeline')
const PAGE = resolve(FIXTURES, 'wikipedia_lead.html')
const PAYLOAD = resolve(FIXTURES, 'wikipedia_lead_blocks.json')
const PAGE_URL = 'https://en.wikipedia.org/wiki/Ada_Lovelace'
const readability = Readability as unknown as ReadabilityCtor

type Block = HtmlBlocksMessage['blocks'][number]

function parse(html: string): Document {
  return new DOMParser().parseFromString(html, 'text/html')
}

function typeOf(body: string, selector = '#x'): string {
  const el = parse(`<!DOCTYPE html><html><body>${body}</body></html>`).querySelector(selector)
  if (!el) throw new Error(`no element matches ${selector}`)
  return blockTypeFor(el).type
}

function wikipedia(options: { Readability?: ReadabilityCtor } = { Readability: readability }) {
  const doc = parse(readFileSync(PAGE, 'utf8'))
  const blocks = extractBlocks(doc, options).blocks
  const find = (start: string): Block => {
    const block = blocks.find((b) => b.text.startsWith(start))
    if (!block) throw new Error(`no block starts with "${start}"`)
    return block
  }
  return { doc, blocks, find }
}

const inInfobox = (b: Block) => /\/table\[1\]\//.test(b.dom_anchor?.xpath ?? '')

describe('extractBlocks on a Wikipedia article lead', () => {
  it('types every infobox block as table or caption, including its nested lists and divs', () => {
    const { blocks, find } = wikipedia()
    const infobox = blocks.filter(inInfobox)
    expect(infobox.length).toBeGreaterThanOrEqual(12)
    expect(new Set(infobox.map((b) => b.type))).toEqual(new Set(['table', 'caption']))
    // A list item and a div inside cells used to go out as list_item and paragraph.
    expect(find('Byron King-Noel, Viscount Ockham')).toMatchObject({ type: 'table' })
    expect(find('Marylebone, London, England')).toMatchObject({ type: 'table' })
    expect(find('Born')).toMatchObject({ type: 'table' })
  })

  it('types the infobox image caption and the thumbnail caption as captions', () => {
    const { blocks, find } = wikipedia()
    expect(find('Daguerreotype of Lovelace')).toMatchObject({ type: 'caption' })
    expect(find('Ada Byron, aged four')).toMatchObject({ type: 'caption' })
    expect(blocks.filter((b) => b.type === 'caption')).toHaveLength(2)
  })

  it('leaves body text as paragraphs, with the lead as the first paragraph block', () => {
    const { blocks, find } = wikipedia()
    const firstParagraph = blocks.find((b) => b.type === 'paragraph')
    expect(firstParagraph?.text).toMatch(/^Augusta Ada King, Countess of Lovelace \(née Byron; 10 December 1815/)
    expect(find('Lovelace was the only legitimate child')).toMatchObject({ type: 'paragraph' })
    // Page chrome, the hatnote and the hidden short description are not article text.
    const all = blocks.map((b) => b.text).join('\n')
    for (const noise of ['For the computer microarchitecture', 'English mathematician (1815', 'Jump to content', 'Main page']) {
      expect(all).not.toContain(noise)
    }
  })

  it('keeps section headings that have an [edit] link beside them, without the link', () => {
    const { blocks } = wikipedia()
    const headings = blocks.filter((b) => b.type === 'heading')
    expect(headings.map((b) => [b.text, b.level])).toEqual([
      ['Biography', 2],
      ['Childhood', 3],
      ['References', 2],
    ])
    expect(blocks.some((b) => /\[\s*edit\s*\]|^edit$/i.test(b.text))).toBe(false)
  })

  it('posts blocks in DOM order with anchors that resolve to the same text on the live page', () => {
    const { doc, blocks, find } = wikipedia()
    expect(blocks.map((b) => b.id)).toEqual(blocks.map((_, i) => `blk_${i + 1}`))
    // DOM order: the infobox precedes the lead here. The engine moves it after the first body paragraph.
    expect(blocks.findIndex(inInfobox)).toBeLessThan(blocks.findIndex((b) => b.type === 'paragraph'))
    for (const block of blocks) {
      const el = resolveXPath(doc, block.dom_anchor?.xpath ?? '')
      expect(el, block.id).not.toBeNull()
      expect(buildTextMap(el as Element).text).toBe(block.text)
    }
    for (const block of [find('Daguerreotype of Lovelace'), find('Augusta Ada King'), find('Byron King-Noel')]) {
      const range = rangeForAnchor(doc, block.dom_anchor as NonNullable<Block['dom_anchor']>)
      expect(range?.toString().replace(/\s+/g, ' ').trim()).toBe(block.text)
    }
    const word = rangeForAnchor(doc, { ...(find('Daguerreotype of Lovelace').dom_anchor as NonNullable<Block['dom_anchor']>), start_offset: 17, end_offset: 25 })
    expect(word?.toString()).toBe('Lovelace')
  })

  it('produces the payload the engine tests ingest (wikipedia_lead_blocks.json)', () => {
    const doc = parse(readFileSync(PAGE, 'utf8'))
    const payload = { ...extractBlocks(doc, { Readability: readability }), url: PAGE_URL }
    // After an intended change to the extractor: OPEN_SPEECH_UPDATE_FIXTURES=1 bun run test, then review the diff.
    if (process.env.OPEN_SPEECH_UPDATE_FIXTURES === '1') writeFileSync(PAYLOAD, `${JSON.stringify(payload, null, 2)}\n`)
    expect(payload).toEqual(JSON.parse(readFileSync(PAYLOAD, 'utf8')))
  })

  it('types the infobox the same way without Readability (plain DOM fallback)', () => {
    const { blocks, find } = wikipedia({})
    expect(new Set(blocks.filter(inInfobox).map((b) => b.type))).toEqual(new Set(['table', 'caption']))
    // Here the whole image cell is the block: a cell that holds a picture and only its caption.
    expect(find('Daguerreotype of Lovelace')).toMatchObject({ type: 'caption' })
    expect(find('Augusta Ada King')).toMatchObject({ type: 'paragraph' })
  })
})

describe('blockTypeFor', () => {
  it('types by tag outside tables and figures', () => {
    expect(blockTypeFor(parse('<h2>Title</h2>').querySelector('h2') as Element)).toEqual({ type: 'heading', level: 2 })
    expect(typeOf('<ul><li id="x">One</li></ul>')).toBe('list_item')
    expect(typeOf('<blockquote id="x">Quoted</blockquote>')).toBe('quote')
    expect(typeOf('<pre id="x">code()</pre>')).toBe('code')
    expect(typeOf('<p id="x">Text</p>')).toBe('paragraph')
  })

  it('types everything inside a data table as table', () => {
    const table = (cell: string) => `<table><tbody><tr><th>Name</th></tr><tr><td>${cell}</td></tr></tbody></table>`
    expect(typeOf(table('<ul><li id="x">One</li></ul>'))).toBe('table')
    expect(typeOf(table('<div id="x">Text in a div</div>'))).toBe('table')
    expect(typeOf(table('<h3 id="x">Group</h3>'))).toBe('table')
    expect(typeOf('<table><caption>Results</caption><tr><td><p id="x">12</p></td></tr></table>')).toBe('table')
    // A layout table nested in a data table is still part of the data table.
    expect(typeOf(table('<table><tr><td><p id="x">Inner</p></td></tr></table>'))).toBe('table')
  })

  it('does not turn the content of a layout table into table blocks', () => {
    expect(typeOf('<table><tr><td><p id="x">The article body, laid out with a table.</p></td></tr></table>')).toBe('paragraph')
    expect(typeOf('<table role="presentation"><tr><th>Menu</th><td><p id="x">Body</p></td></tr></table>')).toBe('paragraph')
    // A data table inside a layout cell does not make the rest of that cell a table.
    const nested = '<table><tr><td><table><tr><th>A</th></tr></table><p id="x">Body beside a small table.</p></td></tr></table>'
    expect(typeOf(nested)).toBe('paragraph')
    // Bare cells were always table blocks.
    expect(typeOf('<table><tr><td id="x">Cell</td></tr></table>')).toBe('table')
  })

  it('types the text that labels a picture as a caption', () => {
    expect(typeOf('<figure><img src="a.jpg"><figcaption><p id="x">In a paragraph</p></figcaption></figure>')).toBe('caption')
    expect(typeOf('<div class="thumb"><a><img src="a.jpg" width="220" height="150"></a><div id="x" class="thumbcaption">Old thumb</div></div>')).toBe('caption')
    expect(typeOf('<div class="wp-caption"><img src="a.jpg" width="600" height="400"><p id="x" class="wp-caption-text">Blog image</p></div>')).toBe('caption')
    expect(typeOf('<figure><img src="a.jpg" width="600" height="400"><div id="x">No figcaption element</div></figure>')).toBe('caption')
    const cell = '<table><tr><th>Portrait</th></tr><tr><td id="x"><img src="a.jpg" width="220" height="300"><br>Portrait, 1843</td></tr></table>'
    expect(typeOf(cell)).toBe('caption')
  })

  it('does not mistake table data or body text for captions', () => {
    const row = (cell: string) => `<table><tr><th>Country</th></tr><tr>${cell}</tr></table>`
    // Icons beside data: a flag, an inline SVG check mark.
    expect(typeOf(row('<td id="x"><img src="fr.png" width="23" height="15"> France</td>'))).toBe('table')
    expect(typeOf(row('<td id="x"><svg viewBox="0 0 16 16"></svg> Yes</td>'))).toBe('table')
    // A cell with a picture and more text than this block: the block is data, not the caption.
    expect(typeOf(row('<td><img src="a.jpg" width="220" height="300"><div id="x">Widget Pro</div><div>From $12, in stock</div></td>'))).toBe('table')
    // A paragraph after a captioned picture, and a wrapper class that also holds the picture.
    expect(typeOf('<div class="wp-caption"><img src="a.jpg"><p class="wp-caption-text">Blog image</p></div><p id="x">Body</p>')).toBe('paragraph')
    expect(typeOf('<div class="image-with-caption"><img src="a.jpg" width="600" height="400"><p>c</p><p id="x">Body text in the same wrapper.</p></div>')).toBe('paragraph')
  })
})
