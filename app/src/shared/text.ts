/**
 * Engine offsets are Python string indices: Unicode code points (document/model.py). JavaScript strings
 * index UTF-16 code units. These helpers convert, with a fast path for text without astral characters.
 */
const SURROGATE = /[\uD800-\uDFFF]/

/** `table[cp]` = UTF-16 index of code point `cp`; `table[length]` = text.length. Null when identical. */
export function codePointIndexTable(text: string): number[] | null {
  if (!SURROGATE.test(text)) return null
  const table: number[] = []
  let u = 0
  for (const ch of text) {
    table.push(u)
    u += ch.length
  }
  table.push(u)
  return table
}

export function cpToUtf16(text: string, cp: number, table: number[] | null = codePointIndexTable(text)): number {
  if (!table) return Math.max(0, Math.min(cp, text.length))
  return table[Math.max(0, Math.min(cp, table.length - 1))] ?? text.length
}

export function codePointLength(text: string): number {
  if (!SURROGATE.test(text)) return text.length
  let n = 0
  for (const _ of text) n++
  return n
}

/** Slice by code point indices. */
export function cpSlice(text: string, start: number, end?: number, table: number[] | null = codePointIndexTable(text)): string {
  const s = cpToUtf16(text, start, table)
  const e = end === undefined ? text.length : cpToUtf16(text, end, table)
  return text.slice(s, e)
}
