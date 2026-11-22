/** How to start the engine: `uv run` from the repo in development, the bundled runtime when packaged. */
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'

import type { SpawnSpec } from './supervisor'

const SERVE_ARGS = ['serve', '--port', '0', '--handshake', 'stdout']

/** GUI apps on macOS start with a minimal PATH; add the usual places uv and Homebrew live. */
export function augmentedPath(current: string | undefined): string {
  const extra = [
    path.join(homedir(), '.local', 'bin'),
    path.join(homedir(), '.cargo', 'bin'),
    '/opt/homebrew/bin',
    '/usr/local/bin',
  ]
  const parts = (current ?? '').split(path.delimiter).filter(Boolean)
  for (const p of extra) if (!parts.includes(p)) parts.push(p)
  return parts.join(path.delimiter)
}

export interface SpawnContext {
  isPackaged: boolean
  /** `process.resourcesPath` in packaged builds. */
  resourcesPath: string
  /** The app/ directory in development (`app.getAppPath()`). */
  appPath: string
  env: Record<string, string | undefined>
  /** Extra browser origins the engine must accept (the Vite dev server in `electron-vite dev`). */
  extraOrigins?: string[]
}

export function resolveSpawnSpec(ctx: SpawnContext): SpawnSpec {
  const env: Record<string, string | undefined> = { ...ctx.env, PATH: augmentedPath(ctx.env.PATH) }
  // Never inherit Electron's own Node/Electron switches into the Python process.
  delete env.ELECTRON_RUN_AS_NODE
  delete env.NODE_OPTIONS
  // onnxruntime (local Kokoro) otherwise keeps a device ID and an event store under ~/Library/Application
  // Support/Microsoft and uploads usage telemetry to Microsoft. The engine is local-first: off unless overridden.
  env.ORT_DISABLE_TELEMETRY ??= '1'
  if (ctx.extraOrigins?.length) {
    const existing = (env.OPEN_SPEECH_ALLOWED_ORIGINS ?? '').split(',').map((s) => s.trim()).filter(Boolean)
    env.OPEN_SPEECH_ALLOWED_ORIGINS = Array.from(new Set([...existing, ...ctx.extraOrigins])).join(',')
  }

  if (ctx.env.OPEN_SPEECH_ENGINE_COMMAND) {
    // Escape hatch for development/e2e: a full command line, whitespace-separated.
    const [command, ...args] = ctx.env.OPEN_SPEECH_ENGINE_COMMAND.split(/\s+/).filter(Boolean)
    return { command: command ?? 'open-speech', args: [...args, ...SERVE_ARGS], env }
  }

  if (ctx.isPackaged) {
    const runtime = path.join(ctx.resourcesPath, 'engine')
    const python = path.join(runtime, 'python', 'bin', 'python3')
    env.PYTHONNOUSERSITE = '1'
    env.PYTHONDONTWRITEBYTECODE = '1'
    env.OPEN_SPEECH_BUNDLED_RUNTIME = runtime
    // The console script's shebang is not relocatable; call the Typer app through the interpreter. The watch
    // thread stops the engine when the app dies without stopping it (force quit, crash), instead of leaving it
    // running under launchd: SIGTERM for a clean shutdown, then a hard exit if that hangs.
    const bootstrap = [
      'import os, signal, sys, threading, time',
      'def _watch_parent(parent=os.getppid()):',
      '    while os.getppid() == parent: time.sleep(1)',
      '    os.kill(os.getpid(), signal.SIGTERM); time.sleep(5); os._exit(1)',
      'threading.Thread(target=_watch_parent, name="parent-watch", daemon=True).start()',
      'sys.argv[0] = "open-speech"',
      'from open_speech.cli.main import app',
      'app()',
    ].join('\n')
    return { command: python, args: ['-c', bootstrap, ...SERVE_ARGS], cwd: runtime, env }
  }

  const engineDir = path.resolve(ctx.appPath, '..', 'engine')
  const uv = ctx.env.UV_BIN || 'uv'
  return {
    command: uv,
    args: ['run', '--project', engineDir, 'open-speech', ...SERVE_ARGS],
    cwd: existsSync(engineDir) ? engineDir : ctx.appPath,
    env,
  }
}
