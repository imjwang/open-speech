/**
 * The shell's windows (03 § App windows): the command bar (frameless, transparent, vibrancy on macOS,
 * created hidden at startup so it appears ≤ 150 ms after the hotkey), the reader (on demand, thin title
 * strip) and the rules editor (on demand). The live web page inside the reader is managed by `WebPageView`.
 * Window chrome follows the app's theme (lavender or ember), like the pages inside.
 */
import { BrowserWindow, nativeTheme, screen, type WebContents } from 'electron'

import { IPC, type BarOpenedEvent, type WebViewState } from '../shared/ipc-channels'
import type { EngineInfo, ReaderTarget, WebModeRequest } from '../shared/ipc'
import { WebPageView, type WebPageViewDeps } from './web-view'

export const BAR_WIDTH = 680
const BAR_MIN_HEIGHT = 72
const BAR_INITIAL_HEIGHT = 96
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
  private suppressBlur = 0
  private engine: EngineInfo | null = null

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
    bar.webContents.on('did-finish-load', () => this.sendEngine(bar.webContents))
    void bar.loadURL(this.opts.pageUrl('commandbar'))
    this.bar = bar
    return bar
  }

  showBar(event: BarOpenedEvent): void {
    const bar = this.createBar()
    if (bar.isVisible()) {
      // A repeat of the hotkey while it is still held (key repeat) must not toggle the bar shut.
      if (event.viaHotkey && Date.now() - this.barOpenedAt < 600) return
      if (event.viaHotkey) {
        this.hideBar()
        return
      }
      bar.focus()
      bar.webContents.send(IPC.barOpened, event)
      return
    }
    const cursor = screen.getCursorScreenPoint()
    const { workArea } = screen.getDisplayNearestPoint(cursor)
    const [, height = BAR_INITIAL_HEIGHT] = bar.getSize()
    bar.setBounds({
      x: Math.round(workArea.x + (workArea.width - BAR_WIDTH) / 2),
      y: Math.round(workArea.y + workArea.height * 0.2),
      width: BAR_WIDTH,
      height,
    })
    this.barOpenedAt = Date.now()
    // Tell the renderer first so the mic and state reset start while the window maps.
    bar.webContents.send(IPC.barOpened, event)
    bar.show()
    bar.focus()
    this.opts.onBarVisibility(true)
  }

  hideBar(): void {
    const bar = this.bar
    if (!bar || bar.isDestroyed() || !bar.isVisible()) return
    bar.hide()
    bar.webContents.send(IPC.barClosed)
    this.opts.onBarVisibility(false)
  }

  resizeBar(height: number): void {
    const bar = this.bar
    if (!bar || bar.isDestroyed()) return
    const { workArea } = screen.getDisplayMatching(bar.getBounds())
    const bounds = bar.getBounds()
    const max = Math.round(workArea.height * 0.7)
    const next = clamp(Math.round(height), BAR_MIN_HEIGHT, max)
    if (next !== bounds.height) bar.setBounds({ ...bounds, height: next })
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

  openReader(target: ReaderTarget): void {
    if (this.reader && !this.reader.isDestroyed()) {
      this.reader.webContents.send(IPC.readerShow, target)
      if (this.reader.isMinimized()) this.reader.restore()
      this.reader.show()
      this.reader.focus()
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
    reader.once('ready-to-show', () => reader.show())
    reader.on('resize', () => this.webPage.layout(reader, READER_STRIP_HEIGHT))
    reader.on('closed', () => {
      this.webPage.destroy()
      this.reader = null
    })
    reader.webContents.on('did-finish-load', () => this.sendEngine(reader.webContents))
    void reader.loadURL(this.opts.pageUrl('reader', query))
    this.reader = reader
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
