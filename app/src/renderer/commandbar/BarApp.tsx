/**
 * The command bar (09 § 1): a voice orb and a composer on top, what just happened below, the transport for
 * what is playing, and a row of icons for everything else, ending with this month's spending.
 *
 * Keyboard: Enter sends the composer (or confirms / runs the selected row when it is empty); ⌘↵ and ⇧↵ are
 * a row's second and third actions; Tab toggles typing ↔ listening, and while the assistant speaks stops it and
 * listens; `@` finds files; Esc stops the assistant, backs out one level, and finally closes the bar.
 *
 * Steady on screen: each part subscribes to just the state it draws (the mic level never goes through the
 * store at all), the window is resized synchronously before the frame that needs the new height paints, and
 * nothing appears for a moment and goes away again: connection and microphone notes are the composer's
 * placeholder, and recent documents are already loaded when the bar opens.
 */
import { ArrowUp, Download, MicOff } from 'lucide-react'
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import { useStore } from 'zustand'
import { useShallow } from 'zustand/react/shallow'

import { unwrap, type DocumentOut } from '@shared/api/client'

import type { Bridge } from '@/bridge'
import { Command, CommandGroup, CommandList } from '@/components/ui/command'
import { IconButton } from '@/components/ui/icon-button'
import { useRenderCount } from '@/debug'
import { useEngine, useEngineStore } from '@/engine'
import { useT } from '@/i18n'
import { cn } from '@/lib/utils'
import { usePrefs } from '@/prefs'
import type { BarActions, RowModifier } from './actions'
import { NowPlaying } from './components/NowPlaying'
import { Orb, orbState, type OrbShortcut } from './components/Orb'
import { RowView, type RowHandlers } from './components/RowView'
import { LiveSpendChip } from './components/SpendChip'
import { Toolbar } from './components/Toolbar'
import { Composer, EMPTY_VALUE, type ComposerHandle, type ComposerValue } from './composer/Composer'
import { fromDataTransfer } from './composer/attachments'
import type { BarController } from './controller'
import { composerPlaceholder, openingPose, PANEL_BORDER_PX } from './bar-view'
import { buildRows, defaultSelection, findRow, rowState } from './rows'
import { composerEmpty, latestPendingAction, type BarState } from './store'

function isTypingKey(e: ReactKeyboardEvent): boolean {
  if (e.metaKey || e.ctrlKey) return false
  if (e.altKey && e.key === ' ') return false
  return e.key.length === 1 || e.key === 'Backspace' || e.key === 'Delete'
}

export function displayShortcut(accel: string | undefined, mac: boolean): string {
  const a = accel ?? 'Alt+Space'
  if (!mac) return a
  return a
    .replace(/Alt\+?/gi, '⌥')
    .replace(/Shift\+?/gi, '⇧')
    .replace(/(Command|Cmd)\+?/gi, '⌘')
    .replace(/(Control|Ctrl)\+?/gi, '⌃')
    .replace(/Space/i, 'Space')
}

function modifierOf(e: { metaKey: boolean; ctrlKey: boolean; shiftKey: boolean }): RowModifier {
  return e.metaKey || e.ctrlKey ? 'secondary' : e.shiftKey ? 'tertiary' : 'primary'
}

export interface BarAppProps {
  controller: BarController
  actions: BarActions
  bridge: Bridge
}

/** Keys cmdk moves the row selection with. */
const ROW_NAV_KEYS = new Set(['ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'])

