/**
 * IPC channel names and plain payload types. Kept free of runtime dependencies so the sandboxed
 * preloads stay small; the zod validators live in `ipc.ts`.
 */
export const IPC = {
  // renderer → main (invoke: request/response)
  engineGet: 'engine:get',
  clipboardRead: 'clipboard:read',
  dialogOpenFile: 'dialog:open-file',
  // renderer → main (send: fire and forget)
  barHide: 'bar:hide',
  barResize: 'bar:resize',
  barListening: 'bar:listening',
  barOpen: 'bar:open',
  readerOpen: 'reader:open',
  shellOpenExternal: 'shell:open-external',
  rulesOpenFile: 'rules:open-file',
  rulesOpenFolder: 'rules:open-folder',
  webSetMode: 'web:set-mode',
  webFollow: 'web:follow',
  // main → renderer
  engineChanged: 'engine:changed',
  barOpened: 'bar:opened',
  barClosed: 'bar:closed',
  readerShow: 'reader:show',
  webState: 'web:state',
  // web preload ↔ main
  webBlocks: 'web:blocks',
  webFollowReleased: 'web:follow-released',
  webPaint: 'web:paint',
  webExtract: 'web:extract',
} as const

export type IpcChannel = (typeof IPC)[keyof typeof IPC]

export interface BarOpenedEvent {
  /** Open with the mic live (the default for the hotkey and the tray). */
  listen: boolean
  /** Opened by a global hotkey that may still be held down (push-to-talk if released after a hold). */
  viaHotkey: boolean
  /** Document context, e.g. ⌘K in the reader. */
  documentId?: string
}

export type ClipboardContent =
  | { kind: 'text'; text: string }
  | { kind: 'image'; base64: string; mediaType: string }
  | { kind: 'empty' }

export interface WebViewState {
  mode: 'page' | 'text'
  url: string | null
  title: string
  loading: boolean
  documentId: string | null
  followReleased: boolean
  error: string | null
}
