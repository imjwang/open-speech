/**
 * The command bar's input: a small Lexical editor (09 § Command bar).
 *
 * - Plain text, one line by default; Shift+Enter adds a line, Enter hands over to the bar.
 * - Files, links and long pasted text are inline **badges** (`AttachmentNode`): drop them, paste them, pick
 *   them from the `@` file menu, or just type a link and keep going.
 * - While the microphone is live the transcript streams into the editor as ordinary text (`setDictation`),
 *   after whatever is already there, without touching the badges.
 *
 * The editor owns its content; the bar reads it with `getValue()` when it needs it and is told when it
 * changes. Everything the bar may do to it goes through `ComposerHandle`.
 */
import { caretFromPoint } from '@lexical/clipboard'
import { HistoryExtension } from '@lexical/history'
import { PlainTextExtension } from '@lexical/plain-text'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import { ContentEditable } from '@lexical/react/LexicalContentEditable'
import { LexicalExtensionComposer } from '@lexical/react/LexicalExtensionComposer'
import {
  $createParagraphNode,
  $createRangeSelection,
  $createTextNode,
  $getNearestNodeFromDOMNode,
  $getNodeByKey,
  $getRoot,
  $getSelection,
  $isElementNode,
  $isLineBreakNode,
  $isRangeSelection,
  $isTextNode,
  $setSelection,
  COMMAND_PRIORITY_HIGH,
  COMMAND_PRIORITY_LOW,
  defineExtension,
  DRAGOVER_COMMAND,
  DROP_COMMAND,
  HISTORY_MERGE_TAG,
  KEY_ARROW_DOWN_COMMAND,
  KEY_ARROW_UP_COMMAND,
  KEY_ENTER_COMMAND,
  PASTE_COMMAND,
  TextNode,
  type LexicalEditor,
  type LexicalNode,
  type NodeKey,
} from 'lexical'
import { useEffect, useImperativeHandle, useRef, type Ref } from 'react'

import type { FileSearchResult } from '@shared/ipc-channels'

import { cn } from '@/lib/utils'
import { $createAttachmentNode, $isAttachmentNode, AttachmentNode } from './AttachmentNode'
import { fromDataTransfer, fromFile, fromPastedText, sameItem, textOf, URL_IN_TEXT, type AttachmentItem } from './attachments'
import { MentionsPlugin } from './MentionsPlugin'

export interface ComposerValue {
  /** The command as text. Badges read as what they stand for ("summarize https://…"); '' when nothing was
   *  typed around them, so attachments alone mean "read these". */
  text: string
  /** Only what was typed, without the badges' stand-ins (shown beside the badges once sent). */
  words: string
  attachments: AttachmentItem[]
  /** More than one line of text: arrow keys belong to the caret, not the rows below. */
  multiline: boolean
}

export interface ComposerHandle {
  focus(): void
  clear(): void
  getValue(): ComposerValue
  setText(text: string): void
  insertAttachments(items: AttachmentItem[]): void
  /** Show the live transcript (after what is already there). `null` keeps the text and stops tracking it. */
  setDictation(text: string | null): void
  /** Remove the transcript: it was submitted as speech. */
  dropDictation(): void
}

export interface ComposerProps {
  ref?: Ref<ComposerHandle>
  placeholder: string
  label: string
  /** Plain Enter with the file menu closed. The editor never inserts a line for it. */
  onEnter(event: KeyboardEvent): void
  /** Content changed by the user (typing, badges), not by dictation. */
  onChange(value: ComposerValue): void
  searchFiles(query: string): Promise<FileSearchResult>
  pathForFile(file: File): string
  className?: string
}

const DICTATION_TAG = 'dictation'
const EMPTY: ComposerValue = { text: '', words: '', attachments: [], multiline: false }

function $readValue(): ComposerValue {
  const attachments: AttachmentItem[] = []
  const lines: string[] = []
  let typed = ''
  for (const block of $getRoot().getChildren()) {
    let line = ''
    const children: LexicalNode[] = $isElementNode(block) ? block.getChildren() : [block]
    for (const node of children) {
      if ($isAttachmentNode(node)) {
        attachments.push(node.getItem())
        line += textOf(node.getItem())
      } else if ($isLineBreakNode(node)) {
        lines.push(line)
        line = ''
      } else {
        const text = node.getTextContent()
        typed += text
        line += text
      }
    }
    lines.push(line)
  }
  const words = typed.replace(/\s+/g, ' ').trim()
  const text = words ? lines.join('\n').replace(/[ \t]+/g, ' ').trim() : ''
  return { text, words, attachments, multiline: lines.length > 1 }
}

function $lastParagraph() {
  const root = $getRoot()
  const last = root.getLastChild()
  if ($isElementNode(last)) return last
  const paragraph = $createParagraphNode()
  root.append(paragraph)
  return paragraph
}

