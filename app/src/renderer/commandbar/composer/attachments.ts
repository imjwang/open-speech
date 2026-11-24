/**
 * What can be attached to a command in the composer (FR-BAR-7): files, links and pasted text. Each shows as
 * an inline badge; on submit they travel with the command and the engine imports each as a document, so
 * "read these" queues them and anything else can refer to them.
 */
import type { Attachment } from '@shared/api/client'
import type { FileHit, FileKind } from '@shared/ipc-channels'

export type AttachmentItem =
  /** A file on disk (the `@` menu, the open panel, a file dragged from Finder). */
  | { kind: 'file'; path: string; name: string; fileKind: FileKind }
  /** A file with no path here (a pasted screenshot, or any file in a plain browser): uploaded on submit. */
  | { kind: 'upload'; file: File; name: string; fileKind: FileKind }
  | { kind: 'url'; url: string }
  | { kind: 'text'; text: string }

export const URL_RE = /^https?:\/\/[^\s]+$/i
/** A link inside running text; trailing punctuation belongs to the sentence, not the link. */
export const URL_IN_TEXT = /\bhttps?:\/\/[^\s<>"']*[^\s<>"'.,;:!?)\]}]/gi
/** Pasted text this long (or with this many lines) is content to read, not a command to edit. */
const LONG_TEXT_CHARS = 280
const LONG_TEXT_LINES = 3

const EXT_KIND: Record<string, FileKind> = {
  pdf: 'pdf',
  docx: 'doc',
  txt: 'text',
  md: 'text',
  markdown: 'text',
  html: 'web',
  htm: 'web',
  png: 'image',
  jpg: 'image',
  jpeg: 'image',
  webp: 'image',
  gif: 'image',
  wav: 'audio',
  m4a: 'audio',
  mp3: 'audio',
  aiff: 'audio',
  aif: 'audio',
  flac: 'audio',
  ogg: 'audio',
  opus: 'audio',
  caf: 'audio',
  aac: 'audio',
}

export function kindOfName(name: string, mime = ''): FileKind {
  if (mime.startsWith('image/')) return 'image'
  if (mime.startsWith('audio/')) return 'audio'
  return EXT_KIND[name.split('.').pop()?.toLowerCase() ?? ''] ?? 'other'
}

export function fromHit(hit: FileHit): AttachmentItem {
  return { kind: 'file', path: hit.path, name: hit.name, fileKind: hit.kind }
}

/** A dropped, pasted or picked `File`: by path when the host can give one, else as an upload. */
export function fromFile(file: File, pathFor: (file: File) => string): AttachmentItem {
  const name = file.name || 'file'
  const fileKind = kindOfName(name, file.type)
  const path = pathFor(file)
  return path ? { kind: 'file', path, name, fileKind } : { kind: 'upload', file, name, fileKind }
}

/** Pasted text: a link becomes a link badge, long text a text badge; short text is just typing (null). */
export function fromPastedText(text: string): AttachmentItem | null {
  const t = text.trim()
  if (!t) return null
  if (URL_RE.test(t)) return { kind: 'url', url: t }
  if (t.length >= LONG_TEXT_CHARS || t.split(/\r?\n/).length >= LONG_TEXT_LINES) return { kind: 'text', text: t }
  return null
}

export function fromDataTransfer(dt: DataTransfer | null, pathFor: (file: File) => string): AttachmentItem[] {
  if (!dt) return []
  const files = Array.from(dt.files ?? [])
  if (files.length) return files.map((f) => fromFile(f, pathFor))
  const uris = dt
    .getData('text/uri-list')
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('#') && URL_RE.test(l))
  if (uris.length) return uris.map((url) => ({ kind: 'url', url }))
  const text = dt.getData('text/plain').trim()
  if (!text) return []
  return [URL_RE.test(text) ? { kind: 'url', url: text } : { kind: 'text', text }]
}

/** What the badge says. */
export function labelOf(item: AttachmentItem): string {
  switch (item.kind) {
    case 'file':
    case 'upload':
      return item.name
    case 'url':
      try {
        const u = new URL(item.url)
        const tail = u.pathname.replace(/\/+$/, '').split('/').pop() ?? ''
        return tail ? `${u.hostname.replace(/^www\./, '')}/${tail}` : u.hostname.replace(/^www\./, '')
      } catch {
        return item.url
      }
    case 'text': {
      const first = item.text.split(/\r?\n/, 1)[0] ?? ''
      return first.length > 28 ? `${first.slice(0, 27)}…` : first
    }
  }
}

/** The badge's colour family and icon. */
export function badgeKind(item: AttachmentItem): 'file' | 'url' | 'text' | 'image' {
  if (item.kind === 'url') return 'url'
  if (item.kind === 'text') return 'text'
  return item.fileKind === 'image' ? 'image' : 'file'
}

/** How an attachment appears in the command's text, so a sentence still reads with its badges in place. */
export function textOf(item: AttachmentItem): string {
  return item.kind === 'url' ? item.url : item.kind === 'text' ? '' : item.name
}

export function sameItem(a: AttachmentItem, b: AttachmentItem): boolean {
  if (a.kind === 'file' && b.kind === 'file') return a.path === b.path
  if (a.kind === 'url' && b.kind === 'url') return a.url === b.url
  if (a.kind === 'text' && b.kind === 'text') return a.text === b.text
  if (a.kind === 'upload' && b.kind === 'upload') return a.file === b.file
  return false
}

/** The wire form. Uploads must have been turned into documents first (`resolved` maps them to ids). */
export function toWire(items: AttachmentItem[], resolved: Map<AttachmentItem, string>): Attachment[] {
  return items.map((item): Attachment => {
    switch (item.kind) {
      case 'file':
        return { kind: 'file', path: item.path, name: item.name }
      case 'upload':
        return { kind: 'document', document_id: resolved.get(item) ?? '', name: item.name }
      case 'url':
        return { kind: 'url', url: item.url }
      case 'text':
        return { kind: 'text', text: item.text }
    }
  })
}
