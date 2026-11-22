import { describe, expect, it } from 'vitest'

import {
  badgeKind,
  fromDataTransfer,
  fromFile,
  fromHit,
  fromPastedText,
  kindOfName,
  labelOf,
  sameItem,
  textOf,
  toWire,
  type AttachmentItem,
} from './attachments'

const file = (name: string, type = ''): File => new File(['x'], name, { type })

function transfer(data: Record<string, string>, files: File[] = []): DataTransfer {
  return { files, getData: (type: string) => data[type] ?? '' } as unknown as DataTransfer
}

describe('what a file is', () => {
  it('goes by extension, and by MIME type for images without one', () => {
    expect(kindOfName('Paper.PDF')).toBe('pdf')
    expect(kindOfName('notes.md')).toBe('text')
    expect(kindOfName('letter.docx')).toBe('doc')
    expect(kindOfName('page.html')).toBe('web')
    expect(kindOfName('shot.png')).toBe('image')
    expect(kindOfName('clipboard', 'image/png')).toBe('image')
    expect(kindOfName('archive.zip')).toBe('other')
  })
})

describe('files: by path when the host has one, else uploaded', () => {
  it('a file dragged from Finder keeps its path', () => {
    expect(fromFile(file('report.pdf'), () => '/Users/me/report.pdf')).toEqual({
      kind: 'file',
      path: '/Users/me/report.pdf',
      name: 'report.pdf',
      fileKind: 'pdf',
    })
  })

  it('a pasted screenshot (no path) becomes an upload', () => {
    const shot = file('image.png', 'image/png')
    expect(fromFile(shot, () => '')).toEqual({ kind: 'upload', file: shot, name: 'image.png', fileKind: 'image' })
  })

  it('a hit from the @ menu is a file by path', () => {
    const hit = { path: '/Users/me/a.md', name: 'a.md', kind: 'text' as const, where: '~', short: '~/a.md' }
    expect(fromHit(hit)).toEqual({ kind: 'file', path: '/Users/me/a.md', name: 'a.md', fileKind: 'text' })
  })
})

describe('pasted text', () => {
  it('a link becomes a link badge', () => {
    expect(fromPastedText(' https://example.org/a?b=1 ')).toEqual({ kind: 'url', url: 'https://example.org/a?b=1' })
  })

  it('long or multi-line text becomes a text badge; short text is just typing', () => {
    expect(fromPastedText('read this')).toBeNull()
    expect(fromPastedText('one\ntwo')).toBeNull()
    expect(fromPastedText('one\ntwo\nthree')).toMatchObject({ kind: 'text' })
    expect(fromPastedText('x'.repeat(280))).toMatchObject({ kind: 'text' })
    expect(fromPastedText('   ')).toBeNull()
  })
})

describe('dropped things', () => {
  it('several files become several badges, in order', () => {
    const items = fromDataTransfer(transfer({}, [file('a.pdf'), file('b.md')]), (f) => `/tmp/${f.name}`)
    expect(items.map((i) => (i.kind === 'file' ? i.path : ''))).toEqual(['/tmp/a.pdf', '/tmp/b.md'])
  })

  it('links from a uri-list (comments skipped), else a link or text from plain text', () => {
    const uris = transfer({ 'text/uri-list': '# from a browser\nhttps://a.example/x\nhttps://b.example/y' })
    expect(fromDataTransfer(uris, () => '')).toEqual([
      { kind: 'url', url: 'https://a.example/x' },
      { kind: 'url', url: 'https://b.example/y' },
    ])
    expect(fromDataTransfer(transfer({ 'text/plain': 'https://c.example' }), () => '')).toEqual([{ kind: 'url', url: 'https://c.example' }])
    expect(fromDataTransfer(transfer({ 'text/plain': 'some words' }), () => '')).toEqual([{ kind: 'text', text: 'some words' }])
    expect(fromDataTransfer(transfer({}), () => '')).toEqual([])
    expect(fromDataTransfer(null, () => '')).toEqual([])
  })
})

describe('how a badge reads', () => {
  const pdf: AttachmentItem = { kind: 'file', path: '/a/report.pdf', name: 'report.pdf', fileKind: 'pdf' }
  const link: AttachmentItem = { kind: 'url', url: 'https://www.example.org/posts/hello-world/' }
  const text: AttachmentItem = { kind: 'text', text: 'A long paragraph that was pasted into the bar\nand more' }

  it('labels: file name, host/last-segment, first words', () => {
    expect(labelOf(pdf)).toBe('report.pdf')
    expect(labelOf(link)).toBe('example.org/hello-world')
    expect(labelOf({ kind: 'url', url: 'https://example.org' })).toBe('example.org')
    expect(labelOf(text)).toBe('A long paragraph that was p…')
  })

  it('colour family', () => {
    expect(badgeKind(pdf)).toBe('file')
    expect(badgeKind({ ...pdf, fileKind: 'image' })).toBe('image')
    expect(badgeKind(link)).toBe('url')
    expect(badgeKind(text)).toBe('text')
  })

  it('in the command text: a file is its name, a link its address, pasted text nothing', () => {
    expect(textOf(pdf)).toBe('report.pdf')
    expect(textOf(link)).toBe(link.kind === 'url' ? link.url : '')
    expect(textOf(text)).toBe('')
  })

  it('the same thing attached twice is recognised', () => {
    expect(sameItem(pdf, { ...pdf })).toBe(true)
    expect(sameItem(pdf, link)).toBe(false)
    expect(sameItem(link, { kind: 'url', url: 'https://other.example' })).toBe(false)
  })
})

describe('toWire', () => {
  it('files go by path, links by address, text as text, uploads as the documents they became', () => {
    const upload: AttachmentItem = { kind: 'upload', file: file('shot.png', 'image/png'), name: 'shot.png', fileKind: 'image' }
    const items: AttachmentItem[] = [
      { kind: 'file', path: '/a/report.pdf', name: 'report.pdf', fileKind: 'pdf' },
      { kind: 'url', url: 'https://example.org' },
      { kind: 'text', text: 'hello' },
      upload,
    ]
    expect(toWire(items, new Map([[upload, 'doc_1']]))).toEqual([
      { kind: 'file', path: '/a/report.pdf', name: 'report.pdf' },
      { kind: 'url', url: 'https://example.org' },
      { kind: 'text', text: 'hello' },
      { kind: 'document', document_id: 'doc_1', name: 'shot.png' },
    ])
  })
})
