/** Mirrors the engine's app-data location (03 § Storage: platformdirs, `OPEN_SPEECH_HOME` override). */
import { homedir } from 'node:os'
import path from 'node:path'

export function engineHome(env: Record<string, string | undefined>, platform: NodeJS.Platform): string {
  if (env.OPEN_SPEECH_HOME) return path.resolve(env.OPEN_SPEECH_HOME.replace(/^~(?=$|\/)/, homedir()))
  if (platform === 'darwin') return path.join(homedir(), 'Library', 'Application Support', 'OpenSpeech')
  if (platform === 'win32') return path.join(env.LOCALAPPDATA ?? path.join(homedir(), 'AppData', 'Local'), 'OpenSpeech')
  return path.join(env.XDG_DATA_HOME ?? path.join(homedir(), '.local', 'share'), 'OpenSpeech')
}

export function rulesDir(env: Record<string, string | undefined>, platform: NodeJS.Platform): string {
  return path.join(engineHome(env, platform), 'rules')
}

/** True when `child` is `parent` or inside it (after resolving `..`). */
export function isInside(parent: string, child: string): boolean {
  const rel = path.relative(path.resolve(parent), path.resolve(child))
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel))
}
