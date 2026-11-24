/**
 * IPC handlers. Every payload is validated (zod) and every channel checks which window sent it:
 * shell channels only from the command bar / reader, web channels only from the live web page.
 */
import type { z } from 'zod'

import {
  BarListeningSchema,
  BarOpenSchema,
  BarResizeSchema,
  BarShownSchema,
  FileSearchSchema,
  HtmlBlocksPayloadSchema,
  IPC,
  OpenExternalSchema,
  parseIpc,
  ReaderOpenSchema,
  RulesOpenSchema,
  WebFollowSchema,
  WebModeSchema,
  type BarOpenRequest,
  type BarShownEvent,
  type ClipboardContent,
  type EngineInfo,
  type FileHit,
  type FileSearchResult,
  type HtmlBlocksMessage,
  type ReaderTarget,
  type RulesOpenRequest,
  type WebModeRequest,
} from '../shared/ipc'

export interface SenderLike {
  id: number
}

export interface IpcEventLike {
  sender: SenderLike
  /** The answer to a `sendSync`; it must be set on every path, or the page waits forever. */
  returnValue?: unknown
}

/** The subset of `ipcMain` used here (lets tests drive the handlers without Electron). */
export interface IpcMainLike {
  on(channel: string, listener: (event: IpcEventLike, ...args: unknown[]) => void): unknown
  handle(channel: string, listener: (event: IpcEventLike, ...args: unknown[]) => unknown): unknown
}

export interface IpcDeps {
  isShell(sender: SenderLike): boolean
  isBar(sender: SenderLike): boolean
  isWebPage(sender: SenderLike): boolean
  getEngine(): EngineInfo | null
  hideBar(): void
  /** Returns the height the window has now. */
  resizeBar(height: number): number
  barShown(shown: BarShownEvent): void
  setListening(listening: boolean, recording?: boolean): void
  openBar(request: BarOpenRequest): void
  openReader(target: ReaderTarget): void
  openExternal(url: string): void
  readClipboard(): Promise<ClipboardContent>
  pickFiles(): Promise<FileHit[]>
  searchFiles(query: string): Promise<FileSearchResult>
  openRules(item: string | undefined): void
  openRulesFolder(): void
  setWebMode(request: WebModeRequest): void
  setWebFollow(follow: boolean): void
  onWebBlocks(payload: HtmlBlocksMessage): void
  onWebFollowReleased(): void
  warn(message: string): void
}

export function registerIpc(ipc: IpcMainLike, deps: IpcDeps): void {
  const shellOnly =
    <T>(channel: string, schema: z.ZodType<T>, fn: (payload: T) => void) =>
    (event: IpcEventLike, payload?: unknown): void => {
      if (!deps.isShell(event.sender)) return deps.warn(`ipc ${channel}: rejected sender ${event.sender.id}`)
      const parsed = parseIpc(schema, payload)
      if (parsed === null) return deps.warn(`ipc ${channel}: invalid payload`)
      fn(parsed)
    }

  // ---- invoke (request/response)
  ipc.handle(IPC.engineGet, (event) => (deps.isShell(event.sender) ? deps.getEngine() : null))
  ipc.handle(IPC.clipboardRead, (event) =>
    deps.isShell(event.sender) ? deps.readClipboard() : Promise.resolve<ClipboardContent>({ kind: 'empty' }),
  )
  ipc.handle(IPC.dialogOpenFiles, (event) => (deps.isBar(event.sender) ? deps.pickFiles() : []))
  // The file menu is the command bar's alone: no other page (and no web content) can list the disk.
  ipc.handle(IPC.fsSearch, (event, payload): Promise<FileSearchResult> | FileSearchResult => {
    const parsed = deps.isBar(event.sender) ? parseIpc(FileSearchSchema, payload) : null
    return parsed ? deps.searchFiles(parsed.query) : { folder: null, hits: [] }
  })

  // ---- command bar only
  ipc.on(IPC.barHide, (event) => {
    if (deps.isBar(event.sender)) deps.hideBar()
  })
  // Synchronous: the page is blocked until this returns, so `returnValue` is set on every path.
  ipc.on(IPC.barResize, (event, payload) => {
    event.returnValue = null
    if (!deps.isBar(event.sender)) return
    const parsed = parseIpc(BarResizeSchema, payload)
    if (parsed) event.returnValue = deps.resizeBar(parsed.height)
    else deps.warn('ipc bar:resize: invalid payload')
  })
  ipc.on(IPC.barShown, (event, payload) => {
    if (!deps.isBar(event.sender)) return
    const parsed = parseIpc(BarShownSchema, payload)
    if (parsed) deps.barShown(parsed)
    else deps.warn('ipc bar:shown: invalid payload')
  })
  ipc.on(IPC.barListening, (event, payload) => {
    if (!deps.isBar(event.sender)) return
    const parsed = parseIpc(BarListeningSchema, payload)
    if (parsed) deps.setListening(parsed.listening, parsed.recording === true)
  })

  // ---- bar or reader
  ipc.on(IPC.barOpen, shellOnly<BarOpenRequest>(IPC.barOpen, BarOpenSchema, deps.openBar))
  ipc.on(IPC.readerOpen, shellOnly<ReaderTarget>(IPC.readerOpen, ReaderOpenSchema, deps.openReader))
  ipc.on(
    IPC.shellOpenExternal,
    shellOnly<{ url: string }>(IPC.shellOpenExternal, OpenExternalSchema, (p) => deps.openExternal(p.url)),
  )
  ipc.on(IPC.rulesOpen, shellOnly<RulesOpenRequest>(IPC.rulesOpen, RulesOpenSchema, (p) => deps.openRules(p.item)))
  ipc.on(IPC.rulesOpenFolder, (event) => {
    if (deps.isShell(event.sender)) deps.openRulesFolder()
  })
  ipc.on(IPC.webSetMode, shellOnly<WebModeRequest>(IPC.webSetMode, WebModeSchema, deps.setWebMode))
  ipc.on(IPC.webFollow, shellOnly<{ follow: boolean }>(IPC.webFollow, WebFollowSchema, (p) => deps.setWebFollow(p.follow)))

  // ---- live web page preload only
  ipc.on(IPC.webBlocks, (event, payload) => {
    if (!deps.isWebPage(event.sender)) return deps.warn('ipc web:blocks: rejected sender')
    const parsed = parseIpc(HtmlBlocksPayloadSchema, payload)
    if (parsed) deps.onWebBlocks(parsed)
    else deps.warn('ipc web:blocks: invalid payload')
  })
  ipc.on(IPC.webFollowReleased, (event) => {
    if (deps.isWebPage(event.sender)) deps.onWebFollowReleased()
  })
}
