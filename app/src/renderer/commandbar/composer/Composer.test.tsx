import { act, render, screen } from '@testing-library/react'
import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { FileSearchResult } from '@shared/ipc-channels'

import { Composer, type ComposerHandle, type ComposerValue } from './Composer'
import type { AttachmentItem } from './attachments'

const report: AttachmentItem = { kind: 'file', path: '/a/report.pdf', name: 'report.pdf', fileKind: 'pdf' }
const notes: AttachmentItem = { kind: 'file', path: '/a/notes.md', name: 'notes.md', fileKind: 'text' }
const link: AttachmentItem = { kind: 'url', url: 'https://example.org/post' }

function mount(search: (q: string) => Promise<FileSearchResult> = async () => ({ folder: null, hits: [] })) {
  const ref = createRef<ComposerHandle>()
  const changes: ComposerValue[] = []
  const onEnter = vi.fn()
  render(
    <Composer
      ref={ref}
      label="Command"
      placeholder="Say or type"
      onEnter={onEnter}
      onChange={(v) => changes.push(v)}
      searchFiles={search}
      pathForFile={() => ''}
    />,
  )
  const handle = () => {
    if (!ref.current) throw new Error('composer not mounted')
    return ref.current
  }
  return { handle, changes, onEnter, editor: () => screen.getByTestId('composer') }
}

/** Lexical commits updates asynchronously; `act` flushes them and React's re-render. */
const settle = () => act(async () => void (await Promise.resolve()))

describe('Composer', () => {
  it('starts empty, with its placeholder and accessible name', () => {
    const c = mount()
    expect(c.editor()).toHaveAttribute('aria-label', 'Command')
    expect(c.editor()).toHaveAttribute('contenteditable', 'true')
    expect(screen.getByText('Say or type')).toBeInTheDocument()
    expect(c.handle().getValue()).toEqual({ text: '', words: '', attachments: [], multiline: false })
  })

  it('badges alone are attachments with no command text: "read these"', async () => {
    const c = mount()
    await act(async () => c.handle().insertAttachments([report, link]))
    await settle()
    const value = c.handle().getValue()
    expect(value.attachments).toEqual([report, link])
    expect(value.text).toBe('')
    expect(screen.getAllByTestId('attachment').map((el) => el.textContent)).toEqual(['report.pdf', 'example.org/post'])
    expect(c.changes.at(-1)?.attachments).toHaveLength(2)
  })

  it('typed words around badges make the command; badges read as what they stand for', async () => {
    const c = mount()
    await act(async () => c.handle().setText('compare '))
    await act(async () => c.handle().insertAttachments([report]))
    await settle()
    const value = c.handle().getValue()
    expect(value.words).toBe('compare')
    expect(value.text).toBe('compare report.pdf')
    expect(value.attachments).toEqual([report])
  })

  it('does not attach the same thing twice', async () => {
    const c = mount()
    await act(async () => c.handle().insertAttachments([report, notes]))
    await act(async () => c.handle().insertAttachments([report]))
    await settle()
    expect(c.handle().getValue().attachments).toEqual([report, notes])
  })

  it('a typed link becomes a badge once the user moves on', async () => {
    const c = mount()
    await act(async () => c.handle().setText('read https://example.org/post'))
    await settle()
    expect(c.handle().getValue().attachments).toEqual([]) // still being typed
    await act(async () => c.handle().setText('read https://example.org/post and stop'))
    await settle()
    const value = c.handle().getValue()
    expect(value.attachments).toEqual([link])
    expect(value.words).toBe('read and stop')
    expect(value.text).toBe('read https://example.org/post and stop')
  })

  it('a live transcript streams in after what is there, without touching badges or reporting a change', async () => {
    const c = mount()
    await act(async () => c.handle().insertAttachments([report]))
    await settle()
    const reported = c.changes.length
    await act(async () => c.handle().setDictation('read'))
    await act(async () => c.handle().setDictation('read these'))
    await settle()
    expect(c.handle().getValue()).toMatchObject({ words: 'read these', attachments: [report] })
    expect(c.changes).toHaveLength(reported) // dictation is not the user typing

    // Dropping it (the utterance was sent) leaves the badges; keeping it leaves the words as ordinary text.
    await act(async () => c.handle().dropDictation())
    await settle()
    expect(c.handle().getValue()).toMatchObject({ words: '', attachments: [report] })
    await act(async () => c.handle().setDictation('summarize it'))
    await act(async () => c.handle().setDictation(null))
    await act(async () => c.handle().setDictation('again'))
    await settle()
    expect(c.handle().getValue().words).toBe('summarize it again')
  })

  it('clear empties text and badges', async () => {
    const c = mount()
    await act(async () => c.handle().setText('hello'))
    await act(async () => c.handle().insertAttachments([report]))
    await act(async () => c.handle().clear())
    await settle()
    expect(c.handle().getValue()).toEqual({ text: '', words: '', attachments: [], multiline: false })
  })
})
