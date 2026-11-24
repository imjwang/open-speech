/**
 * The shell's only way to call the engine's REST API (FR-API-2): a thin wrapper over the
 * `openapi-fetch` client typed by the generated `openapi.gen.ts`. No hand-written request shapes.
 */
import createClient, { type Client } from 'openapi-fetch'

import type { components, paths } from './openapi.gen'

export type { components, paths } from './openapi.gen'

export type Schemas = components['schemas']
export type EngineClient = Client<paths>

export type DocumentOut = Schemas['DocumentOut']
export type DocumentList = Schemas['DocumentList']
export type CreateDocumentRequest = Schemas['CreateDocumentRequest']
export type CreateDocumentResponse = Schemas['CreateDocumentResponse']
export type Block = Schemas['Block']
export type BlockType = Schemas['BlockType']
export type BBox = Schemas['BBox']
export type WordBox = Schemas['WordBox']
export type ScriptOut = Schemas['ScriptOut']
export type Utterance = Schemas['Utterance']
export type TextSegment = Schemas['TextSegment']
export type RuleHit = Schemas['RuleHit']
export type PlaybackState = Schemas['PlaybackState']
export type PlayFrom = Schemas['PlayFrom']
export type Voice = Schemas['Voice']
export type RuleOut = Schemas['RuleOut']
export type UserSettings = Schemas['UserSettings']
export type ReaderSettings = Schemas['ReaderSettings']
export type Shortcuts = Schemas['Shortcuts']
export type Capabilities = Schemas['Capabilities']
export type Attachment = Schemas['Attachment']
export type QueueItem = Schemas['QueueItem']
export type RuleFile = Schemas['RuleFile']
export type RuleFileContent = Schemas['RuleFileContent']
export type RuleFileWrite = Schemas['RuleFileWrite']
export type LocalModel = Schemas['LocalModel']
export type UsageSummary = Schemas['UsageSummary']
/**
 * This month's spending, the budget, and free allowances in use. `GET /usage/budget` and `usage.changed` carry
 * the same shape; the WS schema's is the looser of the two (it lets `unit` default), so both fit this one.
 */
export type BudgetState = import('./ws.gen').BudgetState
export type FreeTierState = import('./ws.gen').FreeTierState
export type CloneOut = Schemas['CloneOut']
export type CloneAccepted = Schemas['CloneAccepted']
export type HtmlBlocksPayload = Schemas['HtmlBlocksPayload']
export type HtmlBlock = Schemas['HtmlBlock']
export type DomAnchor = Schemas['DomAnchor']

export interface EngineEndpoint {
  /** e.g. `http://127.0.0.1:52341` (no trailing slash, no `/api/v1`). */
  baseUrl: string
  token: string
}

export function normalizeBaseUrl(baseUrl: string): string {
  return baseUrl.trim().replace(/\/+$/, '').replace(/\/api\/v1$/, '')
}

export function createEngineClient(endpoint: EngineEndpoint, fetchImpl?: typeof fetch): EngineClient {
  return createClient<paths>({
    baseUrl: normalizeBaseUrl(endpoint.baseUrl),
    headers: { Authorization: `Bearer ${endpoint.token}` },
    ...(fetchImpl ? { fetch: fetchImpl } : {}),
  })
}

/** RFC 9457 problem+json from the engine, with its stable `code` (04 § Conventions). */
export class EngineApiError extends Error {
  readonly status: number
  readonly code: string
  readonly detail: string

