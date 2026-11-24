/**
 * Renderer-side engine connection: the generated REST client + the typed WS client, re-pointed whenever
 * the supervisor restarts the engine (new port/token). Server state lives in the engine; this store
 * only mirrors connection status, the transport snapshot, this month's spending and (for the bar) the recent
 * documents, so they are already there when a window opens instead of arriving a moment later.
 */
import { createContext, useContext, useEffect, useEffectEvent, type ReactNode } from 'react'
import { create } from 'zustand'

import {
  createEngineClient,
  EngineSocket,
  unwrap,
  type BudgetState,
  type ClientMessage,
  type DocumentOut,
  type EngineClient,
  type PlaybackState,
  type ServerMessageOf,
  type ServerMessageType,
  type SocketStatus,
} from '@shared/api'
import { WS_PROTOCOL } from '@shared/api/ws.gen'
import type { EngineInfo } from '@shared/ipc'

import type { Bridge } from './bridge'
import { IDLE_PLAYBACK, normalizePlayback } from './playback'

export interface SocketLike {
  readonly status: SocketStatus
  send(message: ClientMessage): boolean
  sendAudio(pcm: Int16Array): boolean
  on<T extends ServerMessageType>(type: T, handler: (msg: ServerMessageOf<T>) => void): () => void
}

export interface EngineConnection {
  /** Null until the engine is known (read it at call time, not during render). */
  readonly client: EngineClient | null
  readonly socket: SocketLike
}

export interface EngineState {
  status: SocketStatus
  endpoint: EngineInfo | null
  version: string | null
  playback: PlaybackState
  problem: string | null
  /** This month's spending, budget and free allowances (null until the engine has said). */
  usage: BudgetState | null
  /** Recently opened documents, kept fresh while the bar is hidden (only pages that ask for them). */
  recents: DocumentOut[]
}

export const useEngineStore = create<EngineState>(() => ({
  status: 'idle',
  endpoint: null,
  version: null,
  playback: IDLE_PLAYBACK,
  problem: null,
  usage: null,
  recents: [],
}))

const RECENTS_LIMIT = 10

function sameRecents(a: readonly DocumentOut[], b: readonly DocumentOut[]): boolean {
  return a.length === b.length && a.every((d, i) => d.id === b[i]?.id && d.title === b[i]?.title && d.progress === b[i]?.progress && d.kind === b[i]?.kind)
}

export class LiveEngine implements EngineConnection {
  private _client: EngineClient | null = null
  private wantsRecents = false
  readonly socket: EngineSocket

  constructor() {
    this.socket = new EngineSocket({ endpoint: null })
    this.socket.onStatus((status) => {
      useEngineStore.setState({
        status,
        version: this.socket.engineVersion,
        problem:
          status === 'incompatible'
            ? `This app speaks engine protocol ${WS_PROTOCOL}, but the engine (${this.socket.engineVersion ?? '?'}) speaks another. Update one of them.`
            : null,
      })
      if (status === 'open') {
        void this.refreshPlayback()
        void this.refreshUsage()
        if (this.wantsRecents) void this.refreshRecents()
      }
    })
    this.socket.on('playback.state', (msg) => useEngineStore.setState({ playback: normalizePlayback(msg.playback) }))
    this.socket.on('usage.changed', (msg) => useEngineStore.setState({ usage: msg.budget }))
  }

  /** This page shows recent documents: keep them loaded (on connect, and whenever `refreshRecents` is called). */
  trackRecents(): void {
    this.wantsRecents = true
    if (this.socket.status === 'open') void this.refreshRecents()
  }

  async refreshRecents(): Promise<void> {
    const client = this._client
    if (!client) return
    try {
      const list = await unwrap(client.GET('/api/v1/documents', { params: { query: { limit: RECENTS_LIMIT } } }))
      // Same list → same array, so nothing re-renders (or moves) for a refresh that changed nothing.
      if (!sameRecents(useEngineStore.getState().recents, list.items)) useEngineStore.setState({ recents: list.items })
    } catch {
      // transient: the cached list stays
    }
  }

  /** `GET /usage/budget`: on connect and each time the bar opens; `usage.changed` keeps it current between. */
  async refreshUsage(): Promise<void> {
    const client = this._client
    if (!client) return
    try {
      const usage = await unwrap(client.GET('/api/v1/usage/budget'))
      useEngineStore.setState({ usage })
    } catch {
      // older engine or transient: keep what we have
    }
  }

  get client(): EngineClient | null {
    return this._client
  }

  start(bridge: Bridge): () => void {
    void bridge.getEngine().then((info) => this.setEndpoint(info))
    const off = bridge.onEngineChanged((info) => this.setEndpoint(info))
    return () => {
      off()
      this.socket.close()
    }
  }

  setEndpoint(info: EngineInfo | null): void {
    const current = useEngineStore.getState().endpoint
    if (current && info && current.baseUrl === info.baseUrl && current.token === info.token) return
    this._client = info ? createEngineClient(info) : null
    useEngineStore.setState({ endpoint: info })
    this.socket.setEndpoint(info)
    if (info) this.socket.connect()
  }

  async refreshPlayback(): Promise<void> {
    const client = this._client
    if (!client) return
    try {
      const state = await unwrap(client.GET('/api/v1/playback'))
      useEngineStore.setState({ playback: normalizePlayback(state) })
    } catch {
      // transient; the WS snapshot follows on connect
    }
  }
}

const EngineContext = createContext<EngineConnection | null>(null)

export function EngineProvider({ engine, children }: { engine: EngineConnection; children: ReactNode }) {
  return <EngineContext.Provider value={engine}>{children}</EngineContext.Provider>
}

export function useEngine(): EngineConnection {
  const engine = useContext(EngineContext)
  if (!engine) throw new Error('useEngine() outside <EngineProvider>')
  return engine
}

/** Subscribe to one WS message type for the component's lifetime. */
export function useServerEvent<T extends ServerMessageType>(type: T, handler: (msg: ServerMessageOf<T>) => void): void {
  const engine = useEngine()
  const onEvent = useEffectEvent(handler)
  useEffect(() => engine.socket.on(type, (msg) => onEvent(msg)), [engine, type])
}

/** The REST client for event handlers; throws a friendly error when the engine is not up yet. */
export function requireClient(engine: EngineConnection): EngineClient {
  const client = engine.client
  if (!client) throw new Error('The engine is not running yet.')
  return client
}
