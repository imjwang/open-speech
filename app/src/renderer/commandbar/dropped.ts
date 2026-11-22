/** Classify dropped or pasted content (FR-BAR-7) and API keys pasted into the bar (FR-SETUP-2). */
import type { DroppedItem } from './store'

const KEY_RE = /^(sk-ant-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,})$/
const URL_RE = /^https?:\/\/[^\s]+$/i
const LONG_TEXT = 160

export function looksLikeApiKey(text: string): boolean {
  return KEY_RE.test(text.trim())
}

export function detectKeyProvider(text: string): 'anthropic' | 'openai' | 'gemini' | null {
  const t = text.trim()
  if (t.startsWith('sk-ant-')) return 'anthropic'
  if (t.startsWith('sk-')) return 'openai'
  if (t.startsWith('AIza')) return 'gemini'
  return null
}

/**
 * Pasted text: a key goes to the secure key flow (never into the transcript), a URL or long/multi-line
 * text becomes a dropped item; short text is ordinary typing (null).
 */
export function classifyPastedText(text: string): DroppedItem | { kind: 'key'; key: string } | null {
  const t = text.trim()
  if (!t) return null
  if (looksLikeApiKey(t)) return { kind: 'key', key: t }
  if (URL_RE.test(t)) return { kind: 'url', url: t }
  if (t.length >= LONG_TEXT || /\n/.test(t)) return { kind: 'text', text: t }
  return null
}

export function itemFromFile(file: File): DroppedItem {
  return { kind: 'file', file, name: file.name || 'file', isImage: file.type.startsWith('image/') }
}

export function itemFromDataTransfer(dt: DataTransfer | null): DroppedItem | null {
  if (!dt) return null
  const file = dt.files?.[0]
  if (file) return itemFromFile(file)
  const uriList = dt.getData('text/uri-list')
  const firstUri = uriList
    .split(/\r?\n/)
    .map((l) => l.trim())
    .find((l) => l && !l.startsWith('#'))
  if (firstUri && URL_RE.test(firstUri)) return { kind: 'url', url: firstUri }
  const text = dt.getData('text/plain').trim()
  if (!text) return null
  if (URL_RE.test(text)) return { kind: 'url', url: text }
  return { kind: 'text', text }
}

export function describeDropped(item: DroppedItem): string {
  switch (item.kind) {
    case 'file':
      return item.name
    case 'url':
      try {
        return new URL(item.url).hostname
      } catch {
        return item.url
      }
    case 'text': {
      const first = item.text.split(/\r?\n/, 1)[0] ?? ''
      return first.length > 60 ? `${first.slice(0, 57)}…` : first
    }
  }
}
