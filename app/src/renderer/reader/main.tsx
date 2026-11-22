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
import { applyTheme } from '@/theme'
import { ReaderApp } from './ReaderApp'
import { useReaderStore } from './store'

const bridge = getBridge()
let disposeTheme = applyTheme('system')
useReaderStore.subscribe((state, prev) => {
  const theme = state.settings?.theme ?? 'system'
  if (theme !== (prev.settings?.theme ?? 'system')) {
    disposeTheme()
    disposeTheme = applyTheme(theme)
  }
})

const engine = new LiveEngine()
engine.start(bridge)

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
