/**
 * DOM ↔ text mapping for live web pages. A block's text is the whitespace-normalised text of its
 * element; offsets count code points (like the engine). `rangeFor` turns an offset range back into a
 * DOM `Range` so highlights land on the live page without mutating it.
 */
const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'IFRAME', 'OBJECT', 'SVG', 'MATH', 'BUTTON', 'SELECT'])

interface Pos {
  node: Text
  offset: number // UTF-16 offset in node.data
  length: number // UTF-16 length of this code point (1 or 2)
}

export interface TextMap {
  text: string
  positions: Pos[] // one per code point of `text`
}

function skipped(node: Node, root: Node): boolean {
  for (let el = node.parentElement; el && el !== root.parentElement; el = el.parentElement) {
    if (SKIP.has(el.tagName.toUpperCase())) return true
    if (el === root) break
  }
  return false
}

export function buildTextMap(root: Element): TextMap {
  const doc = root.ownerDocument
  const walker = doc.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */)
  let text = ''
  const positions: Pos[] = []
  let pendingSpace: Pos | null = null
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const node = n as Text
    if (skipped(node, root)) continue
    const data = node.data
    let u = 0
    for (const ch of data) {
      const pos: Pos = { node, offset: u, length: ch.length }
      u += ch.length
      if (/\s/.test(ch)) {
        if (text.length > 0 && !pendingSpace) pendingSpace = pos
        continue
      }
      if (pendingSpace) {
        text += ' '
        positions.push(pendingSpace)
        pendingSpace = null
      }
      text += ch
      positions.push(pos)
    }
  }
  return { text, positions }
}

export function rangeFor(map: TextMap, start: number, end: number | null): Range | null {
  const n = map.positions.length
  if (n === 0) return null
  const s = Math.max(0, Math.min(start, n - 1))
  const e = Math.max(s + 1, Math.min(end ?? n, n))
  const first = map.positions[s]
  const last = map.positions[e - 1]
  if (!first || !last) return null
  const range = first.node.ownerDocument.createRange()
  range.setStart(first.node, first.offset)
  range.setEnd(last.node, last.offset + last.length)
  return range
}

/** `/html[1]/body[1]/div[2]/p[3]` — element indices among same-name siblings, 1-based. */
export function xpathFor(element: Element): string {
  const parts: string[] = []
  for (let el: Element | null = element; el; el = el.parentElement) {
    const name = el.localName.toLowerCase()
    let index = 1
    for (let sib = el.previousElementSibling; sib; sib = sib.previousElementSibling) {
      if (sib.localName.toLowerCase() === name) index++
    }
    parts.unshift(`${name}[${index}]`)
  }
  return `/${parts.join('/')}`
}

/** Resolve the simple XPaths produced by `xpathFor` (no namespaces, no predicates beyond indices). */
export function resolveXPath(doc: Document, xpath: string): Element | null {
  const steps = xpath.split('/').filter(Boolean)
  let current: Element | null = null
  for (const step of steps) {
    const m = /^([a-z0-9_-]+)(?:\[(\d+)\])?$/i.exec(step)
    if (!m) return null
    const name = (m[1] ?? '').toLowerCase()
    const index = Number(m[2] ?? '1')
    if (current === null) {
      const root = doc.documentElement
      if (!root || root.localName.toLowerCase() !== name || index !== 1) return null
      current = root
      continue
    }
    let count = 0
    let next: Element | null = null
    for (let child: Element | null = current.firstElementChild; child; child = child.nextElementSibling) {
      if (child.localName.toLowerCase() === name && ++count === index) {
        next = child
        break
      }
    }
    if (!next) return null
    current = next
  }
  return current
}

export interface DomAnchorLike {
  xpath: string
  start_offset?: number
  end_offset?: number | null
}

export function rangeForAnchor(doc: Document, anchor: DomAnchorLike, cache?: Map<string, TextMap>): Range | null {
  const el = resolveXPath(doc, anchor.xpath)
  if (!el) return null
  let map = cache?.get(anchor.xpath)
  if (!map) {
    map = buildTextMap(el)
    cache?.set(anchor.xpath, map)
  }
  return rangeFor(map, anchor.start_offset ?? 0, anchor.end_offset ?? null)
}
