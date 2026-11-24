/**
 * Open Speech shell — main process (03 § Shell). Supervises the engine, owns the tray, global
 * shortcuts and windows, and relays OS features (clipboard, file dialog, external links) to the
 * renderers. It holds no business logic: every action is an engine API call.
 */
import { execFile, spawn } from 'node:child_process'
import { readdir, stat } from 'node:fs/promises'
import { homedir } from 'node:os'
import path from 'node:path'

import { app, dialog, ipcMain, nativeTheme, Notification, shell } from 'electron'

import { isEngineError, unwrap, type UserSettings } from '../shared/api/client'
import type { PlaybackState } from '../shared/api/ws.gen'
import { setLanguage, t } from '../shared/i18n'
import type { EngineInfo, FileHit, HtmlBlocksMessage, ReaderTarget } from '../shared/ipc'
import { postClipboard, readClipboard } from './clipboard'
import { EngineLink } from './engine-link'
import { resolveSpawnSpec } from './engine/spawn-spec'
import { EngineSupervisor, type ChildLike } from './engine/supervisor'
import { fileKind, searchFiles, type FileSearchDeps } from './file-search'
import { registerIpc } from './ipc'
import { rulesDir } from './paths'
import { appPageUrl, installAppProtocol, registerAppSchemePrivileges } from './protocol'
import { toDomAnchor, worthShowing } from './reader-policy'
import { installSecurity, openExternalSafely, shellOrigins } from './security'
import { canListenFallback, ShellStateStore, shellStatePath, type ThemePref } from './shell-state'
import { ShortcutManager } from './shortcuts'
import { TrayController } from './tray'
import { WindowManager, type PageName } from './windows'

const isMac = process.platform === 'darwin'
const devServerUrl = process.env.ELECTRON_RENDERER_URL
const here = import.meta.dirname
const preloadPath = path.join(here, '../preload/index.cjs')
const webPreloadPath = path.join(here, '../preload/web.cjs')
const rendererDir = path.join(here, '../renderer')

function log(level: 'info' | 'warn' | 'error', message: string): void {
  const line = `[open-speech] ${message}`
  if (level === 'error') console.error(line)
  else if (level === 'warn') console.warn(line)
  else console.log(line)
}

/** The theme preference pages paint their first frame in (renderer/shared/prefs.ts reads `theme=`). */
let pageTheme: ThemePref = 'system'

function pageUrl(page: PageName, extra: Record<string, string> = {}): string {
  const query: Record<string, string> = { ...extra, theme: pageTheme }
  // Unpackaged builds count renders for the UI driver (renderer/shared/debug.ts).
  if (!app.isPackaged) query.debug = '1'
  if (devServerUrl) {
    const qs = new URLSearchParams(query).toString()
    return `${devServerUrl.replace(/\/$/, '')}/${page}/index.html${qs ? `?${qs}` : ''}`
  }
  return appPageUrl(page, query)
}

function notify(title: string, body: string): void {
  if (Notification.isSupported()) new Notification({ title, body, silent: true }).show()
}

registerAppSchemePrivileges()

if (process.env.OPEN_SPEECH_FAKE_MIC === '1') {
  // UI automation only: a synthetic capture device, so no OS microphone prompt and no real audio.
  app.commandLine.appendSwitch('use-fake-device-for-media-stream')
  app.commandLine.appendSwitch('use-fake-ui-for-media-stream')
}

const pendingOpenFiles: string[] = []
app.on('open-file', (event, filePath) => {
  // Finder "Open with" / dock drops: route the file to the engine (03 § Main-process duties).
  event.preventDefault()
  pendingOpenFiles.push(filePath)
  flushOpenFiles?.()
})
let flushOpenFiles: (() => void) | null = null

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  void main()
}

