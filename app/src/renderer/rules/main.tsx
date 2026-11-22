import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import '@/styles/globals.css'

import { getBridge } from '@/bridge'
import { TooltipProvider } from '@/components/ui/tooltip'
import { EngineProvider, LiveEngine } from '@/engine'
import { syncPrefs } from '@/prefs'
import { RulesApp } from './RulesApp'

const bridge = getBridge()

const engine = new LiveEngine()
engine.start(bridge)
syncPrefs(engine) // theme and language follow the engine

const root = document.getElementById('root')
if (root) {
  createRoot(root).render(
    <StrictMode>
      <EngineProvider engine={engine}>
        <TooltipProvider>
          <RulesApp bridge={bridge} engine={engine} />
        </TooltipProvider>
      </EngineProvider>
    </StrictMode>,
  )
}
