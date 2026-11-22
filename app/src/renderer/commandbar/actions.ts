/**
 * What rows do when selected. Every effect is an engine call through the generated client, or a
 * bridge call for OS features. Errors become notices in the bar (FR-SETUP-7: nothing crashes).
 */
import {
  EngineApiError,
  unwrap,
  uploadDocument,
  type CreateDocumentRequest,
  type CreateDocumentResponse,
  type EngineClient,
  type KeyTestResult,
  type PlaybackState,
} from '@shared/api/client'
import type { CardRow } from '@shared/api/ws.gen'
import type { ClipboardContent } from '@shared/ipc-channels'

import type { Bridge } from '@/bridge'
import type { EngineConnection } from '@/engine'
import { formatUsd } from '@/format'
import type { BarController } from './controller'
import { cardRowEnabled, type DroppedAction, type Row, type SuggestionId } from './rows'
import type { CardModel, DroppedItem } from './store'

export type RowModifier = 'primary' | 'secondary' | 'tertiary'

function errorText(err: unknown): string {
  if (err instanceof EngineApiError) return err.detail || err.code
  if (err instanceof Error) return err.message
  return String(err)
}

function fileToBase64(file: Blob): Promise<string> {
  return file.arrayBuffer().then((buf) => {
    const bytes = new Uint8Array(buf)
    let binary = ''
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
    return btoa(binary)
  })
}

function rowString(row: CardRow, key: string): string | null {
  const v = row.data?.[key]
  return typeof v === 'string' ? v : null
}

export class BarActions {
  private retries = new Map<string, () => Promise<void>>()
  private retrySeq = 0
  /** Browser mode: the bar provides a hidden <input type=file>. */
  pickFile: (() => Promise<File | null>) | null = null

