/**
 * IPC contract between the main process, the shell preload (command bar + reader) and the web-page
 * preload. Every message that crosses into the main process is validated with these schemas
 * (03 § Security: "IPC validated with schemas"); anything that fails validation is dropped.
 */
import { z } from 'zod'

export { IPC, type IpcChannel } from './ipc-channels'
export type {
  BarOpenedEvent,
  BarShownEvent,
  ClipboardContent,
  FileHit,
  FileKind,
  FileSearchResult,
  WebViewState,
} from './ipc-channels'

// ------------------------------------------------------------------ payloads

const id = z.string().min(1).max(200).regex(/^[A-Za-z0-9_:.\-]+$/)

export const EngineInfoSchema = z.object({
  baseUrl: z.string().url(),
  token: z.string().min(1),
  version: z.string(),
})
export type EngineInfo = z.infer<typeof EngineInfoSchema>

export const BarResizeSchema = z.object({ height: z.number().int().min(40).max(2000) })
export const BarShownSchema = z.object({ openId: z.number().int().min(0), height: z.number().int().min(40).max(2000) })
// `recording`: the microphone is recording a voice to clone (the hotkey then finishes the take, not the bar).
export const BarListeningSchema = z.object({ listening: z.boolean(), recording: z.boolean().optional() })
export const BarOpenSchema = z.object({ documentId: id.optional(), listen: z.boolean().optional() })
export type BarOpenRequest = z.infer<typeof BarOpenSchema>

const httpUrl = z
  .string()
  .max(8192)
  .refine((u) => /^https?:\/\//i.test(u), 'http(s) only')

export const ReaderOpenSchema = z
  .object({
    documentId: id.optional(),
    url: httpUrl.optional(),
    /** Web pages: post the extracted blocks with `play: true` once the page has loaded. */
    autoplay: z.boolean().optional(),
  })
  .refine((v) => v.documentId !== undefined || v.url !== undefined, 'documentId or url required')
export type ReaderTarget = z.infer<typeof ReaderOpenSchema>

export const OpenExternalSchema = z.object({
  url: z
    .string()
    .max(8192)
    .refine((u) => {
      try {
        return ['http:', 'https:', 'mailto:'].includes(new URL(u).protocol)
      } catch {
        return false
      }
    }, 'http(s)/mailto only'),
})

export const RulesOpenSchema = z.object({ item: id.optional() })
export type RulesOpenRequest = z.infer<typeof RulesOpenSchema>
/** What was typed after `@`: a name to search for, or a path to browse ("~/Documents/pa"). */
export const FileSearchSchema = z.object({ query: z.string().max(1024) })
export const WebModeSchema = z.object({
  /** page: show the live site; text: hide it (reader shows extracted text); off: not a web document. */
  mode: z.enum(['page', 'text', 'off']),
  url: httpUrl.optional(),
  documentId: id.optional(),
  autoplay: z.boolean().optional(),
})
export type WebModeRequest = z.infer<typeof WebModeSchema>
export const WebFollowSchema = z.object({ follow: z.boolean() })

// ------------------------------------------------------------------ web page preload

export const DomAnchorSchema = z.object({
  xpath: z.string().min(1).max(4096),
  start_offset: z.number().int().min(0).default(0),
  end_offset: z.number().int().min(0).nullable().default(null),
})

export const HtmlBlockSchema = z.object({
  id: z.string().min(1).max(64),
  type: z.string().min(1).max(32),
  text: z.string().max(100_000),
  level: z.number().int().min(1).max(6).nullable().optional(),
  dom_anchor: DomAnchorSchema.nullable().optional(),
})

export const HtmlBlocksPayloadSchema = z.object({
  url: z.string().max(8192),
  title: z.string().max(1000),
  blocks: z.array(HtmlBlockSchema).max(20_000),
})
export type HtmlBlocksMessage = z.infer<typeof HtmlBlocksPayloadSchema>

export const WebPaintSchema = z.object({
  sentence: z.array(DomAnchorSchema).max(200),
  word: DomAnchorSchema.nullable(),
  scroll: z.boolean(),
})
export type WebPaint = z.infer<typeof WebPaintSchema>

/** Parse an untrusted IPC payload; returns null (and the caller drops the message) when invalid. */
export function parseIpc<T>(schema: z.ZodType<T>, payload: unknown): T | null {
  const result = schema.safeParse(payload)
  return result.success ? result.data : null
}
