/**
 * Readability-based extraction of the main content of a live page into `html_blocks` (04 § Documents),
 * each with a DOM anchor (XPath of the live element + text offsets). Readability runs on a clone whose
 * candidate elements carry their live XPath, so its output maps back onto the live DOM.
 */
import type { HtmlBlocksMessage } from '../../shared/ipc'
import { buildTextMap, resolveXPath, xpathFor } from './dom-text'

const BLOCK_SELECTOR = 'p,h1,h2,h3,h4,h5,h6,li,blockquote,pre,figcaption,td,th,dt,dd'
const XP_ATTR = 'data-open-speech-xp'

export interface ReadabilityLike {
  parse(): { title?: string | null; content?: string | null } | null
}
export type ReadabilityCtor = new (doc: Document, options?: Record<string, unknown>) => ReadabilityLike

export function blockTypeFor(el: Element): { type: string; level: number | null } {
  const tag = el.localName.toLowerCase()
  const heading = /^h([1-6])$/.exec(tag)
  if (heading) return { type: 'heading', level: Number(heading[1]) }
  switch (tag) {
    case 'li':
      return { type: 'list_item', level: null }
    case 'blockquote':
      return { type: 'quote', level: null }
    case 'pre':
      return { type: 'code', level: null }
    case 'figcaption':
      return { type: 'caption', level: null }
    case 'td':
    case 'th':
      return { type: 'table', level: null }
    default:
      return { type: 'paragraph', level: null }
  }
}

function hasOwnText(el: Element): boolean {
  for (let n = el.firstChild; n; n = n.nextSibling) {
    if (n.nodeType === 3 && /\w/.test(n.textContent ?? '')) return true
  }
  return false
}

function isLeaf(el: Element, selector: string): boolean {
  return el.querySelector(selector) === null
}

function visible(el: Element): boolean {
  const win = el.ownerDocument.defaultView
  if (!win || typeof win.getComputedStyle !== 'function') return true
  const style = win.getComputedStyle(el)
  return style.display !== 'none' && style.visibility !== 'hidden'
}

export interface ExtractOptions {
  Readability?: ReadabilityCtor
  maxBlocks?: number
}

export function extractBlocks(doc: Document, options: ExtractOptions = {}): HtmlBlocksMessage {
  const maxBlocks = options.maxBlocks ?? 5000
  let xpaths: string[] = []
  let title = doc.title || ''

  if (options.Readability) {
    try {
      const clone = doc.cloneNode(true) as Document
      for (const el of Array.from(clone.querySelectorAll(BLOCK_SELECTOR))) el.setAttribute(XP_ATTR, xpathFor(el))
      // Text sitting directly in <div>s is common; Readability turns such divs into <p>s.
      for (const el of Array.from(clone.querySelectorAll('div,section,article'))) {
        if (hasOwnText(el)) el.setAttribute(XP_ATTR, xpathFor(el))
      }
      const article = new options.Readability(clone, { keepClasses: false }).parse()
      if (article?.content) {
        const parsed = new DOMParser().parseFromString(article.content, 'text/html')
        const tagged = Array.from(parsed.querySelectorAll(`[${XP_ATTR}]`))
        xpaths = tagged
          .filter((el) => isLeaf(el, `[${XP_ATTR}]`))
          .map((el) => el.getAttribute(XP_ATTR) ?? '')
          .filter(Boolean)
        title = article.title || title
      }
    } catch {
      xpaths = []
    }
  }

  if (xpaths.length === 0 && doc.body) {
    xpaths = Array.from(doc.body.querySelectorAll(BLOCK_SELECTOR))
      .filter((el) => isLeaf(el, BLOCK_SELECTOR) && visible(el))
      .map(xpathFor)
  }

  const seen = new Set<string>()
  const blocks: HtmlBlocksMessage['blocks'] = []
  for (const xpath of xpaths) {
    if (blocks.length >= maxBlocks) break
    if (seen.has(xpath)) continue
    seen.add(xpath)
    const el = resolveXPath(doc, xpath)
    if (!el) continue
    const text = buildTextMap(el).text
    if (!/[\p{L}\p{N}]/u.test(text)) continue
    const { type, level } = blockTypeFor(el)
    blocks.push({
      id: `blk_${blocks.length + 1}`,
      type,
      text: text.slice(0, 100_000),
      level,
      dom_anchor: { xpath, start_offset: 0, end_offset: null },
    })
  }
  return { url: doc.location?.href ?? '', title: title.slice(0, 1000), blocks }
}
