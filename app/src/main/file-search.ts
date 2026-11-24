/**
 * The `@` file menu in the command bar: find things to read by name, or walk folders.
 *
 * - Nothing typed: the usual places (Desktop, Documents, Downloads, Home) and what changed there lately.
 * - A path (`~/Doc`, `/Volumes/…`, or anything with a slash): the entries of that folder that match the last
 *   segment, folders first, so typing or pressing Enter on a folder walks into it.
 * - Anything else: a search by file name across the home folder (Spotlight's index via `mdfind`, with a
 *   shallow scan of the usual places when Spotlight has nothing).
 *
 * Only folders and files Open Speech can read are returned, and only names and paths: nothing here opens a
 * file. Browsing is confined to the home folder and mounted volumes; hidden files never appear.
 */
import type { Dirent, Stats } from 'node:fs'
import path from 'node:path'

import type { FileHit, FileKind, FileSearchResult } from '../shared/ipc-channels'

export interface FileSearchDeps {
  home: string
  /** Run a command and return its stdout (rejects on failure or timeout). */
  run(command: string, args: string[], timeoutMs: number): Promise<string>
  readdir(dir: string): Promise<Dirent[]>
  stat(file: string): Promise<Stats>
}

const KINDS: Record<string, FileKind> = {
  '.pdf': 'pdf',
  '.docx': 'doc',
  '.txt': 'text',
  '.md': 'text',
  '.markdown': 'text',
  '.html': 'web',
  '.htm': 'web',
  '.png': 'image',
  '.jpg': 'image',
  '.jpeg': 'image',
  '.webp': 'image',
  '.gif': 'image',
  // A recording to make a voice from ("clone this voice").
  '.wav': 'audio',
  '.m4a': 'audio',
  '.mp3': 'audio',
  '.aiff': 'audio',
  '.aif': 'audio',
  '.flac': 'audio',
  '.caf': 'audio',
}
/** Folders that are really one item (apps, libraries): not places to browse for documents. */
const BUNDLE = /\.(app|bundle|framework|photoslibrary|musiclibrary|tvlibrary|xcodeproj|xcworkspace|lproj|pkg|kext|plugin)$/i
const NOISE = /(^|\/)(Library|node_modules|\.[^/]+|__pycache__|venv)(\/|$)/
const PLACES = ['Desktop', 'Documents', 'Downloads'] as const
export const MAX_HITS = 40
const RECENT = 8

export function fileKind(name: string, isDirectory: boolean): FileKind | null {
  if (isDirectory) return BUNDLE.test(name) ? null : 'folder'
  return KINDS[path.extname(name).toLowerCase()] ?? null
}

function tilde(p: string, home: string): string {
  return p === home ? '~' : p.startsWith(home + path.sep) ? `~${p.slice(home.length)}` : p
}

function expand(p: string, home: string): string {
  if (p === '~') return home
  return p.startsWith('~/') ? path.join(home, p.slice(2)) : p
}

function allowed(dir: string, home: string): boolean {
  const resolved = path.resolve(dir)
  return resolved === home || resolved.startsWith(home + path.sep) || resolved === '/Volumes' || resolved.startsWith('/Volumes/')
}

function hit(file: string, kind: FileKind, home: string): FileHit {
  return { path: file, name: path.basename(file), kind, where: tilde(path.dirname(file), home), short: tilde(file, home) }
}

async function listFolder(dir: string, prefix: string, deps: FileSearchDeps): Promise<FileHit[]> {
  let entries: Dirent[]
  try {
    entries = await deps.readdir(dir)
  } catch {
    return []
  }
  const needle = prefix.toLowerCase()
  const hits: Array<FileHit & { rank: number }> = []
  for (const entry of entries) {
    if (entry.name.startsWith('.')) continue
    // Like Finder, the home folder does not show its Library.
    if (dir === deps.home && entry.name === 'Library') continue
    const kind = fileKind(entry.name, entry.isDirectory())
    if (!kind) continue
    const lower = entry.name.toLowerCase()
    const at = needle ? lower.indexOf(needle) : 0
    if (at < 0) continue
    hits.push({ ...hit(path.join(dir, entry.name), kind, deps.home), rank: (at === 0 ? 0 : 2) + (kind === 'folder' ? 0 : 1) })
  }
  hits.sort((a, b) => a.rank - b.rank || a.name.localeCompare(b.name, undefined, { numeric: true }))
  return hits.slice(0, MAX_HITS).map(({ rank: _rank, ...h }) => h)
}

