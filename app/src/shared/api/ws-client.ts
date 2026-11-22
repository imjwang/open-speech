/**
 * Typed WebSocket client for the engine event protocol (04 § WebSocket protocol), built on the
 * generated `ws.gen.ts` message types. One socket per client; reconnects with jittered backoff and
 * refuses to talk to an engine whose `hello.protocol` differs from the schema it was generated from.
 */
import { normalizeBaseUrl, type EngineEndpoint } from './client'
import { WS_PROTOCOL, type ClientMessage, type ServerMessage } from './ws.gen'

export type { ClientMessage, ServerMessage } from './ws.gen'
export type ServerMessageType = ServerMessage['type']
export type ServerMessageOf<T extends ServerMessageType> = Extract<ServerMessage, { type: T }>

export type SocketStatus =
  | 'idle' // never connected
  | 'connecting'
  | 'open' // hello received and protocol matches
  | 'reconnecting'
  | 'incompatible' // engine speaks another protocol version; no reconnect
  | 'closed' // closed by us

/** The subset of the WHATWG WebSocket API the client needs (browser `WebSocket` and `ws` both fit). */
export interface WebSocketLike {
  readonly readyState: number
  binaryType: string
  onopen: ((ev: unknown) => void) | null
  onclose: ((ev: { code: number; reason: string }) => void) | null
  onerror: ((ev: unknown) => void) | null
  onmessage: ((ev: { data: unknown }) => void) | null
  send(data: string | ArrayBuffer | ArrayBufferView): void
  close(code?: number, reason?: string): void
}

export type WebSocketFactory = (url: string, endpoint: EngineEndpoint) => WebSocketLike

export interface EngineSocketOptions {
  endpoint: EngineEndpoint | null
  createSocket?: WebSocketFactory
  helloTimeoutMs?: number
  initialBackoffMs?: number
  maxBackoffMs?: number
  random?: () => number
  /** Sent after every successful hello (04: defaults to all topics). */
  topics?: string[]
}

const OPEN = 1

export function wsUrl(endpoint: EngineEndpoint): string {
  const base = normalizeBaseUrl(endpoint.baseUrl).replace(/^http/, 'ws')
  // Browsers cannot set headers on the upgrade, so the token rides in the query (04 § Conventions).
  return `${base}/api/v1/ws?token=${encodeURIComponent(endpoint.token)}`
}

export function parseServerMessage(data: unknown): ServerMessage | null {
  let text: string
  if (typeof data === 'string') text = data
  else if (data instanceof ArrayBuffer) text = new TextDecoder().decode(data)
  else if (ArrayBuffer.isView(data)) text = new TextDecoder().decode(new Uint8Array(data.buffer, data.byteOffset, data.byteLength))
  else return null
  try {
    const value: unknown = JSON.parse(text)
    if (value && typeof value === 'object' && typeof (value as { type?: unknown }).type === 'string') {
      return value as ServerMessage
    }
  } catch {
    // not JSON — ignore
  }
  return null
}

type Handler = (msg: ServerMessage) => void

export class EngineSocket {
  private endpoint: EngineEndpoint | null
  private readonly createSocket: WebSocketFactory
  private readonly helloTimeoutMs: number
  private readonly initialBackoffMs: number
  private readonly maxBackoffMs: number
  private readonly random: () => number
  private readonly topics: string[] | undefined
  private socket: WebSocketLike | null = null
  private helloTimer: ReturnType<typeof setTimeout> | null = null
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private attempt = 0
  private stopped = true
  private handlers = new Map<string, Set<Handler>>()
  private anyHandlers = new Set<Handler>()
  private statusHandlers = new Set<(status: SocketStatus) => void>()
  private _status: SocketStatus = 'idle'
  private _engineVersion: string | null = null

  constructor(options: EngineSocketOptions) {
    this.endpoint = options.endpoint
    this.createSocket =
      options.createSocket ??
      ((url) => {
        const Ctor = (globalThis as { WebSocket?: new (url: string) => WebSocketLike }).WebSocket
        if (!Ctor) throw new Error('WebSocket is not available in this environment')
        return new Ctor(url)
      })
    this.helloTimeoutMs = options.helloTimeoutMs ?? 5000
    this.initialBackoffMs = options.initialBackoffMs ?? 500
    this.maxBackoffMs = options.maxBackoffMs ?? 10_000
    this.random = options.random ?? Math.random
    this.topics = options.topics
  }

  get status(): SocketStatus {
    return this._status
  }

  get engineVersion(): string | null {
    return this._engineVersion
  }

  get isOpen(): boolean {
    return this._status === 'open' && this.socket?.readyState === OPEN
  }

  connect(): void {
    this.stopped = false
    if (this.socket || this._status === 'incompatible') return
    this.open()
  }