  constructor(
    private readonly engine: EngineConnection,
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
      if (err instanceof EngineApiError && err.code === 'estimate_required') {
        this.controller.notice('warn', err.detail || 'This needs a cost estimate first.')
      } else {
        this.controller.notice('error', errorText(err))
      }
    } finally {
      if (this.controller.store.getState().busy === label) this.controller.store.setState({ busy: null })
    }
  }

  // ---------------------------------------------------------------- dispatch

  async runRow(row: Row, modifier: RowModifier = 'primary'): Promise<void> {
    switch (row.kind) {
      case 'submit':
        this.controller.submitText(row.text)
        return
      case 'local-confirm':
        return this.confirmLocal()
      case 'dropped':
        return this.runDropped(row.action, row.item)
      case 'user':
        if (row.turn.recordingId) return this.replay(row.turn.recordingId)
        return
      case 'reply':
        if (row.turn.errorCode === 'missing_key') {
          this.controller.store.setState({ keyEntry: true })
          return
        }
        if (row.turn.replyRecordingId) return this.replay(row.turn.replyRecordingId)
        return
      case 'action':
        if (row.action.status === 'pending') return this.confirmAction(row.action.actionId)
        return
      case 'card-row':
        return this.runCardRow(row.card, row.row, modifier)
      case 'card':
        if (row.card.kind === 'keys') this.controller.store.setState({ keyEntry: true })
        return
      case 'notice':
        this.controller.dismissNotice(row.notice.id)
        return
      case 'now-playing':
        if (modifier === 'secondary' && row.playback.document_id) {
          this.bridge.openReader({ documentId: row.playback.document_id })
          return
        }
        return this.togglePlayback(row.playback)
      case 'recent':
        if (modifier === 'secondary') {
          this.bridge.openReader({ documentId: row.doc.id })
          return
        }
        return this.playDocument(row.doc.id)
      case 'suggestion':
        return this.suggestion(row.id)
      case 'pending':
      case 'job':
        return
    }
  }

  // ---------------------------------------------------------------- playback

  playDocument(documentId: string): Promise<void> {
    return this.run('Starting…', async () => {
      // Resumes at the saved position (FR-BAR-8, FR-READ-14).
      await unwrap(this.client().POST('/api/v1/playback/play', { body: { document_id: documentId } }))
    })
  }

  togglePlayback(p: PlaybackState): Promise<void> {
    return this.run('', async () => {
      const c = this.client()
      if (p.state === 'playing' || p.state === 'loading') await unwrap(c.POST('/api/v1/playback/pause'))
      else if (p.state === 'paused') await unwrap(c.POST('/api/v1/playback/resume'))
      else await unwrap(c.POST('/api/v1/playback/play', { body: { document_id: p.document_id ?? null } }))
    })
  }

  /** ▶ on an utterance or reply replays it through the engine (FR-BAR-6). */
  replay(recordingId: string): Promise<void> {
    return this.run('', async () => {
      await unwrap(this.client().POST('/api/v1/recordings/{recording_id}/play', { params: { path: { recording_id: recordingId } } }))
    })
  }

  // ---------------------------------------------------------------- actions (confirmations)

  confirmAction(actionId: string): Promise<void> {
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

  // ---------------------------------------------------------------- cards

  async runCardRow(card: CardModel, row: CardRow, modifier: RowModifier): Promise<void> {
    switch (card.kind) {
      case 'voices': {
        if (row.data?.group === true) {
          this.controller.store.setState({ keyEntry: true })
          return
        }
        const voiceId = rowString(row, 'voice_id') ?? row.id
        if (modifier === 'tertiary') return this.previewVoice(voiceId)
        return this.setVoice(voiceId, modifier === 'secondary' ? 'default' : 'document')
      }
      case 'rules': {
        const ruleId = rowString(row, 'rule_id') ?? row.id
        if (modifier === 'secondary' || !this.bridge.capabilities.rulesFolder) return this.toggleRule(ruleId, !cardRowEnabled(row))
        this.bridge.openRuleFile(ruleId)
        return
      }
      case 'presets': {
        const name = rowString(row, 'name') ?? row.title
        return this.togglePreset(name, !cardRowEnabled(row))
      }
      case 'models': {
        const installed = row.data?.installed === true
        if (installed) return
        const size = typeof row.data?.size_mb === 'number' ? `${row.data.size_mb} MB download` : 'Download'
        const ram = typeof row.data?.ram_gb === 'number' ? `, needs ${row.data.ram_gb} GB RAM` : ''
        this.controller.store.setState({
          localConfirm: { kind: 'install_model', modelId: rowString(row, 'model_id') ?? row.id, title: `Install ${row.title}?`, detail: `${size}${ram}.` },
          selected: 'local-confirm',
        })
        return
      }
      case 'recents': {
        const docId = rowString(row, 'document_id') ?? row.id
        if (modifier === 'secondary') {
          this.bridge.openReader({ documentId: docId })
          return
        }
        return this.playDocument(docId)
      }
      case 'keys':
        this.controller.store.setState({ keyEntry: true })
        return
      default:
        return
    }
  }

  setVoice(voiceId: string, scope: 'document' | 'default', opts: { quiet?: boolean } = {}): Promise<void> {
    return this.run('Changing voice…', async () => {
      await unwrap(this.client().PUT('/api/v1/playback/voice', { body: { voice_id: voiceId, scope } }))
      // Move the ✓ in any voices list on screen.
      this.controller.store.setState((s) => ({
        turns: s.turns.map((t) => ({
          ...t,
          cards: t.cards.map((c) =>
            c.kind === 'voices' ? { ...c, rows: c.rows.map((r) => ({ ...r, selected: r.id === voiceId })) } : c,
          ),
        })),
      }))
      if (!opts.quiet)
        this.controller.notice('info', scope === 'default' ? 'Default voice changed.' : 'Voice changed for this document.')
    })
  }

  previewVoice(voiceId: string): Promise<void> {
    return this.run('Playing a sample…', async () => {
      await unwrap(
        this.client().POST('/api/v1/voices/{voice_id}/preview', {
          params: { path: { voice_id: voiceId }, query: { play: true } },
        }),
      )
    })
  }

  toggleRule(ruleId: string, enabled: boolean): Promise<void> {
    return this.run('', async () => {
      await unwrap(this.client().PATCH('/api/v1/rules/{rule_id}', { params: { path: { rule_id: ruleId } }, body: { enabled } }))
      this.controller.notice('info', enabled ? 'Rule turned on.' : 'Rule turned off.')
    })
  }

  togglePreset(name: string, enabled: boolean): Promise<void> {
    return this.run('', async () => {
      await unwrap(this.client().PUT('/api/v1/rules/presets', { body: { presets: { [name]: enabled } } }))
      this.controller.notice('info', `${name}: ${enabled ? 'skipped' : 'read'}.`)
    })
  }

  async confirmLocal(): Promise<void> {
    const confirm = this.controller.store.getState().localConfirm
    if (!confirm) return
    this.controller.store.setState({ localConfirm: null })
    if (confirm.kind === 'install_model') {
      return this.run('Installing…', async () => {
        const job = await unwrap(
          this.client().POST('/api/v1/models/local/{model_id}', { params: { path: { model_id: confirm.modelId } } }),
        )
        this.controller.onJobProgress({ type: 'job.progress', job_id: job.job_id, pct: 0, label: confirm.title.replace(/\?$/, '') })
      })
    }
    const retry = this.retries.get(confirm.retryId)
    this.retries.delete(confirm.retryId)
    if (retry) await this.run('Starting…', retry)
  }

  /** `PUT /keys/auto`: provider detected from the prefix, tested before saving (FR-SETUP-2). */
  async saveKey(key: string): Promise<KeyTestResult | null> {
    let result: KeyTestResult | null = null
    await this.run('Testing key…', async () => {
      result = await unwrap(this.client().PUT('/api/v1/keys/{provider}', { params: { path: { provider: 'auto' } }, body: { key } }))
    })
    const r = result as KeyTestResult | null
    if (r?.ok && (r.provider === 'openai' || r.provider === 'gemini')) this.controller.store.setState({ canListen: true })
    if (r) this.controller.notice(r.ok ? 'info' : 'error', r.ok ? `Saved your ${r.provider} key. ${r.detail}`.trim() : r.detail)
    return r
  }

  // ---------------------------------------------------------------- sources

  private async create(body: CreateDocumentRequest, again: (confirm: boolean) => Promise<CreateDocumentResponse>): Promise<CreateDocumentResponse | null> {
    let res: CreateDocumentResponse
    try {
      res = await again(false)
    } catch (err) {
      if (err instanceof EngineApiError && err.code === 'estimate_required') {
        this.offerEstimate(err.detail || 'This costs money to read.', [], async () => {
          await again(true)
        })
        return null
      }
      throw err
    }
    if (res.estimate?.requires_confirmation && !res.playing && body.play) {
      const est = res.estimate
      this.offerEstimate(`Reading this costs about ${formatUsd(est.usd_estimate)} (estimated).`, est.assumptions ?? [], async () => {
        await again(true)
      })
    }
    return res
  }

  private offerEstimate(title: string, assumptions: string[], retry: () => Promise<void>): void {
    const retryId = `r${++this.retrySeq}`
    this.retries.set(retryId, retry)
    this.controller.store.setState({
      localConfirm: { kind: 'estimate', title, detail: assumptions.slice(0, 2).join(' · ') || 'Press Enter to go ahead.', retryId },
      selected: 'local-confirm',
    })
  }

  private postJson(body: CreateDocumentRequest): Promise<CreateDocumentResponse | null> {
    return this.create(body, (confirm) =>
      unwrap(this.client().POST('/api/v1/documents', { body: { ...body, confirm_estimate: confirm } })),
    )
  }

  private upload(file: File, play: boolean): Promise<CreateDocumentResponse | null> {
    return this.create({ kind: 'file_path', play, image_mode: 'auto', confirm_estimate: false }, (confirm) =>
      uploadDocument(this.client(), file, file.name || 'upload', { play, confirmEstimate: confirm }),
    )
  }

  private async documentFor(item: DroppedItem, play: boolean, imageMode: 'auto' | 'describe' = 'auto') {
    const base = { play, confirm_estimate: false, image_mode: imageMode }
    switch (item.kind) {
      case 'file':
        if (item.isImage && imageMode === 'describe') {
          return this.postJson({
            ...base,
            kind: 'image',
            image_base64: await fileToBase64(item.file),
            media_type: item.file.type || 'image/png',
            title: item.name,
          })
        }
        return this.upload(item.file, play)
      case 'url':
        return this.postJson({ ...base, kind: 'url', url: item.url })
      case 'text':
        return this.postJson({ ...base, kind: 'text', text: item.text })
    }
  }

  runDropped(action: DroppedAction, item: DroppedItem): Promise<void> {
    this.controller.store.setState({ dropped: null })
    return this.run(action === 'read' ? 'Opening…' : 'Working…', async () => {
      if (action === 'open' || (action === 'read' && item.kind === 'url' && this.bridge.capabilities.webView)) {
        if (item.kind === 'url' && this.bridge.capabilities.webView) {
          // Live page in the reader; its Readability blocks are posted (and played) from there.
          this.bridge.openReader({ url: item.url, autoplay: action === 'read' })
          return
        }
        const res = await this.documentFor(item, false)
        if (res) this.bridge.openReader({ documentId: res.document.id })
        return
      }
      if (action === 'read' || action === 'describe') {
        await this.documentFor(item, true, action === 'describe' ? 'describe' : 'auto')
        return
      }
      // summarize: create the document, then ask the assistant about it.
      const res = await this.documentFor(item, false)
      if (res) {
        this.controller.store.setState((s) => ({ context: { ...s.context, documentId: res.document.id } }))
        this.controller.submitText('Summarize this document.')
      }
    })
  }

  readClipboard(): Promise<void> {
    return this.run('Reading clipboard…', async () => {
      const content: ClipboardContent = await this.bridge.readClipboard()
      if (content.kind === 'empty') {
        this.controller.notice('warn', 'The clipboard is empty, or this browser cannot read it.')
        return
      }
      if (content.kind === 'text') await this.postJson({ kind: 'text', text: content.text, play: true, image_mode: 'auto', confirm_estimate: false })
      else
        await this.postJson({
          kind: 'image',
          image_base64: content.base64,
          media_type: content.mediaType,
          play: true,
          image_mode: 'auto',
          confirm_estimate: false,
        })
    })
  }

  openFile(): Promise<void> {
    return this.run('Opening…', async () => {
      if (this.bridge.capabilities.fileDialog) {
        const picked = await this.bridge.openFileDialog()
        if (picked) await this.postJson({ kind: 'file_path', path: picked.path, play: true, image_mode: 'auto', confirm_estimate: false })
        return
      }
      const file = await this.pickFile?.()
      if (file) await this.upload(file, true)
    })
  }

  suggestion(id: SuggestionId): Promise<void> {
    switch (id) {
      case 'read-clipboard':
        return this.readClipboard()
      case 'open-file':
        return this.openFile()
      case 'show-voices':
        this.controller.submitText('Show voices')
        return Promise.resolve()
      case 'spend':
        this.controller.submitText('How much have I spent this month?')
        return Promise.resolve()
    }
  }
}
