/** Renderer build pieces shared by electron-vite (Electron) and the plain-Vite browser build (`dev:web`). */
import { cpSync, createReadStream, existsSync, statSync } from 'node:fs'
import { extname, join, normalize, resolve } from 'node:path'

import babel from '@rolldown/plugin-babel'
import tailwindcss from '@tailwindcss/vite'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import type { Plugin, PluginOption } from 'vite'

import { buildCsp } from '../src/shared/csp'

/** The app/ directory. Config files get bundled (and moved), so search upward instead of using '..'. */
function findAppRoot(start: string): string {
  let dir = start
  for (let i = 0; i < 6; i++) {
    if (existsSync(join(dir, 'electron.vite.config.ts')) && existsSync(join(dir, 'package.json'))) return dir
    dir = resolve(dir, '..')
  }
  return process.cwd()
}

export const appRoot = findAppRoot(import.meta.dirname)
export const rendererRoot = resolve(appRoot, 'src/renderer')

export const alias = {
  '@shared': resolve(appRoot, 'src/shared'),
  '@': resolve(appRoot, 'src/renderer/shared'),
}

/** Never inline worklets as data: URLs (the CSP has no `data:` in script-src). */
export const assetsInlineLimit = (file: string): boolean | undefined => (file.endsWith('.worklet.js') ? false : undefined)

export const rendererPages = {
  commandbar: resolve(rendererRoot, 'commandbar/index.html'),
  reader: resolve(rendererRoot, 'reader/index.html'),
  rules: resolve(rendererRoot, 'rules/index.html'),
}

/** Injects the CSP <meta> tag: strict for builds, HMR-friendly in dev. */
export function cspPlugin(): Plugin {
  let dev = false
  return {
    name: 'open-speech:csp',
    configResolved(config) {
      dev = config.command === 'serve'
    },
    transformIndexHtml() {
      return [
        {
          tag: 'meta',
          attrs: { 'http-equiv': 'Content-Security-Policy', content: buildCsp({ dev }) },
          injectTo: 'head-prepend',
        },
      ]
    },
  }
}

const PDFJS_DIRS = ['cmaps', 'standard_fonts', 'wasm', 'iccs']
const PDFJS_MIME: Record<string, string> = {
  '.wasm': 'application/wasm',
  '.bcmap': 'application/octet-stream',
  '.pfb': 'application/octet-stream',
  '.ttf': 'font/ttf',
  '.icc': 'application/vnd.iccprofile',
}

/** Serves (dev) and copies (build) pdf.js runtime assets under `/pdfjs/` so nothing loads remotely. */
export function pdfjsAssetsPlugin(): Plugin {
  const pkg = resolve(appRoot, 'node_modules/pdfjs-dist')
  let outDir = ''
  return {
    name: 'open-speech:pdfjs-assets',
    configResolved(config) {
      outDir = config.build.outDir
    },
    configureServer(server) {
      server.middlewares.use('/pdfjs', (req, res, next) => {
        const rel = normalize(decodeURIComponent((req.url ?? '/').split('?')[0] ?? '/')).replace(/^([/\\])+/, '')
        const top = rel.split(/[/\\]/)[0] ?? ''
        const file = join(pkg, rel)
        if (!PDFJS_DIRS.includes(top) || !file.startsWith(pkg) || !existsSync(file) || !statSync(file).isFile()) return next()
        res.setHeader('Content-Type', PDFJS_MIME[extname(file)] ?? 'application/octet-stream')
        createReadStream(file).pipe(res)
      })
    },
    writeBundle() {
      for (const dir of PDFJS_DIRS) {
        const from = join(pkg, dir)
        if (existsSync(from)) cpSync(from, join(outDir, 'pdfjs', dir), { recursive: true })
      }
    },
  }
}

export function rendererPlugins(): PluginOption[] {
  // React Compiler via babel-plugin-react-compiler (03 § Shell).
  return [react(), babel({ presets: [reactCompilerPreset()] }), tailwindcss(), cspPlugin(), pdfjsAssetsPlugin()]
}
