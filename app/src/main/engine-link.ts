/**
 * The main process's own connection to the engine: a generated REST client plus a WebSocket (via the
 * `ws` package, so no browser Origin header is sent) used for the tray state, relaying `playback.word`
 * anchors to the live web page, and opening the reader when something worth seeing starts playing.
 */
import WebSocket from 'ws'

import { createEngineClient, type EngineClient } from '../shared/api/client'
import { EngineSocket, type ServerMessageOf, type ServerMessageType, type WebSocketLike } from '../shared/api/ws-client'
import type { EngineInfo } from '../shared/ipc'

export class EngineLink {
  private _client: EngineClient | null = null
  private _endpoint: EngineInfo | null = null
  readonly socket: EngineSocket

  constructor() {
    this.socket = new EngineSocket({
      endpoint: null,
      createSocket: (url, endpoint) =>
        new WebSocket(url, { headers: { Authorization: `Bearer ${endpoint.token}` } }) as unknown as WebSocketLike,
    })
  }

  get client(): EngineClient | null {
    return this._client
  }

  get endpoint(): EngineInfo | null {
    return this._endpoint
  }

  setEndpoint(info: EngineInfo | null): void {
    this._endpoint = info
    this._client = info ? createEngineClient(info) : null
    this.socket.setEndpoint(info)
    if (info) this.socket.connect()
  }

  on<T extends ServerMessageType>(type: T, handler: (msg: ServerMessageOf<T>) => void): () => void {
    return this.socket.on(type, handler)
  }

  close(): void {
    this.socket.close()
  }
}
