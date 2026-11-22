/**
 * The command bar (09 § 1): a voice orb and a composer on top, what just happened below, the transport for
 * what is playing, and a row of icons for everything else.
 *
 * Keyboard: Enter sends the composer (or confirms / runs the selected row when it is empty); ⌘↵ and ⇧↵ are
 * a row's second and third actions; Tab toggles typing ↔ listening; `@` finds files; Esc backs out one level
 * and finally closes the bar.
 */
import { ArrowUp, Download, MicOff } from 'lucide-react'
import { useEffect, useRef, useState, type DragEvent, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useStore } from 'zustand'

import { unwrap, type DocumentOut } from '@shared/api/client'

import type { Bridge } from '@/bridge'
import { Command, CommandGroup, CommandList } from '@/components/ui/command'
import { IconButton } from '@/components/ui/icon-button'
import { Kbd } from '@/components/ui/kbd'
import { useEngine, useEngineStore } from '@/engine'
import { useT } from '@/i18n'
import { cn } from '@/lib/utils'
import { usePrefs } from '@/prefs'
import type { BarActions, RowModifier } from './actions'
import { NowPlaying } from './components/NowPlaying'
import { Onboarding } from './components/Onboarding'
import { Orb, orbState } from './components/Orb'
import { RowView, type RowHandlers } from './components/RowView'
import { Toolbar } from './components/Toolbar'
import { Composer, EMPTY_VALUE, type ComposerHandle, type ComposerValue } from './composer/Composer'
import { fromDataTransfer } from './composer/attachments'
import type { BarController } from './controller'
import { buildRows, defaultSelection, findRow } from './rows'
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

/** How the shortcut is said aloud ("Option Space"), in the interface language. */
function spokenShortcut(accel: string | undefined, lang: string): string {
  const es = lang === 'es'
  return (accel ?? 'Alt+Space')
    .split('+')
    .map((part) => {
      const k = part.toLowerCase()
      if (k === 'alt') return es ? 'Opción' : 'Option'
      if (k === 'shift') return es ? 'Mayúsculas' : 'Shift'
      if (k === 'command' || k === 'cmd') return es ? 'Comando' : 'Command'
      if (k === 'control' || k === 'ctrl') return 'Control'
      if (k === 'space') return es ? 'Espacio' : 'Space'
      return part
    })
    .join(' ')
}

function modifierOf(e: { metaKey: boolean; ctrlKey: boolean; shiftKey: boolean }): RowModifier {
  return e.metaKey || e.ctrlKey ? 'secondary' : e.shiftKey ? 'tertiary' : 'primary'
}

export interface BarAppProps {
  controller: BarController
  actions: BarActions
  bridge: Bridge
}

