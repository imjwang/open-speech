import type { BarOpenedEvent, ClipboardContent, FileHit, FileSearchResult, WebViewState } from './ipc-channels'
import type { EngineInfo, ReaderTarget, WebModeRequest } from './ipc'

/**
 * What the shell preload exposes on `window.openSpeech` (contextBridge). The command bar and reader
 * renderers use it through `src/renderer/shared/bridge.ts`, which falls back to browser equivalents
 * when it is absent (UI tests in plain Chrome).
 */
export interface OpenSpeechBridge {
  readonly platform: string
  getEngine(): Promise<EngineInfo | null>
  onEngineChanged(cb: (info: EngineInfo | null) => void): () => void
  // command bar window
  hideBar(): void
  /** Synchronous: returns once the window has the new height (the height it has, after clamping). */
  resizeBar(height: number): number
  /** The bar painted open `openId` (from `onBarOpened`) at `height`: main may now show it. */
  barShown(openId: number, height: number): void
  setListening(listening: boolean, recording?: boolean): void
  onBarOpened(cb: (event: BarOpenedEvent) => void): () => void
  onBarClosed(cb: () => void): () => void
  openBar(request?: { documentId?: string; listen?: boolean }): void
  // reader window
  openReader(target: ReaderTarget): void
  onReaderShow(cb: (target: ReaderTarget) => void): () => void
  setWebMode(request: WebModeRequest): void
  webBackToPlayhead(): void
  onWebState(cb: (state: WebViewState) => void): () => void
  // OS
  openExternal(url: string): void
  readClipboard(): Promise<ClipboardContent>
  /** The native open panel; several files may be chosen. */
  pickFiles(): Promise<FileHit[]>
  /** The `@` menu: search files by name, or browse a folder when the query is a path. */
  searchFiles(query: string): Promise<FileSearchResult>
  /** The path of a file dropped or pasted into the page ('' when it has none, e.g. a pasted screenshot). */
  pathForFile(file: File): string
  // rules editor window
  openRules(item?: string): void
  onRulesShow(cb: (item: string | null) => void): () => void
  openRulesFolder(): void
}

declare global {
  interface Window {
    openSpeech?: OpenSpeechBridge
  }
}
