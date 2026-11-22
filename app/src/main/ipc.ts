/**
 * IPC handlers. Every payload is validated (zod) and every channel checks which window sent it:
 * shell channels only from the command bar / reader, web channels only from the live web page.
 */
import type { z } from 'zod'

import {
  BarListeningSchema,
  BarOpenSchema,
  BarResizeSchema,
  HtmlBlocksPayloadSchema,
  IPC,
  OpenExternalSchema,
  parseIpc,
  ReaderOpenSchema,
  RuleFileSchema,
  WebFollowSchema,
  WebModeSchema,
  type BarOpenRequest,
  type ClipboardContent,
  type EngineInfo,
  type HtmlBlocksMessage,
  type ReaderTarget,
  type WebModeRequest,
} from '../shared/ipc'

export interface SenderLike {
  id: number
}

export interface IpcEventLike {
  sender: SenderLike
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
  resizeBar(height: number): void
  setListening(listening: boolean): void
  openBar(request: BarOpenRequest): void
  openReader(target: ReaderTarget): void
  openExternal(url: string): void
  readClipboard(): Promise<ClipboardContent>
  openFileDialog(): Promise<{ path: string } | null>
  openRuleFile(ruleId: string): void
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
  ipc.handle(IPC.dialogOpenFile, (event) => (deps.isShell(event.sender) ? deps.openFileDialog() : null))

  // ---- command bar only
  ipc.on(IPC.barHide, (event) => {
    if (deps.isBar(event.sender)) deps.hideBar()
  })
  ipc.on(IPC.barResize, (event, payload) => {
    if (!deps.isBar(event.sender)) return
    const parsed = parseIpc(BarResizeSchema, payload)
    if (parsed) deps.resizeBar(parsed.height)
    else deps.warn('ipc bar:resize: invalid payload')
  })
  ipc.on(IPC.barListening, (event, payload) => {
    if (!deps.isBar(event.sender)) return
    const parsed = parseIpc(BarListeningSchema, payload)
    if (parsed) deps.setListening(parsed.listening)
  })

  // ---- bar or reader
  ipc.on(IPC.barOpen, shellOnly<BarOpenRequest>(IPC.barOpen, BarOpenSchema, deps.openBar))
  ipc.on(IPC.readerOpen, shellOnly<ReaderTarget>(IPC.readerOpen, ReaderOpenSchema, deps.openReader))
  ipc.on(
    IPC.shellOpenExternal,
    shellOnly<{ url: string }>(IPC.shellOpenExternal, OpenExternalSchema, (p) => deps.openExternal(p.url)),
  )
  ipc.on(IPC.rulesOpenFile, shellOnly<{ ruleId: string }>(IPC.rulesOpenFile, RuleFileSchema, (p) => deps.openRuleFile(p.ruleId)))
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
