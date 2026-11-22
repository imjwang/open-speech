import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import '@/styles/globals.css'
import './bar.css'

import { BrowserMic } from '@/audio/mic'
import { getBridge } from '@/bridge'
import { TooltipProvider } from '@/components/ui/tooltip'
import { EngineProvider, LiveEngine } from '@/engine'
import { newSessionId } from '@/ids'
import { applyTheme } from '@/theme'
import { BarActions } from './actions'
import { BarApp } from './BarApp'
import { BarController, wireBarEvents } from './controller'
import { createBarStore } from './store'

const bridge = getBridge()
document.documentElement.classList.toggle('in-browser', !bridge.capabilities.electron)
applyTheme('system')

const engine = new LiveEngine()
engine.start(bridge)

const store = createBarStore()
const controller = new BarController(store, {
  send: (m) => engine.socket.send(m),
  sendAudio: (pcm) => engine.socket.sendAudio(pcm),
  mic: new BrowserMic(),
  setListening: (listening) => bridge.setListening(listening),
  endOfSpeechMs: () => store.getState().settings?.voice_input?.end_of_speech_ms ?? 800,
  newSessionId,
  now: () => performance.now(),
})
wireBarEvents(engine.socket, controller)
const actions = new BarActions(engine, controller, bridge)

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
