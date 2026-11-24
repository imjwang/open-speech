/**
 * What the bar's controls do. Every effect is an engine call through the generated client, or a bridge call
 * for OS features. Errors become notices in the bar (nothing crashes).
 */
import { EngineApiError, unwrap, uploadDocument, type EngineClient, type PlaybackState } from '@shared/api/client'
import type { CardRow } from '@shared/api/ws.gen'

import type { Bridge } from '@/bridge'
import type { LiveEngine } from '@/engine'
import { patchSettings } from '@/prefs'
import type { ComposerHandle, ComposerValue } from './composer/Composer'
import { fromFile, fromHit, labelOf, toWire, type AttachmentItem } from './composer/attachments'
import type { BarController } from './controller'
import { cardRowEnabled, type Row } from './rows'
import type { CardModel } from './store'

export type RowModifier = 'primary' | 'secondary' | 'tertiary'
/** Toolbar presses that ask the engine for a list. The words are a fixed protocol, not interface text. */
export type Show = 'voices' | 'rules' | 'usage' | 'recents' | 'settings' | 'queue' | 'models'

function errorText(err: unknown): string {
  if (err instanceof EngineApiError) return err.detail || err.code
  if (err instanceof Error) return err.message
  return String(err)
}

function rowString(row: CardRow, key: string): string | null {
  const v = row.data?.[key]
  return typeof v === 'string' ? v : null
}

/** `{'reader.font_size': 20}` → `{reader: {font_size: 20}}` (the settings API takes a merge patch). */
export function settingPatch(key: string, value: unknown): Record<string, unknown> {
  const parts = key.split('.')
  let patch: Record<string, unknown> = { [parts.pop() as string]: value }
  while (parts.length) patch = { [parts.pop() as string]: patch }
  return patch
}

export class BarActions {
  /** The composer, once mounted. */
  composer: ComposerHandle | null = null
  /** Browser mode: the bar provides a hidden <input type=file multiple>. */
  pickFiles: (() => Promise<File[]>) | null = null

  constructor(
    private readonly engine: LiveEngine,
    private readonly controller: BarController,
    private readonly bridge: Bridge,
  ) {}

  private client(): EngineClient {
    const c = this.engine.client
    if (!c) throw new Error('The engine is not running yet.')
    return c
  }

  private async run(label: string, fn: () => Promise<void>): Promise<void> {
    this.controller.store.setState({ busy: label })
    try {
      await fn()
    } catch (err) {
      this.controller.notice('error', errorText(err))
    } finally {
      if (this.controller.store.getState().busy === label) this.controller.store.setState({ busy: null })
    }
  }

  // ---------------------------------------------------------------- the composer

  /** Enter in the composer: send what it holds (text, badges, or both). */
  async submit(value: ComposerValue): Promise<void> {
    const { text, words, attachments } = value
    if (!text && attachments.length === 0) return
    const only = attachments.length === 1 ? attachments[0] : undefined
    if (!text && only?.kind === 'url' && this.bridge.capabilities.webView) {
      // One link on its own reads as the live page in the reader: in-page highlight, the user's logins.
      this.composer?.clear()
      this.bridge.openReader({ url: only.url, autoplay: true })
      return
    }
    await this.run('…', async () => {
      const resolved = new Map<AttachmentItem, string>()
      for (const item of attachments) {
        if (item.kind !== 'upload') continue
        const res = await uploadDocument(this.client(), item.file, item.name, { play: false, confirmEstimate: false })
        resolved.set(item, res.document.id)
      }
      const shown = attachments.map((a) => ({ kind: a.kind === 'upload' ? 'file' : a.kind, title: labelOf(a) }))
      if (this.controller.submitCommand(text, toWire(attachments, resolved), { shown, label: words })) this.composer?.clear()
    })
  }

  /** The paperclip: choose files and add them as badges. */
  async attach(): Promise<void> {
    if (this.bridge.capabilities.files) {
      const hits = await this.bridge.pickFiles()
      if (hits.length) this.composer?.insertAttachments(hits.map(fromHit))
    } else {
      const files = (await this.pickFiles?.()) ?? []
      if (files.length) this.composer?.insertAttachments(files.map((f) => fromFile(f, () => '')))
    }
    this.composer?.focus()
  }

  /** A toolbar press: ask the engine to show a list. Nothing is shown as "you said". */
  show(what: Show): void {
    this.controller.submitCommand(`show ${what}`, [], { source: 'button' })
  }

