/**
 * Paints the current sentence and word on the live page with the CSS Custom Highlight API: Range
 * objects registered under names styled by a user stylesheet the main process injects. The page's DOM
 * is never modified. Follow mode scrolls the sentence into view; a manual scroll releases it.
 */
import type { WebPaint } from '../../shared/ipc'
import { rangeForAnchor, type TextMap } from './dom-text'

export const SENTENCE_HIGHLIGHT = 'open-speech-sentence'
export const WORD_HIGHLIGHT = 'open-speech-word'

const SCROLL_KEYS = new Set(['PageUp', 'PageDown', 'Home', 'End', 'ArrowUp', 'ArrowDown', ' '])

export class HighlightPainter {
  private follow = true
  private programmaticUntil = 0
  private cache = new Map<string, TextMap>()
  private lastSentence: Range | null = null
  private readonly onUserScroll = () => {
    if (this.follow && Date.now() > this.programmaticUntil) {
      this.follow = false
      this.onFollowReleased()
    }
  }

  constructor(
    private readonly doc: Document,
    private readonly onFollowReleased: () => void,
  ) {
    const win = doc.defaultView
    win?.addEventListener('wheel', this.onUserScroll, { passive: true })
    win?.addEventListener('touchmove', this.onUserScroll, { passive: true })
    win?.addEventListener('keydown', (e) => {
      if (SCROLL_KEYS.has(e.key)) this.onUserScroll()
    })
  }

  /** The DOM may change (SPAs); drop cached text maps when it does. */
  invalidate(): void {
    this.cache.clear()
  }

  paint(paint: WebPaint): void {
    const registry = (globalThis as { CSS?: { highlights?: HighlightRegistry } }).CSS?.highlights
    if (!registry || typeof Highlight === 'undefined') return
    const sentence = paint.sentence
      .map((a) => rangeForAnchor(this.doc, a, this.cache))
      .filter((r): r is Range => r !== null)
    const word = paint.word ? rangeForAnchor(this.doc, paint.word, this.cache) : null
    registry.set(SENTENCE_HIGHLIGHT, new Highlight(...sentence))
    if (word) registry.set(WORD_HIGHLIGHT, new Highlight(word))
    else registry.delete(WORD_HIGHLIGHT)
    this.lastSentence = sentence[0] ?? word
    if (paint.scroll && this.follow) this.scrollIntoView(this.lastSentence)
  }

  clear(): void {
    const registry = (globalThis as { CSS?: { highlights?: HighlightRegistry } }).CSS?.highlights
    registry?.delete(SENTENCE_HIGHLIGHT)
    registry?.delete(WORD_HIGHLIGHT)
  }

  setFollow(follow: boolean): void {
    this.follow = follow
    if (follow) this.scrollIntoView(this.lastSentence)
  }

  private scrollIntoView(range: Range | null): void {
    const win = this.doc.defaultView
    if (!range || !win) return
    const rect = range.getBoundingClientRect()
    const margin = Math.min(120, win.innerHeight * 0.15)
    if (rect.top >= margin && rect.bottom <= win.innerHeight - margin) return
    const reduce = win.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
    this.programmaticUntil = Date.now() + 800
    win.scrollBy({ top: rect.top - win.innerHeight * 0.33, behavior: reduce ? 'auto' : 'smooth' })
  }
}