export function BarApp({ controller, actions, bridge }: BarAppProps) {
  useRenderCount('BarApp')
  const t = useT()
  const engine = useEngine()
  const store = controller.store
  // Only what the rows and the frame need: not the transcript, the hint or the busy flag (those have their own
  // components), and never the mic level (it is not in the store at all).
  const live = useStore(
    store,
    useShallow((x: BarState) => ({ ...rowState(x), open: x.open, canListen: x.canListen, micError: x.micError, hint: x.hint })),
  )
  const s = useMemo(() => openingPose(live, live.open, live.canListen), [live])
  const endpoint = useEngineStore((x) => x.endpoint)
  const engineProblem = useEngineStore((x) => x.problem)
  const recents = useEngineStore((x) => x.recents)
  const settings = usePrefs((x) => x.settings)
  const capabilities = usePrefs((x) => x.capabilities)
  const [search, setSearch] = useState<DocumentOut[] | null>(null)
  const [openId, setOpenId] = useState(0)
  const rootRef = useRef<HTMLElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const composerRef = useRef<ComposerHandle | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const lastHeight = useRef(0)
  /** Set by a row-navigation key just before cmdk reports the selection it moved to. */
  const navigated = useRef(false)
  /** The window's height as main last set it (after clamping): what the viewport will become. */
  const windowHeight = useRef(0)
  const mac = bridge.platform === 'darwin'

  const groups = useMemo(() => buildRows(s, { recents, search }), [s, recents, search])
  const selected = defaultSelection(groups, s)
  const speech = capabilities?.speech_input
  const accel = settings?.shortcuts?.open_bar ?? 'Alt+Space'
  const shortcut = useMemo<OrbShortcut>(() => ({ display: displayShortcut(accel, mac), aria: accel }), [accel, mac])

  const attachComposer = useCallback(
    (handle: ComposerHandle | null) => {
      composerRef.current = handle
      controller.attachComposer(handle)
      actions.composer = handle
    },
    [controller, actions],
  )

  // ---------------------------------------------------------------- data: capabilities, search

  useEffect(() => {
    // What the engine says about listening replaces what the shell remembered (BarOpenedEvent.canListen).
    if (speech) controller.setCanListen(speech.available, speech.reason ?? null)
  }, [speech, controller])

  const query = s.mode !== 'listening' && s.attachments.length === 0 ? s.draft.trim() : ''
  useEffect(() => {
    if (!query) {
      setSearch(null)
      return
    }
    const client = engine.client
    if (!client) return
    let cancelled = false
    const timer = setTimeout(() => {
      // Typing finds documents by title and text (FR-BAR-8).
      void unwrap(client.GET('/api/v1/documents', { params: { query: { q: query, limit: 6 } } })).then(
        (list) => !cancelled && setSearch(list.items),
        () => !cancelled && setSearch([]),
      )
    }, 150)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [query, engine, endpoint])

  // ---------------------------------------------------------------- window plumbing

  /** The bar has been opened at least once since the page loaded. */
  const everOpened = useRef(false)

  /**
   * Make the window exactly as tall as the content. Synchronous IPC: done before this frame paints. Not while
   * the bar is closed after an open: the window keeps the height it was seen at, which is usually the height of
   * the next open too (so opening needs no resize, and shows sooner). Before the first open it follows the
   * closed bar, which is drawn in its opening pose, so the first open needs no resize either.
   */
  const syncHeight = useCallback((): number => {
    const el = contentRef.current
    const open = store.getState().open
    if (open) everOpened.current = true
    if (!el || (!open && everOpened.current)) return lastHeight.current
    const height = Math.ceil(el.getBoundingClientRect().height) + PANEL_BORDER_PX
    if (height !== lastHeight.current) {
      lastHeight.current = height
      windowHeight.current = bridge.resizeBar(height)
    }
    return height
  }, [bridge, store])

  // After every render of the rows and the frame (laid out, not yet painted)…
  useLayoutEffect(() => {
    syncHeight()
  })
  // …and for size changes that don't render this component (the editor growing, the transport strip).
  useEffect(() => {
    const el = contentRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => syncHeight())
    ro.observe(el)
    return () => ro.disconnect()
  }, [syncHeight])

  useEffect(() => bridge.onBarOpened((event) => setOpenId(event.openId)), [bridge])

  useLayoutEffect(() => {
    // The window is up but invisible. Once this open's state has painted at the window's new size, say so:
    // main shows it then, so the first frame anyone sees is the steady one. The page learns its new size a
    // moment after the window gets it; from then, two frames: one painted at that size, one to be sure it's out.
    if (!openId) return
    syncHeight()
    let frame = 0
    let waits = 0
    const shown = () => {
      bridge.barShown(openId, syncHeight())
      document.documentElement.classList.remove('bar-opening')
    }
    const settle = () => {
      const sized = !bridge.capabilities.electron || Math.abs(window.innerHeight - windowHeight.current) <= 1 || windowHeight.current === 0
      if (!sized && waits++ < 12) {
        frame = requestAnimationFrame(settle)
        return
      }
      frame = requestAnimationFrame(() => {
        frame = requestAnimationFrame(shown)
      })
    }
    frame = requestAnimationFrame(settle)
    return () => cancelAnimationFrame(frame)
  }, [openId, bridge, syncHeight])

  useEffect(() => {
    if (s.open) composerRef.current?.focus()
  }, [s.open, s.mode])

  useEffect(() => {
    // Push-to-talk: the first keyup of the held hotkey (Space or Option) inside the focused bar.
    const onKeyUp = (e: globalThis.KeyboardEvent) => {
      if (e.key === ' ' || e.key === 'Alt') controller.hotkeyReleased()
    }
    window.addEventListener('keyup', onKeyUp)
    return () => window.removeEventListener('keyup', onKeyUp)
  }, [controller])

  useEffect(() => {
    actions.pickFiles = () =>
      new Promise<File[]>((resolve) => {
        const input = fileRef.current
        if (!input) return resolve([])
        input.value = ''
        input.onchange = () => resolve(Array.from(input.files ?? []))
        input.click()
      })
    return () => {
      actions.pickFiles = null
    }
  }, [actions])

  // ---------------------------------------------------------------- handlers

  const handlers: RowHandlers = useMemo(
    () => ({
      onSelect: (row) => void actions.runRow(row, 'primary'),
      onSecondary: (row) => void actions.runRow(row, 'secondary'),
      onTertiary: (row) => void actions.runRow(row, 'tertiary'),
      onCancelAction: (action) => void actions.cancelAction(action.actionId),
      onSetSetting: (card, row, value) => void actions.setSetting(card, row, value),
      onRecord: (action) => void actions.recordClone(action.actionId),
      onStopRecording: () => void controller.finishCapture(),
      subscribeLevel: (fn) => controller.subscribeLevel(fn),
      onReplay: (recordingId) => void actions.replay(recordingId),
      onDeleteVoice: (row) => actions.deleteVoice(row),
    }),
    [actions, controller],
  )

  const close = () => {
    controller.close()
    bridge.hideBar()
  }

  /** Esc always backs out one level (09 § Accessibility), then closes the bar. */
  const escape = () => {
    const now = store.getState()
    // Recording a voice to clone: throw the take away (the card can record again).
    if (now.mode === 'recording') return controller.interrupt('idle')
    // The assistant is talking: stop it, and stop the conversation (Tab would stop it and listen).
    if (controller.speakingNow) return controller.interrupt('idle')
    if (now.localConfirm) return store.setState({ localConfirm: null })
    const pending = latestPendingAction(now)
    if (pending) return void actions.cancelAction(pending.actionId)
    if (now.mode === 'listening' && now.transcript) return controller.toggle()
    if (!composerEmpty(now)) return composerRef.current?.clear()
    close()
  }

  /** Enter in the composer. */
  const onEnter = (event: globalThis.KeyboardEvent) => {
    const now = store.getState()
    const value: ComposerValue = composerRef.current?.getValue() ?? EMPTY_VALUE
    const row = findRow(groups, selected)
    if (now.mode === 'recording') return void controller.finishCapture() // done reading: send it now
    if (now.mode === 'listening') {
      // With nothing said yet, Enter answers a pending confirmation instead of submitting silence.
      if (!now.transcript.trim() && row?.kind === 'action' && row.action.status === 'pending') {
        controller.keystroke()
        void actions.runRow(row, 'primary')
      } else {
        controller.submit('enter')
      }
      return
    }
    // A row the user moved to wins; otherwise what is in the composer is sent; otherwise the default row runs.
    if (row && (now.selected === row.value || !(value.text || value.attachments.length))) void actions.runRow(row, modifierOf(event))
    else void actions.submit(value)
  }

  const onRootKeyDown = (e: ReactKeyboardEvent<HTMLElement>) => {
    navigated.current = ROW_NAV_KEYS.has(e.key) || (e.ctrlKey && (e.key === 'n' || e.key === 'p'))
    if (e.key === 'Escape') {
      e.preventDefault()
      escape()
      return
    }
    if (e.key === 'Tab' && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
      const target = e.target as HTMLElement
      if (target.closest('[data-slot="composer"]') || target === rootRef.current) {
        e.preventDefault()
        if (store.getState().canListen !== false || controller.speakingNow) controller.toggle()
      }
    }
  }

  /** Keys on their way into the composer. */
  const onComposerKeyDown = (e: ReactKeyboardEvent<HTMLElement>) => {
    if (e.repeat && (e.key === ' ' || e.key === 'Alt') && controller.openedViaHotkey) {
      // Auto-repeat of the still-held hotkey: not typing (and ⌥Space would insert a no-break space).
      e.preventDefault()
      return
    }
    if (isTypingKey(e)) controller.keystroke() // stops listening (and the first-run nudge); the text stays
    if (e.key === 'Home' || e.key === 'End') e.stopPropagation() // the caret's, not the rows'
  }

  const onDrop = (e: DragEvent<HTMLElement>) => {
    // Anywhere on the bar outside the editor (the editor places its own drops at the caret).
    const items = fromDataTransfer(e.dataTransfer, bridge.pathForFile)
    if (!items.length) return
    e.preventDefault()
    composerRef.current?.insertAttachments(items)
    composerRef.current?.focus()
  }

  const installSpeechModel = async () => {
    const client = engine.client
    const model = speech?.install_model
    if (!client || !model) return
    try {
      const job = await unwrap(client.POST('/api/v1/models/local/{model_id}', { params: { path: { model_id: model } } }))
      controller.onJobProgress({ type: 'job.progress', job_id: job.job_id, pct: 0, label: t('speech.installing') })
    } catch (err) {
      controller.notice('error', (err as Error).message)
    }
  }

  return (
    <main
      ref={rootRef}
      aria-label={t('app.name')}
      data-open={s.open}
      data-mode={s.mode}
      // In the window the panel fills it (bar.css): whatever the window's height for a frame, no band of
      // window shows below the panel. Its own height comes from the content (`contentRef`).
      className="bar-panel overflow-hidden rounded-2xl border bg-background/95 text-foreground"
      onDragOver={(e) => e.preventDefault()}
      onDrop={onDrop}
    >
      <div ref={contentRef} className="flex flex-col" data-testid="bar-content">
        <h1 className="sr-only">{t('app.name')}</h1>
        <Announcers controller={controller} />
        {engineProblem && (
          <p role="status" className="border-b px-4 py-1.5 text-xs text-muted-foreground">
            {engineProblem}
          </p>
        )}
        <Command
          label={t('app.name')}
          shouldFilter={false}
          loop
          vimBindings={false}
          // Selection follows the keyboard and the conversation, not wherever the pointer happens to rest
          // when new rows appear (Enter must act on what the user chose). Clicking still runs a row.
          disablePointerSelection
          value={selected}
          // Only a move the user made counts: cmdk also reports its own automatic pick of the first row (often
          // "earlier", the history row) when rows change, and keeping that made Enter open the history.
          onValueChange={(value) => {
            if (!navigated.current) return
            navigated.current = false
            store.setState({ selected: value })
          }}
          onKeyDown={onRootKeyDown}
        >
          <VoiceRow
            controller={controller}
            actions={actions}
            bridge={bridge}
            shortcut={shortcut}
            attachComposer={attachComposer}
            onEnter={onEnter}
            onKeyDownCapture={onComposerKeyDown}
            composerRef={composerRef}
          />
          {speech && !speech.available && (
            <p role="status" className="flex items-center gap-2 px-4 pb-2 text-xs text-muted-foreground" data-testid="speech-unavailable">
              <MicOff aria-hidden="true" className="size-3.5 shrink-0" />
              <span className="min-w-0 flex-1">{speech.reason}</span>
              {speech.install_model && (
                <IconButton label={t('row.install')} onClick={() => void installSpeechModel()} tabIndex={-1} className="size-6">
                  <Download className="size-3.5" />
                </IconButton>
              )}
            </p>
          )}
          <CommandList className={cn(groups.length > 0 && 'border-t')}>
            {groups.map((group) => (
              <CommandGroup key={group.id}>
                {group.rows.map((row) => (
                  <RowView key={row.value} row={row} handlers={handlers} />
                ))}
              </CommandGroup>
            ))}
          </CommandList>
          <Transport actions={actions} bridge={bridge} />
          <Toolbar
            handlers={{
              attach: () => void actions.attach(),
              clipboard: () => actions.readClipboard(),
              show: (what) => actions.show(what),
              rules: () => actions.openRules(),
            }}
            trailing={<LiveSpendChip onPress={() => actions.show('usage')} />}
          />
        </Command>
        <input ref={fileRef} type="file" multiple className="hidden" tabIndex={-1} aria-hidden="true" />
      </div>
    </main>
  )
}

