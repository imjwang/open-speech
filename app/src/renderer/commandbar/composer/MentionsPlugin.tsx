/**
 * The `@` file menu: type `@` to find something to read. A name searches the disk; a path (`@~/Doc…`) walks
 * folders. Up/Down move, Enter or Tab picks: a file becomes a badge, a folder is entered. Escape closes it.
 *
 * The menu is rendered in the flow of the page (not floating), because the bar is a small frameless window
 * that grows with its content: a floating menu would be clipped by the window's edge.
 */
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import {
  $createTextNode,
  $getNodeByKey,
  $getSelection,
  $isRangeSelection,
  $isTextNode,
  COMMAND_PRIORITY_HIGH,
  KEY_ARROW_DOWN_COMMAND,
  KEY_ARROW_UP_COMMAND,
  KEY_ENTER_COMMAND,
  KEY_ESCAPE_COMMAND,
  KEY_TAB_COMMAND,
  type NodeKey,
} from 'lexical'
import { AudioLines, CornerDownLeft, FileText, FileType2, Folder, FolderOpen, Globe, Image as ImageIcon, Loader2 } from 'lucide-react'
import { useEffect, useRef, useState, type JSX } from 'react'

import type { FileHit, FileKind, FileSearchResult } from '@shared/ipc-channels'

import { useT } from '@/i18n'
import { cn } from '@/lib/utils'
import { $createAttachmentNode } from './AttachmentNode'
import { fromHit } from './attachments'

/** `@query` right before the caret: at the start or after a space, running to the caret with no spaces. */
const TRIGGER = /(?:^|\s)@([^\s@]*)$/
const DEBOUNCE_MS = 70

interface Match {
  nodeKey: NodeKey
  /** Offsets of `@query` inside the text node. */
  start: number
  end: number
  query: string
}

function $findMatch(): Match | null {
  const selection = $getSelection()
  if (!$isRangeSelection(selection) || !selection.isCollapsed()) return null
  const node = selection.anchor.getNode()
  if (!$isTextNode(node) || !node.isSimpleText()) return null
  const offset = selection.anchor.offset
  const found = TRIGGER.exec(node.getTextContent().slice(0, offset))
  if (!found) return null
  const query = found[1] ?? ''
  return { nodeKey: node.getKey(), start: offset - query.length - 1, end: offset, query }
}

function KindIcon({ kind }: { kind: FileKind }): JSX.Element {
  switch (kind) {
    case 'folder':
      return <Folder aria-hidden="true" className="text-tint-folder-foreground" />
    case 'image':
      return <ImageIcon aria-hidden="true" className="text-tint-image-foreground" />
    case 'web':
      return <Globe aria-hidden="true" className="text-tint-web-foreground" />
    case 'doc':
      return <FileType2 aria-hidden="true" className="text-tint-file-foreground" />
    case 'audio':
      return <AudioLines aria-hidden="true" className="text-tint-file-foreground" />
    default:
      return <FileText aria-hidden="true" className="text-tint-file-foreground" />
  }
}

