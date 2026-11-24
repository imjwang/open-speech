/**
 * Development aid for the UI driver's `renders` op (app/e2e/driver.mjs): how many times each component
 * rendered. Only on when the page URL carries `debug=1`, which the main process adds to unpackaged builds;
 * a packaged app never counts anything.
 */
const enabled = typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('debug') === '1'
const counts: Record<string, number> = {}
if (enabled) (window as unknown as { __openSpeechRenders?: Record<string, number> }).__openSpeechRenders = counts

/** Count one render of `name`. A hook so the React Compiler keeps it on every render. */
export function useRenderCount(name: string): void {
  if (enabled) counts[name] = (counts[name] ?? 0) + 1
}

export function renderCounts(): Record<string, number> | null {
  return enabled ? counts : null
}

/** Count one event (e.g. a `playback.word`), next to the render counts, so renders can be read per event. */
export function countEvent(name: string): void {
  if (enabled) counts[name] = (counts[name] ?? 0) + 1
}
