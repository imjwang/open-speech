import { describe, expect, it } from 'vitest'

import { baseUrlFor, LineSplitter, parseHandshakeLine } from './handshake'

const TOKEN = 'abcdefghijklmnop0123'

describe('parseHandshakeLine', () => {
  it('parses the ready line', () => {
    expect(parseHandshakeLine(`  {"port": 52341, "token": "${TOKEN}", "version": "0.1.0", "pid": 4242}\n`)).toEqual({
      port: 52341,
      token: TOKEN,
      version: '0.1.0',
      pid: 4242,
    })
  })

  it('defaults a missing version and pid', () => {
    expect(parseHandshakeLine(JSON.stringify({ port: 1, token: TOKEN }))).toEqual({ port: 1, token: TOKEN, version: '', pid: -1 })
  })

  it('ignores log lines and malformed JSON', () => {
    expect(parseHandshakeLine('INFO: starting engine')).toBeNull()
    expect(parseHandshakeLine('{not json}')).toBeNull()
    expect(parseHandshakeLine('[1, 2]')).toBeNull()
    expect(parseHandshakeLine('{}')).toBeNull()
  })

  it.each([0, 65536, 1.5, '8080'])('rejects port %j', (port) => {
    expect(parseHandshakeLine(JSON.stringify({ port, token: TOKEN }))).toBeNull()
  })

  it('rejects short tokens and tokens with whitespace', () => {
    expect(parseHandshakeLine(JSON.stringify({ port: 80, token: 'short' }))).toBeNull()
    expect(parseHandshakeLine(JSON.stringify({ port: 80, token: 'abcdefgh ijklmnopq' }))).toBeNull()
    expect(parseHandshakeLine(JSON.stringify({ port: 80, token: 1234567890123456 }))).toBeNull()
  })
})

describe('LineSplitter', () => {
  it('buffers partial lines across chunks and handles CRLF', () => {
    const s = new LineSplitter()
    expect(s.push('hello\nwor')).toEqual(['hello'])
    expect(s.push('ld\r\n{"port"')).toEqual(['world'])
    expect(s.push(': 1}\n\n')).toEqual(['{"port": 1}', ''])
  })

  it('drops a runaway line without a newline', () => {
    const s = new LineSplitter(8)
    expect(s.push('0123456789')).toEqual([])
    expect(s.push('ab\ncd\n')).toEqual(['ab', 'cd'])
  })
})

describe('baseUrlFor', () => {
  it('always uses the IPv4 loopback', () => {
    expect(baseUrlFor({ port: 52341, token: TOKEN, version: '', pid: 1 })).toBe('http://127.0.0.1:52341')
  })
})