// ------------------------------------------------------------------ parts with their own subscriptions

/** The orb, the composer and the send button. Re-renders for listening/typing changes, not for the rows. */
function VoiceRow({
  controller,
  actions,
  bridge,
  shortcut,
  attachComposer,
  onEnter,
  onKeyDownCapture,
  composerRef,
}: {
  controller: BarController
  actions: BarActions
  bridge: Bridge
  shortcut: OrbShortcut
  attachComposer(handle: ComposerHandle | null): void
  onEnter(event: globalThis.KeyboardEvent): void
  onKeyDownCapture(e: ReactKeyboardEvent<HTMLElement>): void
  composerRef: { current: ComposerHandle | null }
}) {
  useRenderCount('VoiceRow')
  const t = useT()
  const live = useStore(
    controller.store,
    useShallow((x: BarState) => ({
      open: x.open,
      mode: x.mode,
      engineSpeaking: x.engineSpeaking,
      canListen: x.canListen,
      micError: x.micError,
      hint: x.hint,
      filled: !composerEmpty(x),
    })),
  )
  const s = useMemo(() => openingPose(live, live.open, live.canListen), [live])
  const status = useEngineStore((x) => x.status)
  const connecting = status === 'reconnecting' || status === 'connecting'
  const orb = orbState(s.mode, { engineSpeaking: s.engineSpeaking, canListen: s.canListen, error: !!s.micError })
  const subscribeLevel = useCallback((fn: (level: number) => void) => controller.subscribeLevel(fn), [controller])
  return (
    <div className="flex items-start gap-3 px-4 pt-3 pb-1.5" onKeyDownCapture={onKeyDownCapture}>
      <Orb state={orb} subscribeLevel={subscribeLevel} shortcut={shortcut} detail={s.micError} onPress={() => controller.toggle()} />
      <Composer
        ref={attachComposer}
        label={t('composer.label')}
        placeholder={composerPlaceholder(s, connecting, t, shortcut.display)}
        onEnter={onEnter}
        onChange={(v) => controller.setDraft(v.text, v.attachments, v.multiline)}
        searchFiles={bridge.searchFiles}
        pathForFile={bridge.pathForFile}
      />
      {s.filled && s.mode !== 'listening' && (
        <IconButton
          label={t('send')}
          shortcut="↵"
          tabIndex={-1}
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => void actions.submit(composerRef.current?.getValue() ?? EMPTY_VALUE)}
          className="mt-1 bg-primary text-primary-foreground hover:bg-primary/90 hover:text-primary-foreground"
        >
          <ArrowUp />
        </IconButton>
      )}
      {/* The placeholder is drawn, not read: screen readers hear these notes here. */}
      <div className="sr-only" role="alert" data-testid="mic-error">
        {s.micError ?? ''}
      </div>
      <div className="sr-only" role="status" data-testid="bar-status">
        {s.micError ? '' : (s.hint ?? (connecting ? t('connecting') : ''))}
      </div>
    </div>
  )
}