async function main(): Promise<void> {
  await app.whenReady()
  if (isMac) app.dock?.hide() // menu-bar app: no Dock icon
  if (!devServerUrl) installAppProtocol(rendererDir)

  const link = new EngineLink()
  const shortcuts = new ShortcutManager()
  const shellState = new ShellStateStore(shellStatePath(process.env, app.getPath('userData')), undefined, (m) => log('warn', m))
  let playback: PlaybackState | null = null
  let attention: string | null = null
  let engineDown = true
  let listening = false
  /** The bar's microphone is recording a voice to clone (see windows.showBar). */
  let recording = false
  let speaking = false
  let lastPlayingDoc: string | null = null

  // The saved theme applies before any window exists, so the first frame of every page is already in it.
  const themeSource = (pref: ThemePref) => (pref === 'system' ? 'system' : pref === 'ember' ? 'dark' : 'light')
  pageTheme = shellState.get().themePref
  nativeTheme.themeSource = themeSource(pageTheme)

  const client = () => link.client

  const windows = new WindowManager({
    preloadPath,
    pageUrl,
    isMac,
    hideOnBlur: process.env.OPEN_SPEECH_BAR_KEEP_OPEN !== '1',
    macPanel: process.env.OPEN_SPEECH_BAR_PANEL !== '0',
    web: {
      preloadPath: webPreloadPath,
      log,
      postBlocks: async (payload: HtmlBlocksMessage, play: boolean) => {
        const c = client()
        if (!c) throw new Error('The engine is not running.')
        const res = await unwrap(
          c.POST('/api/v1/documents', {
            body: { kind: 'html_blocks', html_blocks: payload, play, confirm_estimate: false, image_mode: 'auto' },
          }),
        )
        windows.noteReaderDocument(res.document.id)
        return res.document.id
      },
    },
    onBarVisibility: (visible) => {
      if (visible && attention) {
        // Opening the bar acknowledges a budget warning or async error.
        attention = null
        refreshTray()
      }
    },
    barContext: () => ({ canListen: canListenFallback(shellState.get(), app.isPackaged), speaking, listening, recording }),
  })

  installSecurity({
    devServerUrl,
    // The command bar listens for commands; the rules editor takes dictation. Nothing else gets the microphone.
    micAllowed: (contents) => windows.isBar(contents) || windows.isRules(contents),
    isWebView: (contents) => windows.isWebPage(contents),
    withBlurSuppressed: (fn) => windows.withBlurSuppressed(fn),
  })

  // ---------------------------------------------------------------- actions

  const openBar = (documentId?: string, viaHotkey = false) =>
    windows.showBar({ listen: true, viaHotkey, ...(documentId ? { documentId } : {}) })

  /**
   * The first launch has no questions (09 § First run): the shell opens the bar by itself, listening, once per
   * launch while the engine says onboarding isn't completed. The bar says how to talk to it if nothing is said,
   * and marks onboarding completed after the first turn or when it closes.
   */
  let firstRunOpened = false
  const openFirstRun = () => {
    if (firstRunOpened) return
    firstRunOpened = true
    log('info', 'first run: opening the bar')
    windows.showBar({ listen: true, viaHotkey: false, firstRun: true })
  }
  /** What the engine said about listening and onboarding, remembered for the next launch's first instant. */
  const rememberEngine = (settings: UserSettings | null, canListen?: boolean) => {
    const patch: { canListen?: boolean; onboardingCompleted?: boolean } = {}
    if (canListen !== undefined) patch.canListen = canListen
    if (typeof settings?.onboarding_completed === 'boolean') patch.onboardingCompleted = settings.onboarding_completed
    shellState.update(patch)
    if (settings?.onboarding_completed === false) openFirstRun()
  }

  const reportError = (what: string, err: unknown) => {
    const detail = isEngineError(err) ? err.detail || err.code : (err as Error)?.message || String(err)
    log('warn', `${what}: ${detail}`)
    notify('Open Speech', `${what}: ${detail}`)
    attention = detail
    refreshTray()
  }

  const playPause = async () => {
    const c = client()
    if (!c) return
    try {
      if (playback?.state === 'playing' || playback?.state === 'loading') await unwrap(c.POST('/api/v1/playback/pause'))
      else if (playback?.state === 'paused') await unwrap(c.POST('/api/v1/playback/resume'))
      else await unwrap(c.POST('/api/v1/playback/play', { body: { confirm_estimate: false } }))
    } catch (err) {
      reportError('Play/pause failed', err)
    }
  }

  const skip = async (count: number) => {
    const c = client()
    if (!c) return
    try {
      await unwrap(c.POST('/api/v1/playback/skip', { body: { unit: 'sentence', count } }))
    } catch (err) {
      reportError('Skip failed', err)
    }
  }

  const readClipboardHotkey = async () => {
    const c = client()
    if (!c) return notify('Open Speech', 'The engine is still starting.')
    const content = await readClipboard()
    if (content.kind === 'empty') return notify('Open Speech', 'The clipboard is empty.')
    try {
      const res = await postClipboard(c, content)
      if (res?.estimate?.requires_confirmation && !res.playing) {
        notify('Open Speech', `Reading this costs about $${res.estimate.usd_estimate} (estimated). Open the bar to confirm.`)
        openBar(res.document.id)
      }
    } catch (err) {
      reportError('Could not read the clipboard', err)
    }
  }

  const openRulesFolder = async () => {
    let dir = rulesDir(process.env, process.platform)
    try {
      const c = client()
      const files = c ? (await unwrap(c.GET('/api/v1/rules/files'))).items : []
      const first = files.find((f) => f.path && path.isAbsolute(f.path))
      if (first) dir = path.dirname(first.path)
    } catch {
      // fall back to the default location
    }
    const err = await shell.openPath(dir)
    if (err) log('warn', `open rules folder: ${err}`)
  }

  const home = homedir()
  const fileSearch: FileSearchDeps = {
    home,
    run: (command, args, timeoutMs) =>
      new Promise((resolve, reject) => {
        execFile(command, args, { timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024 }, (err, stdout) => (err ? reject(err) : resolve(stdout)))
      }),
    readdir: (dir) => readdir(dir, { withFileTypes: true }),
    stat,
  }

  /** The paperclip: the native open panel, any number of files. */
  const pickFiles = async (): Promise<FileHit[]> => {
    const parent = windows.barWindow()
    const options: Electron.OpenDialogOptions = {
      properties: ['openFile', 'multiSelections'],
      filters: [
        { name: 'Documents', extensions: ['pdf', 'docx', 'txt', 'md', 'markdown', 'html', 'htm'] },
        { name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'webp', 'gif'] },
      ],
    }
    const result = await windows.withBlurSuppressed(() => (parent ? dialog.showOpenDialog(parent, options) : dialog.showOpenDialog(options)))
    if (result.canceled) return []
    return result.filePaths.map((file) => {
      const short = file === home ? '~' : file.startsWith(home + path.sep) ? `~${file.slice(home.length)}` : file
      return { path: file, name: path.basename(file), kind: fileKind(path.basename(file), false) ?? 'other', where: path.dirname(short), short }
    })
  }

  const openReader = (target: ReaderTarget) => {
    if (target.documentId) windows.noteReaderDocument(target.documentId)
    windows.openReader(target)
  }

  flushOpenFiles = () => {
    const c = client()
    if (!c) return
    for (const file of pendingOpenFiles.splice(0)) {
      unwrap(
        c.POST('/api/v1/documents', {
          body: { kind: 'file_path', path: file, play: true, confirm_estimate: false, image_mode: 'auto' },
        }),
      ).catch((err: unknown) => reportError(`Could not open ${path.basename(file)}`, err))
    }
  }

  // ---------------------------------------------------------------- tray

  const tray = new TrayController(
    app.isPackaged ? path.join(process.resourcesPath, 'tray') : path.join(app.getAppPath(), 'resources', 'tray'),
    {
      openBar: () => openBar(),
      playPause: () => void playPause(),
      playDocument: (documentId) => {
        const c = client()
        if (c) unwrap(c.POST('/api/v1/playback/play', { body: { document_id: documentId, confirm_estimate: false } })).catch((e: unknown) => reportError('Play failed', e))
      },
      loadRecents: async () => {
        const c = client()
        if (!c) return []
        const list = await unwrap(c.GET('/api/v1/documents', { params: { query: { limit: 10 } } }))
        return list.items.map((d) => ({ id: d.id, title: d.title, progress: d.progress }))
      },
      openRules: () => windows.openRules(),
      setShowSpend: (show) => tray.setShowSpend(shellState.update({ traySpend: show }).traySpend),
      quit: () => app.quit(),
    },
  )
  tray.setShowSpend(shellState.get().traySpend)
  tray.create()

  function refreshTray(): void {
    const playing = playback?.state === 'playing' || playback?.state === 'loading'
    const detail = engineDown ? t('tray.engine_down') : attention
    tray.update({ listening, speaking, playing, attention: engineDown || attention !== null }, detail)
  }

  // This month's spending for the tray: fetched on every (re)connect, then pushed by the engine on change.
  const refreshBudget = async () => {
    const c = client()
    if (!c) return
    try {
      tray.setBudget(await unwrap(c.GET('/api/v1/usage/budget')))
    } catch (err) {
      log('warn', `loading this month's spending failed: ${(err as Error).message}`)
    }
  }
  link.socket.onStatus((status) => {
    if (status === 'open') void refreshBudget()
    else if (speaking) {
      // The connection dropped mid-sentence: no `ended` will come for it.
      speaking = false
      refreshTray()
    }
  })
  link.on('usage.changed', (msg) => tray.setBudget(msg.budget))

  // ---------------------------------------------------------------- engine events

  link.on('playback.state', (msg) => {
    const p = msg.playback
    playback = p
    if (p.state === 'error' && p.error) attention = p.error
    refreshTray()
    const doc = p.document_id
    if (doc && doc !== lastPlayingDoc && (p.state === 'playing' || p.state === 'loading')) {
      lastPlayingDoc = doc
      if (windows.readerWindow()) return // the reader follows playback on its own
      const c = client()
      if (!c) return
      unwrap(c.GET('/api/v1/documents/{document_id}', { params: { path: { document_id: doc } } }))
        .then((d) => {
          if (worthShowing(d) && lastPlayingDoc === doc) openReader({ documentId: doc })
        })
        .catch(() => undefined)
    }
  })
  link.on('playback.word', (msg) => {
    if (!windows.webDocumentId || msg.document_id !== windows.webDocumentId) return
    const blockAnchor = (id: string) => windows.web.blockAnchor(id)
    const sentence = (msg.sentence_anchors ?? [])
      .map((a) => toDomAnchor(a, blockAnchor))
      .filter((a): a is NonNullable<typeof a> => a !== null)
    windows.web.paint({ sentence, word: toDomAnchor(msg.anchor, blockAnchor), scroll: true })
  })
  link.on('usage.alert', (msg) => {
    attention = msg.message
    refreshTray()
    if (!windows.barVisible) notify('Open Speech budget', msg.message)
  })
  // The engine is talking (a reply, a notice): the tray says so until it has finished.
  link.on('assistant.speech', (msg) => {
    const next = msg.state === 'started'
    if (next === speaking) return
    speaking = next
    refreshTray()
  })
  link.on('error', (msg) => {
    const detail = typeof msg.problem.detail === 'string' ? msg.problem.detail : null
    if (detail && !windows.barVisible) {
      attention = detail
      refreshTray()
    }
  })

  // The assistant asks for a window ("open the rules editor", "show it in the reader").
  link.on('ui.open', (msg) => {
    if (msg.target === 'rules') windows.openRules(msg.item ?? undefined)
    else if (msg.target === 'reader' && msg.item) openReader({ documentId: msg.item })
    else if (msg.target === 'bar') openBar()
  })

  // ---------------------------------------------------------------- settings → shortcuts, theme, language

  let themePref: ThemePref = pageTheme
  const applyTheme = () => {
    // Native chrome (menus, vibrancy, the web reader's scrollbars) follows the same choice as the pages.
    nativeTheme.themeSource = themeSource(themePref)
    windows.setTheme(themePref === 'system' ? (nativeTheme.shouldUseDarkColors ? 'ember' : 'lavender') : themePref)
    // Remembered for the next launch, and for pages opened from now on.
    pageTheme = shellState.update({ themePref }).themePref
  }
  nativeTheme.on('updated', applyTheme)

  const useSettings = (settings: UserSettings, language?: string) => {
    const result = shortcuts.apply(settings.shortcuts ?? null, {
      openBar: () => openBar(undefined, true),
      playPause: () => void playPause(),
      readClipboard: () => void readClipboardHotkey(),
      skip: (n) => void skip(n),
    })
    if (result?.failed.length) log('warn', `shortcuts not registered: ${result.failed.join(', ')}`)
    themePref = settings.appearance?.theme ?? 'system'
    applyTheme()
    if (language) {
      setLanguage(language) // the tray menu is built in this language when it opens
      refreshTray()
    }
  }

  // A voice command, the settings card or a hand edit changed settings: apply at once, no restart.
  link.on('settings.changed', (msg) => {
    useSettings(msg.settings as UserSettings, msg.language)
    rememberEngine(msg.settings as UserSettings)
  })
  // The local speech model finished installing (or went away): the next open knows at once.
  link.on('capabilities.changed', (msg) => rememberEngine(null, msg.capabilities.speech_input?.available))

  const applySettings = async () => {
    const c = client()
    if (!c) return
    try {
      const [settings, capabilities] = await Promise.all([unwrap(c.GET('/api/v1/settings')), unwrap(c.GET('/api/v1/capabilities'))])
      useSettings(settings, capabilities.language)
      rememberEngine(settings, capabilities.speech_input?.available)
    } catch (err) {
      log('warn', `loading settings failed: ${(err as Error).message}`)
    }
  }
  // Reading the settings is also what makes the engine notice a hand edit of settings.toml.
  const settingsTimer = setInterval(() => void applySettings(), 30_000)

  // Until the engine answers, the defaults still open the bar.
  shortcuts.apply(null, {
    openBar: () => openBar(undefined, true),
    playPause: () => void playPause(),
    readClipboard: () => void readClipboardHotkey(),
    skip: (n) => void skip(n),
  })

  const onEndpoint = (info: EngineInfo | null) => {
    link.setEndpoint(info)
    windows.setEngine(info)
    engineDown = info === null
    refreshTray()
    if (info) {
      void applySettings()
      flushOpenFiles?.()
    }
  }

  // ---------------------------------------------------------------- IPC

  registerIpc(ipcMain, {
    isShell: (s) => windows.isBar(s) || windows.isReader(s) || windows.isRules(s),
    isBar: (s) => windows.isBar(s),
    isWebPage: (s) => windows.isWebPage(s),
    getEngine: () => link.endpoint,
    hideBar: () => windows.hideBar(),
    resizeBar: (h) => windows.resizeBar(h),
    barShown: (shown) => windows.barShown(shown),
    setListening: (value, isRecording = false) => {
      listening = value
      recording = value && isRecording
      refreshTray()
    },
    openBar: (req) => windows.showBar({ listen: req.listen ?? true, viaHotkey: false, ...(req.documentId ? { documentId: req.documentId } : {}) }),
    openReader,
    openExternal: openExternalSafely,
    readClipboard,
    pickFiles,
    searchFiles: (query) => searchFiles(query, fileSearch),
    openRules: (item) => windows.openRules(item),
    openRulesFolder: () => void openRulesFolder(),
    setWebMode: (req) => {
      if (req.documentId) windows.noteReaderDocument(req.documentId)
      windows.setWebMode(req)
    },
    setWebFollow: (follow) => windows.web.setFollow(follow),
    onWebBlocks: (payload) => void windows.web.onBlocks(payload),
    onWebFollowReleased: () => windows.web.onFollowReleased(),
    warn: (m) => log('warn', m),
  })

  // ---------------------------------------------------------------- windows + engine

  windows.createBar()
  // A profile known not to be onboarded opens at once, before the engine answers: the bar listens from its
  // first frame and keeps what is said until the engine is there. A profile never seen waits for the engine.
  if (shellState.get().onboardingCompleted === false) openFirstRun()

  let supervisor: EngineSupervisor | null = null
  const attachUrl = process.env.OPEN_SPEECH_ENGINE_URL
  const attachToken = process.env.OPEN_SPEECH_ENGINE_TOKEN
  if (attachUrl && attachToken) {
    log('info', `attaching to engine at ${attachUrl}`)
    onEndpoint({ baseUrl: attachUrl.replace(/\/$/, ''), token: attachToken, version: '' })
  } else {
    const spec = resolveSpawnSpec({
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      appPath: app.getAppPath(),
      env: process.env,
      // `electron-vite dev` serves the pages from Vite; the engine must accept that origin too.
      extraOrigins: devServerUrl ? shellOrigins(devServerUrl).slice(1) : [],
    })
    supervisor = new EngineSupervisor(spec, {
      spawn: (s) =>
        spawn(s.command, s.args, {
          cwd: s.cwd,
          env: s.env as NodeJS.ProcessEnv,
          stdio: ['ignore', 'pipe', 'pipe'],
        }) as unknown as ChildLike,
      fetch: globalThis.fetch.bind(globalThis),
      log,
    })
    supervisor.onEndpoint(onEndpoint)
    supervisor.onState((state) => {
      engineDown = state !== 'running' && state !== 'unhealthy'
      refreshTray()
    })
    supervisor.start()
  }

  app.on('second-instance', () => openBar())
  app.on('activate', () => openBar())
  app.on('window-all-closed', () => {
    // Menu-bar app: keep running (and reading) with every window closed (FR-READ-17).
  })

  let quitting = false
  app.on('before-quit', (event) => {
    if (quitting) return
    quitting = true
    event.preventDefault()
    clearInterval(settingsTimer)
    shortcuts.dispose()
    tray.destroy()
    link.close()
    const done = () => app.exit(0)
    if (supervisor) void supervisor.stop().finally(done)
    else done()
  })
}