export function MentionsPlugin({ searchFiles, openRef }: {
  searchFiles(query: string): Promise<FileSearchResult>
  /** Kept true while the menu is open, so the editor's own Enter/arrow handling stands aside. */
  openRef: { current: boolean }
}): JSX.Element | null {
  const t = useT()
  const [editor] = useLexicalComposerContext()
  const [match, setMatch] = useState<Match | null>(null)
  const [result, setResult] = useState<FileSearchResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [active, setActive] = useState(0)
  /** The query the user closed the menu on (Escape): don't reopen until it changes. */
  const dismissed = useRef<string | null>(null)
  const live = useRef({ match, result, active })
  live.current = { match, result, active }

  const open = match !== null
  openRef.current = open
  const hits = result?.hits ?? []
  const query = match?.query ?? null

  useEffect(
    () =>
      editor.registerUpdateListener(({ editorState, tags }) => {
        if (tags.has('dictation')) return // a transcript is streaming in: not the user typing `@`
        const next = editorState.read($findMatch)
        if (next && dismissed.current === next.query) return setMatch(null)
        if (!next) dismissed.current = null
        setMatch((prev) =>
          prev && next && prev.nodeKey === next.nodeKey && prev.start === next.start && prev.query === next.query ? prev : next,
        )
      }),
    [editor],
  )

  useEffect(() => {
    if (query === null) {
      setResult(null)
      setLoading(false)
      return
    }
    let stale = false
    setLoading(true)
    const timer = setTimeout(() => {
      searchFiles(query).then(
        (found) => {
          if (stale) return
          setResult(found)
          setActive(0)
          setLoading(false)
        },
        () => {
          if (stale) return
          setResult({ folder: null, hits: [] })
          setLoading(false)
        },
      )
    }, DEBOUNCE_MS)
    return () => {
      stale = true
      clearTimeout(timer)
    }
  }, [query, searchFiles])

  const choose = (hit: FileHit) => {
    const current = live.current.match
    if (!current) return
    editor.update(() => {
      const node = $getNodeByKey(current.nodeKey)
      if (!$isTextNode(node)) return
      const pieces = current.start === 0 ? node.splitText(current.end) : node.splitText(current.start, current.end)
      const target = current.start === 0 ? pieces[0] : pieces[1]
      if (!target) return
      if (hit.kind === 'folder') {
        // Walk into it: the query becomes the folder's path and the menu lists what is inside.
        const text = `@${hit.short.replace(/\/+$/, '')}/`
        target.setTextContent(text)
        target.select(text.length, text.length)
        return
      }
      const badge = $createAttachmentNode(fromHit(hit))
      target.replace(badge)
      const space = $createTextNode(' ')
      badge.insertAfter(space)
      space.select()
    })
  }

  useEffect(() => {
    if (!open) return
    const consume = (event: KeyboardEvent | null) => {
      event?.preventDefault()
      event?.stopPropagation() // these keys are the menu's while it is open, not the bar's
    }
    const move = (delta: number) => (event: KeyboardEvent) => {
      const n = live.current.result?.hits.length ?? 0
      consume(event)
      if (n) setActive((i) => (i + delta + n) % n)
      return true
    }
    const pick = (event: KeyboardEvent | null) => {
      const { result: r, active: i } = live.current
      consume(event)
      const hit = r?.hits[i]
      if (hit) choose(hit)
      else if (r) {
        // Nothing matches: close, and leave what was typed as it is.
        dismissed.current = live.current.match?.query ?? null
        setMatch(null)
      }
      return true
    }
    const unregister = [
      editor.registerCommand(KEY_ARROW_DOWN_COMMAND, move(1), COMMAND_PRIORITY_HIGH),
      editor.registerCommand(KEY_ARROW_UP_COMMAND, move(-1), COMMAND_PRIORITY_HIGH),
      editor.registerCommand(KEY_ENTER_COMMAND, pick, COMMAND_PRIORITY_HIGH),
      editor.registerCommand(KEY_TAB_COMMAND, pick, COMMAND_PRIORITY_HIGH),
      editor.registerCommand(
        KEY_ESCAPE_COMMAND,
        (event) => {
          consume(event)
          dismissed.current = live.current.match?.query ?? null
          setMatch(null)
          return true
        },
        COMMAND_PRIORITY_HIGH,
      ),
    ]
    return () => unregister.forEach((off) => off())
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `choose` reads live refs
  }, [editor, open])

  // Tell assistive technology about the list the editor now controls.
  useEffect(() => {
    const root = editor.getRootElement()
    if (!root) return
    root.setAttribute('role', 'combobox')
    root.setAttribute('aria-autocomplete', 'list')
    root.setAttribute('aria-expanded', String(open))
    if (open) root.setAttribute('aria-controls', 'file-menu')
    else root.removeAttribute('aria-controls')
    const id = open && hits[active] ? `file-menu-${active}` : null
    if (id) root.setAttribute('aria-activedescendant', id)
    else root.removeAttribute('aria-activedescendant')
  }, [editor, open, hits, active])

  if (!open) return null
  return (
    <div
      id="file-menu"
      role="listbox"
      aria-label={t('mention.title')}
      data-testid="file-menu"
      className="-ml-1 mb-2 max-h-64 overflow-y-auto rounded-lg border bg-popover p-1 text-popover-foreground shadow-sm"
    >
      {result?.folder && (
        <div className="flex items-center gap-1.5 px-2 py-1 text-xs text-muted-foreground" aria-hidden="true">
          <FolderOpen className="size-3.5" />
          <span className="truncate font-mono">{result.folder}</span>
        </div>
      )}
      {hits.map((hit, i) => (
        <div
          key={hit.path}
          id={`file-menu-${i}`}
          role="option"
          aria-selected={i === active}
          data-active={i === active}
          className={cn(
            'flex cursor-default items-center gap-2 rounded-md px-2 py-1.5 text-sm select-none [&_svg]:size-4 [&_svg]:shrink-0',
            i === active && 'bg-accent text-accent-foreground',
          )}
          onMouseDown={(e) => e.preventDefault()} // keep the caret in the editor
          onMouseEnter={() => setActive(i)}
          onClick={() => choose(hit)}
        >
          <KindIcon kind={hit.kind} />
          <span className="truncate">{hit.name}</span>
          {!result?.folder && <span className="ml-auto max-w-[45%] truncate pl-3 text-xs text-muted-foreground">{hit.where}</span>}
          {i === active && (
            <CornerDownLeft
              aria-hidden="true"
              className={cn('size-3.5! text-muted-foreground', result?.folder ? 'ml-auto' : 'ml-1')}
            />
          )}
        </div>
      ))}
      {hits.length === 0 && (
        <div role="status" className="flex items-center gap-2 px-2 py-1.5 text-sm text-muted-foreground">
          {loading || !result ? (
            <>
              <Loader2 aria-hidden="true" className="size-4 animate-spin motion-reduce:animate-none" />
              {t('mention.searching')}
            </>
          ) : (
            t('mention.empty')
          )}
        </div>
      )}
    </div>
  )
}
