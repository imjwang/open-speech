/** Read-clipboard (FR-READ-5): Electron clipboard text or image → engine document, no OS prompts. */
import { clipboard } from 'electron'

import { unwrap, type CreateDocumentResponse, type EngineClient } from '../shared/api/client'
import type { ClipboardContent } from '../shared/ipc-channels'

const MAX_TEXT = 2_000_000
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif']

/** Text wins when both are present (word processors also put an image rendition on the clipboard). */
export async function readClipboard(): Promise<ClipboardContent> {
  const text = await clipboard.readText()
  if (text.trim()) return { kind: 'text', text: text.slice(0, MAX_TEXT) }
  for (const item of await clipboard.read()) {
    const type = IMAGE_TYPES.find((t) => item.types.includes(t)) ?? item.types.find((t) => t.startsWith('image/'))
    if (!type) continue
    const blob = await item.getType(type)
    if (blob instanceof Blob && blob.size > 0) {
      const base64 = Buffer.from(await blob.arrayBuffer()).toString('base64')
      return { kind: 'image', base64, mediaType: type }
    }
  }
  return { kind: 'empty' }
}

export function titleFromText(text: string): string {
  const first = text.trim().split(/\r?\n/, 1)[0] ?? ''
  return first.length > 80 ? `${first.slice(0, 77)}…` : first || 'Clipboard'
}

/** POST the clipboard as `kind: text|image` with `play: true`. Returns null when the clipboard is empty. */
export async function postClipboard(
  client: EngineClient,
  content: ClipboardContent,
): Promise<CreateDocumentResponse | null> {
  if (content.kind === 'empty') return null
  const base = { play: true, confirm_estimate: false, image_mode: 'auto' as const }
  const body =
    content.kind === 'text'
      ? { ...base, kind: 'text' as const, text: content.text, title: titleFromText(content.text) }
      : { ...base, kind: 'image' as const, image_base64: content.base64, media_type: content.mediaType, title: 'Clipboard image' }
  return unwrap(client.POST('/api/v1/documents', { body }))
}
