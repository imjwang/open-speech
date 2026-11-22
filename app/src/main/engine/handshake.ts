/**
 * The engine prints one JSON handshake line on stdout when it is ready (03 § Engine supervision):
 *   {"port": 52341, "token": "…", "version": "0.1.0", "pid": 4242}
 * Anything else on stdout (log lines, banners) is ignored.
 */
export interface Handshake {
  port: number
  token: string
  version: string
  pid: number
}

export function parseHandshakeLine(line: string): Handshake | null {
  const trimmed = line.trim()
  if (!trimmed.startsWith('{') || !trimmed.endsWith('}')) return null
  let value: unknown
  try {
    value = JSON.parse(trimmed)
  } catch {
    return null
  }
  if (!value || typeof value !== 'object') return null
  const v = value as Record<string, unknown>
  const port = v.port
  const token = v.token
  if (typeof port !== 'number' || !Number.isInteger(port) || port < 1 || port > 65535) return null
  if (typeof token !== 'string' || token.length < 16 || /\s/.test(token)) return null
  const version = typeof v.version === 'string' ? v.version : ''
  const pid = typeof v.pid === 'number' && Number.isInteger(v.pid) ? v.pid : -1
  return { port, token, version, pid }
}

/** Splits a byte/char stream into complete lines (handles chunks that end mid-line). */
export class LineSplitter {
  private buffer = ''
  private readonly maxLine: number

  constructor(maxLine = 64 * 1024) {
    this.maxLine = maxLine
  }

  push(chunk: string): string[] {
    this.buffer += chunk
    const parts = this.buffer.split(/\r?\n/)
    this.buffer = parts.pop() ?? ''
    if (this.buffer.length > this.maxLine) this.buffer = '' // runaway line without newline: drop it
    return parts
  }
}

export function baseUrlFor(handshake: Handshake): string {
  return `http://127.0.0.1:${handshake.port}`
}
