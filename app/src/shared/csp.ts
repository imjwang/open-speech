/**
 * Content-Security-Policy for the shell's own pages (03 § Security: strict CSP, no remote code).
 *
 * The renderer talks only to the loopback engine (`http://127.0.0.1:<port>` + `ws://`). pdf.js needs
 * `'wasm-unsafe-eval'` for its image decoders and a same-origin worker; nothing else is relaxed in
 * production. Development adds what Vite's HMR client needs.
 */
export interface CspOptions {
  dev: boolean
  /** Include directives that only work as an HTTP header (not in a <meta> tag). */
  header?: boolean
}

const LOOPBACK = ['http://127.0.0.1:*', 'ws://127.0.0.1:*', 'http://localhost:*', 'ws://localhost:*']

export function buildCsp({ dev, header = false }: CspOptions): string {
  const directives: Record<string, string[]> = {
    'default-src': ["'self'"],
    'script-src': ["'self'", "'wasm-unsafe-eval'", ...(dev ? ["'unsafe-inline'"] : [])],
    'style-src': ["'self'", "'unsafe-inline'"],
    'img-src': ["'self'", 'data:', 'blob:'],
    'font-src': ["'self'", 'data:'],
    'connect-src': ["'self'", ...LOOPBACK],
    'media-src': ["'self'", 'blob:'],
    'worker-src': ["'self'", 'blob:'],
    'object-src': ["'none'"],
    'base-uri': ["'none'"],
    'form-action': ["'none'"],
    'frame-src': ["'none'"],
  }
  if (header) directives['frame-ancestors'] = ["'none'"]
  return Object.entries(directives)
    .map(([name, values]) => `${name} ${values.join(' ')}`)
    .join('; ')
}
