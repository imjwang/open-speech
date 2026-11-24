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
  /** Synchronous (`sendSync`): the window has its new height before the bar paints at that height. */
  barResize: 'bar:resize',
  barListening: 'bar:listening',
  barOpen: 'bar:open',
  /** The bar has painted the state for an open: main sizes the window to it and makes it visible. */
  barShown: 'bar:shown',
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
  /** Counts up with every open. The bar answers `bar:shown` with it once it has painted that open, and the
   *  window stays invisible (opacity 0) until then, so nothing from the previous open is ever seen. */
  openId: number
  /** Open with the mic live (the default for the hotkey and the tray). */
  listen: boolean
  /** Opened by a global hotkey that may still be held down (push-to-talk if released after a hold). */
  viaHotkey: boolean
  /** Document context, e.g. ⌘K in the reader. */
  documentId?: string
  /** Whether this Mac can listen, as the shell last heard from the engine (`shell-state.json`; with nothing saved,
   *  true in packaged builds, which bundle the speech model). The bar opens the microphone at once unless this
   *  (or what the engine has said since) is `false`. Null: not known. */
  canListen?: boolean | null
  /** The first launch: the shell opened the bar by itself. It listens, and says once how to talk to it. */
  firstRun?: boolean
  /** The hotkey was pressed while the assistant was speaking: stop it and listen (barge-in by key). */
  bargeIn?: boolean
}

/** The bar painted open `openId` at this height (CSS px, window height). */
export interface BarShownEvent {
  openId: number
  height: number
}

/** What kind of thing a file is, for its icon and for whether it can be read aloud. */
export type FileKind = 'folder' | 'pdf' | 'doc' | 'text' | 'web' | 'image' | 'audio' | 'other'

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
