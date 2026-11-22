import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import '@/styles/globals.css'
import './bar.css'

import { BrowserMic } from '@/audio/mic'
import { getBridge } from '@/bridge'
import { TooltipProvider } from '@/components/ui/tooltip'
import { EngineProvider, LiveEngine } from '@/engine'
import { newSessionId } from '@/ids'
import { syncPrefs, usePrefs } from '@/prefs'
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
const controller = new BarController(store, {
  send: (m) => engine.socket.send(m),
  sendAudio: (pcm) => engine.socket.sendAudio(pcm),
  mic: new BrowserMic(),
  setListening: (listening) => bridge.setListening(listening),
  endOfSpeechMs: () => usePrefs.getState().settings?.voice_input?.end_of_speech_ms ?? 800,
  newSessionId,
  newConversationId: newSessionId,
  now: () => performance.now(),
})
wireBarEvents(engine.socket, controller)
engine.socket.onStatus((status) => {
  if (status === 'open') controller.connected()
})
const actions = new BarActions(engine, controller, bridge)
// Read-only state handle and a content-free event log for UI automation and debugging.
const debugLog: string[] = []
for (const type of ['stt.partial', 'stt.final', 'assistant.turn', 'assistant.done', 'error'] as const)
  // Event types and sizes only: transcripts and replies never go into the debug log.
  engine.socket.on(type, (m) => {
    const text = (m as { text?: unknown }).text
    debugLog.push(`${Math.round(performance.now())} ${type}${typeof text === 'string' ? ` chars=${text.length}` : ''}`)
  })
store.subscribe((s, prev) => {
  if (s.mode !== prev.mode || s.prompting !== prev.prompting)
    debugLog.push(`${Math.round(performance.now())} state mode=${s.mode} prompting=${s.prompting} chars=${s.transcript.length}`)
  if (debugLog.length > 200) debugLog.splice(0, debugLog.length - 200)
})
;(window as unknown as { __openSpeechBar?: unknown }).__openSpeechBar = {
  getState: () => store.getState(),
  getPrefs: () => usePrefs.getState(),
  composer: () => actions.composer,
  log: debugLog,
}

bridge.onBarOpened((event) => controller.open(event))
bridge.onBarClosed(() => controller.close())

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