export function BarApp({ controller, actions, bridge }: BarAppProps) {
  const t = useT()
  const engine = useEngine()
  const store = controller.store
  const state = useStore(store) as BarState
  const playback = useEngineStore((s) => s.playback)
  const endpoint = useEngineStore((s) => s.endpoint)
  const engineProblem = useEngineStore((s) => s.problem)
  const socketStatus = useEngineStore((s) => s.status)
  const settings = usePrefs((s) => s.settings)
  const capabilities = usePrefs((s) => s.capabilities)
  const language = usePrefs((s) => s.language)
  const [recents, setRecents] = useState<DocumentOut[]>([])
  const [search, setSearch] = useState<DocumentOut[] | null>(null)
  const [partialAnnouncement, setPartialAnnouncement] = useState('')
  const rootRef = useRef<HTMLElement>(null)
  const composerRef = useRef<ComposerHandle | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const mac = bridge.platform === 'darwin'

  const groups = buildRows(state, { recents, search })
  const selected = defaultSelection(groups, state)
  const onboarding = state.open && settings !== null && settings.onboarding_completed === false
  const filled = !composerEmpty(state)
  const speech = capabilities?.speech_input

  const attachComposer = (handle: ComposerHandle | null) => {
    composerRef.current = handle
    controller.attachComposer(handle)
    actions.composer = handle
  }

  // ---------------------------------------------------------------- data: capabilities, recents, search

  useEffect(() => {
    // Whether the bar can listen is known before the first open, so the microphone (and its OS prompt)
    // is never touched on a Mac where listening isn't set up yet.
    if (speech) controller.setCanListen(speech.available)
  }, [speech, controller])

  useEffect(() => {
    if (!state.open || !endpoint) return
    const client = engine.client
    if (!client) return
    let cancelled = false
    void unwrap(client.GET('/api/v1/documents', { params: { query: { limit: 10 } } })).then(
      (list) => !cancelled && setRecents(list.items),
      () => undefined,
    )
    return () => {
      cancelled = true
    }
  }, [state.open, endpoint, engine])

  const query = state.mode !== 'listening' && state.attachments.length === 0 ? state.draft.trim() : ''
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
  }, [query, engine])

  // ---------------------------------------------------------------- window plumbing

  useEffect(() => {
    // Keep the native window as tall as the content (frameless bar).
    const el = rootRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => bridge.resizeBar(Math.ceil(el.getBoundingClientRect().height) + 2))
    ro.observe(el)
    return () => ro.disconnect()
  }, [bridge])

  useEffect(() => {
    if (state.open && !onboarding) composerRef.current?.focus()
  }, [state.open, onboarding, state.mode])

  useEffect(() => {
    // Push-to-talk: the first keyup of the held hotkey (Space or Option) inside the focused bar.
    const onKeyUp = (e: globalThis.KeyboardEvent) => {
      if (e.key === ' ' || e.key === 'Alt') controller.hotkeyReleased()
    }
    window.addEventListener('keyup', onKeyUp)
    return () => window.removeEventListener('keyup', onKeyUp)
  }, [controller])

  useEffect(() => {
    // Rate-limited announcement of the live transcript (finals and replies go through `announcement`).
    if (state.mode !== 'listening' || !state.transcript) return
    const timer = setTimeout(() => setPartialAnnouncement(state.transcript), 1500)
    return () => clearTimeout(timer)
  }, [state.mode, state.transcript])

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

  const handlers: RowHandlers = {
    onSelect: (row) => void actions.runRow(row, 'primary'),
    onSecondary: (row) => void actions.runRow(row, 'secondary'),
    onTertiary: (row) => void actions.runRow(row, 'tertiary'),
    onCancelAction: (action) => void actions.cancelAction(action.actionId),
    onTogglePreset: (card, row) => void actions.togglePreset(card, row),
    onSetSetting: (card, row, value) => void actions.setSetting(card, row, value),
  }

  const close = () => {
    controller.close()
    bridge.hideBar()
  }

  /** Esc always backs out one level (09 § Accessibility), then closes the bar. */
  const escape = () => {
    const s = store.getState()
    if (s.localConfirm) return store.setState({ localConfirm: null })
    const pending = latestPendingAction(s)
    if (pending) return void actions.cancelAction(pending.actionId)
    if (s.mode === 'listening' && s.transcript) return controller.toggle()
    if (!composerEmpty(s)) return composerRef.current?.clear()
    close()
  }

  /** Enter in the composer. */
  const onEnter = (event: globalThis.KeyboardEvent) => {
    const s = store.getState()
    const value: ComposerValue = composerRef.current?.getValue() ?? EMPTY_VALUE
    const row = findRow(groups, selected)
    if (s.mode === 'listening') {
      // With nothing said yet, Enter answers a pending confirmation instead of submitting silence.
      if (!s.transcript.trim() && row?.kind === 'action' && row.action.status === 'pending') {
        controller.keystroke()
        void actions.runRow(row, 'primary')
      } else {
        controller.submit('enter')
      }
      return
    }
    // A row the user moved to wins; otherwise what is in the composer is sent; otherwise the default row runs.
    if (row && (s.selected === row.value || !(value.text || value.attachments.length))) void actions.runRow(row, modifierOf(event))
    else void actions.submit(value)
  }

  const onRootKeyDown = (e: ReactKeyboardEvent<HTMLElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      escape()
      return
    }
    if (e.key === 'Tab' && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey && !onboarding) {
      const target = e.target as HTMLElement
      if (target.closest('[data-slot="composer"]') || target === rootRef.current) {
        e.preventDefault()
        if (store.getState().canListen !== false) controller.toggle()
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
    const s = store.getState()
    if (s.mode === 'listening' && isTypingKey(e)) controller.keystroke()
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
      controller.onJobProgress({ type: 'job.progress', job_id: job.job_id, pct: 0, label: t('onboard.model') })
    } catch (err) {
      controller.notice('error', (err as Error).message)
    }
  }

  const placeholder =
    state.mode === 'listening'
      ? t('composer.placeholder.listening')
      : state.canListen === false
        ? t('composer.placeholder.typing_only')
        : t('composer.placeholder.typing')
  const orb = orbState(state.mode, { prompting: state.prompting, canListen: state.canListen, error: !!state.micError })
  const announcement = state.announcement?.text ?? ''
  const shortcut = displayShortcut(settings?.shortcuts?.open_bar, mac)
  const connecting = socketStatus === 'reconnecting' || socketStatus === 'connecting'
  const showTransport = !!playback.document_id && playback.state !== 'idle'
  const status = state.micError ?? state.hint

  return (
    <main
      ref={rootRef}
      aria-label={t('app.name')}
      data-open={state.open}
      data-mode={state.mode}
      className="overflow-hidden rounded-2xl border bg-background/95 text-foreground shadow-2xl backdrop-blur-xl"
      onDragOver={(e) => e.preventDefault()}
      onDrop={onDrop}
    >
      <h1 className="sr-only">{t('app.name')}</h1>
      <div className="sr-only" aria-live="polite" aria-atomic="true" data-testid="announcer">
        {announcement}
      </div>
      <div className="sr-only" aria-live="polite" aria-atomic="true">
        {state.mode === 'listening' ? partialAnnouncement : ''}
      </div>
      {(engineProblem || connecting) && (
        <p role="status" className="border-b px-4 py-1.5 text-xs text-muted-foreground">
          {engineProblem ?? t('connecting')}
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
        onValueChange={(value) => store.setState({ selected: value })}
        onKeyDown={onRootKeyDown}
      >
        {!onboarding && (
          <div className="flex items-start gap-3 px-4 pt-3 pb-1.5" onKeyDownCapture={onComposerKeyDown}>
            <Orb state={orb} level={state.level} onPress={() => controller.toggle()} />
            <Composer
              ref={attachComposer}
              label={t('composer.label')}
              placeholder={placeholder}
              onEnter={onEnter}
              onChange={(v) => controller.setDraft(v.text, v.attachments, v.multiline)}
              searchFiles={bridge.searchFiles}
              pathForFile={bridge.pathForFile}
            />
            {filled && state.mode !== 'listening' && (
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
          </div>
        )}
        {onboarding ? (
          <Onboarding
            controller={controller}
            actions={actions}
            orb={<Orb state={orb} level={state.level} onPress={() => controller.toggle()} />}
            heard={state.transcript}
            openBarShortcut={shortcut}
            openBarSpoken={spokenShortcut(settings?.shortcuts?.open_bar, language)}
            onFinished={() =>
              usePrefs.setState((s) => ({ settings: s.settings ? { ...s.settings, onboarding_completed: true } : s.settings }))
            }
          />
        ) : (
          <>
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
            {status && (
              <p role={state.micError ? 'alert' : 'status'} className="px-4 pb-2 text-xs text-muted-foreground">
                {status}
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
            {showTransport && (
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
            )}
            <Toolbar
              handlers={{
                attach: () => void actions.attach(),
                clipboard: () => actions.readClipboard(),
                show: (what) => actions.show(what),
                rules: () => actions.openRules(),
              }}
              trailing={<Kbd aria-hidden="true">{shortcut}</Kbd>}
            />
          </>
        )}
      </Command>
      <input ref={fileRef} type="file" multiple className="hidden" tabIndex={-1} aria-hidden="true" />
    </main>
  )
}
