/**
 * The rules editor (06): every rule is a plain text file, and this window is a text editor over them.
 *
 * Left: the files (standing preferences, pronunciations, the user's rules, the built-in presets), each with
 * its on/off switch. Right: the text of the one that is open. Typing saves by itself; for a rule, saving
 * what it *says* makes the engine work out again how to apply it, so the two never drift apart. `{ }` shows
 * the whole file, header included, for anyone who wants it. The microphone dictates into the text.
 */
import {
  BookA,
  Braces,
  Check,
  CircleAlert,
  FolderOpen,
  Globe,
  Loader2,
  Mic,
  NotebookPen,
  Plus,
  Sparkles,
  Trash2,
  Zap,
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { EngineApiError, unwrap, type RuleFile, type RuleFileContent, type RuleFileWrite } from '@shared/api/client'

import type { Bridge } from '@/bridge'
import { IconButton } from '@/components/ui/icon-button'
import { Switch } from '@/components/ui/switch'
import { PlainEditor, type PlainEditorHandle } from '@/editor/PlainEditor'
import type { LiveEngine } from '@/engine'
import { useEngineStore, useServerEvent } from '@/engine'
import { useT, useTMaybe } from '@/i18n'
import { cn } from '@/lib/utils'
import { PRESET_ICONS } from '../commandbar/components/icons'
import { useDictation } from './dictation'

const SAVE_AFTER_MS = 700
const NEW = '+new'

type Status = { kind: 'saved' } | { kind: 'dirty' } | { kind: 'saving' } | { kind: 'error'; message: string }

/** Preset file names ("page-numbers") → the preset's icon. */
const PRESET_BY_FILE: Record<string, LucideIcon | undefined> = Object.fromEntries(
  Object.entries(PRESET_ICONS).map(([title, icon]) => [title.toLowerCase().replace(/\s+/g, '-'), icon]),
)

function fileIcon(file: Pick<RuleFile, 'kind' | 'name' | 'tier'>): LucideIcon {
  if (file.kind === 'preferences') return NotebookPen
  if (file.kind === 'lexicon') return BookA
  if (file.kind === 'preset') return PRESET_BY_FILE[file.name] ?? Zap
  return file.tier === 'filter' ? Zap : Sparkles
}

/** The name shown for a file: the built-in kinds in the interface language, a rule by its own name. */
function pretty(file: Pick<RuleFile, 'kind' | 'name'>, tMaybe: ReturnType<typeof useTMaybe>): string {
  if (file.kind === 'preferences' || file.kind === 'lexicon') return file.name
  const words = file.name.replace(/-/g, ' ')
  const name = words.charAt(0).toUpperCase() + words.slice(1)
  return file.kind === 'preset' ? tMaybe(`preset.${name === 'Urls' ? 'URLs' : name}`, name) : name
}

function scopeText(file: RuleFile, everywhere: string): string {
  const s = file.scope
  if (!s) return everywhere
  if (s.docs?.length) return `${s.docs.length} doc`
  if (s.sites?.length) return s.sites.join(', ')
  if (s.types?.length) return s.types.join(', ').toUpperCase()
  if (s.languages?.length) return s.languages.join(', ').toUpperCase()
  return everywhere
}

function errorText(err: unknown): string {
  if (err instanceof EngineApiError) return err.detail || err.code
  return err instanceof Error ? err.message : String(err)
}

export function RulesApp({ bridge, engine }: { bridge: Bridge; engine: LiveEngine }) {
  const t = useT()
  const tMaybe = useTMaybe()
  const endpoint = useEngineStore((s) => s.endpoint)
  const [files, setFiles] = useState<RuleFile[]>([])
  const [openId, setOpenId] = useState<string | null>(null)
  const [open, setOpen] = useState<RuleFileContent | null>(null)
  const [raw, setRaw] = useState(false)
  const [status, setStatus] = useState<Status>({ kind: 'saved' })
  const [confirmDelete, setConfirmDelete] = useState(false)
  /** Changes only when a different text must be loaded into the editor (never while the user is typing). */
  const [editorKey, setEditorKey] = useState(0)
  const editor = useRef<PlainEditorHandle>(null)
  const text = useRef('')
  const dirty = useRef(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const state = useRef({ openId, open, raw })
  state.current = { openId, open, raw }
  const mac = bridge.platform === 'darwin'

  const client = () => {
    const c = engine.client
    if (!c) throw new Error(t('connecting'))
    return c
  }

  const refresh = async (): Promise<RuleFile[]> => {
    const list = await unwrap(client().GET('/api/v1/rules/files'))
    setFiles(list.items)
    return list.items
  }

  /** Put a file's text in the editor (`whole` = the file as it is on disk, header included). */
  const load = async (id: string, whole = state.current.raw) => {
    const content = await unwrap(client().GET('/api/v1/rules/files/{file_id}', { params: { path: { file_id: id } } }))
    const plain = content.kind === 'preferences' || content.kind === 'lexicon'
    // Preferences and pronunciations are edited whole; a rule by what it says, unless its file is shown as is.
    text.current = plain ? content.body : whole ? content.text : content.body
    dirty.current = false
    setOpen(content)
    setOpenId(content.id)
    setRaw(whole && !plain)
    setStatus({ kind: 'saved' })
    setConfirmDelete(false)
    setEditorKey((k) => k + 1)
  }

  const save = async (): Promise<void> => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
    if (!dirty.current) return
    const { openId: id, open: content, raw: whole } = state.current
    const value = text.current
    if (id === NEW && !value.trim()) return
    dirty.current = false
    setStatus({ kind: 'saving' })
    try {
      const plain = content?.kind === 'preferences' || content?.kind === 'lexicon'
      const body: RuleFileWrite = whole || plain ? { text: value } : { body: value }
      const saved =
        id === NEW || id === null
          ? await unwrap(client().POST('/api/v1/rules/files', { body }))
          : await unwrap(client().PUT('/api/v1/rules/files/{file_id}', { params: { path: { file_id: id } }, body }))
      // Keep what is in the editor (the user may still be typing); only the facts around it change.
      setOpen(saved)
      setOpenId(saved.id)
      if (!dirty.current) setStatus({ kind: 'saved' })
      await refresh()
    } catch (err) {
      dirty.current = true
      setStatus({ kind: 'error', message: errorText(err) })
    }
  }

  const select = async (id: string) => {
    await save()
    if (id === NEW) {
      text.current = ''
      dirty.current = false
      setOpen(null)
      setOpenId(NEW)
      setRaw(false)
      setStatus({ kind: 'saved' })
      setConfirmDelete(false)
      setEditorKey((k) => k + 1)
      setTimeout(() => editor.current?.focus(), 0)
      return
    }
    try {
      await load(id, false)
    } catch (err) {
      setStatus({ kind: 'error', message: errorText(err) })
    }
  }

  const onChange = (value: string) => {
    text.current = value
    dirty.current = true
    setStatus({ kind: 'dirty' })
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => void save(), SAVE_AFTER_MS)
  }

  const patch = async (id: string, body: RuleFileWrite) => {
    try {
      const saved = await unwrap(client().PUT('/api/v1/rules/files/{file_id}', { params: { path: { file_id: id } }, body }))
      if (state.current.openId === id) {
        setOpen(saved)
        setOpenId(saved.id)
      }
      await refresh()
    } catch (err) {
      setStatus({ kind: 'error', message: errorText(err) })
    }
  }

  const remove = async () => {
    const id = state.current.openId
    if (!id || id === NEW) return select(files[0]?.id ?? NEW)
    try {
      await unwrap(client().DELETE('/api/v1/rules/{rule_id}', { params: { path: { rule_id: id } } }))
      dirty.current = false
      const list = await refresh()
      await select(list[0]?.id ?? NEW)
    } catch (err) {
      setStatus({ kind: 'error', message: errorText(err) })
    }
  }

  // ---------------------------------------------------------------- lifecycle

  useEffect(() => {
    if (!endpoint) return
    void refresh().then(
      (list) => {
        if (state.current.openId === null && list[0]) void load(list[0].id, false)
      },
      (err) => setStatus({ kind: 'error', message: errorText(err) }),
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps -- when the engine (re)connects
  }, [endpoint])

  useEffect(
    () =>
      bridge.onRulesShow((item) => {
        if (item) void select(item)
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- subscribe once
    [bridge],
  )

  // Rules changed somewhere else (a voice command, a hand edit on disk): show it, unless that would
  // overwrite what is being typed here.
  useServerEvent('rules.changed', () => {
    void refresh().catch(() => undefined)
    const id = state.current.openId
    if (id && id !== NEW && !dirty.current && status.kind !== 'saving') void load(id).catch(() => undefined)
  })

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        void save()
      } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'n') {
        e.preventDefault()
        void select(NEW)
      }
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('blur', onBlur)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('blur', onBlur)
    }
    function onBlur() {
      void save() // leaving the window never loses an edit
    }
  })

  const dictation = useDictation(engine, (said) => editor.current?.insert(said.endsWith(' ') ? said : `${said} `))

  // ---------------------------------------------------------------- render

  const isNew = openId === NEW
  const kind = open?.kind ?? 'rule'
  const plain = kind === 'preferences' || kind === 'lexicon'
  const placeholder = kind === 'preferences' ? t('rules.preferences.placeholder') : kind === 'lexicon' ? t('rules.lexicon.placeholder') : t('rules.new.placeholder')
  const special = files.filter((f) => f.kind === 'preferences' || f.kind === 'lexicon')
  const yours = files.filter((f) => f.kind === 'rule')
  const builtin = files.filter((f) => f.kind === 'preset')
  const TierIcon = open && !plain ? (open.tier === 'filter' ? Zap : Sparkles) : null

  const item = (file: RuleFile) => {
    const Icon = fileIcon(file)
    const active = file.id === openId
    return (
      <li key={file.id}>
        <div
          className={cn(
            'group flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm',
            active ? 'bg-accent text-accent-foreground' : 'hover:bg-muted',
            !file.enabled && 'opacity-60',
          )}
        >
          <button
            type="button"
            aria-current={active ? 'true' : undefined}
            className="flex min-w-0 flex-1 items-center gap-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onClick={() => void select(file.id)}
          >
            <Icon aria-hidden="true" className={cn('size-4 shrink-0', active ? 'text-primary' : 'text-muted-foreground')} />
            <span className="truncate">{pretty(file, tMaybe)}</span>
          </button>
          {(file.kind === 'rule' || file.kind === 'preset') && (
            <Switch
              checked={file.enabled}
              aria-label={pretty(file, tMaybe)}
              onCheckedChange={(enabled) => void patch(file.id, { enabled })}
              className="scale-75"
            />
          )}
        </div>
      </li>
    )
  }

  return (
    <div className="flex h-screen flex-col bg-background text-foreground" data-testid="rules-app">
      <header className={cn('flex h-10 shrink-0 items-center gap-1 border-b pr-2 [-webkit-app-region:drag]', mac ? 'pl-20' : 'pl-3')}>
        <h1 className="text-sm font-medium">{t('rules.title')}</h1>
        <div className="ml-auto flex items-center gap-0.5 [-webkit-app-region:no-drag]">
          <IconButton label={t('rules.new')} shortcut={mac ? '⌘N' : 'Ctrl+N'} side="bottom" onClick={() => void select(NEW)}>
            <Plus />
          </IconButton>
          {bridge.capabilities.rulesFolder && (
            <IconButton label={t('rules.folder')} side="bottom" onClick={() => bridge.openRulesFolder()}>
              <FolderOpen />
            </IconButton>
          )}
        </div>
      </header>
      <div className="flex min-h-0 flex-1">
        <nav aria-label={t('rules.title')} className="w-60 shrink-0 overflow-y-auto border-r p-2">
          <ul className="space-y-0.5">{special.map(item)}</ul>
          <h2 className="mt-3 px-2 pb-1 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">{t('rules.group.yours')}</h2>
          <ul className="space-y-0.5">
            {yours.map(item)}
            <li>
              <button
                type="button"
                aria-current={isNew ? 'true' : undefined}
                className={cn(
                  'flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  isNew ? 'bg-accent text-accent-foreground' : 'hover:bg-muted',
                )}
                onClick={() => void select(NEW)}
              >
                <Plus aria-hidden="true" className="size-4" />
                {t('rules.new')}
              </button>
            </li>
          </ul>
          <h2 className="mt-3 px-2 pb-1 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">{t('rules.group.builtin')}</h2>
          <ul className="space-y-0.5">{builtin.map(item)}</ul>
        </nav>

        <section className="flex min-w-0 flex-1 flex-col" aria-label={open ? pretty(open, tMaybe) : t('rules.new')}>
          <div className="flex h-11 shrink-0 items-center gap-2 border-b px-4">
            {open && !plain ? (
              <>
                {TierIcon && (
                  <span title={t(open.tier === 'filter' ? 'rules.filter' : 'rules.needs_ai')} className="grid place-items-center">
                    <TierIcon aria-label={t(open.tier === 'filter' ? 'rules.filter' : 'rules.needs_ai')} className="size-4 text-primary" />
                  </span>
                )}
                <input
                  key={open.id}
                  defaultValue={open.name}
                  aria-label={t('rules.name')}
                  spellCheck={false}
                  readOnly={open.kind === 'preset'}
                  className="min-w-0 flex-1 truncate bg-transparent font-mono text-sm outline-none focus-visible:underline"
                  onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
                  onBlur={(e) => {
                    const name = e.currentTarget.value.trim()
                    if (name && name !== open.name) void patch(open.id, { name })
                  }}
                />
                <span className="inline-flex max-w-[35%] items-center gap-1 truncate rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                  <Globe aria-hidden="true" className="size-3 shrink-0" />
                  <span className="truncate">{scopeText(open, t('rules.scope.everywhere'))}</span>
                </span>
                <Switch checked={open.enabled} aria-label={t('rules.enabled')} onCheckedChange={(enabled) => void patch(open.id, { enabled })} />
                <IconButton label={t('rules.source')} side="bottom" active={raw} onClick={() => void save().then(() => load(open.id, !raw))}>
                  <Braces />
                </IconButton>
                {open.kind === 'rule' &&
                  (confirmDelete ? (
                    <IconButton label={t('rules.delete.confirm')} side="bottom" onClick={() => void remove()} onBlur={() => setConfirmDelete(false)} className="bg-destructive text-white hover:bg-destructive/90 hover:text-white">
                      <Check />
                    </IconButton>
                  ) : (
                    <IconButton label={t('rules.delete')} side="bottom" onClick={() => setConfirmDelete(true)}>
                      <Trash2 />
                    </IconButton>
                  ))}
              </>
            ) : (
              <span className="flex items-center gap-2 text-sm font-medium">
                {open ? (
                  <>
                    {open.kind === 'preferences' ? <NotebookPen aria-hidden="true" className="size-4 text-primary" /> : <BookA aria-hidden="true" className="size-4 text-primary" />}
                    {open.name}
                  </>
                ) : (
                  <>
                    <Plus aria-hidden="true" className="size-4 text-primary" />
                    {t('rules.new')}
                  </>
                )}
              </span>
            )}
          </div>
          <div className="min-h-0 flex-1">
            <PlainEditor
              ref={editor}
              docKey={`${openId ?? ''}:${editorKey}`}
              value={text.current}
              onChange={onChange}
              label={open ? pretty(open, tMaybe) : t('rules.new')}
              placeholder={placeholder}
              mono={raw || kind === 'lexicon'}
            />
          </div>
          <footer className="flex h-10 shrink-0 items-center gap-2 border-t px-4 text-xs text-muted-foreground">
            <span role="status" aria-live="polite" className={cn('flex min-w-0 flex-1 items-center gap-1.5', status.kind === 'error' && 'text-destructive')} data-testid="save-status" data-status={status.kind}>
              {status.kind === 'saving' ? (
                <Loader2 aria-hidden="true" className="size-3.5 animate-spin motion-reduce:animate-none" />
              ) : status.kind === 'error' ? (
                <CircleAlert aria-hidden="true" className="size-3.5 shrink-0" />
              ) : status.kind === 'saved' ? (
                <Check aria-hidden="true" className="size-3.5 text-success" />
              ) : (
                <span aria-hidden="true" className="size-1.5 rounded-full bg-warning" />
              )}
              <span className="truncate">
                {status.kind === 'error' ? status.message : status.kind === 'saving' ? t('rules.saving') : status.kind === 'saved' ? t('rules.saved') : t('rules.unsaved')}
              </span>
            </span>
            {dictation.transcript && <span className="max-w-[45%] truncate italic">{dictation.transcript}</span>}
            {dictation.available && (
              <button
                type="button"
                aria-label={dictation.active ? t('orb.listening') : t('rules.dictate')}
                title={dictation.active ? t('orb.listening') : t('rules.dictate')}
                aria-pressed={dictation.active}
                data-state={dictation.active ? 'listening' : 'idle'}
                className="orb size-7 outline-none focus-visible:ring-2 focus-visible:ring-ring"
                style={{ '--level': dictation.level.toFixed(3) } as React.CSSProperties}
                onMouseDown={(e) => e.preventDefault()}
                onClick={dictation.toggle}
              >
                <Mic aria-hidden="true" className="size-4" />
              </button>
            )}
          </footer>
        </section>
      </div>
    </div>
  )
}
