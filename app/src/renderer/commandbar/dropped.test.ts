import { describe, expect, it } from 'vitest'

import { classifyPastedText, describeDropped, detectKeyProvider, itemFromDataTransfer, itemFromFile, looksLikeApiKey } from './dropped'

const ANTHROPIC = `sk-ant-api03-${'A1b2C3d4'.repeat(12)}`
const OPENAI = `sk-proj-${'x9Y8z7W6'.repeat(20)}` // project keys are ~164 chars: longer than the long-text cutoff
const GEMINI = `AIza${'Sy0123456789abcdefghijklmnopqrstuv'.slice(0, 35)}`

function dataTransfer(data: Record<string, string>, files: File[] = []): DataTransfer {
  return { files, getData: (type: string) => data[type] ?? '' } as unknown as DataTransfer
}

describe('classifyPastedText', () => {
  it('classifies API keys as keys, never as text', () => {
    for (const key of [ANTHROPIC, OPENAI, GEMINI]) {
      expect(classifyPastedText(key)).toEqual({ kind: 'key', key })
      expect(classifyPastedText(`  ${key}\n`)).toEqual({ kind: 'key', key })
    }
    expect(OPENAI.length).toBeGreaterThan(160)
  })

  it('classifies URLs', () => {
    expect(classifyPastedText(' https://example.com/article?id=1 ')).toEqual({ kind: 'url', url: 'https://example.com/article?id=1' })
    expect(classifyPastedText('HTTP://EXAMPLE.COM')).toEqual({ kind: 'url', url: 'HTTP://EXAMPLE.COM' })
  })

  it('turns long or multi-line text into a dropped text item', () => {
    const long = 'word '.repeat(40).trim()
    expect(classifyPastedText(long)).toEqual({ kind: 'text', text: long })
    expect(classifyPastedText('line one\nline two')).toEqual({ kind: 'text', text: 'line one\nline two' })
  })

  it('leaves short text (and URLs inside sentences) as ordinary typing', () => {
    expect(classifyPastedText('read this')).toBeNull()
    expect(classifyPastedText('see https://example.com')).toBeNull()
    expect(classifyPastedText('   ')).toBeNull()
    expect(classifyPastedText('sk-short')).toBeNull()
  })
})

describe('key detection', () => {
  it('looksLikeApiKey', () => {
    expect(looksLikeApiKey(ANTHROPIC)).toBe(true)
    expect(looksLikeApiKey(`${OPENAI} extra`)).toBe(false)
    expect(looksLikeApiKey('AIzaTooShort')).toBe(false)
  })

  it('detectKeyProvider', () => {
    expect(detectKeyProvider(ANTHROPIC)).toBe('anthropic')
    expect(detectKeyProvider(` ${OPENAI}`)).toBe('openai')
    expect(detectKeyProvider(GEMINI)).toBe('gemini')
    expect(detectKeyProvider('hello')).toBeNull()
  })
})

describe('dropped items', () => {
  it('itemFromFile flags images', () => {
    expect(itemFromFile(new File(['x'], 'cat.png', { type: 'image/png' }))).toMatchObject({ kind: 'file', name: 'cat.png', isImage: true })
    expect(itemFromFile(new File(['x'], 'a.pdf', { type: 'application/pdf' }))).toMatchObject({ isImage: false })
  })

  it('itemFromDataTransfer: file, then uri-list, then plain text', () => {
    const file = new File(['x'], 'a.pdf', { type: 'application/pdf' })
    expect(itemFromDataTransfer(dataTransfer({ 'text/plain': 'ignored' }, [file]))).toMatchObject({ kind: 'file', name: 'a.pdf' })
    expect(itemFromDataTransfer(dataTransfer({ 'text/uri-list': '# comment\r\nhttps://example.com/x\r\n' }))).toEqual({
      kind: 'url',
      url: 'https://example.com/x',
    })
    expect(itemFromDataTransfer(dataTransfer({ 'text/plain': ' https://example.com ' }))).toEqual({ kind: 'url', url: 'https://example.com' })
    expect(itemFromDataTransfer(dataTransfer({ 'text/plain': 'some words' }))).toEqual({ kind: 'text', text: 'some words' })
    expect(itemFromDataTransfer(dataTransfer({}))).toBeNull()
    expect(itemFromDataTransfer(null)).toBeNull()
  })

  it('describeDropped', () => {
    expect(describeDropped({ kind: 'url', url: 'https://www.example.com/a/b' })).toBe('www.example.com')
    expect(describeDropped({ kind: 'url', url: 'not a url' })).toBe('not a url')
    expect(describeDropped({ kind: 'text', text: 'first line\nsecond' })).toBe('first line')
    const long = 'x'.repeat(80)
    expect(describeDropped({ kind: 'text', text: long })).toBe(`${'x'.repeat(57)}…`)
    expect(describeDropped(itemFromFile(new File(['x'], 'notes.txt')))).toBe('notes.txt')
  })
})
