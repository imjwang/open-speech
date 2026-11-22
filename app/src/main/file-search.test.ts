import type { Dirent, Stats } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { fileKind, searchFiles, type FileSearchDeps } from './file-search'

const HOME = '/Users/me'

/** An in-memory disk: folder path → entries ("name/" is a folder), with optional mtimes by full path. */
function disk(tree: Record<string, string[]>, opts: { mdfind?: string[] | Error; mtimes?: Record<string, number> } = {}): FileSearchDeps & { ran: string[][] } {
  const isDir = (p: string) => p in tree
  const ran: string[][] = []
  return {
    home: HOME,
    ran,
    run: async (command, args) => {
      ran.push([command, ...args])
      if (opts.mdfind instanceof Error) throw opts.mdfind
      return (opts.mdfind ?? []).join('\n')
    },
    readdir: async (dir) => {
      const entries = tree[dir]
      if (!entries) throw new Error('ENOENT')
      return entries.map((e) => {
        const folder = e.endsWith('/')
        return { name: folder ? e.slice(0, -1) : e, isDirectory: () => folder } as Dirent
      })
    },
    stat: async (file) => {
      const known = isDir(file) || Object.values(tree).some((entries, i) => entries.includes(path.basename(file)) && Object.keys(tree)[i] === path.dirname(file))
      if (!known) throw new Error('ENOENT')
      return { isDirectory: () => isDir(file), mtimeMs: opts.mtimes?.[file] ?? 0 } as Stats
    },
  }
}

const TREE = {
  [HOME]: ['Desktop/', 'Documents/', 'Downloads/', '.ssh/', 'Library/'],
  [`${HOME}/Desktop`]: ['shot.png', '.DS_Store'],
  [`${HOME}/Documents`]: ['Papers/', 'draft.md', 'budget.xlsx', 'Notes.app/'],
  [`${HOME}/Documents/Papers`]: ['attention.pdf', 'Attention notes.txt', 'data.csv'],
  [`${HOME}/Downloads`]: ['invoice.pdf'],
  [`${HOME}/Library`]: ['Caches/'],
  [`${HOME}/Library/Caches`]: ['attention.pdf'],
  [`${HOME}/.cache`]: ['attention.md'],
}

describe('fileKind', () => {
  it('names what can be read, and nothing else', () => {
    expect(fileKind('a.PDF', false)).toBe('pdf')
    expect(fileKind('a.docx', false)).toBe('doc')
    expect(fileKind('a.md', false)).toBe('text')
    expect(fileKind('a.html', false)).toBe('web')
    expect(fileKind('a.webp', false)).toBe('image')
    expect(fileKind('a.zip', false)).toBeNull()
    expect(fileKind('Papers', true)).toBe('folder')
    expect(fileKind('Notes.app', true)).toBeNull() // a bundle, not a place to browse
  })
})

describe('searchFiles', () => {
  it('nothing typed: recent readable files from the usual places, newest first, then the places', async () => {
    const mtimes = { [`${HOME}/Downloads/invoice.pdf`]: 300, [`${HOME}/Documents/draft.md`]: 200, [`${HOME}/Desktop/shot.png`]: 100 }
    const { folder, hits } = await searchFiles('', disk(TREE, { mtimes }))
    expect(folder).toBeNull()
    expect(hits.map((h) => h.name)).toEqual(['invoice.pdf', 'draft.md', 'shot.png', 'Desktop', 'Documents', 'Downloads', 'me'])
    expect(hits.at(-1)).toMatchObject({ kind: 'folder', short: '~' })
    expect(hits[0]).toMatchObject({ kind: 'pdf', where: '~/Downloads', short: '~/Downloads/invoice.pdf' })
  })

  it('a path browses that folder: folders first, readable files only, hidden files never', async () => {
    const { folder, hits } = await searchFiles('~/Documents/', disk(TREE))
    expect(folder).toBe('~/Documents/')
    expect(hits.map((h) => `${h.kind}:${h.name}`)).toEqual(['folder:Papers', 'text:draft.md'])
    // "~/" is the home folder itself; like Finder, it does not show the Library.
    const home = await searchFiles('~/', disk(TREE))
    expect(home.folder).toBe('~/')
    expect(home.hits.map((h) => h.name)).toEqual(['Desktop', 'Documents', 'Downloads'])
    expect((await searchFiles('~', disk(TREE))).hits.map((h) => h.name)).toEqual(['Desktop', 'Documents', 'Downloads'])
  })

  it('the last path segment filters, case-insensitively, prefix matches first', async () => {
    const { folder, hits } = await searchFiles('~/Documents/Papers/att', disk(TREE))
    expect(folder).toBe('~/Documents/Papers/')
    expect(hits.map((h) => h.name)).toEqual(['Attention notes.txt', 'attention.pdf'])
    const d = disk({ ...TREE, [`${HOME}/Notes`]: ['meeting notes.md', 'Notes 2.txt', 'notes 10.txt', 'Notebooks/'] })
    expect((await searchFiles('~/Notes/note', d)).hits.map((h) => h.name)).toEqual(['Notebooks', 'Notes 2.txt', 'notes 10.txt', 'meeting notes.md'])
  })

  it('never lists anything outside the home folder and mounted volumes', async () => {
    const d = disk({ ...TREE, '/etc': ['passwd.txt'], '/private/tmp': ['x.pdf'] })
    expect((await searchFiles('/etc/', d)).hits).toEqual([])
    expect((await searchFiles('/private/tmp/', d)).hits).toEqual([])
    expect((await searchFiles('~/../../etc/', d)).hits).toEqual([])
  })

  it('a name searches Spotlight under the home folder, skipping library and hidden paths', async () => {
    const found = [`${HOME}/Documents/Papers/attention.pdf`, `${HOME}/Library/Caches/attention.pdf`, `${HOME}/.cache/attention.md`, `${HOME}/Documents/Papers/data.csv`]
    const d = disk(TREE, { mdfind: found })
    const { hits } = await searchFiles('attention', d)
    expect(d.ran[0]?.slice(0, 3)).toEqual(['mdfind', '-onlyin', HOME])
    expect(hits.map((h) => h.path)).toEqual([`${HOME}/Documents/Papers/attention.pdf`])
  })

  it('quotes in the name cannot break out of the Spotlight query', async () => {
    const d = disk(TREE, { mdfind: [] })
    await searchFiles('a"*\\b', d)
    expect(d.ran[0]?.[3]).toBe('kMDItemFSName == "*ab*"cd')
  })

  it('falls back to scanning the usual places when Spotlight is unavailable', async () => {
    const { hits } = await searchFiles('attention', disk(TREE, { mdfind: new Error('mdfind: not found') }))
    expect(hits.map((h) => h.name).sort()).toEqual(['Attention notes.txt', 'attention.pdf'])
  })
})
