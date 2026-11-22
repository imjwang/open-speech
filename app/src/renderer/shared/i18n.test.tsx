import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { setLanguage } from '@shared/i18n'

import { Toolbar } from '../commandbar/components/Toolbar'
import { TooltipProvider } from './components/ui/tooltip'
import { useT } from './i18n'

afterEach(() => setLanguage('en'))

function Label() {
  const t = useT()
  return <p>{t('tool.voices')}</p>
}

describe('useT', () => {
  it('re-renders text when the language changes', () => {
    render(<Label />)
    expect(screen.getByText('Voices')).toBeInTheDocument()
    act(() => setLanguage('es'))
    expect(screen.getByText('Voces')).toBeInTheDocument()
  })

  it('gives a new function per language, so memoized components cannot keep the old labels', () => {
    const seen: Array<ReturnType<typeof useT>> = []
    function Probe() {
      seen.push(useT())
      return null
    }
    const view = render(<Probe />)
    view.rerender(<Probe />)
    expect(seen[1]).toBe(seen[0])
    act(() => setLanguage('es'))
    expect(seen.at(-1)).not.toBe(seen[0])
  })

  it('the toolbar is relabelled when the language changes', () => {
    const handlers = { attach() {}, clipboard() {}, show() {}, rules() {} }
    render(
      <TooltipProvider>
        <Toolbar handlers={handlers} />
      </TooltipProvider>,
    )
    expect(screen.getByRole('button', { name: 'Voices' })).toBeInTheDocument()
    act(() => setLanguage('es'))
    expect(screen.getByRole('button', { name: 'Voces' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Voices' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Ajustes' })).toBeInTheDocument()
  })
})
