import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { $createParagraphNode, $createTextNode, $getRoot, type LexicalEditor } from 'lexical'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import type { RuleFile, RuleFileContent, RuleFileWrite } from '@shared/api/client'
import type { ServerMessage } from '@shared/api/ws-client'
import { setLanguage } from '@shared/i18n'

import { fakeEngine, problem, type RecordedCall } from '../../../test/fakes'
import { createBrowserBridge } from '../shared/bridge'
import { TooltipProvider } from '../shared/components/ui/tooltip'
import { EngineProvider, useEngineStore, type LiveEngine } from '../shared/engine'
import { RulesApp } from './RulesApp'

const LEGACY_PREFERENCES = '<!--\nSPEECH.md: standing preferences…\n-->\n'

/** A small rules folder, served the way the engine serves it. */
function rulesFolder() {
  const files: Record<string, RuleFileContent> = {
    preferences: { id: 'preferences', kind: 'preferences', name: 'Preferences', path: '/r/SPEECH.md', description: '', enabled: true, text: LEGACY_PREFERENCES, body: '' },
    lexicon: { id: 'lexicon', kind: 'lexicon', name: 'Pronunciations', path: '/r/lexicon.csv', description: '', enabled: true, text: 'term,spoken,scope\n', body: 'term,spoken,scope\n' },
    'rule_no-footnotes': {
      id: 'rule_no-footnotes', kind: 'rule', name: 'no-footnotes', path: '/r/no-footnotes.md', description: "Don't read footnotes on arxiv.org.",
      enabled: true, tier: 'filter', scope: { sites: ['arxiv.org'], types: [], docs: [], languages: [] },
      text: '---\nname: no-footnotes\ntier: filter\n---\nSkip the footnotes on arxiv.org\n', body: 'Skip the footnotes on arxiv.org',
    },
    'rule_page-numbers': { id: 'rule_page-numbers', kind: 'preset', name: 'page-numbers', path: '/r/page-numbers.md', description: '', enabled: true, tier: 'filter', text: '---\nname: page-numbers\n---\nSkip page numbers\n', body: 'Skip page numbers' },
    rule_tables: { id: 'rule_tables', kind: 'preset', name: 'tables', path: '/r/tables.md', description: '', enabled: false, tier: 'filter', text: '---\nname: tables\n---\nSkip tables\n', body: 'Skip tables' },
  }
  const summary = ({ text: _t, body: _b, ...file }: RuleFileContent): RuleFile => file
  const idOf = (call: RecordedCall) => decodeURIComponent(call.path.split('/').pop() ?? '')
  const engine = fakeEngine({
    'GET /api/v1/rules/files': () => ({ items: Object.values(files).map(summary), errors: [] }),
    'GET /api/v1/rules/files/{id}': (call) => files[idOf(call)] ?? problem(404, 'not_found', 'No such rule.'),
    'PUT /api/v1/rules/files/{id}': (call) => {
      const file = files[idOf(call)]
      if (!file) return problem(404, 'not_found', 'No such rule.')
      const w = call.body as RuleFileWrite
      if (w.body === 'this cannot be saved') return problem(422, 'invalid_rule', "I can't work that rule out here.")
      if (w.enabled !== undefined && w.enabled !== null) file.enabled = w.enabled
      if (typeof w.body === 'string') file.body = w.body
      if (typeof w.text === 'string') file.text = file.body = w.text
      return file
    },
    'POST /api/v1/rules/files': (call) => {
      const w = call.body as RuleFileWrite
      const made: RuleFileContent = { id: 'rule_new-rule', kind: 'rule', name: 'new-rule', path: '/r/new-rule.md', description: '', enabled: true, tier: 'filter', text: `---\nname: new-rule\n---\n${w.body}\n`, body: w.body ?? '' }
      files[made.id] = made
      return made
    },
    'DELETE /api/v1/rules/{id}': (call) => {
      delete files[idOf(call)]
      return new Response(null, { status: 204 })
    },
  })
  return { engine, files }
}

function mount() {
  const { engine, files } = rulesFolder()
  useEngineStore.setState({ endpoint: { baseUrl: 'http://127.0.0.1:9999', token: 'test-token-0123456789', version: '0.1.0' } })
  render(
    <EngineProvider engine={engine}>
      <TooltipProvider>
        <RulesApp bridge={createBrowserBridge()} engine={engine as unknown as LiveEngine} />
      </TooltipProvider>
    </EngineProvider>,
  )
  const editorText = () => screen.getByTestId('plain-editor').textContent
  /** What typing does, as far as the app can tell: the editor's text changes. */
  const type = (text: string) =>
    act(() => {
      const editor = (screen.getByTestId('plain-editor') as unknown as { __lexicalEditor: LexicalEditor }).__lexicalEditor
      editor.update(
        () => {
          const paragraph = $createParagraphNode()
          paragraph.append($createTextNode(text))
          $getRoot().clear().append(paragraph)
        },
        { discrete: true },
      )
    })
  const calls = (method: string) => engine.calls.filter((c) => c.method === method)
  return { engine, files, editorText, type, calls }
}

beforeEach(() => setLanguage('en'))
afterEach(() => {
  setLanguage('en')
  useEngineStore.setState({ endpoint: null })
})

