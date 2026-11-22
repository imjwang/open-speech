import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { resolveSpawnSpec } from './spawn-spec'

const resourcesPath = '/Applications/Open Speech.app/Contents/Resources'

describe('resolveSpawnSpec', () => {
  it('starts the bundled interpreter in packaged builds (layout built by scripts/bundle-engine.sh)', () => {
    const spec = resolveSpawnSpec({ isPackaged: true, resourcesPath, appPath: '/x', env: { PATH: '/usr/bin' } })
    const runtime = path.join(resourcesPath, 'engine')
    expect(spec.command).toBe(path.join(runtime, 'python', 'bin', 'python3'))
    expect(spec.args.slice(-5)).toEqual(['serve', '--port', '0', '--handshake', 'stdout'])
    expect(spec.cwd).toBe(runtime)
    expect(spec.env).toMatchObject({ PYTHONNOUSERSITE: '1', OPEN_SPEECH_BUNDLED_RUNTIME: runtime })
    expect(spec.args[0]).toBe('-c')
    expect(spec.args[1]).toContain('from open_speech.cli.main import app')
    expect(spec.args[1]).toContain('os.getppid()') // exits when the app dies (no orphaned engine)
  })

  it('turns onnxruntime telemetry off unless the user set it', () => {
    const ctx = { isPackaged: true, resourcesPath, appPath: '/x' }
    expect(resolveSpawnSpec({ ...ctx, env: {} }).env.ORT_DISABLE_TELEMETRY).toBe('1')
    expect(resolveSpawnSpec({ ...ctx, isPackaged: false, env: {} }).env.ORT_DISABLE_TELEMETRY).toBe('1')
    expect(resolveSpawnSpec({ ...ctx, env: { ORT_DISABLE_TELEMETRY: '0' } }).env.ORT_DISABLE_TELEMETRY).toBe('0')
  })
})
