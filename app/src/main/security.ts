/**
 * Electron hardening (03 § Security, NFR-4): permissions denied by default (the microphone only for
 * the command bar), no new windows, no navigation away from the shell's own pages, external links in
 * the OS browser, no <webview>.
 */
import { app, session, shell, systemPreferences, type Session, type WebContents } from 'electron'

import { APP_ORIGIN } from './protocol'

export const WEB_PARTITION = 'persist:web'

export function isSafeExternalUrl(raw: string): boolean {
  try {
    const url = new URL(raw)
    return url.protocol === 'https:' || url.protocol === 'http:' || url.protocol === 'mailto:'
  } catch {
    return false
  }
}

export function openExternalSafely(raw: string): void {
  if (isSafeExternalUrl(raw)) void shell.openExternal(raw)
}

/** Origins the shell's own pages may be served from (app:// in production, Vite in development). */
export function shellOrigins(devServerUrl: string | undefined): string[] {
  const origins = [APP_ORIGIN]
  if (devServerUrl) {
    try {
      origins.push(new URL(devServerUrl).origin)
    } catch {
      // ignore malformed env
    }
  }
  return origins
}

function originOf(url: string): string | null {
  try {
    return new URL(url).origin
  } catch {
    return null
  }
}

async function ensureMacMicrophoneAccess(): Promise<boolean> {
  if (process.platform !== 'darwin') return true
  const status = systemPreferences.getMediaAccessStatus('microphone')
  if (status === 'granted') return true
  if (status === 'denied' || status === 'restricted') return false
  return systemPreferences.askForMediaAccess('microphone')
}

export interface SecurityOptions {
  devServerUrl: string | undefined
  /** The command bar's webContents id; only it may use the microphone. */
  micAllowed: (contents: WebContents | null) => boolean
  /** Web-reader contents may navigate anywhere http(s) inside their own view. */
  isWebView: (contents: WebContents) => boolean
}

export function installSecurity(opts: SecurityOptions): Session {
  const allowedOrigins = new Set(shellOrigins(opts.devServerUrl))

  const defaultSession = session.defaultSession
  defaultSession.setPermissionRequestHandler((contents, permission, callback, details) => {
    const mediaTypes = 'mediaTypes' in details ? (details.mediaTypes ?? []) : []
    const audioOnly = mediaTypes.length > 0 && mediaTypes.every((t) => t === 'audio')
    if (permission === 'media' && audioOnly && opts.micAllowed(contents)) {
      void ensureMacMicrophoneAccess().then(callback, () => callback(false))
      return
    }
    callback(false)
  })
  defaultSession.setPermissionCheckHandler((contents, permission) => {
    return permission === 'media' && opts.micAllowed(contents)
  })

  const webSession = session.fromPartition(WEB_PARTITION)
  webSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
  webSession.setPermissionCheckHandler(() => false)
  webSession.setDevicePermissionHandler(() => false)
  // Downloads keep Electron's default behaviour: a save dialog prompts the user.

  app.on('web-contents-created', (_event, contents) => {
    contents.on('will-attach-webview', (event) => event.preventDefault())
    contents.setWindowOpenHandler(({ url }) => {
      // Links that try to open windows (target=_blank, window.open) go to the OS browser instead.
      openExternalSafely(url)
      return { action: 'deny' }
    })
    contents.on('will-navigate', (event, url) => {
      if (opts.isWebView(contents)) {
        if (!/^https?:\/\//i.test(url)) event.preventDefault()
        return
      }
      const origin = originOf(url)
      if (!origin || !allowedOrigins.has(origin)) {
        event.preventDefault()
        openExternalSafely(url)
      }
    })
    contents.on('will-redirect', (event, url) => {
      if (!opts.isWebView(contents) && !allowedOrigins.has(originOf(url) ?? '')) event.preventDefault()
    })
  })
  return webSession
}
