/**
 * The shell's windows (03 § App windows): the command bar (frameless, transparent, vibrancy on macOS,
 * created hidden at startup so it appears ≤ 150 ms after the hotkey), the reader (on demand, thin title
 * strip) and the rules editor (on demand). The live web page inside the reader is managed by `WebPageView`.
 * Window chrome follows the app's theme (lavender or ember), like the pages inside.
 *
 * Opening the bar never shows a stale frame (09 § Command bar): the window is shown at opacity 0, the page
 * renders the new open and answers `bar:shown` once that state has painted, and only then is the window sized
 * to it and made visible (or after `REVEAL_FALLBACK_MS` if the page doesn't answer).
 *
 * The hotkey while the bar is up: while the assistant speaks it interrupts (`bargeIn`: the bar stops the voice
 * and listens); while the bar listens it closes the bar; otherwise (idle, working, typing) it talks again.
 */
import { BrowserWindow, nativeTheme, screen, type WebContents } from 'electron'

import { IPC, type BarOpenedEvent, type BarShownEvent, type WebViewState } from '../shared/ipc-channels'
import type { EngineInfo, ReaderTarget, WebModeRequest } from '../shared/ipc'
import { WebPageView, type WebPageViewDeps } from './web-view'

export const BAR_WIDTH = 680
const BAR_MIN_HEIGHT = 72
const BAR_INITIAL_HEIGHT = 96
/** Show the bar anyway if its page hasn't said it painted the new open by then. */
export const REVEAL_FALLBACK_MS = 150
export const READER_STRIP_HEIGHT = 40

export type PageName = 'commandbar' | 'reader' | 'rules'
export type ThemeName = 'lavender' | 'ember'

/** What the window shows before its page has painted, per theme (matches --background in globals.css). */
const SURFACE: Record<ThemeName, { window: string; page: string }> = {
  lavender: { window: '#f8f5ff', page: '#ffffff' },
  ember: { window: '#15110e', page: '#15110e' },
}

export interface WindowManagerOptions {
  preloadPath: string
  pageUrl: (page: PageName, query?: Record<string, string>) => string
  isMac: boolean
  /** Hide the bar when it loses focus (Spotlight-like). Off for debugging. */
  hideOnBlur: boolean
  /** Use an NSPanel for the bar on macOS (floats over full-screen apps). */
  macPanel: boolean
  web: Omit<WebPageViewDeps, 'sendState'>
  onBarVisibility: (visible: boolean) => void
  /** What the shell knows when the bar opens: can it listen (for `BarOpenedEvent.canListen`), is the assistant
   *  speaking (`assistant.speech`), is the bar listening (`bar:listening`). */
  barContext?: () => { canListen: boolean | null; speaking: boolean; listening: boolean; recording?: boolean }
}

function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n))
}

export class WindowManager {
  private bar: BrowserWindow | null = null
  private reader: BrowserWindow | null = null
  private rules: BrowserWindow | null = null
  private theme: ThemeName = nativeTheme.shouldUseDarkColors ? 'ember' : 'lavender'
  private readonly webPage: WebPageView
  private barOpenedAt = 0
  private openSeq = 0
  /** The open waiting for its first painted frame (the window is shown at opacity 0 until then). */
  private pendingReveal: { openId: number; timer: ReturnType<typeof setTimeout> } | null = null
  private suppressBlur = 0
  private engine: EngineInfo | null = null
  /** The bar's page has loaded (its `bar:opened` listener exists). */
  private barLoaded = false
  /** An open asked for before the bar's page had loaded (the first launch). */
  private queuedOpen: Omit<BarOpenedEvent, 'openId'> | null = null

  constructor(private readonly opts: WindowManagerOptions) {
    this.webPage = new WebPageView({
      ...opts.web,
      sendState: (state: WebViewState) => this.reader?.webContents.send(IPC.webState, state),
    })
  }

  // ---------------------------------------------------------------- identity checks (IPC, permissions)

  isBar(contents: { id: number } | null | undefined): boolean {
    return !!contents && !!this.bar && !this.bar.isDestroyed() && contents.id === this.bar.webContents.id
  }