describe('the rules editor', () => {
  it('lists the files in groups and opens the preferences, empty, with an explanation instead of a template', async () => {
    const app = mount()
    expect(await screen.findByRole('button', { name: 'Preferences' })).toHaveAttribute('aria-current', 'true')
    expect(screen.getByRole('button', { name: 'Pronunciations' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'No footnotes' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Page numbers' })).toBeInTheDocument()
    // The old explanatory comment is not shown as if the user had written it.
    await waitFor(() => expect(app.editorText()).toBe(''))
    expect(screen.getByText(/Standing preferences, in your own words/)).toBeInTheDocument()
  })

  it('each rule has a switch named after it, and flipping it saves at once', async () => {
    const app = mount()
    const tables = await screen.findByRole('switch', { name: 'Tables' })
    expect(tables).not.toBeChecked()
    expect(screen.getByRole('switch', { name: 'Page numbers' })).toBeChecked()
    fireEvent.click(tables)
    await waitFor(() => expect(app.calls('PUT').at(-1)?.body).toEqual({ enabled: true }))
    expect(app.calls('PUT').at(-1)?.path).toBe('/api/v1/rules/files/rule_tables')
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Tables' })).toBeChecked())
  })

  it('a rule opens as what it says, not as its file header; typing saves by itself', async () => {
    const app = mount()
    fireEvent.click(await screen.findByRole('button', { name: 'No footnotes' }))
    await waitFor(() => expect(app.editorText()).toBe('Skip the footnotes on arxiv.org'))
    expect(screen.getByText('arxiv.org')).toBeInTheDocument()

    app.type('Skip the captions on arxiv.org')
    expect(screen.getByRole('status')).toHaveTextContent('Not saved yet')
    await waitFor(() => expect(app.calls('PUT').at(-1)?.body).toEqual({ body: 'Skip the captions on arxiv.org' }), { timeout: 3000 })
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved'))
    expect(app.editorText()).toBe('Skip the captions on arxiv.org')
  })

  it('the file can be shown as it is on disk, and is then saved whole', async () => {
    const app = mount()
    fireEvent.click(await screen.findByRole('button', { name: 'No footnotes' }))
    await waitFor(() => expect(app.editorText()).toBe('Skip the footnotes on arxiv.org'))
    fireEvent.click(screen.getByRole('button', { name: 'Show the whole file' }))
    await waitFor(() => expect(app.editorText()).toContain('tier: filter'))
    app.type('---\nname: no-footnotes\ntier: annotate\n---\nSomething else\n')
    await waitFor(() => expect(app.calls('PUT').at(-1)?.body).toHaveProperty('text'), { timeout: 3000 })
    expect((app.calls('PUT').at(-1)?.body as RuleFileWrite).body).toBeUndefined()
  })

  it('a new rule is written in plain words and becomes a file when it is saved', async () => {
    const app = mount()
    fireEvent.click((await screen.findAllByRole('button', { name: 'New rule' }))[0]!)
    await waitFor(() => expect(app.editorText()).toBe(''))
    app.type('Skip the figure captions')
    await waitFor(() => expect(app.calls('POST').at(-1)?.body).toEqual({ body: 'Skip the figure captions' }), { timeout: 3000 })
    // It joins the list under its own name, switched on, and stays open in the editor.
    expect(await screen.findByRole('switch', { name: 'New rule' })).toBeChecked()
    expect(app.editorText()).toBe('Skip the figure captions')
    // Nothing is created for an empty rule.
    expect(app.calls('POST')).toHaveLength(1)
  })

  it('a rule the engine cannot apply stays in the editor with the reason, and is not lost', async () => {
    const app = mount()
    fireEvent.click(await screen.findByRole('button', { name: 'No footnotes' }))
    await waitFor(() => expect(app.editorText()).toBe('Skip the footnotes on arxiv.org'))
    app.type('this cannot be saved')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent("I can't work that rule out here."), { timeout: 3000 })
    expect(app.editorText()).toBe('this cannot be saved')
  })

  it('deleting asks once more, then removes the file', async () => {
    const app = mount()
    fireEvent.click(await screen.findByRole('button', { name: 'No footnotes' }))
    await waitFor(() => expect(app.editorText()).toBe('Skip the footnotes on arxiv.org'))
    fireEvent.click(screen.getByRole('button', { name: 'Delete this rule' }))
    expect(app.calls('DELETE')).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(app.calls('DELETE').map((c) => c.path)).toEqual(['/api/v1/rules/rule_no-footnotes']))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'No footnotes' })).toBeNull())
  })

  it('a change made elsewhere (by voice, or by hand on disk) shows up, but never over unsaved typing', async () => {
    const app = mount()
    fireEvent.click(await screen.findByRole('button', { name: 'No footnotes' }))
    await waitFor(() => expect(app.editorText()).toBe('Skip the footnotes on arxiv.org'))

    app.files['rule_no-footnotes']!.body = 'Skip the endnotes on arxiv.org'
    act(() => app.engine.socket.emit({ type: 'rules.changed', reason: 'voice' } as unknown as ServerMessage))
    await waitFor(() => expect(app.editorText()).toBe('Skip the endnotes on arxiv.org'))

    app.type('my own unsaved words')
    app.files['rule_no-footnotes']!.body = 'something from outside'
    act(() => app.engine.socket.emit({ type: 'rules.changed', reason: 'voice' } as unknown as ServerMessage))
    await new Promise((r) => setTimeout(r, 50))
    expect(app.editorText()).toBe('my own unsaved words')
  })

  it('built-in rules are named in the interface language', async () => {
    mount()
    await screen.findByRole('switch', { name: 'Tables' })
    act(() => setLanguage('es'))
    expect(await screen.findByRole('switch', { name: 'Tablas' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Números de página' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Nueva regla' }).length).toBeGreaterThan(0)
  })
})
