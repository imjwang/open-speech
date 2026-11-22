/** Reader UI state (Zustand). Server state (documents, scripts) is fetched from the engine. */
import { create } from 'zustand'

import type { Block, DocumentOut, ReaderSettings, ScriptOut } from '@shared/api/client'
import type { Anchor } from '@shared/api/ws.gen'
import type { ReaderTarget } from '@shared/ipc'
import type { WebViewState } from '@shared/ipc-channels'

export interface WordState {
  documentId: string
  utteranceId: string
  utteranceIndex: number
  anchor: Anchor
  sentenceAnchors: Anchor[]
  fidelity: 'exact' | 'block' | 'section' | 'media'
  spokenStart: number
  spokenEnd: number
}

export interface ReaderState {
  target: ReaderTarget | null
  documentId: string | null
  doc: DocumentOut | null
  blocks: Block[]
  script: ScriptOut | null
  source: ArrayBuffer | null
  imageUrl: string | null
  loading: boolean
  error: string | null
  word: WordState | null
  scriptView: boolean
  follow: boolean
  /** Web documents: the live page (WebContentsView) or the extracted text. */
  webMode: 'page' | 'text'
  web: WebViewState | null
  settings: ReaderSettings | null
}

export const initialReaderState: ReaderState = {
  target: null,
  documentId: null,
  doc: null,
  blocks: [],
  script: null,
  source: null,
  imageUrl: null,
  loading: false,
  error: null,
  word: null,
  scriptView: false,
  follow: true,
  webMode: 'page',
  web: null,
  settings: null,
}

export const useReaderStore = create<ReaderState>(() => ({ ...initialReaderState }))