/** Insert badges at the caret (or the end), each followed by a space so typing can continue. */
function $insertAttachments(items: AttachmentItem[]): void {
  const present = $readValue().attachments
  const fresh = items.filter((item, i) => !present.some((p) => sameItem(p, item)) && items.findIndex((x) => sameItem(x, item)) === i)
  if (!fresh.length) return
  let selection = $getSelection()
  if (!$isRangeSelection(selection)) {
    $lastParagraph().selectEnd()
    selection = $getSelection()
  }
  if (!$isRangeSelection(selection)) return
  const nodes: LexicalNode[] = []
  for (const item of fresh) nodes.push($createAttachmentNode(item), $createTextNode(' '))
  selection.insertNodes(nodes)
}

/** A typed link becomes a badge as soon as the user moves on (types a space after it). */
function $linkify(node: TextNode): void {
  if (!node.isSimpleText()) return
  const text = node.getTextContent()
  URL_IN_TEXT.lastIndex = 0
  const match = URL_IN_TEXT.exec(text)
  if (!match) return
  const start = match.index
  const end = start + match[0].length
  if (end >= text.length || !/\s/.test(text[end] ?? '')) return // still being typed
  const parts = start === 0 ? node.splitText(end) : node.splitText(start, end)
  const link = start === 0 ? parts[0] : parts[1]
  link?.replace($createAttachmentNode({ kind: 'url', url: match[0] }))
}

function ControlPlugin({ handle, onEnter, onChange, pathForFile, menuOpen }: {
  handle: Ref<ComposerHandle> | undefined
  onEnter: ComposerProps['onEnter']
  onChange: ComposerProps['onChange']
  pathForFile: ComposerProps['pathForFile']
  menuOpen: { current: boolean }
}): null {
  const [editor] = useLexicalComposerContext()
  const dictation = useRef<NodeKey | null>(null)
  const callbacks = useRef({ onEnter, onChange, pathForFile })
  callbacks.current = { onEnter, onChange, pathForFile }

  useImperativeHandle(
    handle,
    (): ComposerHandle => ({
      focus: () => editor.focus(),
      clear: () => {
        dictation.current = null
        editor.update(
          () => {
            const root = $getRoot()
            root.clear()
            const paragraph = $createParagraphNode()
            root.append(paragraph)
            paragraph.select()
          },
          { tag: HISTORY_MERGE_TAG },
        )
      },
      getValue: () => editor.getEditorState().read($readValue),
      setText: (text) => {
        dictation.current = null
        editor.update(() => {
          const root = $getRoot()
          root.clear()
          const paragraph = $createParagraphNode()
          if (text) paragraph.append($createTextNode(text))
          root.append(paragraph)
          paragraph.selectEnd()
        })
      },
      insertAttachments: (items) => editor.update(() => $insertAttachments(items)),
      setDictation: (text) => {
        if (text === null) {
          dictation.current = null
          return
        }
        editor.update(
          () => {
            const existing = dictation.current ? $getNodeByKey(dictation.current) : null
            if (!text) {
              existing?.remove()
              dictation.current = null
              return
            }
            if ($isTextNode(existing)) {
              const lead = /^\s/.test(existing.getTextContent()) ? ' ' : ''
              existing.setTextContent(lead + text)
              existing.selectEnd()
              return
            }
            const paragraph = $lastParagraph()
            const last = paragraph.getLastChild()
            const needsSpace = last !== null && !/\s$/.test(last.getTextContent()) && !$isLineBreakNode(last)
            // Its own node (never merged into typed text), so later partials replace exactly the transcript.
            const node = $createTextNode((needsSpace ? ' ' : '') + text).toggleUnmergeable()
            paragraph.append(node)
            node.selectEnd()
            dictation.current = node.getKey()
          },
          { tag: [HISTORY_MERGE_TAG, DICTATION_TAG] },
        )
      },
      dropDictation: () => {
        const key = dictation.current
        dictation.current = null
        if (key) editor.update(() => $getNodeByKey(key)?.remove(), { tag: [HISTORY_MERGE_TAG, DICTATION_TAG] })
      },
    }),
    [editor],
  )

  useEffect(() => {
    const insert = (items: AttachmentItem[]) => $insertAttachments(items)
    const unregister = [
      editor.registerUpdateListener(({ dirtyElements, dirtyLeaves, tags, editorState }) => {
        if (tags.has(DICTATION_TAG) || (dirtyElements.size === 0 && dirtyLeaves.size === 0)) return
        callbacks.current.onChange(editorState.read($readValue))
      }),
      editor.registerNodeTransform(TextNode, $linkify),
      editor.registerCommand(
        KEY_ENTER_COMMAND,
        (event) => {
          if (!event || event.isComposing || menuOpen.current) return false
          // Shift+Enter adds a line once there is something to add it to; in an empty composer it is the
          // bar's (the selected row's third action), like plain Enter.
          const value = editor.getEditorState().read($readValue)
          if (event.shiftKey && (value.text || value.attachments.length)) return false
          event.preventDefault()
          callbacks.current.onEnter(event)
          return true
        },
        COMMAND_PRIORITY_LOW,
      ),
      // With several lines of text, Up and Down move the caret; otherwise they are the bar's (the rows).
      ...[KEY_ARROW_UP_COMMAND, KEY_ARROW_DOWN_COMMAND].map((command) =>
        editor.registerCommand(
          command,
          (event) => {
            if (!menuOpen.current && editor.getEditorState().read($readValue).multiline) event.stopPropagation()
            return false
          },
          COMMAND_PRIORITY_LOW,
        ),
      ),
      editor.registerCommand(
        PASTE_COMMAND,
        (event) => {
          const data = 'clipboardData' in event ? event.clipboardData : null
          if (!data) return false
          const files = Array.from(data.files ?? [])
          const items = files.length
            ? files.map((f) => fromFile(f, callbacks.current.pathForFile))
            : [fromPastedText(data.getData('text/plain'))].filter((x): x is AttachmentItem => x !== null)
          if (!items.length) return false // ordinary text: the editor pastes it
          event.preventDefault()
          insert(items)
          return true
        },
        COMMAND_PRIORITY_HIGH,
      ),
      editor.registerCommand(
        DRAGOVER_COMMAND,
        (event) => {
          event.preventDefault() // required for a drop to be allowed
          return true
        },
        COMMAND_PRIORITY_HIGH,
      ),
      editor.registerCommand(
        DROP_COMMAND,
        (event) => {
          const items = fromDataTransfer(event.dataTransfer, callbacks.current.pathForFile)
          if (!items.length) return false
          event.preventDefault()
          event.stopPropagation() // the bar's own drop handler must not add them a second time
          $selectAtPoint(event.clientX, event.clientY)
          const only = items.length === 1 ? items[0] : undefined
          if (only?.kind === 'text' && fromPastedText(only.text) === null) $getSelection()?.insertText(only.text)
          else insert(items)
          return true
        },
        COMMAND_PRIORITY_HIGH,
      ),
    ]
    return () => unregister.forEach((off) => off())
  }, [editor, menuOpen])

  return null
}

