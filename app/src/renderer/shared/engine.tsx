/**
 * Renderer-side engine connection: the generated REST client + the typed WS client, re-pointed whenever
 * the supervisor restarts the engine (new port/token). Server state lives in the engine; this store
 * only mirrors connection status and the transport snapshot for rendering.
 */
import { createContext, useContext, useEffect, useEffectEvent, type ReactNode } from 'react'
import { create } from 'zustand'

import {
  createEngineClient,
  EngineSocket,
  unwrap,
  type ClientMessage,
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
}

export const useEngineStore = create<EngineState>(() => ({
  status: 'idle',
  endpoint: null,
  version: null,
  playback: IDLE_PLAYBACK,
  problem: null,
}))

export class LiveEngine implements EngineConnection {
  private _client: EngineClient | null = null
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
      if (status === 'open') void this.refreshPlayback()
    })
    this.socket.on('playback.state', (msg) => useEngineStore.setState({ playback: normalizePlayback(msg.playback) }))
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
