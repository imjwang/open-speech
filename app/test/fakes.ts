/** Test doubles shared by renderer tests: a fake WS, a fake mic, and a routed fetch for the generated client. */
import { vi } from 'vitest'

import { createEngineClient, type EngineClient } from '@shared/api/client'
import type { ClientMessage, ServerMessage, ServerMessageOf, ServerMessageType, SocketStatus } from '@shared/api/ws-client'

import type { MicHandlers, MicSource } from '../src/renderer/shared/audio/mic'
import type { EngineConnection, SocketLike } from '../src/renderer/shared/engine'

export class FakeSocket implements SocketLike {
  status: SocketStatus = 'open'
  sent: ClientMessage[] = []
  audio: Int16Array[] = []
  connected = true
  private handlers = new Map<string, Set<(m: ServerMessage) => void>>()

  send(message: ClientMessage): boolean {
    if (!this.connected) return false
    this.sent.push(message)
    return true
  }

  sendAudio(pcm: Int16Array): boolean {
    if (!this.connected) return false
    this.audio.push(pcm)
    return true
  }

  on<T extends ServerMessageType>(type: T, handler: (msg: ServerMessageOf<T>) => void): () => void {
    let set = this.handlers.get(type)
    if (!set) {
      set = new Set()
      this.handlers.set(type, set)
    }
    const h = handler as (m: ServerMessage) => void
    set.add(h)
    return () => set.delete(h)
  }

  emit(msg: ServerMessage): void {
    for (const h of this.handlers.get(msg.type) ?? []) h(msg)
  }

  ofType<T extends ClientMessage['type']>(type: T): Extract<ClientMessage, { type: T }>[] {
    return this.sent.filter((m): m is Extract<ClientMessage, { type: T }> => m.type === type)
  }
}

export class FakeMic implements MicSource {
  handlers: MicHandlers | null = null
  starts = 0
  stops = 0
  get active(): boolean {
    return this.handlers !== null
  }
  async start(handlers: MicHandlers): Promise<void> {
    this.starts++
    this.handlers = handlers
  }
  stop(): void {
    this.stops++
    this.handlers = null
  }
  /** Feed `ms` of audio at an RMS level, in 32 ms chunks. */
  feed(level: number, ms: number): void {
    for (let t = 0; t < ms; t += 32) {
      if (!this.handlers) return
      this.handlers.onPcm(new Int16Array(512))
      this.handlers.onLevel(level, 32)
    }
  }
}

export interface RecordedCall {
  method: string
  path: string
  query: URLSearchParams
  body: unknown
  headers: Headers
}

export type Route = (call: RecordedCall) => unknown | Response | Promise<unknown | Response>

/** A generated client whose fetch is answered by `routes["METHOD /api/v1/path"]` (default `{}`). */
export function routedClient(routes: Record<string, Route> = {}): { client: EngineClient; calls: RecordedCall[]; fetch: ReturnType<typeof vi.fn> } {
  const calls: RecordedCall[] = []
  const fetchMock = vi.fn(async (input: Request | string | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const url = new URL(req.url)
    let body: unknown = null
    const ct = req.headers.get('content-type') ?? ''
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      if (ct.includes('application/json')) body = await req.json()
      else if (ct.includes('multipart/form-data')) body = await req.formData()
      else body = await req.text()
    }
    const call: RecordedCall = { method: req.method, path: url.pathname, query: url.searchParams, body, headers: req.headers }
    calls.push(call)
    const key = `${req.method} ${url.pathname}`
    const route = routes[key] ?? Object.entries(routes).find(([k]) => new RegExp(`^${k.replace(/\{[^}]+\}/g, '[^/]+')}$`).test(key))?.[1]
    const result = route ? await route(call) : {}
    if (result instanceof Response) return result
    return new Response(JSON.stringify(result ?? {}), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  const client = createEngineClient({ baseUrl: 'http://127.0.0.1:9999', token: 'test-token-0123456789' }, fetchMock as unknown as typeof fetch)
  return { client, calls, fetch: fetchMock }
}

export function fakeEngine(routes: Record<string, Route> = {}): EngineConnection & { socket: FakeSocket; calls: RecordedCall[] } {
  const { client, calls } = routedClient(routes)
  return { client, socket: new FakeSocket(), calls }
}

export function problem(status: number, code: string, detail: string): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title: code, status, detail, code }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  })
}