/** Put the caret where something was dropped. */
function $selectAtPoint(x: number, y: number): void {
  const point = caretFromPoint(x, y)
  const node = point ? $getNearestNodeFromDOMNode(point.node) : null
  if (!point || !node) return
  const selection = $createRangeSelection()
  if ($isTextNode(node)) {
    selection.anchor.set(node.getKey(), point.offset, 'text')
    selection.focus.set(node.getKey(), point.offset, 'text')
  } else {
    const parent = node.getParent()
    if (!parent) return
    const offset = node.getIndexWithinParent() + 1
    selection.anchor.set(parent.getKey(), offset, 'element')
    selection.focus.set(parent.getKey(), offset, 'element')
  }
  $setSelection(selection)
}

function onError(error: Error, _editor: LexicalEditor): void {
  // An editor error must never take the bar down: log it and let the boundary recover.
  console.error('[composer]', error)
}

/** Module scope: the editor is created once per mount from a stable extension (it is re-created if this changes). */
const composerExtension = defineExtension({
  name: '@open-speech/composer',
  namespace: 'open-speech-composer',
  nodes: [AttachmentNode],
  dependencies: [PlainTextExtension, HistoryExtension],
  onError,
})

export function Composer({ ref, placeholder, label, onEnter, onChange, searchFiles, pathForFile, className }: ComposerProps) {
  const menuOpen = useRef(false)
  return (
    // `contentEditable={null}`: we render it ourselves below, so a changing placeholder can't re-create the editor.
    <LexicalExtensionComposer extension={composerExtension} contentEditable={null}>
      <div className={cn('relative min-w-0 flex-1', className)} data-slot="composer">
        <ContentEditable
          aria-label={label}
          aria-placeholder={placeholder}
          aria-multiline="false"
          spellCheck={false}
          data-testid="composer"
          className="max-h-40 min-h-7 overflow-y-auto py-2.5 text-lg leading-7 break-words whitespace-pre-wrap outline-hidden"
          placeholder={
            <div
              aria-hidden="true"
              className="pointer-events-none absolute top-2.5 left-0 max-w-full truncate text-lg leading-7 text-muted-foreground select-none"
            >
              {placeholder}
            </div>
          }
        />
        <ControlPlugin handle={ref} onEnter={onEnter} onChange={onChange} pathForFile={pathForFile} menuOpen={menuOpen} />
        <MentionsPlugin searchFiles={searchFiles} openRef={menuOpen} />
      </div>
    </LexicalExtensionComposer>
  )
}

export { EMPTY as EMPTY_VALUE }