  readClipboard(): void {
    this.controller.submitCommand('read my clipboard', [], { source: 'button' })
  }

  openRules(item?: string): void {
    this.bridge.openRules(item)
  }

  // ---------------------------------------------------------------- rows

  async runRow(row: Row, modifier: RowModifier = 'primary'): Promise<void> {
    switch (row.kind) {
      case 'local-confirm':
        return this.confirmLocal()
      case 'history':
        this.controller.store.setState({ showHistory: true })
        return
      case 'user':
        if (row.turn.recordingId) return this.replay(row.turn.recordingId)
        return
      case 'reply':
        if (row.turn.replyRecordingId) return this.replay(row.turn.replyRecordingId)
        return
      case 'action':
        // The passage card's Enter is its record button (pressing it confirms the consent line on the card).
        if (row.action.kind === 'clone_record') return this.recordClone(row.action.actionId)
        if (row.action.status === 'pending') return this.confirmAction(row.action.actionId)
        return
      case 'card-row':
        return this.runCardRow(row.card, row.row, modifier)
      case 'notice':
        this.controller.dismissNotice(row.notice.id)
        return
      case 'recent':
        if (modifier === 'secondary') {
          this.bridge.openReader({ documentId: row.doc.id })
          return
        }
        return this.playDocument(row.doc.id)
      case 'card':
      case 'pending':
      case 'job':
        return
    }
  }

  // ---------------------------------------------------------------- playback

  playDocument(documentId: string): Promise<void> {
    return this.run('…', async () => {
      // Resumes at the saved position (FR-BAR-8, FR-READ-14).
      await unwrap(this.client().POST('/api/v1/playback/play', { body: { document_id: documentId, confirm_estimate: false } }))
    })
  }

  // The transport strip: the user wants reading, not the conversation. Each press ends the conversation first
  // (the assistant stops talking, the microphone is released) so what it does to reading is heard at once.

  togglePlayback(p: PlaybackState): Promise<void> {
    this.controller.endConversation()
    return this.run('', async () => {
      const c = this.client()
      if (p.state === 'playing' || p.state === 'loading') await unwrap(c.POST('/api/v1/playback/pause'))
      else if (p.state === 'paused') await unwrap(c.POST('/api/v1/playback/resume'))
      else await unwrap(c.POST('/api/v1/playback/play', { body: { document_id: p.document_id ?? null, confirm_estimate: false } }))
    })
  }

  skip(count: number): Promise<void> {
    this.controller.endConversation()
    return this.run('', async () => {
      await unwrap(this.client().POST('/api/v1/playback/skip', { body: { unit: 'sentence', count } }))
    })
  }

  changeSpeed(p: PlaybackState, delta: number): Promise<void> {
    this.controller.endConversation()
    return this.run('', async () => {
      const speed = Math.max(0.5, Math.min(5, Math.round((p.speed + delta) * 100) / 100))
      await unwrap(this.client().PUT('/api/v1/playback/speed', { body: { speed } }))
    })
  }

  nextDocument(): Promise<void> {
    this.controller.endConversation()
    return this.run('', async () => {
      await unwrap(this.client().POST('/api/v1/playback/next'))
    })
  }

  /** ▶ on an utterance or reply replays it through the engine (FR-BAR-6). */
  replay(recordingId: string): Promise<void> {
    return this.run('', async () => {
      await unwrap(this.client().POST('/api/v1/recordings/{recording_id}/play', { params: { path: { recording_id: recordingId } } }))
    })
  }

  // ---------------------------------------------------------------- confirmation cards

  /**
   * A card whose answer is long or worth hearing (a voice being made, voices designed, a voice kept or deleted):
   * answered as a turn, as if "yes" or "no" had been said, so the result is spoken in the conversation.
   * The words are the protocol's, not interface text (the engine understands them in every language).
   */
  private viaTurn(actionId: string): boolean {
    return this.controller.store.getState().actions[actionId]?.details?.via_turn === true
  }

