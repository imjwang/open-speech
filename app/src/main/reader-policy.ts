/** 09 § Reader: "Opens only for content worth seeing: PDFs, web pages, images, and long text." */
export const LONG_TEXT_BLOCKS = 30

export function worthShowing(doc: { kind: string; block_count?: number }): boolean {
  if (doc.kind === 'pdf' || doc.kind === 'web' || doc.kind === 'image') return true
  return (doc.block_count ?? 0) >= LONG_TEXT_BLOCKS
}

/** Turn a `playback.word` anchor into the DOM anchor the web preload paints. */
export function toDomAnchor(
  anchor: {
    block_id?: string | null
    char_start?: number | null
    char_end?: number | null
    dom_anchor?: { xpath: string; start_offset?: number; end_offset?: number | null } | null
  },
  blockAnchor: (blockId: string) => { xpath: string; start_offset?: number; end_offset?: number | null } | null,
): { xpath: string; start_offset: number; end_offset: number | null } | null {
  if (anchor.dom_anchor) {
    return {
      xpath: anchor.dom_anchor.xpath,
      start_offset: anchor.dom_anchor.start_offset ?? 0,
      end_offset: anchor.dom_anchor.end_offset ?? null,
    }
  }
  if (anchor.block_id) {
    const base = blockAnchor(anchor.block_id)
    if (!base) return null
    const origin = base.start_offset ?? 0
    if (anchor.char_start == null) return { xpath: base.xpath, start_offset: origin, end_offset: base.end_offset ?? null }
    return {
      xpath: base.xpath,
      start_offset: origin + anchor.char_start,
      end_offset: anchor.char_end == null ? null : origin + anchor.char_end,
    }
  }
  return null
}
