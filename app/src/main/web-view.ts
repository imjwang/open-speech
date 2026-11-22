/**
 * The live web page in the reader (03 § Highlighting paths, FR-READ-2): a `WebContentsView` in the
 * persistent `persist:web` partition (logins survive restarts), sandboxed, context-isolated, with a
 * minimal preload that extracts Readability blocks with DOM anchors and paints highlights through the
 * CSS Custom Highlight API. The page never sees the engine token: blocks go to the main process, which
 * posts them to the engine.
 */
import { WebContentsView, type BrowserWindow } from 'electron'

import { IPC, type WebViewState } from '../shared/ipc-channels'
import type { HtmlBlocksMessage, WebModeRequest, WebPaint } from '../shared/ipc'
import { WEB_PARTITION } from './security'

/** Highlight styles injected as a stylesheet (no DOM mutation in the page). */
export const WEB_HIGHLIGHT_CSS = `
::highlight(open-speech-sentence) { background-color: rgba(37, 99, 235, 0.22); }
::highlight(open-speech-word) {
  background-color: #1d4ed8;
  color: #ffffff;
  text-decoration: underline;
  text-decoration-thickness: 2px;
  text-underline-offset: 3px;
}
@media (prefers-color-scheme: dark) {
  ::highlight(open-speech-sentence) { background-color: rgba(147, 197, 253, 0.28); }
  ::highlight(open-speech-word) { background-color: #93c5fd; color: #0b1220; }
}
@media (forced-colors: active) {
  ::highlight(open-speech-word) { background-color: Highlight; color: HighlightText; }
}
`

export interface WebPageViewDeps {
  preloadPath: string
  /** Post extracted blocks to the engine; returns the new document id. */
  postBlocks(payload: HtmlBlocksMessage, play: boolean): Promise<string | null>
  sendState(state: WebViewState): void
  log(level: 'info' | 'warn' | 'error', message: string): void
}

export class WebPageView {
  private view: WebContentsView | null = null
  private state: WebViewState = {
    mode: 'text',
    url: null,
    title: '',
    loading: false,
    documentId: null,
    followReleased: false,
    error: null,
  }
  private pendingAutoplay = false
  private blocksById = new Map<string, HtmlBlocksMessage['blocks'][number]>()

  constructor(private readonly deps: WebPageViewDeps) {}

  owns(contents: { id: number } | null | undefined): boolean {
    return !!contents && !!this.view && !this.view.webContents.isDestroyed() && contents.id === this.view.webContents.id
  }

  get documentId(): string | null {
    return this.state.documentId
  }

  /** DOM anchor for a block posted from this page (to map `{block_id, char_start, char_end}`). */
  blockAnchor(blockId: string): { xpath: string; start_offset: number; end_offset: number | null } | null {
    return this.blocksById.get(blockId)?.dom_anchor ?? null
  }

  setMode(win: BrowserWindow, stripHeight: number, request: WebModeRequest): void {
    if (request.mode === 'off') {
      this.destroy(win)
      return
    }
    if (request.mode === 'text') {
      this.view?.setVisible(false)
      this.update({ mode: 'text' })
      return
    }
    const view = this.ensure(win)
    this.layout(win, stripHeight)
    view.setVisible(true)
    this.update({ mode: 'page' })
    if (request.url && request.url !== this.state.url) {
      this.pendingAutoplay = request.autoplay ?? false
      this.update({ url: request.url, documentId: request.documentId ?? null, error: null, followReleased: false })
      void view.webContents.loadURL(request.url).catch((err: Error) => {
        this.update({ error: err.message, loading: false })
      })
    }
  }

  layout(win: BrowserWindow, stripHeight: number): void {
    if (!this.view) return
    const [width = 0, height = 0] = win.getContentSize()
    this.view.setBounds({ x: 0, y: stripHeight, width, height: Math.max(0, height - stripHeight) })
  }

  paint(paint: WebPaint): void {
    if (this.view && !this.view.webContents.isDestroyed()) this.view.webContents.send(IPC.webPaint, paint)
  }

  setFollow(follow: boolean): void {
    if (this.view && !this.view.webContents.isDestroyed()) this.view.webContents.send(IPC.webFollow, { follow })
    this.update({ followReleased: !follow })
  }

  onFollowReleased(): void {
    this.update({ followReleased: true })
  }

  async onBlocks(payload: HtmlBlocksMessage): Promise<void> {
    if (payload.blocks.length === 0) {
      this.update({ error: 'No readable text found on this page.' })
      return
    }
    this.blocksById = new Map(payload.blocks.map((b) => [b.id, b]))
    const play = this.pendingAutoplay
    this.pendingAutoplay = false
    try {
      const documentId = await this.deps.postBlocks(payload, play)
      this.update({ documentId, title: payload.title || this.state.title, error: null })
    } catch (err) {
      this.deps.log('warn', `posting page blocks failed: ${(err as Error).message}`)
      this.update({ error: (err as Error).message })
    }
  }

  destroy(win?: BrowserWindow): void {
    const view = this.view
    this.view = null
    this.blocksById.clear()
    if (view) {
      try {
        win?.contentView.removeChildView(view)
      } catch {
        // window already gone
      }
      if (!view.webContents.isDestroyed()) view.webContents.close()
    }
    this.state = { ...this.state, mode: 'text', url: null, documentId: null, loading: false, error: null }
  }

  private ensure(win: BrowserWindow): WebContentsView {
    if (this.view) return this.view
    const view = new WebContentsView({
      webPreferences: {
        partition: WEB_PARTITION,
        preload: this.deps.preloadPath,
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        nodeIntegrationInSubFrames: false,
        webSecurity: true,
        safeDialogs: true,
        spellcheck: false,
      },
    })
    const wc = view.webContents
    // Author origin: Chromium ignores ::highlight() rules in user-origin sheets. Still no DOM mutation.
    wc.on('dom-ready', () => void wc.insertCSS(WEB_HIGHLIGHT_CSS, { cssOrigin: 'author' }))
    wc.on('did-start-loading', () => this.update({ loading: true }))
    wc.on('did-stop-loading', () => this.update({ loading: false }))
    wc.on('page-title-updated', (_e, title) => this.update({ title }))
    wc.on('did-navigate', (_e, url) => {
      // A new page means new blocks and a new document.
      this.blocksById.clear()
      this.update({ url, documentId: null, error: null, followReleased: false })
    })
    wc.on('did-fail-load', (_e, code, description, _url, isMainFrame) => {
      if (isMainFrame && code !== -3) this.update({ error: description || `Load failed (${code})`, loading: false })
    })
    win.contentView.addChildView(view)
    this.view = view
    return view
  }

  private update(patch: Partial<WebViewState>): void {
    this.state = { ...this.state, ...patch }
    this.deps.sendState(this.state)
  }
}
