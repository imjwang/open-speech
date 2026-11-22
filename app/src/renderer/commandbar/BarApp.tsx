/**
 * The command bar (09 § 1): opens listening, live transcript in the input, results as cmdk rows.
 * Keyboard: Enter submits / confirms / runs the selected row; ⌘↵ secondary; ⇧↵ preview; Tab toggles
 * typing ↔ listening; Esc backs out one level and finally closes the bar.
 */
import { useEffect, useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent } from 'react'
import { useStore } from 'zustand'

import { unwrap, type DocumentOut } from '@shared/api/client'

import type { Bridge } from '@/bridge'
import { Command, CommandGroup, CommandInput, CommandList } from '@/components/ui/command'
import { Kbd } from '@/components/ui/kbd'
import { useEngine, useEngineStore } from '@/engine'
import { cn } from '@/lib/utils'
import type { BarActions } from './actions'
import { KeyEntry } from './components/KeyEntry'
import { MicIndicator } from './components/MicIndicator'
import { Onboarding } from './components/Onboarding'
import { RowView, type RowHandlers } from './components/RowView'
import type { BarController } from './controller'
import { classifyPastedText, itemFromDataTransfer, itemFromFile } from './dropped'
import { buildRows, defaultSelection, findRow, type Row } from './rows'
import { latestPendingAction, type BarState } from './store'

function isTypingKey(e: KeyboardEvent): boolean {
  if (e.metaKey || e.ctrlKey) return false
  if (e.altKey && e.key === ' ') return false
  return e.key.length === 1 || e.key === 'Backspace' || e.key === 'Delete'
}

function displayShortcut(accel: string | undefined, mac: boolean): string {
  const a = accel ?? 'Alt+Space'
  if (!mac) return a
  return a
    .replace(/Alt\+?/gi, '⌥')
    .replace(/Shift\+?/gi, '⇧')
    .replace(/(Command|Cmd)\+?/gi, '⌘')
    .replace(/(Control|Ctrl)\+?/gi, '⌃')
    .replace(/Space/i, 'Space')
}

export interface BarAppProps {
  controller: BarController
  actions: BarActions
  bridge: Bridge
}

