import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import '@fontsource/atkinson-hyperlegible/400.css'
import '@fontsource/atkinson-hyperlegible/700.css'
import '@fontsource/opendyslexic/400.css'
import '@fontsource/opendyslexic/700.css'
import '@/styles/globals.css'

import { getBridge } from '@/bridge'
import { TooltipProvider } from '@/components/ui/tooltip'
import { EngineProvider, LiveEngine } from '@/engine'
import { syncPrefs } from '@/prefs'
import { ReaderApp } from './ReaderApp'

const bridge = getBridge()

const engine = new LiveEngine()
engine.start(bridge)
syncPrefs(engine) // theme, language and reader options follow the engine

const root = document.getElementById('root')
if (root) {
  createRoot(root).render(
    <StrictMode>
      <EngineProvider engine={engine}>
        <TooltipProvider>
          <ReaderApp bridge={bridge} />
        </TooltipProvider>
      </EngineProvider>
    </StrictMode>,
  )
}
