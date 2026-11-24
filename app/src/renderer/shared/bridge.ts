/**
 * The renderer's view of the host. In Electron it wraps `window.openSpeech` (shell preload). In a plain
 * browser (UI tests via `bun run dev:web`) it reads the engine from `?engine=<baseUrl>&token=<t>` (kept in
 * sessionStorage so in-app navigation keeps working) and degrades OS-only features gracefully.
 */
import type { OpenSpeechBridge } from '@shared/bridge-types'
import type { EngineInfo, ReaderTarget, WebModeRequest } from '@shared/ipc'
import type { BarOpenedEvent, ClipboardContent, FileSearchResult, WebViewState } from '@shared/ipc-channels'

export interface Capabilities {
  /** Running inside the Electron shell. */
  electron: boolean
  /** Files have paths here: the native open panel, the `@` file menu, dropped files. */
  files: boolean
  /** Reveal the rules folder in the OS. */
  rulesFolder: boolean
  /** Live web pages in a WebContentsView. */
  webView: boolean
}

export interface Bridge extends OpenSpeechBridge {
  readonly capabilities: Capabilities
}

const STORAGE_KEY = 'open-speech:engine'

function readEngineFromLocation(search: string): EngineInfo | null {
  const params = new URLSearchParams(search)
  const baseUrl = params.get('engine')
  const token = params.get('token')
  if (baseUrl && token) {
    const info = { baseUrl: baseUrl.replace(/\/+$/, ''), token, version: '' }
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(info))
    } catch {
      // storage unavailable
    }
    return info
  }
  try {
    const stored = sessionStorage.getItem(STORAGE_KEY)
    if (stored) {
      const parsed = JSON.parse(stored) as Partial<EngineInfo>
      if (typeof parsed.baseUrl === 'string' && typeof parsed.token === 'string') {
        return { baseUrl: parsed.baseUrl, token: parsed.token, version: '' }
      }
    }
  } catch {
    // ignore
  }
  return null
}

function arrayBufferToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf)
  let binary = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk))
  }
  return btoa(binary)
}

export function pageHref(
  page: 'commandbar' | 'reader' | 'rules',
  query: Record<string, string>,
  engine: EngineInfo | null,
): string {
  const params = new URLSearchParams(query)
  if (engine) {
    params.set('engine', engine.baseUrl)
    params.set('token', engine.token)
  }
  return `/${page}/index.html?${params.toString()}`
}

export function createBrowserBridge(win: Window = window): Bridge {
  const engine = readEngineFromLocation(win.location.search)
  const params = new URLSearchParams(win.location.search)
  const closedListeners = new Set<() => void>()
  return {
    platform: 'browser',
    capabilities: { electron: false, files: false, rulesFolder: false, webView: false },
    getEngine: async () => engine,
    onEngineChanged: () => () => undefined,
    hideBar: () => {
      // No window to hide: tell the page it closed (tests observe data-open="false").
      for (const cb of closedListeners) cb()
    },
    resizeBar: (height) => Math.round(height),
    barShown: () => undefined,
    setListening: () => undefined,
    onBarOpened: (cb: (e: BarOpenedEvent) => void) => {
      // Loading the page counts as opening the bar; `?listen=0` starts in typing mode (no mic prompt).
      const event: BarOpenedEvent = { openId: 1, listen: params.get('listen') !== '0', viaHotkey: false }
      const doc = params.get('doc')
      if (doc) event.documentId = doc
      const t = win.setTimeout(() => cb(event), 0)
      return () => win.clearTimeout(t)
    },
    onBarClosed: (cb) => {
      closedListeners.add(cb)
      return () => closedListeners.delete(cb)
    },
    openBar: (request = {}) => {
      const query: Record<string, string> = {}
      if (request.documentId) query.doc = request.documentId
      if (request.listen === false) query.listen = '0'
      win.open(pageHref('commandbar', query, engine), 'open-speech-bar')
    },
    openReader: (target: ReaderTarget) => {
      const query: Record<string, string> = {}
      if (target.documentId) query.doc = target.documentId
      if (target.url) query.url = target.url
      win.open(pageHref('reader', query, engine), 'open-speech-reader')
    },
    onReaderShow: () => () => undefined,
    setWebMode: (_request: WebModeRequest) => undefined,
    webBackToPlayhead: () => undefined,
    onWebState: (_cb: (s: WebViewState) => void) => () => undefined,
    openExternal: (url) => {
      if (/^(https?:|mailto:)/i.test(url)) win.open(url, '_blank', 'noopener,noreferrer')
    },
    readClipboard: async (): Promise<ClipboardContent> => {
      const clip = win.navigator.clipboard
      try {
        if (clip && 'read' in clip) {
          for (const item of await clip.read()) {
            const type = item.types.find((t) => t.startsWith('image/'))
            if (type) {
              const blob = await item.getType(type)
              return { kind: 'image', base64: arrayBufferToBase64(await blob.arrayBuffer()), mediaType: type }
            }
          }
        }
        const text = clip ? await clip.readText() : ''
        return text.trim() ? { kind: 'text', text } : { kind: 'empty' }
      } catch {
        return { kind: 'empty' }
      }
    },
    pickFiles: async () => [],
    searchFiles: async (): Promise<FileSearchResult> => ({ folder: null, hits: [] }),
    pathForFile: () => '',
    openRules: (item) => {
      win.open(pageHref('rules', item ? { item } : {}, engine), 'open-speech-rules')
    },
    onRulesShow: (cb) => {
      const t = win.setTimeout(() => cb(params.get('item')), 0)
      return () => win.clearTimeout(t)
    },
    openRulesFolder: () => undefined,
  }
}

export function createElectronBridge(api: OpenSpeechBridge): Bridge {
  return {
    ...api,
    capabilities: { electron: true, files: true, rulesFolder: true, webView: true },
  }
}

let cached: Bridge | null = null

export function getBridge(): Bridge {
  if (!cached) cached = window.openSpeech ? createElectronBridge(window.openSpeech) : createBrowserBridge()
  return cached
}

/** For tests. */
export function setBridge(bridge: Bridge | null): void {
  cached = bridge
}