export function BarApp({ controller, actions, bridge }: BarAppProps) {
  const engine = useEngine()
  const store = controller.store
  const state = useStore(store) as BarState
  const playback = useEngineStore((s) => s.playback)
  const endpoint = useEngineStore((s) => s.endpoint)
  const engineProblem = useEngineStore((s) => s.problem)
  const socketStatus = useEngineStore((s) => s.status)
  const [recents, setRecents] = useState<DocumentOut[]>([])
  const [search, setSearch] = useState<DocumentOut[] | null>(null)
  const [partialAnnouncement, setPartialAnnouncement] = useState('')
  const rootRef = useRef<HTMLElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const mac = bridge.platform === 'darwin'

  const groups = buildRows(state, { playback, recents, search })
  const selected = defaultSelection(groups, state)
  const onboarding = state.open && state.settings !== null && state.settings.onboarding_completed === false

  // ---------------------------------------------------------------- data: settings, recents, search

  useEffect(() => {
    if (!state.open || !endpoint) return
    const client = engine.client
    if (!client) return
    let cancelled = false
    void unwrap(client.GET('/api/v1/settings')).then(
      (settings) => !cancelled && store.setState({ settings }),
      () => undefined,
    )
    void unwrap(client.GET('/api/v1/documents', { params: { query: { limit: 10 } } })).then(
      (list) => !cancelled && setRecents(list.items),
      () => undefined,
    )
    void Promise.all([unwrap(client.GET('/api/v1/keys')), unwrap(client.GET('/api/v1/health'))]).then(
      ([keys, health]) => {
        if (cancelled) return
        const canListen = keys.openai === 'configured' || keys.gemini === 'configured' || health.fixture_providers === true
        store.setState({ canListen })
      },
      () => undefined,
    )
    return () => {
      cancelled = true
    }
  }, [state.open, endpoint, engine, store])

  const query = state.mode === 'typing' && !state.dropped ? state.text.trim() : ''
  useEffect(() => {
    if (!query) {
      setSearch(null)
      return
    }
    const client = engine.client
    if (!client) return
    let cancelled = false
    const t = setTimeout(() => {
      // Typing filters titles and full text (FR-BAR-8).
      void unwrap(client.GET('/api/v1/documents', { params: { query: { q: query, limit: 10 } } })).then(
        (list) => !cancelled && setSearch(list.items),
        () => !cancelled && setSearch([]),
      )
    }, 150)
    return () => {
      cancelled = true
      clearTimeout(t)
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
    if (state.open && !state.keyEntry && !onboarding) inputRef.current?.focus()
  }, [state.open, state.keyEntry, onboarding, state.mode])

  useEffect(() => {
    // Push-to-talk: the first keyup of the held hotkey (Space or Option) inside the focused bar.
    const onKeyUp = (e: globalThis.KeyboardEvent) => {
      if (e.key === ' ' || e.key === 'Alt') controller.hotkeyReleased()
    }
    window.addEventListener('keyup', onKeyUp)
    return () => window.removeEventListener('keyup', onKeyUp)
  }, [controller])

  useEffect(() => {
    // Rate-limited announcement of live partials (finals and replies go through `announcement`).
    if (state.mode !== 'listening' || !state.text) return
    const t = setTimeout(() => setPartialAnnouncement(state.text), 1500)
    return () => clearTimeout(t)
  }, [state.mode, state.text])

  useEffect(() => {
    actions.pickFile = () =>
      new Promise<File | null>((resolve) => {
        const input = fileRef.current
        if (!input) return resolve(null)
        input.value = ''
        input.onchange = () => resolve(input.files?.[0] ?? null)
        input.click()
      })
    return () => {
      actions.pickFile = null
    }
  }, [actions])

  // ---------------------------------------------------------------- rows

  const handlers: RowHandlers = {
    onSelect: (row) => void actions.runRow(row, 'primary'),
    onSecondary: (row) => void actions.runRow(row, 'secondary'),
    onTertiary: (row) => void actions.runRow(row, 'tertiary'),
    onCancelAction: (action) => void actions.cancelAction(action.actionId),
  }

  const close = () => {
    controller.close()
    bridge.hideBar()
  }

  /** Esc always backs out one level (09 § Accessibility), then closes the bar. */
  const escape = () => {
    const s = store.getState()
    if (s.keyEntry) return store.setState({ keyEntry: false })
    if (s.localConfirm) return store.setState({ localConfirm: null })
    const pending = latestPendingAction(s)
    if (pending) return void actions.cancelAction(pending.actionId)
    if (s.dropped) return store.setState({ dropped: null })
    if (s.mode === 'typing' && s.text) return controller.setText('')
    close()
  }

  const onRootKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    const s = store.getState()
    if (e.key === 'Escape') {
      e.preventDefault()
      escape()
      return
    }
    if (e.key === 'Tab' && !e.shiftKey && !e.metaKey && !e.ctrlKey && e.target === inputRef.current) {
      e.preventDefault()
      controller.toggle()
      return
    }
    if (e.key !== 'Enter' || e.nativeEvent.isComposing) return
    const row: Row | undefined = findRow(groups, selected)
    if (s.mode === 'listening') {
      e.preventDefault()
      // With nothing said yet, Enter answers a pending confirmation instead of submitting silence.
      if (!s.text.trim() && row?.kind === 'action' && row.action.status === 'pending') {
        controller.keystroke()
        void actions.runRow(row, 'primary')
      } else {
        controller.submit('enter')
      }
      return
    }
    if (s.mode === 'finalizing' || s.mode === 'waiting') {
      e.preventDefault()
      return
    }
    if (!row) {
      e.preventDefault()
      controller.submit('enter')
      return
    }
    if (e.metaKey || e.ctrlKey) {
      e.preventDefault()
      void actions.runRow(row, 'secondary')
    } else if (e.shiftKey) {
      e.preventDefault()
      void actions.runRow(row, 'tertiary')
    }
    // Plain Enter: cmdk calls the selected row's onSelect.
  }

  const onInputKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.repeat && (e.key === ' ' || e.key === 'Alt') && controller.openedViaHotkey) {
      // Auto-repeat of the still-held hotkey: not typing (and ⌥Space would insert a no-break space).
      e.preventDefault()
      return
    }
    const s = store.getState()
    if (s.mode === 'listening' && isTypingKey(e)) controller.keystroke()
    if ((e.key === 'Home' || e.key === 'End') && s.mode === 'typing') e.stopPropagation() // caret, not rows
  }

  const onPaste = (e: ClipboardEvent<HTMLInputElement>) => {
    const file = e.clipboardData.files?.[0]
    if (file) {
      e.preventDefault()
      store.setState({ dropped: itemFromFile(file), selected: 'dropped:read' })
      return
    }
    const classified = classifyPastedText(e.clipboardData.getData('text/plain'))
    if (!classified) {
      if (store.getState().mode === 'listening') controller.keystroke()
      return
    }
    e.preventDefault()
    if (classified.kind === 'key') {
      // Never put a key in the transcript (it would be sent to the assistant).
      if (store.getState().mode === 'listening') controller.keystroke()
      void actions.saveKey(classified.key)
      return
    }
    if (store.getState().mode === 'listening') controller.keystroke()
    store.setState({ dropped: classified, selected: 'dropped:read' })
  }

  const onDrop = (e: DragEvent<HTMLElement>) => {
    const item = itemFromDataTransfer(e.dataTransfer)
    if (!item) return
    e.preventDefault()
    if (store.getState().mode === 'listening') controller.keystroke()
    store.setState({ dropped: item, selected: 'dropped:read' })
  }

  const placeholder =
    state.mode === 'listening'
      ? 'Listening… say what to read, or start typing'
      : state.mode === 'finalizing'
        ? 'Got it…'
        : state.mode === 'waiting'
          ? 'Working…'
          : state.hint ??
            (state.canListen ? 'Type a command, or press Tab to talk' : 'Type what to read, or a command')

  const announcement = state.announcement?.text ?? ''
  const shortcutLabel = displayShortcut(state.settings?.shortcuts?.open_bar, mac)

  return (
    <main
      ref={rootRef}
      aria-label="Open Speech"
      data-open={state.open}
      data-mode={state.mode}
      className="overflow-hidden rounded-xl border bg-background/90 shadow-2xl backdrop-blur-xl supports-[backdrop-filter]:bg-background/80"
      onDragOver={(e) => e.preventDefault()}
      onDrop={onDrop}
    >
      <h1 className="sr-only">Open Speech</h1>
      <div className="sr-only" aria-live="polite" aria-atomic="true" data-testid="announcer">
        {announcement}
      </div>
      <div className="sr-only" aria-live="polite" aria-atomic="true">
        {state.mode === 'listening' ? partialAnnouncement : ''}
      </div>
      {(engineProblem || (socketStatus !== 'open' && socketStatus !== 'idle')) && (
        <p role="status" className="border-b px-4 py-1.5 text-xs text-muted-foreground">
          {engineProblem ?? (socketStatus === 'reconnecting' || socketStatus === 'connecting' ? 'Connecting to the engine…' : '')}
        </p>
      )}
      {onboarding ? (
        <>
          <div className="flex min-h-14 items-center gap-3 px-4">
            <MicIndicator mode={state.mode} level={state.level} error={!!state.micError} />
            <p className="flex-1 truncate text-lg text-muted-foreground" aria-live="polite">
              {state.text ||
                (state.prompting ? 'Speaking…' : state.mode === 'listening' ? 'Listening…' : state.canListen ? '' : 'Type or click to answer')}
            </p>
            <Kbd>esc</Kbd>
          </div>
          <Onboarding
            controller={controller}
            actions={actions}
            openBarShortcut={shortcutLabel}
            openBarSpoken={mac ? 'Option-Space' : shortcutLabel}
            onFinished={() =>
              store.setState((s) => ({ settings: s.settings ? { ...s.settings, onboarding_completed: true } : s.settings }))
            }
          />
        </>
      ) : (
        <Command
          label="Open Speech command bar"
          shouldFilter={false}
          loop
          vimBindings={false}
          value={selected}
          onValueChange={(value) => store.setState({ selected: value })}
          onKeyDown={onRootKeyDown}
        >
          <CommandInput
            ref={inputRef}
            value={state.text}
            onValueChange={(text) => {
              controller.setText(text)
              if (text.trim()) store.setState({ selected: 'submit' })
            }}
            readOnly={state.mode === 'finalizing' || state.mode === 'waiting'}
            placeholder={placeholder}
            aria-describedby="bar-hints"
            onKeyDown={onInputKeyDown}
            onPaste={onPaste}
            leading={<MicIndicator mode={state.mode} level={state.level} error={!!state.micError} />}
            trailing={<Kbd aria-hidden="true">{shortcutLabel}</Kbd>}
          />
          {state.keyEntry && <KeyEntry onSave={(key) => actions.saveKey(key)} onClose={() => store.setState({ keyEntry: false })} />}
          {state.micError && (
            <p role="alert" className="border-t px-4 py-1.5 text-xs text-muted-foreground">
              {state.micError}
            </p>
          )}
          <CommandList className={cn(groups.length > 0 && 'border-t')}>
            {groups.map((group) => (
              <CommandGroup key={group.id} heading={group.heading}>
                {group.rows.map((row) => (
                  <RowView key={row.value} row={row} handlers={handlers} />
                ))}
              </CommandGroup>
            ))}
          </CommandList>
          <p id="bar-hints" className="flex items-center gap-3 border-t px-4 py-1.5 text-[11px] text-muted-foreground">
            {state.mode === 'listening' ? (
              <>
                <span>Speak, or start typing</span>
                <span><Kbd>↵</Kbd> send</span>
                <span><Kbd>tab</Kbd> type</span>
              </>
            ) : (
              <>
                <span><Kbd>↵</Kbd> run</span>
                {state.canListen ? (
                  <span><Kbd>tab</Kbd> talk</span>
                ) : (
                  <button
                    type="button"
                    tabIndex={-1}
                    className="underline-offset-2 hover:text-foreground hover:underline"
                    onClick={() => store.setState({ keyEntry: true })}
                  >
                    Add an OpenAI or Gemini key to talk
                  </button>
                )}
              </>
            )}
            <span><Kbd>esc</Kbd> {latestPendingAction(state) ? 'cancel' : 'close'}</span>
            {state.busy && <span className="ml-auto" role="status">{state.busy}</span>}
          </p>
        </Command>
      )}
      <input ref={fileRef} type="file" className="hidden" tabIndex={-1} aria-hidden="true" />
    </main>
  )
}