  constructor(status: number, code: string, detail: string) {
    super(detail || code || `Engine request failed (${status})`)
    this.name = 'EngineApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }

  static from(error: unknown, status: number): EngineApiError {
    if (error && typeof error === 'object') {
      const e = error as Record<string, unknown>
      const code = typeof e.code === 'string' ? e.code : status === 422 ? 'invalid_request' : 'error'
      const detail =
        typeof e.detail === 'string'
          ? e.detail
          : typeof e.title === 'string'
            ? e.title
            : Array.isArray(e.detail)
              ? 'Invalid request.'
              : ''
      return new EngineApiError(status, code, detail)
    }
    return new EngineApiError(status, 'error', typeof error === 'string' ? error : '')
  }
}

type Result<T> = { data?: T; error?: unknown; response: Response }

/** Resolve an openapi-fetch result to its data, or throw an `EngineApiError`. */
export async function unwrap<T>(request: Promise<Result<T>>): Promise<T> {
  const { data, error, response } = await request
  if (!response.ok || error !== undefined) {
    throw EngineApiError.from(error, response.status)
  }
  return data as T
}

export interface UploadOptions {
  play?: boolean
  voiceId?: string
  confirmEstimate?: boolean
}

/** `POST /documents/upload` (multipart) for dropped/pasted files. */
export function uploadDocument(
  client: EngineClient,
  file: Blob,
  filename: string,
  options: UploadOptions = {},
): Promise<CreateDocumentResponse> {
  return unwrap(
    client.POST('/api/v1/documents/upload', {
      body: {
        // The generator models binary as `string`; the serializer below sends the real Blob.
        file: filename,
        play: options.play ?? false,
        confirm_estimate: options.confirmEstimate ?? false,
        voice_id: options.voiceId ?? null,
      },
      bodySerializer: (body) => {
        const form = new FormData()
        form.append('file', file, filename)
        form.append('play', String(body.play))
        form.append('confirm_estimate', String(body.confirm_estimate))
        if (body.voice_id) form.append('voice_id', body.voice_id)
        return form
      },
    }),
  )
}

/** What goes with a recording to be cloned (`POST /voices/clones`). */
export interface CloneUpload {
  name?: string | null
  language?: string | null
  passageId?: string | null
  /** The person confirmed it is their voice or they have the speaker's permission (required by the engine). */
  consent: boolean
  source: 'recording' | 'file'
  /** false: unlisted until the user hears it and keeps it ("Keep it?"). */
  keep: boolean
  /** The bar's session: the engine answers in this conversation (the check sentence, then "Keep it?"). */
  sessionId?: string | null
  conversationId?: string | null
}

/** `POST /voices/clones` (multipart): a recorded reference, to become a voice. */
export function uploadClone(client: EngineClient, wav: Blob, filename: string, fields: CloneUpload): Promise<CloneAccepted> {
  return unwrap(
    client.POST('/api/v1/voices/clones', {
      body: {
        file: filename,
        consent: fields.consent,
        source: fields.source,
        keep: fields.keep,
        name: fields.name ?? null,
        language: fields.language ?? null,
        passage_id: fields.passageId ?? null,
        session_id: fields.sessionId ?? null,
        conversation_id: fields.conversationId ?? null,
      },
      bodySerializer: (body) => {
        const form = new FormData()
        form.append('file', wav, filename)
        form.append('consent', String(body.consent))
        form.append('source', body.source)
        form.append('keep', String(body.keep))
        for (const key of ['name', 'language', 'passage_id', 'session_id', 'conversation_id'] as const) {
          const value = body[key]
          if (value) form.append(key, value)
        }
        return form
      },
    }),
  )
}

/** Fetch every block of a document (the endpoint is paged). */
export async function fetchAllBlocks(client: EngineClient, documentId: string, pageSize = 2000): Promise<Block[]> {
  const blocks: Block[] = []
  let offset = 0
  for (;;) {
    const page = await unwrap(
      client.GET('/api/v1/documents/{document_id}/blocks', {
        params: { path: { document_id: documentId }, query: { offset, limit: pageSize } },
      }),
    )
    blocks.push(...page.items)
    offset += page.items.length
    if (page.items.length === 0 || offset >= page.total) break
  }
  return blocks
}

/** Fetch the original source file (PDF/image) through the generated client, as bytes. */
export async function fetchSource(client: EngineClient, documentId: string): Promise<ArrayBuffer> {
  const { data, error, response } = await client.GET('/api/v1/documents/{document_id}/source', {
    params: { path: { document_id: documentId } },
    parseAs: 'arrayBuffer',
  })
  if (!response.ok || error !== undefined || !data) throw EngineApiError.from(error, response.status)
  return data
}

export function isEngineError(err: unknown, code?: string): err is EngineApiError {
  return err instanceof EngineApiError && (code === undefined || err.code === code)
}
