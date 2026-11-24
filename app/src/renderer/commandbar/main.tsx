import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import '@/styles/globals.css'
import './bar.css'

import { unwrap, uploadClone } from '@shared/api/client'
import { t } from '@shared/i18n'

import { BrowserMic } from '@/audio/mic'
import { BrowserReferenceRecorder } from '@/audio/reference'
import { getBridge } from '@/bridge'
import { TooltipProvider } from '@/components/ui/tooltip'
import { renderCounts } from '@/debug'
import { EngineProvider, LiveEngine, useEngineStore } from '@/engine'
import { newSessionId } from '@/ids'
import { patchSettings, syncPrefs, usePrefs } from '@/prefs'
import { BarActions } from './actions'
import { BarApp } from './BarApp'
import { BarController, wireBarEvents } from './controller'
import { createBarStore } from './store'

const bridge = getBridge()
document.documentElement.classList.toggle('in-browser', !bridge.capabilities.electron)

const engine = new LiveEngine()
engine.start(bridge)
syncPrefs(engine) // theme and language follow the engine from here on

const store = createBarStore()
// The audio graph is built now, while nobody is waiting: opening the bar then only has to open the microphone.
const mic = new BrowserMic()
void mic.prepare().catch(() => undefined) // retried on the first open

/** The open-bar shortcut as said aloud: "Option-Space" (the first-run nudge). */
function spokenShortcut(): string {
  const accel = usePrefs.getState().settings?.shortcuts?.open_bar ?? 'Alt+Space'
  const names: Record<string, Parameters<typeof t>[0]> = {
    alt: 'key.option',
    option: 'key.option',
    shift: 'key.shift',
    command: 'key.command',
    cmd: 'key.command',
    commandorcontrol: 'key.command',
    control: 'key.control',
    ctrl: 'key.control',
    space: 'key.space',
  }
  return accel
    .split('+')
    .map((part) => {
      const key = names[part.toLowerCase()]
      return key ? t(key) : part
    })
    .join('-')
}
// A content-free event log for UI automation and latency measurement (`first-pcm`, `vad-end`, `speech-started`).
const debugLog: string[] = []
const debug = (line: string) => {
  debugLog.push(`${Math.round(performance.now())} ${line}`)
  if (debugLog.length > 300) debugLog.splice(0, debugLog.length - 300)
}

// Recent documents are loaded before the bar opens (on connect, and again whenever it closes or something new
// starts playing while it is closed), so the empty bar's list is there on its first frame.
engine.trackRecents()
let playingDoc: string | null = null
useEngineStore.subscribe((s) => {
  const doc = s.playback.document_id ?? null
  if (doc === playingDoc) return
  playingDoc = doc
  if (!store.getState().open) void engine.refreshRecents()
})
const controller = new BarController(store, {
  send: (m) => engine.socket.send(m),
  sendAudio: (pcm) => engine.socket.sendAudio(pcm),
  mic,
  setListening: (listening, recording) => bridge.setListening(listening, recording),
  endOfSpeechMs: () => usePrefs.getState().settings?.voice_input?.end_of_speech_ms ?? 800,
  newSessionId,
  newConversationId: newSessionId,
  now: () => performance.now(),
  say: async (text) => {
    const client = engine.client
    if (!client) throw new Error('not connected')
    await unwrap(client.POST('/api/v1/assistant/say', { body: { text } }))
  },
  completeOnboarding: () => {
    usePrefs.setState((p) => ({ settings: p.settings ? { ...p.settings, onboarding_completed: true } : p.settings }))
    void patchSettings(engine, { onboarding_completed: true }).catch(() => undefined) // asked again next launch
  },
  spokenShortcut,
  debug,
  // A voice to clone is recorded with its own capture path (no echo cancellation, noise suppression or gain control).
  recorder: new BrowserReferenceRecorder(),
  uploadClone: async (wav, fields) => {
    const client = engine.client
    if (!client) throw new Error(t('no_engine'))
    return await uploadClone(client, wav, 'recording.wav', fields)
  },
})
wireBarEvents(engine.socket, controller)
engine.socket.onStatus((status) => {
  if (status === 'open') controller.connected()
  else controller.disconnected()
})
const actions = new BarActions(engine, controller, bridge)
for (const type of ['stt.partial', 'stt.final', 'assistant.turn', 'assistant.done', 'error'] as const)
  // Event types and sizes only: transcripts and replies never go into the debug log.
  engine.socket.on(type, (m) => {
    const text = (m as { text?: unknown }).text
    const speaks = type === 'assistant.done' ? ` speaks=${String((m as { speaks?: unknown }).speaks ?? '')}` : ''
    debug(`${type}${typeof text === 'string' ? ` chars=${text.length}` : ''}${speaks}`)
  })
store.subscribe((s, prev) => {
  if (s.mode !== prev.mode) debug(`state mode=${s.mode} chars=${s.transcript.length}`)
  if (s.engineSpeaking !== prev.engineSpeaking) debug(`engine-speaking ${s.engineSpeaking}`)
})
;(window as unknown as { __openSpeechBar?: unknown }).__openSpeechBar = {
  getState: () => store.getState(),
  /** Where the microphone's audio goes right now (drop / buffer / send) and how much is held. */
  audio: () => ({ gate: controller.gate, buffered: controller.bufferedAudio, live: mic.live }),
  getPrefs: () => usePrefs.getState(),
  composer: () => actions.composer,
  log: debugLog,
  /** React render counts per component (unpackaged builds only; the driver's `renders` op). */
  renders: () => renderCounts(),
}

bridge.onBarOpened((event) => {
  // Until this open has painted (BarApp), changes land without transitions: the orb is already the colour it
  // will be when the window becomes visible, not halfway there.
  document.documentElement.classList.add('bar-opening')
  setTimeout(() => document.documentElement.classList.remove('bar-opening'), 400) // in case it never paints
  debug(`open id=${event.openId}${event.firstRun ? ' first-run' : ''}${event.bargeIn ? ' barge-in' : ''} canListen=${String(event.canListen)}`)
  controller.open(event)
  void engine.refreshUsage() // this month's spending, fresh for the chip
})
bridge.onBarClosed(() => {
  controller.close()
  void engine.refreshRecents() // what was just opened or played is first next time
})

const root = document.getElementById('root')
if (root) {
  createRoot(root).render(
    <StrictMode>
      <EngineProvider engine={engine}>
        <TooltipProvider>
          <BarApp controller={controller} actions={actions} bridge={bridge} />
        </TooltipProvider>
      </EngineProvider>
    </StrictMode>,
  )
}