  isReader(contents: { id: number } | null | undefined): boolean {
    return !!contents && !!this.reader && !this.reader.isDestroyed() && contents.id === this.reader.webContents.id
  }

  isRules(contents: { id: number } | null | undefined): boolean {
    return !!contents && !!this.rules && !this.rules.isDestroyed() && contents.id === this.rules.webContents.id
  }

  isWebPage(contents: { id: number } | null | undefined): boolean {
    return this.webPage.owns(contents)
  }

  /** The theme changed (settings, or the Mac's appearance while following it): recolour the window chrome. */
  setTheme(theme: ThemeName): void {
    if (theme === this.theme) return
    this.theme = theme
    const bar = this.barWindow()
    if (bar && this.opts.isMac) bar.setVibrancy(theme === 'ember' ? 'hud' : 'popover')
    this.readerWindow()?.setBackgroundColor(SURFACE[theme].page)
    this.rulesWindow()?.setBackgroundColor(SURFACE[theme].window)
  }

  get barVisible(): boolean {
    return !!this.bar && !this.bar.isDestroyed() && this.bar.isVisible()
  }

  get webDocumentId(): string | null {
    return this.webPage.documentId
  }

  get web(): WebPageView {
    return this.webPage
  }

  // ---------------------------------------------------------------- command bar