  /** Point the client at a new engine (e.g. after the supervisor restarted it) and reconnect. */
  setEndpoint(endpoint: EngineEndpoint | null): void {
    const same =
      endpoint && this.endpoint && endpoint.baseUrl === this.endpoint.baseUrl && endpoint.token === this.endpoint.token
    if (same) return
    this.endpoint = endpoint
    this.attempt = 0
    if (this._status === 'incompatible') this._status = 'idle'
    this.teardown()
    if (!this.stopped) this.open()
  }

  close(): void {
    this.stopped = true
    this.teardown()
    this.setStatus('closed')
  }

  on<T extends ServerMessageType>(type: T, handler: (msg: ServerMessageOf<T>) => void): () => void {
    let set = this.handlers.get(type)
    if (!set) {
      set = new Set()
      this.handlers.set(type, set)
    }
    const h = handler as Handler
    set.add(h)
    return () => set.delete(h)
  }

  onAny(handler: Handler): () => void {
    this.anyHandlers.add(handler)
    return () => this.anyHandlers.delete(handler)
  }

  onStatus(handler: (status: SocketStatus) => void): () => void {
    this.statusHandlers.add(handler)
    return () => this.statusHandlers.delete(handler)
  }

  /** Send a control message. Returns false when not connected (callers surface the failure). */
  send(message: ClientMessage): boolean {
    if (!this.isOpen || !this.socket) return false
    const withTs = { ...message, ts: message.ts ?? new Date().toISOString() }
    this.socket.send(JSON.stringify(withTs))
    return true
  }

  /** Mic audio: 16-bit LE PCM, 16 kHz mono, between `mic.start` and `mic.stop`. */
  sendAudio(pcm: Int16Array | ArrayBuffer): boolean {
    if (!this.isOpen || !this.socket) return false
    this.socket.send(pcm instanceof Int16Array ? pcm : new Uint8Array(pcm))
    return true
  }

  // ---------------------------------------------------------------- internals

  private open(): void {
    if (!this.endpoint) {
      this.setStatus('idle')
      return
    }
    const endpoint = this.endpoint
    this.setStatus(this.attempt === 0 ? 'connecting' : 'reconnecting')
    let socket: WebSocketLike
    try {
      socket = this.createSocket(wsUrl(endpoint), endpoint)
    } catch {
      this.scheduleReconnect()
      return
    }
    this.socket = socket
    socket.binaryType = 'arraybuffer'
    socket.onopen = () => {
      if (this.socket !== socket) return
      this.helloTimer = setTimeout(() => {
        if (this.socket === socket) socket.close(4000, 'no hello')
      }, this.helloTimeoutMs)
    }
    socket.onmessage = (ev) => {
      if (this.socket !== socket) return
      const msg = parseServerMessage(ev.data)
      if (!msg) return
      if (this._status !== 'open') {
        // Nothing is trusted until the engine says which protocol it speaks.
        if (msg.type !== 'hello') return
        this.clearHello()
        if (msg.protocol !== undefined && msg.protocol !== WS_PROTOCOL) {
          this._engineVersion = msg.version
          this.stopped = true
          this.setStatus('incompatible')
          this.dispatch(msg)
          this.teardown()
          return
        }
        this._engineVersion = msg.version
        this.attempt = 0
        this.setStatus('open')
        if (this.topics?.length) this.send({ type: 'subscribe', topics: this.topics })
      }
      this.dispatch(msg)
    }
    socket.onerror = () => {
      // `onclose` follows; reconnection is handled there.
    }
    socket.onclose = () => {
      if (this.socket !== socket) return
      this.clearHello()
      this.socket = null
      if (this.stopped || this._status === 'incompatible') return
      this.scheduleReconnect()
    }
  }

  private scheduleReconnect(): void {
    if (this.stopped) return
    this.setStatus('reconnecting')
    const base = Math.min(this.maxBackoffMs, this.initialBackoffMs * 2 ** this.attempt)
    const delay = Math.round(base * (0.5 + this.random() * 0.5))
    this.attempt++
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null
      if (!this.stopped && !this.socket) this.open()
    }, delay)
  }

  private dispatch(msg: ServerMessage): void {
    for (const h of this.handlers.get(msg.type) ?? []) h(msg)
    for (const h of this.anyHandlers) h(msg)
  }

  private clearHello(): void {
    if (this.helloTimer) clearTimeout(this.helloTimer)
    this.helloTimer = null
  }

  private teardown(): void {
    this.clearHello()
    if (this.retryTimer) clearTimeout(this.retryTimer)
    this.retryTimer = null
    const socket = this.socket
    this.socket = null
    if (socket) {
      socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null
      try {
        socket.close(1000, 'client closing')
      } catch {
        // already closed
      }
    }
    if (this._status === 'open' || this._status === 'connecting' || this._status === 'reconnecting') {
      this.setStatus(this.stopped ? 'closed' : 'idle')
    }
  }

  private setStatus(status: SocketStatus): void {
    if (this._status === status) return
    this._status = status
    for (const h of this.statusHandlers) h(status)
  }
}
