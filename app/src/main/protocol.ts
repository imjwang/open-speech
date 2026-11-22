/**
 * `app://open-speech/…` serves the built renderer pages. The engine only accepts this origin from a
 * browser context (03 § Security), so production pages must come from here rather than file://.
 */
import { readFile } from 'node:fs/promises'
import path from 'node:path'

import { protocol } from 'electron'

import { buildCsp } from '../shared/csp'
import { isInside } from './paths'

export const APP_SCHEME = 'app'
export const APP_HOST = 'open-speech'
export const APP_ORIGIN = `${APP_SCHEME}://${APP_HOST}`

const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.wasm': 'application/wasm',
  '.bcmap': 'application/octet-stream',
  '.pfb': 'application/octet-stream',
}

/** Must run before `app.whenReady()`. */
export function registerAppSchemePrivileges(): void {
  protocol.registerSchemesAsPrivileged([
    {
      scheme: APP_SCHEME,
      privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, codeCache: true },
    },
  ])
}

/** Map an `app://open-speech/<path>` URL to a file under `rendererDir`, refusing traversal. */
export function resolveAppPath(rendererDir: string, requestUrl: string): string | null {
  let url: URL
  try {
    url = new URL(requestUrl)
  } catch {
    return null
  }
  if (url.protocol !== `${APP_SCHEME}:` || url.host !== APP_HOST) return null
  let pathname: string
  try {
    pathname = decodeURIComponent(url.pathname)
  } catch {
    return null
  }
  if (pathname.includes('\0')) return null
  if (pathname === '/' || pathname === '') pathname = '/commandbar/index.html'
  if (pathname.endsWith('/')) pathname += 'index.html'
  const file = path.resolve(rendererDir, `.${pathname}`)
  return isInside(rendererDir, file) ? file : null
}

export function appPageUrl(page: 'commandbar' | 'reader', query?: Record<string, string>): string {
  const qs = query && Object.keys(query).length ? `?${new URLSearchParams(query).toString()}` : ''
  return `${APP_ORIGIN}/${page}/index.html${qs}`
}

export function installAppProtocol(rendererDir: string): void {
  const csp = buildCsp({ dev: false, header: true })
  protocol.handle(APP_SCHEME, async (request) => {
    const file = resolveAppPath(rendererDir, request.url)
    if (!file || request.method !== 'GET') return new Response('Not found', { status: 404 })
    try {
      const body = await readFile(file)
      const ext = path.extname(file).toLowerCase()
      const headers = new Headers({
        'Content-Type': MIME[ext] ?? 'application/octet-stream',
        'X-Content-Type-Options': 'nosniff',
        'Cross-Origin-Opener-Policy': 'same-origin',
      })
      if (ext === '.html') headers.set('Content-Security-Policy', csp)
      return new Response(body, { status: 200, headers })
    } catch {
      return new Response('Not found', { status: 404 })
    }
  })
}