/** Live regions for screen readers: replies and cards (`announcement`), and the transcript while listening. */
function Announcers({ controller }: { controller: BarController }) {
  const s = useStore(
    controller.store,
    useShallow((x: BarState) => ({ announcement: x.announcement?.text ?? '', mode: x.mode, transcript: x.transcript })),
  )
  const [partial, setPartial] = useState('')
  useEffect(() => {
    // Rate-limited announcement of the live transcript (finals and replies go through `announcement`).
    if (s.mode !== 'listening' || !s.transcript) return
    const timer = setTimeout(() => setPartial(s.transcript), 1500)
    return () => clearTimeout(timer)
  }, [s.mode, s.transcript])
  return (
    <>
      <div className="sr-only" aria-live="polite" aria-atomic="true" data-testid="announcer">
        {s.announcement}
      </div>
      <div className="sr-only" aria-live="polite" aria-atomic="true">
        {s.mode === 'listening' ? partial : ''}
      </div>
    </>
  )
}

/** The transport strip, following playback on its own (it updates every second while reading). */
function Transport({ actions, bridge }: { actions: BarActions; bridge: Bridge }) {
  const playback = useEngineStore((x) => x.playback)
  if (!playback.document_id || playback.state === 'idle') return null
  return (
    <NowPlaying
      playback={playback}
      handlers={{
        toggle: () => void actions.togglePlayback(playback),
        skip: (n) => void actions.skip(n),
        speed: (d) => void actions.changeSpeed(playback, d),
        next: () => void actions.nextDocument(),
        openReader: () => playback.document_id && bridge.openReader({ documentId: playback.document_id }),
      }}
    />
  )
}
