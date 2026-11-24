/**
 * Readability-based extraction of the main content of a live page into `html_blocks` (04 § Documents),
 * each with a DOM anchor (XPath of the live element + text offsets). Readability runs on a clone whose
 * candidate elements carry their live XPath, so its output maps back onto the live DOM.
 */
import type { HtmlBlocksMessage } from '../../shared/ipc'
import { buildTextMap, resolveXPath, xpathFor } from './dom-text'

const BLOCK_SELECTOR = 'p,h1,h2,h3,h4,h5,h6,li,blockquote,pre,figcaption,td,th,dt,dd'
const HEADING_SELECTOR = 'h1,h2,h3,h4,h5,h6'
const HEADING_LINK_MAX_CHARS = 24
const XP_ATTR = 'data-open-speech-xp'

export interface ReadabilityLike {
  parse(): { title?: string | null; content?: string | null } | null
}
export type ReadabilityCtor = new (doc: Document, options?: Record<string, unknown>) => ReadabilityLike

const PICTURE_SELECTOR = 'img,video,canvas,svg'
const ICON_MAX_PX = 40
const CAPTION_MAX_CHARS = 300
const CAPTION_CLASS = /caption/i
const DATA_TABLE_PARTS =
  ':scope > caption, :scope > thead, :scope > tfoot, :scope > colgroup, :scope > tbody > tr > th, :scope > tr > th'

/** A table that presents data (an infobox, a results table), as opposed to one used to lay out the page. */
function isDataTable(table: Element): boolean {
  const role = (table.getAttribute('role') ?? '').toLowerCase()
  if (role === 'presentation' || role === 'none') return false
  return table.hasAttribute('summary') || table.querySelector(DATA_TABLE_PARTS) !== null
}

function inDataTable(el: Element, known?: Map<Element, boolean>): boolean {
  for (let table = el.closest('table'); table; table = table.parentElement?.closest('table') ?? null) {
    let data = known?.get(table)
    if (data === undefined) {
      data = isDataTable(table)
      known?.set(table, data)
    }
    if (data) return true
  }
  return false
}

/**
 * A picture that is content. Icons (flags, padlocks, check marks) don't make the text beside them a
 * caption: size comes from layout on a live page, else from the width/height attributes; when neither
 * is known, an `<img>` or `<video>` counts as a picture and an inline `<svg>` or `<canvas>` as an icon.
 */
function isPicture(pic: Element): boolean {
  const rect = typeof pic.getBoundingClientRect === 'function' ? pic.getBoundingClientRect() : null
  const laidOut = Math.max(rect?.width ?? 0, rect?.height ?? 0)
  const side = laidOut || Math.max(Number(pic.getAttribute('width')) || 0, Number(pic.getAttribute('height')) || 0)
  if (side === 0) return pic.localName === 'img' || pic.localName === 'video'
  return side > ICON_MAX_PX
}

function hasPicture(el: Element): boolean {
  return Array.from(el.querySelectorAll(PICTURE_SELECTOR)).some(isPicture)
}

/**
 * Text that labels a picture: anything in a `<figcaption>`, an element named as a caption
 * (`thumbcaption`, `wp-caption-text`, `infobox-caption`, …), or the only text in a table cell or
 * `<figure>` that holds a picture (an infobox image cell).
 */
function isCaption(el: Element): boolean {
  if (el.closest('figcaption')) return true
  for (let a: Element | null = el, depth = 0; a && depth < 3; a = a.parentElement, depth++) {
    // The wrapper that also holds the picture (`div.wp-caption`) is not itself the caption.
    if (CAPTION_CLASS.test(a.getAttribute('class') ?? '') && !hasPicture(a)) return true
    if (a.matches('td,th,figure,table,body')) break
  }
  const box = el.closest('td,th,figure')
  if (!box || !hasPicture(box)) return false
  const text = buildTextMap(el).text
  return text.length <= CAPTION_MAX_CHARS && (el === box || text === buildTextMap(box).text)
}

/**
 * The engine type of a block element. Blocks are typed as what they are on the page, not only by
 * tag, so skip lines for tables and captions apply to all of an infobox: everything inside a data table
 * is `table` (also its nested lists and divs), and picture captions are `caption`.
 */
export function blockTypeFor(el: Element, knownTables?: Map<Element, boolean>): { type: string; level: number | null } {
  const tag = el.localName.toLowerCase()
  if (isCaption(el)) return { type: 'caption', level: null }
  if (tag === 'td' || tag === 'th' || inDataTable(el, knownTables)) return { type: 'table', level: null }
  const heading = /^h([1-6])$/.exec(tag)
  if (heading) return { type: 'heading', level: Number(heading[1]) }
  switch (tag) {
    case 'li':
      return { type: 'list_item', level: null }
    case 'blockquote':
      return { type: 'quote', level: null }
    case 'pre':
      return { type: 'code', level: null }
    default:
      return { type: 'paragraph', level: null }
  }
}

/** An inline element next to a heading that is only a short link or button: "[edit]", "#", "¶", "Copy link". */
function isHeadingLink(el: Element): boolean {
  if (!el.matches('a,span,button,small,sup')) return false
  const actionable = el.matches('a,button') || el.querySelector('a,button') !== null
  return actionable && (el.textContent ?? '').trim().length <= HEADING_LINK_MAX_CHARS
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
      // A link beside a heading ("[edit]", "#", "¶") makes Readability discard short headings together
      // with their wrapper as link chrome. Such links are never read, so they leave the clone (after
      // tagging, so the XPaths above still describe the live page).
      for (const heading of Array.from(clone.querySelectorAll(HEADING_SELECTOR))) {
        for (const sibling of Array.from(heading.parentElement?.children ?? [])) {
          if (sibling !== heading && isHeadingLink(sibling)) sibling.remove()
        }
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

  // Blocks stay in DOM order, each with its own anchor. The engine decides the reading order (side
  // material ahead of the first body paragraph is read after it), which needs these types to be right.
  const seen = new Set<string>()
  const knownTables = new Map<Element, boolean>()
  const blocks: HtmlBlocksMessage['blocks'] = []
  for (const xpath of xpaths) {
    if (blocks.length >= maxBlocks) break
    if (seen.has(xpath)) continue
    seen.add(xpath)
    const el = resolveXPath(doc, xpath)
    if (!el) continue
    const text = buildTextMap(el).text
    if (!/[\p{L}\p{N}]/u.test(text)) continue
    const { type, level } = blockTypeFor(el, knownTables)
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
