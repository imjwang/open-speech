/**
 * What the shell itself remembers between launches, apart from the engine's settings: a small typed JSON file.
 * It exists for what the main process needs *before* the engine answers (the theme to paint the first frame
 * in, whether the bar can open the microphone the instant it opens, whether this is still the first run) and
 * for shell-only preferences (the month's spending beside the menu-bar icon).
 *
 * The file lives with the profile: `$OPEN_SPEECH_HOME/shell-state.json` when that is set (a fresh profile for
 * tests starts fresh here too), else Electron's userData folder. Unknown or invalid fields fall back to their
 * defaults, so adding a field is one line in `ShellStateSchema`.
 */
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import path from 'node:path'

import { z } from 'zod'

export const ThemePrefSchema = z.enum(['system', 'lavender', 'ember'])
export type ThemePref = z.infer<typeof ThemePrefSchema>

export const ShellStateSchema = z.object({
  /** The engine's `appearance.theme`, as last seen: applied before any window exists. */
  themePref: ThemePrefSchema.catch('system').default('system'),
  /** Show this month's spending next to the menu-bar icon (tray menu checkbox). Off by default. */
  traySpend: z.boolean().catch(false).default(false),
  /** Whether this Mac can listen (`capabilities.speech_input.available`), as last seen. Null: never seen; the
   *  shell then assumes what the build implies (`canListenFallback`). */
  canListen: z.boolean().nullable().catch(null).default(null),
  /** The engine's `onboarding_completed`, as last seen. Null: never seen (wait for the engine before deciding
   *  that this is a first run, so an existing profile never sees the bar pop open). */
  onboardingCompleted: z.boolean().nullable().catch(null).default(null),
})
export type ShellState = z.infer<typeof ShellStateSchema>

export const SHELL_STATE_FILE = 'shell-state.json'

/**
 * Whether the bar may open the microphone the instant it opens, before the engine has said anything: what it
 * said last time, else yes in packaged builds (they bundle the local speech model) and no in development.
 */
export function canListenFallback(state: Pick<ShellState, 'canListen'>, isPackaged: boolean): boolean {
  return state.canListen ?? isPackaged
}

export function shellStatePath(env: Record<string, string | undefined>, userData: string): string {
  return path.join(env.OPEN_SPEECH_HOME ? path.resolve(env.OPEN_SPEECH_HOME) : userData, SHELL_STATE_FILE)
}

/** Anything (a missing file, garbage, an older or newer version) → a complete, valid state. */
export function parseShellState(raw: unknown): ShellState {
  const result = ShellStateSchema.safeParse(raw && typeof raw === 'object' ? raw : {})
  return result.success ? result.data : ShellStateSchema.parse({})
}

export interface ShellStateFs {
  read(file: string): string | null
  write(file: string, text: string): void
}

const nodeFs: ShellStateFs = {
  read(file) {
    try {
      return readFileSync(file, 'utf8')
    } catch {
      return null
    }
  },
  write(file, text) {
    mkdirSync(path.dirname(file), { recursive: true })
    const tmp = `${file}.tmp`
    writeFileSync(tmp, text, 'utf8')
    renameSync(tmp, file) // atomic: a crash mid-write never leaves half a file
  },
}

export class ShellStateStore {
  private state: ShellState

  constructor(
    private readonly file: string,
    private readonly fs: ShellStateFs = nodeFs,
    private readonly warn: (message: string) => void = () => undefined,
  ) {
    const text = fs.read(file)
    let raw: unknown = {}
    if (text) {
      try {
        raw = JSON.parse(text)
      } catch {
        warn(`${SHELL_STATE_FILE} is not valid JSON; using defaults`)
      }
    }
    this.state = parseShellState(raw)
  }

  get(): ShellState {
    return this.state
  }

  /** Change some fields; writes the file only when something actually changed. */
  update(patch: Partial<ShellState>): ShellState {
    const next = parseShellState({ ...this.state, ...patch })
    if (JSON.stringify(next) === JSON.stringify(this.state)) return this.state
    this.state = next
    try {
      this.fs.write(this.file, `${JSON.stringify(next, null, 2)}\n`)
    } catch (err) {
      this.warn(`could not save ${SHELL_STATE_FILE}: ${(err as Error).message}`)
    }
    return next
  }
}
