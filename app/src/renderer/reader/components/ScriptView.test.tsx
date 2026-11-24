import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { ScriptOut } from '@shared/api/client'
import { setLanguage } from '@shared/i18n'

import { TooltipProvider } from '@/components/ui/tooltip'
import { ScriptView } from './ScriptView'

afterEach(() => setLanguage('en'))

const script = (patch: Partial<ScriptOut>): ScriptOut => ({
  document_id: 'd',
  mode: 'ai_enhanced',
  utterances: [{ id: 'u1', index: 0, text: 'Hello there.', block_ids: ['b1'], fidelity: 'exact', is_heading: false, paragraph_start: true }],
  removed: [],
  ...patch,
})

function show(s: ScriptOut) {
  render(
    <TooltipProvider>
      <ScriptView script={s} blocks={[]} onStart={() => undefined} />
    </TooltipProvider>,
  )
  return screen.getByTestId('script-summary').querySelector('[data-cleanup]') as HTMLElement
}

describe('ScriptView: whether Claude cleaned the text', () => {
  it('says "AI cleanup" when Claude cleaned it', () => {
    const badge = show(script({ mode: 'ai_enhanced' }))
    expect(badge).toHaveAttribute('data-cleanup', 'ai')
    expect(badge).toHaveAttribute('title', 'AI cleanup')
    expect(badge).toHaveTextContent('AI cleanup')
  })

  it('says it was unavailable when Claude could not run, with the reason on hover', () => {
    const badge = show(script({ mode: 'classic', warnings: ['AI cleanup needs Claude, and this build has none.', 'Another note.'] }))
    expect(badge).toHaveAttribute('data-cleanup', 'unavailable')
    expect(badge).toHaveAttribute('title', 'AI cleanup unavailable\nAI cleanup needs Claude, and this build has none.')
    expect(badge).toHaveTextContent('AI cleanup unavailable')
  })

  it('without a reason, the tooltip is just the label, in the interface language', () => {
    setLanguage('es')
    const badge = show(script({ mode: 'classic' }))
    expect(badge).toHaveAttribute('title', 'Limpieza con IA no disponible')
  })
})
