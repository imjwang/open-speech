/**
 * Preload for the shell's own pages (command bar, reader). Exposes a narrow, typed bridge on
 * `window.openSpeech` via contextBridge. No Node APIs reach the page; every call is an IPC message the
 * main process validates.
 */
import { contextBridge, ipcRenderer, webUtils, type IpcRendererEvent } from 'electron'

import type { OpenSpeechBridge } from '../shared/bridge-types'
import {
  IPC,
  type BarOpenedEvent,
  type ClipboardContent,
  type FileHit,
  type FileSearchResult,
  type WebViewState,
} from '../shared/ipc-channels'
import type { EngineInfo, ReaderTarget } from '../shared/ipc'

function subscribe<T>(channel: string, cb: (value: T) => void): () => void {
  const listener = (_event: IpcRendererEvent, value: T) => cb(value)
  ipcRenderer.on(channel, listener)
  return () => {
    ipcRenderer.removeListener(channel, listener)
  }
}

const bridge: OpenSpeechBridge = {
  platform: process.platform,
  getEngine: () => ipcRenderer.invoke(IPC.engineGet) as Promise<EngineInfo | null>,
  onEngineChanged: (cb) => subscribe<EngineInfo | null>(IPC.engineChanged, cb),
  hideBar: () => ipcRenderer.send(IPC.barHide),
  // Synchronous on purpose: the window must have its new height before this frame paints (no band, no clip).
  resizeBar: (height) => {
    const applied: unknown = ipcRenderer.sendSync(IPC.barResize, { height: Math.round(height) })
    return typeof applied === 'number' && applied > 0 ? applied : Math.round(height)
  },
  barShown: (openId, height) => ipcRenderer.send(IPC.barShown, { openId, height: Math.round(height) }),
  setListening: (listening, recording = false) => ipcRenderer.send(IPC.barListening, { listening, recording }),
  onBarOpened: (cb) => subscribe<BarOpenedEvent>(IPC.barOpened, cb),
  onBarClosed: (cb) => subscribe<void>(IPC.barClosed, () => cb()),
  openBar: (request = {}) => ipcRenderer.send(IPC.barOpen, request),
  openReader: (target) => ipcRenderer.send(IPC.readerOpen, target),
  onReaderShow: (cb) => subscribe<ReaderTarget>(IPC.readerShow, cb),
  setWebMode: (request) => ipcRenderer.send(IPC.webSetMode, request),
  webBackToPlayhead: () => ipcRenderer.send(IPC.webFollow, { follow: true }),
  onWebState: (cb) => subscribe<WebViewState>(IPC.webState, cb),
  openExternal: (url) => ipcRenderer.send(IPC.shellOpenExternal, { url }),
  readClipboard: () => ipcRenderer.invoke(IPC.clipboardRead) as Promise<ClipboardContent>,
  pickFiles: () => ipcRenderer.invoke(IPC.dialogOpenFiles) as Promise<FileHit[]>,
  searchFiles: (query) => ipcRenderer.invoke(IPC.fsSearch, { query }) as Promise<FileSearchResult>,
  pathForFile: (file) => {
    try {
      return webUtils.getPathForFile(file)
    } catch {
      return ''
    }
  },
  openRules: (item) => ipcRenderer.send(IPC.rulesOpen, item ? { item } : {}),
  onRulesShow: (cb) => subscribe<string | null>(IPC.rulesShow, cb),
  openRulesFolder: () => ipcRenderer.send(IPC.rulesOpenFolder),
}

contextBridge.exposeInMainWorld('openSpeech', bridge)
