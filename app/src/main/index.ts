/**
 * Open Speech shell — main process (03 § Shell). Supervises the engine, owns the tray, global
 * shortcuts and windows, and relays OS features (clipboard, file dialog, external links) to the
 * renderers. It holds no business logic: every action is an engine API call.
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'

import { app, dialog, ipcMain, Notification, shell } from 'electron'

import { isEngineError, unwrap, type UserSettings } from '../shared/api/client'
import type { PlaybackState } from '../shared/api/ws.gen'
import type { EngineInfo, HtmlBlocksMessage, ReaderTarget } from '../shared/ipc'
import { postClipboard, readClipboard } from './clipboard'
import { EngineLink } from './engine-link'
import { resolveSpawnSpec } from './engine/spawn-spec'
import { EngineSupervisor, type ChildLike } from './engine/supervisor'
import { registerIpc } from './ipc'
import { rulesDir } from './paths'
import { appPageUrl, installAppProtocol, registerAppSchemePrivileges } from './protocol'
import { toDomAnchor, worthShowing } from './reader-policy'
import { installSecurity, openExternalSafely, shellOrigins } from './security'
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

function pageUrl(page: PageName, query: Record<string, string> = {}): string {
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
  let playback: PlaybackState | null = null
  let attention: string | null = null
  let engineDown = true
  let listening = false
  let lastPlayingDoc: string | null = null

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
  })

  installSecurity({
    devServerUrl,
    micAllowed: (contents) => windows.isBar(contents),
    isWebView: (contents) => windows.isWebPage(contents),
  })

  // ---------------------------------------------------------------- actions

  const openBar = (documentId?: string, viaHotkey = false) =>
    windows.showBar({ listen: true, viaHotkey, ...(documentId ? { documentId } : {}) })

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
      else await unwrap(c.POST('/api/v1/playback/play', { body: {} }))
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

  const ruleList = async () => {
    const c = client()
    return c ? (await unwrap(c.GET('/api/v1/rules', { params: { query: {} } }))).items : []
  }

  const openRuleFile = async (ruleId: string) => {
    try {
      const rule = (await ruleList()).find((r) => r.id === ruleId)
      // Only paths the engine reports for its own rule files are opened.
      if (rule?.path && path.isAbsolute(rule.path) && /\.(md|csv)$/i.test(rule.path) && existsSync(rule.path)) {
        const err = await shell.openPath(rule.path)
        if (err) log('warn', `open rule file: ${err}`)
      }
    } catch (err) {
      reportError('Could not open the rule file', err)
    }
  }

  const openRulesFolder = async () => {
    let dir = rulesDir(process.env, process.platform)
    try {
      const first = (await ruleList()).find((r) => r.path && path.isAbsolute(r.path))
      if (first?.path) dir = path.dirname(first.path)
    } catch {
      // fall back to the default location
    }
    const err = await shell.openPath(dir)
    if (err) log('warn', `open rules folder: ${err}`)
  }

  const openFileDialog = async () => {
    const parent = windows.barWindow()
    const result = await windows.withBlurSuppressed(() =>
      parent
        ? dialog.showOpenDialog(parent, {
            properties: ['openFile'],
            filters: [
              { name: 'Documents', extensions: ['pdf', 'docx', 'txt', 'md', 'markdown', 'html', 'htm'] },
              { name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'webp', 'gif'] },
              { name: 'All files', extensions: ['*'] },
            ],
          })
        : dialog.showOpenDialog({ properties: ['openFile'] }),
    )
    const file = result.canceled ? undefined : result.filePaths[0]
    return file ? { path: file } : null
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
        if (c) unwrap(c.POST('/api/v1/playback/play', { body: { document_id: documentId } })).catch((e: unknown) => reportError('Play failed', e))
      },
      loadRecents: async () => {
        const c = client()
        if (!c) return []
        const list = await unwrap(c.GET('/api/v1/documents', { params: { query: { limit: 10 } } }))
        return list.items.map((d) => ({ id: d.id, title: d.title, progress: d.progress }))
      },
      openRulesFolder: () => void openRulesFolder(),
      quit: () => app.quit(),
    },
  )
  tray.create()

  function refreshTray(): void {
    const playing = playback?.state === 'playing' || playback?.state === 'loading'
    const detail = engineDown ? 'engine not running' : attention
    tray.update({ listening, playing, attention: engineDown || attention !== null }, detail)
  }

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
  link.on('error', (msg) => {
    const detail = typeof msg.problem.detail === 'string' ? msg.problem.detail : null
    if (detail && !windows.barVisible) {
      attention = detail
      refreshTray()
    }
  })

  // ---------------------------------------------------------------- settings → shortcuts

  const applySettings = async () => {
    const c = client()
    if (!c) return
    try {
      const settings: UserSettings = await unwrap(c.GET('/api/v1/settings'))
      const result = shortcuts.apply(settings.shortcuts ?? null, {
        openBar: () => openBar(undefined, true),
        playPause: () => void playPause(),
        readClipboard: () => void readClipboardHotkey(),
        skip: (n) => void skip(n),
      })
      if (result?.failed.length) log('warn', `shortcuts not registered: ${result.failed.join(', ')}`)
    } catch (err) {
      log('warn', `loading settings failed: ${(err as Error).message}`)
    }
  }
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
    isShell: (s) => windows.isBar(s) || windows.isReader(s),
    isBar: (s) => windows.isBar(s),
    isWebPage: (s) => windows.isWebPage(s),
    getEngine: () => link.endpoint,
    hideBar: () => windows.hideBar(),
    resizeBar: (h) => windows.resizeBar(h),
    setListening: (value) => {
      listening = value
      refreshTray()
    },
    openBar: (req) => windows.showBar({ listen: req.listen ?? true, viaHotkey: false, ...(req.documentId ? { documentId: req.documentId } : {}) }),
    openReader,
    openExternal: openExternalSafely,
    readClipboard,
    openFileDialog,
    openRuleFile: (id) => void openRuleFile(id),
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
