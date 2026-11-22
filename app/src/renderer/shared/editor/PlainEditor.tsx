/**
 * A plain-text Lexical editor for writing prose that is stored as a text file (rule files, preferences, the
 * pronunciation list). The parent owns which document is open (`docKey`) and gets every change; the editor
 * keeps its own undo history per document.
 */
import { HistoryExtension } from '@lexical/history'
import { PlainTextExtension } from '@lexical/plain-text'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import { ContentEditable } from '@lexical/react/LexicalContentEditable'
import { LexicalExtensionComposer } from '@lexical/react/LexicalExtensionComposer'
import {
  $createLineBreakNode,
  $createParagraphNode,
  $createTextNode,
  $getRoot,
  $getSelection,
  $isRangeSelection,
  CLEAR_HISTORY_COMMAND,
  defineExtension,
  HISTORY_MERGE_TAG,
  type LexicalEditor,
} from 'lexical'
import { useEffect, useImperativeHandle, useRef, type Ref } from 'react'

import { cn } from '@/lib/utils'

export interface PlainEditorHandle {
  focus(): void
  /** Insert text at the caret (dictation). */
  insert(text: string): void
  getText(): string
}

const LOAD_TAG = 'load'

function $setText(text: string): void {
  const root = $getRoot()
  root.clear()
  const paragraph = $createParagraphNode()
  const lines = text.replace(/\r\n?/g, '\n').split('\n')
  lines.forEach((line, i) => {
    if (i > 0) paragraph.append($createLineBreakNode())
    if (line) paragraph.append($createTextNode(line))
  })
  root.append(paragraph)
}

function Control({ handle, docKey, value, onChange }: {
  handle: Ref<PlainEditorHandle> | undefined
  docKey: string
  value: string
  onChange(text: string): void
}): null {
  const [editor] = useLexicalComposerContext()
  const latest = useRef({ value, onChange })
  latest.current = { value, onChange }

  useImperativeHandle(
    handle,
    () => ({
      focus: () => editor.focus(),
      insert: (text) =>
        editor.update(() => {
          let selection = $getSelection()
          if (!$isRangeSelection(selection)) {
            $getRoot().selectEnd()
            selection = $getSelection()
          }
          if ($isRangeSelection(selection)) selection.insertText(text)
        }),
      getText: () => editor.getEditorState().read(() => $getRoot().getTextContent()),
    }),
    [editor],
  )

  // A different document: replace the content and start its undo history afresh.
  useEffect(() => {
    editor.update(() => $setText(latest.current.value), { tag: [HISTORY_MERGE_TAG, LOAD_TAG], discrete: true })
    editor.dispatchCommand(CLEAR_HISTORY_COMMAND, undefined)
  }, [editor, docKey])

  useEffect(
    () =>
      editor.registerUpdateListener(({ dirtyElements, dirtyLeaves, tags, editorState }) => {
        if (tags.has(LOAD_TAG) || (dirtyElements.size === 0 && dirtyLeaves.size === 0)) return
        latest.current.onChange(editorState.read(() => $getRoot().getTextContent()))
      }),
    [editor],
  )
  return null
}

function onError(error: Error, _editor: LexicalEditor): void {
  console.error('[editor]', error)
}

const editorExtension = defineExtension({
  name: '@open-speech/plain-editor',
  namespace: 'open-speech-editor',
  dependencies: [PlainTextExtension, HistoryExtension],
  onError,
})

export function PlainEditor({ ref, docKey, value, onChange, placeholder, label, mono, className }: {
  ref?: Ref<PlainEditorHandle>
  /** Identifies the open document: when it changes, `value` is loaded and undo history is reset. */
  docKey: string
  value: string
  onChange(text: string): void
  placeholder: string
  label: string
  /** Fixed-width text, for files where columns and symbols matter (the raw file, the pronunciation list). */
  mono?: boolean
  className?: string
}) {
  return (
    <LexicalExtensionComposer extension={editorExtension} contentEditable={null}>
      <div className={cn('relative h-full', className)}>
        <ContentEditable
          aria-label={label}
          aria-placeholder={placeholder}
          spellCheck={!mono}
          data-testid="plain-editor"
          className={cn(
            'h-full overflow-y-auto px-6 py-5 leading-7 break-words whitespace-pre-wrap outline-hidden',
            mono ? 'font-mono text-[13px] leading-6' : 'text-[15px]',
          )}
          placeholder={
            <div
              aria-hidden="true"
              className={cn(
                'pointer-events-none absolute top-5 left-6 right-6 whitespace-pre-wrap text-muted-foreground select-none',
                mono ? 'font-mono text-[13px] leading-6' : 'text-[15px] leading-7',
              )}
            >
              {placeholder}
            </div>
          }
        />
        <Control handle={ref} docKey={docKey} value={value} onChange={onChange} />
      </div>
    </LexicalExtensionComposer>
  )
}
