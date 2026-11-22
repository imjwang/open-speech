/**
 * Engine supervision (03 § Engine supervision, NFR-2): spawn `open-speech serve --port 0 --handshake
 * stdout`, read the handshake, health-check `/api/v1/health` every 5 s, and restart with exponential
 * backoff when the process exits or stops answering. Electron-free so it can be unit-tested.
 */
import { createEngineClient } from '../../shared/api/client'
import type { EngineInfo } from '../../shared/ipc'
import { baseUrlFor, LineSplitter, parseHandshakeLine, type Handshake } from './handshake'

export interface SpawnSpec {
  command: string
  args: string[]
  cwd?: string
  env: Record<string, string | undefined>
}

export interface ReadableLike {
  on(event: 'data', listener: (chunk: Buffer | string) => void): unknown
}

export interface ChildLike {
  readonly pid?: number | undefined
  readonly stdout: ReadableLike | null
  readonly stderr: ReadableLike | null
  on(event: 'exit', listener: (code: number | null, signal: string | null) => void): unknown
  on(event: 'error', listener: (err: Error) => void): unknown
  kill(signal?: NodeJS.Signals | number): boolean
}

export type SupervisorState = 'stopped' | 'starting' | 'running' | 'unhealthy' | 'restarting'

export interface SupervisorDeps {
  spawn(spec: SpawnSpec): ChildLike
  fetch: typeof fetch
  log(level: 'info' | 'warn' | 'error', message: string): void
  now?: () => number
}

export interface SupervisorOptions {
  healthIntervalMs?: number
  healthTimeoutMs?: number
  handshakeTimeoutMs?: number
  initialBackoffMs?: number
  maxBackoffMs?: number
  /** Consecutive failed health checks before the engine is restarted. */
  failuresBeforeRestart?: number
  /** Uptime after which the backoff resets to its initial value. */
  stableAfterMs?: number
  killGraceMs?: number
}

const DEFAULTS: Required<SupervisorOptions> = {
  healthIntervalMs: 5000,
  healthTimeoutMs: 3000,
  handshakeTimeoutMs: 90_000, // first `uv run` may sync the environment
  initialBackoffMs: 1000,
  maxBackoffMs: 30_000,
  failuresBeforeRestart: 2,
  stableAfterMs: 60_000,
  killGraceMs: 3000,
}

export function backoffDelay(attempt: number, initialMs: number, maxMs: number): number {
  return Math.min(maxMs, initialMs * 2 ** Math.max(0, attempt))
}

type Listener<T> = (value: T) => void

export class EngineSupervisor {
  private readonly opts: Required<SupervisorOptions>
  private readonly now: () => number
  private child: ChildLike | null = null
  private handshake: Handshake | null = null
  private _state: SupervisorState = 'stopped'
  private attempt = 0
  private startedAt = 0
  private failures = 0
  private stopping = false
  private healthTimer: ReturnType<typeof setInterval> | null = null
  private restartTimer: ReturnType<typeof setTimeout> | null = null
  private handshakeTimer: ReturnType<typeof setTimeout> | null = null
  private endpointListeners = new Set<Listener<EngineInfo | null>>()
  private stateListeners = new Set<Listener<SupervisorState>>()
  private readyWaiters: Array<(info: EngineInfo | null) => void> = []
  private healthInFlight = false

  constructor(
    private readonly spec: SpawnSpec,
    private readonly deps: SupervisorDeps,
    options: SupervisorOptions = {},
  ) {
    this.opts = { ...DEFAULTS, ...options }
    this.now = deps.now ?? Date.now
  }

  get state(): SupervisorState {
    return this._state
  }

  get endpoint(): EngineInfo | null {
    if (!this.handshake || (this._state !== 'running' && this._state !== 'unhealthy')) return null
    return { baseUrl: baseUrlFor(this.handshake), token: this.handshake.token, version: this.handshake.version }
  }

  /** The delay the next restart will wait. */
  get nextBackoffMs(): number {
    return backoffDelay(this.attempt, this.opts.initialBackoffMs, this.opts.maxBackoffMs)
  }

  onEndpoint(listener: Listener<EngineInfo | null>): () => void {
    this.endpointListeners.add(listener)
    return () => this.endpointListeners.delete(listener)
  }

  onState(listener: Listener<SupervisorState>): () => void {
    this.stateListeners.add(listener)
    return () => this.stateListeners.delete(listener)
  }

  /** Resolves with the first endpoint (or null if the supervisor is stopped first). */
  ready(): Promise<EngineInfo | null> {
    const current = this.endpoint
    if (current) return Promise.resolve(current)
    return new Promise((resolve) => this.readyWaiters.push(resolve))
  }

  start(): void {
    if (this._state !== 'stopped') return
    this.stopping = false
    this.attempt = 0
    this.spawnChild()
  }

  async stop(): Promise<void> {
    this.stopping = true
    this.clearTimers()
    const child = this.child
    this.child = null
    this.handshake = null
    this.setState('stopped')
    this.emitEndpoint(null)
    for (const w of this.readyWaiters.splice(0)) w(null)
    if (child) await this.terminate(child)
  }

  // ---------------------------------------------------------------- internals