async function recentFiles(deps: FileSearchDeps): Promise<FileHit[]> {
  const found: Array<FileHit & { mtime: number }> = []
  await Promise.all(
    PLACES.map(async (place) => {
      const dir = path.join(deps.home, place)
      let entries: Dirent[]
      try {
        entries = await deps.readdir(dir)
      } catch {
        return
      }
      await Promise.all(
        entries.map(async (entry) => {
          if (entry.name.startsWith('.') || entry.isDirectory()) return
          const kind = fileKind(entry.name, false)
          if (!kind) return
          try {
            const st = await deps.stat(path.join(dir, entry.name))
            found.push({ ...hit(path.join(dir, entry.name), kind, deps.home), mtime: st.mtimeMs })
          } catch {
            // vanished between readdir and stat
          }
        }),
      )
    }),
  )
  found.sort((a, b) => b.mtime - a.mtime)
  return found.slice(0, RECENT).map(({ mtime: _mtime, ...h }) => h)
}

async function searchByName(name: string, deps: FileSearchDeps): Promise<FileHit[]> {
  const needle = name.toLowerCase()
  const safe = name.replace(/["\\*]/g, '')
  let lines: string[] = []
  if (safe) {
    try {
      const out = await deps.run('mdfind', ['-onlyin', deps.home, `kMDItemFSName == "*${safe}*"cd`], 1500)
      lines = out.split('\n').filter(Boolean)
    } catch {
      lines = []
    }
  }
  const candidates = lines.filter((p) => !NOISE.test(p.slice(deps.home.length))).slice(0, 300)
  const hits: Array<FileHit & { rank: number; mtime: number }> = []
  await Promise.all(
    candidates.map(async (file) => {
      try {
        const st = await deps.stat(file)
        const kind = fileKind(path.basename(file), st.isDirectory())
        if (!kind) return
        const lower = path.basename(file).toLowerCase()
        hits.push({ ...hit(file, kind, deps.home), rank: lower.startsWith(needle) ? 0 : 1, mtime: st.mtimeMs })
      } catch {
        // not there any more
      }
    }),
  )
  if (hits.length === 0) {
    // Spotlight is off or still indexing: look in the usual places ourselves (two levels deep).
    for (const place of PLACES) {
      const top = await listFolder(path.join(deps.home, place), '', deps)
      for (const h of top) {
        if (h.name.toLowerCase().includes(needle)) hits.push({ ...h, rank: h.name.toLowerCase().startsWith(needle) ? 0 : 1, mtime: 0 })
        if (h.kind !== 'folder') continue
        for (const inner of await listFolder(h.path, name, deps)) hits.push({ ...inner, rank: 1, mtime: 0 })
      }
    }
  }
  hits.sort((a, b) => a.rank - b.rank || b.mtime - a.mtime || a.name.localeCompare(b.name))
  const seen = new Set<string>()
  return hits
    .filter((h) => !seen.has(h.path) && seen.add(h.path))
    .slice(0, MAX_HITS)
    .map(({ rank: _rank, mtime: _mtime, ...h }) => h)
}

export async function searchFiles(query: string, deps: FileSearchDeps): Promise<FileSearchResult> {
  const q = query.trim()
  if (!q) {
    const places: FileHit[] = [
      ...PLACES.map((p) => hit(path.join(deps.home, p), 'folder', deps.home)),
      { path: deps.home, name: path.basename(deps.home), kind: 'folder', where: '~', short: '~' },
    ]
    return { folder: null, hits: [...(await recentFiles(deps)), ...places] }
  }
  if (q.startsWith('~') || q.startsWith('/') || q.includes('/')) {
    const expanded = expand(q.startsWith('~') || q.startsWith('/') ? q : `~/${q}`, deps.home)
    const endsInSlash = q.endsWith('/') || q === '~'
    const dir = endsInSlash ? expanded : path.dirname(expanded)
    const prefix = endsInSlash ? '' : path.basename(expanded)
    if (!allowed(dir, deps.home)) return { folder: tilde(path.resolve(dir), deps.home), hits: [] }
    const resolved = path.resolve(dir)
    return { folder: `${tilde(resolved, deps.home)}/`.replace(/\/\/$/, '/'), hits: await listFolder(resolved, prefix, deps) }
  }
  return { folder: null, hits: await searchByName(q, deps) }
}
