import type { BarOpenedEvent, ClipboardContent, WebViewState } from './ipc-channels'
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
  resizeBar(height: number): void
  setListening(listening: boolean): void
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
  openFileDialog(): Promise<{ path: string } | null>
  openRuleFile(ruleId: string): void
  openRulesFolder(): void
}

declare global {
  interface Window {
    openSpeech?: OpenSpeechBridge
  }
}
