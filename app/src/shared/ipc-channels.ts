/**
 * IPC channel names and plain payload types. Kept free of runtime dependencies so the sandboxed
 * preloads stay small; the zod validators live in `ipc.ts`.
 */
export const IPC = {
  // renderer → main (invoke: request/response)
  engineGet: 'engine:get',
  clipboardRead: 'clipboard:read',
  dialogOpenFiles: 'dialog:open-files',
  fsSearch: 'fs:search',
  // renderer → main (send: fire and forget)
  barHide: 'bar:hide',
  barResize: 'bar:resize',
  barListening: 'bar:listening',
  barOpen: 'bar:open',
  readerOpen: 'reader:open',
  shellOpenExternal: 'shell:open-external',
  rulesOpen: 'rules:open',
  rulesOpenFolder: 'rules:open-folder',
  webSetMode: 'web:set-mode',
  webFollow: 'web:follow',
  // main → renderer
  engineChanged: 'engine:changed',
  barOpened: 'bar:opened',
  barClosed: 'bar:closed',
  readerShow: 'reader:show',
  rulesShow: 'rules:show',
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

/** What kind of thing a file is, for its icon and for whether it can be read aloud. */
export type FileKind = 'folder' | 'pdf' | 'doc' | 'text' | 'web' | 'image' | 'other'

/** One result in the `@` file menu. Names and paths only: file contents never cross this channel. */
export interface FileHit {
  path: string
  name: string
  kind: FileKind
  /** Where it lives, shortened for display ("~/Documents/Papers"). */
  where: string
  /** Its own path with the home folder as `~` ("~/Documents/Papers/draft.pdf"): what `@` browsing types. */
  short: string
}

export interface FileSearchResult {
  /** The folder being browsed ("~/Documents/"), or null for a search by name. */
  folder: string | null
  hits: FileHit[]
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