  confirmAction(actionId: string): Promise<void> {
    if (this.viaTurn(actionId)) {
      this.controller.setActionStatus(actionId, 'confirming')
      // Not sent (the engine isn't connected): the card stays answerable instead of hanging on "confirming…".
      if (!this.controller.submitCommand('yes', [], { source: 'button' })) this.controller.setActionStatus(actionId, 'pending')
      return Promise.resolve()
    }
    this.controller.setActionStatus(actionId, 'confirming')
    return this.run('', async () => {
      try {
        const res = await unwrap(
          this.client().POST('/api/v1/actions/{action_id}/confirm', { params: { path: { action_id: actionId } } }),
        )
        this.controller.setActionStatus(actionId, res.status, res.message)
      } catch (err) {
        this.controller.setActionStatus(actionId, 'pending')
        throw err
      }
    })
  }

  cancelAction(actionId: string): Promise<void> {
    if (this.viaTurn(actionId)) {
      this.controller.setActionStatus(actionId, 'cancelling')
      // Not sent (the engine isn't connected): the card stays answerable instead of hanging on "cancelling…".
      if (!this.controller.submitCommand('no', [], { source: 'button' })) this.controller.setActionStatus(actionId, 'pending')
      return Promise.resolve()
    }
    this.controller.setActionStatus(actionId, 'cancelling')
    return this.run('', async () => {
      try {
        const res = await unwrap(
          this.client().POST('/api/v1/actions/{action_id}/cancel', { params: { path: { action_id: actionId } } }),
        )
        this.controller.setActionStatus(actionId, res.status, res.message)
      } catch (err) {
        this.controller.setActionStatus(actionId, 'pending')
        throw err
      }
    })
  }

  // ---------------------------------------------------------------- cloning a voice

  /**
   * The passage card's record button. The first press confirms the card (its consent line); the engine says so
   * (`action_resolved`) and the bar records at once. Pressed again after a take that didn't work, it just records.
   */
  recordClone(actionId: string): Promise<void> {
    const action = this.controller.store.getState().actions[actionId]
    if (!action) return Promise.resolve()
    if (action.status !== 'pending') {
      this.controller.armCapture(actionId)
      return Promise.resolve()
    }
    this.controller.setActionStatus(actionId, 'confirming')
    return this.run('', async () => {
      try {
        const res = await unwrap(
          this.client().POST('/api/v1/actions/{action_id}/confirm', { params: { path: { action_id: actionId } } }),
        )
        this.controller.setActionStatus(actionId, res.status, res.message)
        if (res.status === 'confirmed') this.controller.armCapture(actionId)
      } catch (err) {
        this.controller.setActionStatus(actionId, 'pending')
        throw err
      }
    })
  }

  /** A trash button on one of the user's own voices: asked on a card first (the engine's "delete the voice …"). */
  deleteVoice(row: CardRow): void {
    const command = rowString(row, 'delete_command')
    if (command) this.controller.submitCommand(command, [], { source: 'button' })
  }

  // ---------------------------------------------------------------- cards

  async runCardRow(card: CardModel, row: CardRow, modifier: RowModifier): Promise<void> {
    switch (card.kind) {
      case 'voices': {
        // A row that is a command, not a voice ("More voices"): ask for it as if pressed on the toolbar.
        const command = rowString(row, 'command')
        if (command) {
          this.controller.submitCommand(command, [], { source: 'button' })
          return
        }
        const voiceId = rowString(row, 'voice_id') ?? row.id
        if (modifier === 'tertiary') return this.previewVoice(voiceId)
        return this.setVoice(voiceId, modifier === 'secondary' ? 'default' : 'document')
      }
      case 'rules': {
        const ruleId = rowString(row, 'rule_id') ?? row.id
        if (modifier === 'secondary') return this.openRules(ruleId)
        return this.toggleRule(card, ruleId, !cardRowEnabled(row))
      }
      case 'design': {
        // A designed candidate: ↵ keeps it (said as a command, so the result is spoken), ⇧↵ plays it.
        const recordingId = rowString(row, 'recording_id')
        if (modifier === 'tertiary' && recordingId) return this.replay(recordingId)
        const command = rowString(row, 'command')
        if (command) this.controller.submitCommand(command, [], { source: 'button' })
        return
      }
      case 'models': {
        if (row.data?.installed === true) return
        const size = typeof row.data?.size_mb === 'number' ? `${row.data.size_mb} MB` : ''
        this.controller.store.setState({
          localConfirm: { kind: 'install_model', modelId: rowString(row, 'model_id') ?? row.id, title: row.title, detail: size },
          selected: 'local-confirm',
        })
        return
      }
      case 'recents':
      case 'queue': {
        const docId = rowString(row, 'document_id') ?? row.id
        if (modifier === 'secondary') {
          this.bridge.openReader({ documentId: docId })
          return
        }
        return this.playDocument(docId)
      }
      case 'settings':
        return this.cycleSetting(card, row, modifier === 'secondary' ? -1 : 1)
      default:
        return
    }
  }