  createBar(): BrowserWindow {
    if (this.bar && !this.bar.isDestroyed()) return this.bar
    const { isMac } = this.opts
    const bar = new BrowserWindow({
      width: BAR_WIDTH,
      height: BAR_INITIAL_HEIGHT,
      show: false,
      frame: false,
      transparent: true,
      backgroundColor: '#00000000',
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      alwaysOnTop: true,
      hasShadow: true,
      title: 'Open Speech',
      ...(isMac
        ? {
            vibrancy: this.theme === 'ember' ? ('hud' as const) : ('popover' as const),
            visualEffectState: 'active' as const,
            roundedCorners: true,
            ...(this.opts.macPanel ? { type: 'panel' } : {}),
          }
        : {}),
      webPreferences: {
        preload: this.opts.preloadPath,
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        webSecurity: true,
        spellcheck: false,
        backgroundThrottling: false,
        // The capture graph (AudioContext + worklet) is built when the page loads, long before any key is
        // pressed: without this it would wait for a user gesture and the first words after the hotkey were lost.
        autoplayPolicy: 'no-user-gesture-required',
      },
    })
    bar.setAlwaysOnTop(true, 'floating')
    bar.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })
    bar.on('blur', () => {
      if (this.opts.hideOnBlur && this.suppressBlur === 0 && bar.isVisible()) this.hideBar()
    })
    bar.on('closed', () => {
      this.bar = null
    })
    this.barLoaded = false
    bar.webContents.on('did-start-loading', () => {
      this.barLoaded = false
    })
    bar.webContents.on('did-finish-load', () => {
      this.barLoaded = true
      this.sendEngine(bar.webContents)
      // An open asked for while the page was loading (the first launch): now it can hear it.
      const queued = this.queuedOpen
      this.queuedOpen = null
      if (queued) this.showBar(queued)
    })
    void bar.loadURL(this.opts.pageUrl('commandbar'))
    this.bar = bar
    return bar
  }

  showBar(request: Omit<BarOpenedEvent, 'openId'>): void {
    const bar = this.createBar()
    if (!this.barLoaded) {
      // The page can't hear `bar:opened` yet (the first launch opens the bar before it has loaded): once it has.
      this.queuedOpen = request
      return
    }
    const ctx = this.opts.barContext?.() ?? { canListen: null, speaking: false, listening: false }
    const event: BarOpenedEvent = {
      ...request,
      openId: ++this.openSeq,
      canListen: request.canListen ?? ctx.canListen,
      ...(request.viaHotkey && ctx.speaking ? { bargeIn: true } : {}),
    }
    if (bar.isVisible()) {
      // A repeat of the hotkey while it is still held (key repeat) must not toggle the bar shut.
      if (event.viaHotkey && Date.now() - this.barOpenedAt < 600) return
      // The hotkey closes a listening bar; while the assistant speaks or the bar is idle it talks instead, and
      // while a voice is being recorded for cloning it finishes the take (closing would throw the take away).
      if (event.viaHotkey && ctx.listening && !ctx.speaking && !ctx.recording) {
        this.hideBar()
        return
      }
      if (event.viaHotkey) this.barOpenedAt = Date.now()
      // Opened again before the first open was revealed: the page now answers for this open, so wait for that.
      if (this.pendingReveal) {
        this.clearReveal()
        this.pendingReveal = { openId: event.openId, timer: setTimeout(() => this.reveal(event.openId), REVEAL_FALLBACK_MS) }
      }
      bar.focus()
      bar.webContents.send(IPC.barOpened, event)
      return
    }
    const cursor = screen.getCursorScreenPoint()
    const { workArea } = screen.getDisplayNearestPoint(cursor)
    const [, height = BAR_INITIAL_HEIGHT] = bar.getSize()
    this.barOpenedAt = Date.now()
    // Invisible until the page has painted this open: the previous open's last frame is never seen.
    bar.setOpacity(0)
    bar.setBounds(
      {
        x: Math.round(workArea.x + (workArea.width - BAR_WIDTH) / 2),
        y: Math.round(workArea.y + workArea.height * 0.2),
        width: BAR_WIDTH,
        height,
      },
      false,
    )
    // Tell the renderer first so the mic and state reset start while the window maps.
    bar.webContents.send(IPC.barOpened, event)
    bar.show()
    bar.focus()
    this.clearReveal()
    this.pendingReveal = { openId: event.openId, timer: setTimeout(() => this.reveal(event.openId), REVEAL_FALLBACK_MS) }
    this.opts.onBarVisibility(true)
  }

  /**
   * The page painted open `openId` at `height`: size the window to it, then make it visible once the compositor
   * has drawn that paint. Right after a window is shown Chromium can take tens of milliseconds to draw again; a
   * frame copy completes only after a draw, so waiting for one keeps the stale frame from being the first seen.
   */
  barShown(shown: BarShownEvent): void {
    if (this.pendingReveal?.openId !== shown.openId) return
    this.resizeBar(shown.height)
    const bar = this.barWindow()
    if (!bar) return this.reveal(shown.openId)
    bar.webContents.capturePage().then(
      () => this.reveal(shown.openId),
      () => this.reveal(shown.openId),
    )
  }

  private reveal(openId: number): void {
    if (this.pendingReveal?.openId !== openId) return
    this.clearReveal()
    const bar = this.barWindow()
    if (bar?.isVisible()) bar.setOpacity(1)
  }

  private clearReveal(): void {
    if (this.pendingReveal) clearTimeout(this.pendingReveal.timer)
    this.pendingReveal = null
  }

  hideBar(): void {
    const bar = this.bar
    if (!bar || bar.isDestroyed() || !bar.isVisible()) return
    this.clearReveal()
    bar.hide()
    bar.webContents.send(IPC.barClosed)
    this.opts.onBarVisibility(false)
  }

  /** Called synchronously by the page (`bar:resize` is `sendSync`): returns the height the window now has. */
  resizeBar(height: number): number {
    const bar = this.bar
    if (!bar || bar.isDestroyed()) return 0
    const bounds = bar.getBounds()
    const { workArea } = screen.getDisplayMatching(bounds)
    const max = Math.round(workArea.height * 0.7)
    const next = clamp(Math.round(height), BAR_MIN_HEIGHT, max)
    if (next !== bounds.height) {
      bar.setBounds({ ...bounds, height: next }, false)
      // The native shadow is drawn from the window's shape: recompute it, or it keeps the old height.
      if (this.opts.isMac) bar.invalidateShadow()
    }
    return next
  }

  /** Native dialogs steal focus; keep the bar open while one is up. */
  async withBlurSuppressed<T>(fn: () => Promise<T>): Promise<T> {
    this.suppressBlur++
    try {
      return await fn()
    } finally {
      this.suppressBlur--
    }
  }

  barWindow(): BrowserWindow | null {
    return this.bar && !this.bar.isDestroyed() ? this.bar : null
  }

  // ---------------------------------------------------------------- reader

  /**
   * Show a document in the reader. While the command bar is up the reader comes up without taking focus
   * (`showInactive`): taking it would hide the bar in the middle of a conversation.
   */
  openReader(target: ReaderTarget): void {
    if (this.reader && !this.reader.isDestroyed()) {
      this.reader.webContents.send(IPC.readerShow, target)
      if (this.reader.isMinimized()) this.reader.restore()
      this.showReader(this.reader)
      return
    }
    const { isMac } = this.opts
    const reader = new BrowserWindow({
      width: 920,
      height: 1000,
      minWidth: 480,
      minHeight: 360,
      show: false,
      title: 'Open Speech Reader',
      backgroundColor: SURFACE[this.theme].page,
      ...(isMac ? { titleBarStyle: 'hiddenInset' as const, trafficLightPosition: { x: 14, y: 13 } } : {}),
      webPreferences: {
        preload: this.opts.preloadPath,
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        webSecurity: true,
        spellcheck: false,
      },
    })
    const query: Record<string, string> = {}
    if (target.documentId) query.doc = target.documentId
    if (target.url) query.url = target.url
    if (target.autoplay) query.autoplay = '1'
    reader.once('ready-to-show', () => this.showReader(reader))
    reader.on('resize', () => this.webPage.layout(reader, READER_STRIP_HEIGHT))
    reader.on('closed', () => {
      this.webPage.destroy()
      this.reader = null
    })
    reader.webContents.on('did-finish-load', () => this.sendEngine(reader.webContents))
    void reader.loadURL(this.opts.pageUrl('reader', query))
    this.reader = reader
  }

  private showReader(reader: BrowserWindow): void {
    if (this.barVisible) reader.showInactive()
    else {
      reader.show()
      reader.focus()
    }
  }

  readerWindow(): BrowserWindow | null {
    return this.reader && !this.reader.isDestroyed() ? this.reader : null
  }

  readerShowsDocument(documentId: string): boolean {
    return !!this.readerWindow() && this.lastReaderDoc === documentId
  }

  private lastReaderDoc: string | null = null

  noteReaderDocument(documentId: string | null): void {
    this.lastReaderDoc = documentId
  }

  // ---------------------------------------------------------------- rules editor

  /** Open (or bring forward) the rules editor, optionally on one file. */
  openRules(item?: string): void {
    if (this.rules && !this.rules.isDestroyed()) {
      if (item) this.rules.webContents.send(IPC.rulesShow, item)
      if (this.rules.isMinimized()) this.rules.restore()
      this.rules.show()
      this.rules.focus()
      return
    }
    const { isMac } = this.opts
    const rules = new BrowserWindow({
      width: 880,
      height: 620,
      minWidth: 640,
      minHeight: 400,
      show: false,
      title: 'Open Speech Rules',
      backgroundColor: SURFACE[this.theme].window,
      ...(isMac ? { titleBarStyle: 'hiddenInset' as const, trafficLightPosition: { x: 14, y: 13 } } : {}),
      webPreferences: {
        preload: this.opts.preloadPath,
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        webSecurity: true,
        spellcheck: true,
      },
    })
    rules.once('ready-to-show', () => rules.show())
    rules.on('closed', () => {
      this.rules = null
    })
    rules.webContents.on('did-finish-load', () => {
      this.sendEngine(rules.webContents)
      if (item) rules.webContents.send(IPC.rulesShow, item)
    })
    void rules.loadURL(this.opts.pageUrl('rules', item ? { item } : {}))
    this.rules = rules
  }

  rulesWindow(): BrowserWindow | null {
    return this.rules && !this.rules.isDestroyed() ? this.rules : null
  }

  setWebMode(request: WebModeRequest): void {
    const reader = this.readerWindow()
    if (!reader) return
    this.webPage.setMode(reader, READER_STRIP_HEIGHT, request)
  }

  // ---------------------------------------------------------------- engine endpoint fan-out

  setEngine(info: EngineInfo | null): void {
    this.engine = info
    for (const win of [this.barWindow(), this.readerWindow(), this.rulesWindow()]) {
      if (win) this.sendEngine(win.webContents)
    }
  }

  private sendEngine(contents: WebContents): void {
    contents.send(IPC.engineChanged, this.engine)
  }
}
