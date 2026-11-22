import { mkdirSync, mkdtempSync, rmSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import path from 'node:path'

import { afterAll, describe, expect, it } from 'vitest'

import { augmentedPath, resolveSpawnSpec, type SpawnContext } from './spawn-spec'

const SERVE = ['serve', '--port', '0', '--handshake', 'stdout']

const root = mkdtempSync(path.join(tmpdir(), 'os-spawn-'))
const repoApp = path.join(root, 'repo', 'app')
mkdirSync(repoApp, { recursive: true })
mkdirSync(path.join(root, 'repo', 'engine'))
const lonelyApp = path.join(root, 'lonely', 'app')
mkdirSync(lonelyApp, { recursive: true })

afterAll(() => rmSync(root, { recursive: true, force: true }))

const ctx = (patch: Partial<SpawnContext> = {}): SpawnContext => ({
  isPackaged: false,
  resourcesPath: '/Applications/Open Speech.app/Contents/Resources',
  appPath: repoApp,
  env: { PATH: '/usr/bin:/bin', HOME: '/Users/test' },
  ...patch,
})

describe('augmentedPath', () => {
  it('adds the usual uv/Homebrew locations after the existing PATH', () => {
    const p = augmentedPath('/usr/bin:/bin').split(path.delimiter)
    expect(p.slice(0, 2)).toEqual(['/usr/bin', '/bin'])
    expect(p).toEqual(expect.arrayContaining([path.join(homedir(), '.local', 'bin'), path.join(homedir(), '.cargo', 'bin'), '/opt/homebrew/bin', '/usr/local/bin']))
  })

  it('does not duplicate entries and drops empty segments', () => {
    const p = augmentedPath('/opt/homebrew/bin::/usr/bin:').split(path.delimiter)
    expect(p.filter((x) => x === '/opt/homebrew/bin')).toHaveLength(1)
    expect(p).not.toContain('')
    expect(p[0]).toBe('/opt/homebrew/bin')
  })

  it('works without a PATH', () => {
    expect(augmentedPath(undefined).split(path.delimiter)).toContain('/usr/local/bin')
  })
})

describe('resolveSpawnSpec', () => {
  it('development: uv run against the sibling engine project', () => {
    const spec = resolveSpawnSpec(ctx())
    const engineDir = path.join(root, 'repo', 'engine')
    expect(spec.command).toBe('uv')
    expect(spec.args).toEqual(['run', '--project', engineDir, 'open-speech', ...SERVE])
    expect(spec.cwd).toBe(engineDir)
  })

  it('development: honours UV_BIN and falls back to the app dir when engine/ is missing', () => {
    const spec = resolveSpawnSpec(ctx({ appPath: lonelyApp, env: { UV_BIN: '/custom/uv' } }))
    expect(spec.command).toBe('/custom/uv')
    expect(spec.cwd).toBe(lonelyApp)
  })

  it('packaged: the bundled interpreter, isolated from user site-packages', () => {
    const spec = resolveSpawnSpec(ctx({ isPackaged: true }))
    const runtime = path.join('/Applications/Open Speech.app/Contents/Resources', 'engine')
    expect(spec.command).toBe(path.join(runtime, 'python', 'bin', 'python3'))
    expect(spec.args[0]).toBe('-c')
    expect(spec.args[1]).toContain('open_speech.cli.main')
    expect(spec.args.slice(2)).toEqual(SERVE)
    expect(spec.cwd).toBe(runtime)
    expect(spec.env).toMatchObject({ PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1', OPEN_SPEECH_BUNDLED_RUNTIME: runtime })
  })

  it('OPEN_SPEECH_ENGINE_COMMAND overrides both dev and packaged', () => {
    for (const isPackaged of [false, true]) {
      const spec = resolveSpawnSpec(ctx({ isPackaged, env: { OPEN_SPEECH_ENGINE_COMMAND: '  python3   -m open_speech.cli ' } }))
      expect(spec.command).toBe('python3')
      expect(spec.args).toEqual(['-m', 'open_speech.cli', ...SERVE])
      expect(spec.cwd).toBeUndefined()
    }
  })

  it('never passes Electron/Node switches to Python and does not mutate the input env', () => {
    const env = { PATH: '/usr/bin', ELECTRON_RUN_AS_NODE: '1', NODE_OPTIONS: '--inspect', KEEP: 'yes' }
    const spec = resolveSpawnSpec(ctx({ env }))
    expect(spec.env).not.toHaveProperty('ELECTRON_RUN_AS_NODE')
    expect(spec.env).not.toHaveProperty('NODE_OPTIONS')
    expect(spec.env.KEEP).toBe('yes')
    expect(spec.env.PATH).toContain('/opt/homebrew/bin')
    expect(env).toEqual({ PATH: '/usr/bin', ELECTRON_RUN_AS_NODE: '1', NODE_OPTIONS: '--inspect', KEEP: 'yes' })
  })

  it('joins extra origins with commas, merged with existing ones and deduplicated', () => {
    const spec = resolveSpawnSpec(
      ctx({ env: { OPEN_SPEECH_ALLOWED_ORIGINS: 'http://a.test, http://b.test,' }, extraOrigins: ['http://b.test', 'http://localhost:5173'] }),
    )
    expect(spec.env.OPEN_SPEECH_ALLOWED_ORIGINS).toBe('http://a.test,http://b.test,http://localhost:5173')

    expect(resolveSpawnSpec(ctx({ extraOrigins: ['http://x', 'http://y'] })).env.OPEN_SPEECH_ALLOWED_ORIGINS).toBe('http://x,http://y')
  })

  it('leaves OPEN_SPEECH_ALLOWED_ORIGINS alone without extra origins', () => {
    expect(resolveSpawnSpec(ctx({ extraOrigins: [] })).env).not.toHaveProperty('OPEN_SPEECH_ALLOWED_ORIGINS')
    expect(resolveSpawnSpec(ctx({ env: { OPEN_SPEECH_ALLOWED_ORIGINS: ' http://a ' } })).env.OPEN_SPEECH_ALLOWED_ORIGINS).toBe(' http://a ')
  })
})