  private spawnChild(): void {
    this.setState(this.attempt === 0 ? 'starting' : 'restarting')
    this.handshake = null
    this.failures = 0
    let child: ChildLike
    try {
      child = this.deps.spawn(this.spec)
    } catch (err) {
      this.deps.log('error', `engine spawn failed: ${(err as Error).message}`)
      this.scheduleRestart('spawn failed')
      return
    }
    this.child = child
    const splitter = new LineSplitter()
    child.stdout?.on('data', (chunk) => {
      for (const line of splitter.push(chunk.toString())) {
        if (this.child !== child) return
        if (!this.handshake) {
          const hs = parseHandshakeLine(line)
          if (hs) {
            this.onHandshake(child, hs)
            continue
          }
        }
        if (line.trim()) this.deps.log('info', `[engine] ${line}`)
      }
    })
    const errSplitter = new LineSplitter()
    child.stderr?.on('data', (chunk) => {
      for (const line of errSplitter.push(chunk.toString())) if (line.trim()) this.deps.log('info', `[engine] ${line}`)
    })
    child.on('error', (err) => {
      if (this.child !== child) return
      this.deps.log('error', `engine process error: ${err.message}`)
      this.child = null
      this.scheduleRestart('process error')
    })
    child.on('exit', (code, signal) => {
      if (this.child !== child) return
      this.child = null
      if (this.stopping) return
      this.deps.log('warn', `engine exited (code ${code ?? 'null'}, signal ${signal ?? 'none'})`)
      this.scheduleRestart('exited')
    })
    this.handshakeTimer = setTimeout(() => {
      if (this.child === child && !this.handshake) {
        this.deps.log('error', 'engine handshake timed out')
        this.restart(child, 'handshake timeout')
      }
    }, this.opts.handshakeTimeoutMs)
  }

  private onHandshake(child: ChildLike, hs: Handshake): void {
    if (this.handshakeTimer) clearTimeout(this.handshakeTimer)
    this.handshakeTimer = null
    this.handshake = hs
    this.startedAt = this.now()
    this.failures = 0
    this.setState('running')
    this.deps.log('info', `engine ${hs.version || '?'} ready on 127.0.0.1:${hs.port} (pid ${hs.pid})`)
    const info = this.endpoint
    this.emitEndpoint(info)
    for (const w of this.readyWaiters.splice(0)) w(info)
    if (this.healthTimer) clearInterval(this.healthTimer)
    this.healthTimer = setInterval(() => void this.checkHealth(child), this.opts.healthIntervalMs)
  }

  /** One health check; exported behaviour for tests via the interval. */
  async checkHealth(child: ChildLike | null = this.child): Promise<boolean> {
    const info = this.endpoint
    if (!info || !child || this.child !== child || this.healthInFlight) return false
    this.healthInFlight = true
    let ok = false
    try {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), this.opts.healthTimeoutMs)
      try {
        const client = createEngineClient(info, this.deps.fetch)
        const { data, response } = await client.GET('/api/v1/health', { signal: controller.signal })
        ok = response.ok && data?.ok === true
      } finally {
        clearTimeout(timer)
      }
    } catch {
      ok = false
    } finally {
      this.healthInFlight = false
    }
    if (this.child !== child) return ok
    if (ok) {
      this.failures = 0
      if (this._state === 'unhealthy') this.setState('running')
      if (this.attempt > 0 && this.now() - this.startedAt >= this.opts.stableAfterMs) this.attempt = 0
    } else {
      this.failures++
      this.deps.log('warn', `engine health check failed (${this.failures})`)
      if (this.failures >= this.opts.failuresBeforeRestart) this.restart(child, 'unhealthy')
      else this.setState('unhealthy')
    }
    return ok
  }

  private restart(child: ChildLike, reason: string): void {
    if (this.child === child) this.child = null
    void this.terminate(child)
    this.scheduleRestart(reason)
  }

  private scheduleRestart(reason: string): void {
    if (this.stopping) return
    this.clearTimers()
    this.handshake = null
    this.emitEndpoint(null)
    const delay = this.nextBackoffMs
    this.attempt++
    this.setState('restarting')
    this.deps.log('warn', `restarting engine in ${delay} ms (${reason})`)
    this.restartTimer = setTimeout(() => {
      this.restartTimer = null
      if (!this.stopping) this.spawnChild()
    }, delay)
  }

  private terminate(child: ChildLike): Promise<void> {
    return new Promise((resolve) => {
      let done = false
      const finish = () => {
        if (!done) {
          done = true
          resolve()
        }
      }
      child.on('exit', finish)
      try {
        if (!child.kill('SIGTERM')) finish()
      } catch {
        finish()
      }
      setTimeout(() => {
        if (!done) {
          try {
            child.kill('SIGKILL')
          } catch {
            // already gone
          }
          finish()
        }
      }, this.opts.killGraceMs)
    })
  }

  private clearTimers(): void {
    if (this.healthTimer) clearInterval(this.healthTimer)
    if (this.restartTimer) clearTimeout(this.restartTimer)
    if (this.handshakeTimer) clearTimeout(this.handshakeTimer)
    this.healthTimer = this.restartTimer = this.handshakeTimer = null
  }

  private setState(state: SupervisorState): void {
    if (this._state === state) return
    this._state = state
    for (const l of this.stateListeners) l(state)
  }

  private lastEmitted: string | null = null

  private emitEndpoint(info: EngineInfo | null): void {
    const key = info ? `${info.baseUrl}|${info.token}` : null
    if (key === this.lastEmitted) return
    this.lastEmitted = key
    for (const l of this.endpointListeners) l(info)
  }
}