  private patchCard(cardId: string, fn: (rows: CardRow[]) => CardRow[]): void {
    this.controller.store.setState((s) => ({
      turns: s.turns.map((turn) => ({
        ...turn,
        cards: turn.cards.map((c) => (c.id === cardId ? { ...c, rows: fn(c.rows) } : c)),
      })),
    }))
  }

  setVoice(voiceId: string, scope: 'document' | 'default'): Promise<void> {
    return this.run('…', async () => {
      await unwrap(this.client().PUT('/api/v1/playback/voice', { body: { voice_id: voiceId, scope } }))
      // Move the ✓ in any voices list on screen.
      this.controller.store.setState((s) => ({
        turns: s.turns.map((turn) => ({
          ...turn,
          cards: turn.cards.map((c) => (c.kind === 'voices' ? { ...c, rows: c.rows.map((r) => ({ ...r, selected: r.id === voiceId })) } : c)),
        })),
      }))
    })
  }

  previewVoice(voiceId: string): Promise<void> {
    return this.run('…', async () => {
      await unwrap(
        this.client().POST('/api/v1/voices/{voice_id}/preview', { params: { path: { voice_id: voiceId }, query: { play: true } } }),
      )
    })
  }

  toggleRule(card: CardModel, ruleId: string, enabled: boolean): Promise<void> {
    return this.run('', async () => {
      await unwrap(this.client().PATCH('/api/v1/rules/{rule_id}', { params: { path: { rule_id: ruleId } }, body: { enabled } }))
      this.patchCard(card.id, (rows) => rows.map((r) => (r.id === ruleId ? { ...r, selected: enabled } : r)))
    })
  }

  /** A settings row: set a value directly (a segment was clicked) … */
  setSetting(card: CardModel, row: CardRow, value: unknown): Promise<void> {
    const key = rowString(row, 'key') ?? row.id
    return this.run('', async () => {
      await patchSettings(this.engine, settingPatch(key, value))
      this.patchCard(card.id, (rows) =>
        rows.map((r) => (r.id === row.id ? { ...r, value: String(value), data: { ...r.data, value, auto: false } } : r)),
      )
      // A local voice size or precision that isn't downloaded yet: offer the download right away (its size shown).
      const downloads = row.data?.downloads as Record<string, { model_id?: unknown; size?: unknown }> | undefined
      const need = downloads?.[String(value)]
      if (need && typeof need.model_id === 'string') {
        this.controller.store.setState({
          localConfirm: { kind: 'install_model', modelId: need.model_id, title: row.title, detail: typeof need.size === 'string' ? need.size : '' },
          selected: 'local-confirm',
        })
      }
    })
  }

  /** … or step to the next one (Enter on the row; ⌘Enter steps back). */
  cycleSetting(card: CardModel, row: CardRow, direction: 1 | -1): Promise<void> {
    const data = row.data ?? {}
    const options = Array.isArray(data.options) ? (data.options as unknown[]) : null
    if (options?.length) {
      const i = options.indexOf(data.value)
      return this.setSetting(card, row, options[(i + direction + options.length) % options.length])
    }
    if (typeof data.value === 'number' && typeof data.step === 'number') {
      const min = typeof data.min === 'number' ? data.min : -Infinity
      const max = typeof data.max === 'number' ? data.max : Infinity
      const next = Math.max(min, Math.min(max, Math.round((data.value + direction * data.step) * 100) / 100))
      return this.setSetting(card, row, next)
    }
    if (rowString(row, 'key') === 'defaults.voice') this.show('voices')
    return Promise.resolve()
  }

  async confirmLocal(): Promise<void> {
    const confirm = this.controller.store.getState().localConfirm
    if (!confirm) return
    this.controller.store.setState({ localConfirm: null })
    return this.run('…', async () => {
      const job = await unwrap(
        this.client().POST('/api/v1/models/local/{model_id}', { params: { path: { model_id: confirm.modelId } } }),
      )
      this.controller.onJobProgress({ type: 'job.progress', job_id: job.job_id, pct: 0, label: confirm.title })
    })
  }
}
