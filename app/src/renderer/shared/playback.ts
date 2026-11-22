import type { PlaybackState } from '@shared/api/client'
import type { PlaybackState as WsPlaybackState } from '@shared/api/ws.gen'

export const IDLE_PLAYBACK: PlaybackState = {
  state: 'idle',
  document_id: null,
  document_title: null,
  utterance_id: null,
  utterance_index: 0,
  utterance_count: 0,
  offset_ms: 0,
  speed: 1,
  voice_id: null,
  time_left_s: null,
  page: null,
  page_count: null,
  heading: null,
  progress: 0,
  fallback_notice: null,
  error: null,
}

/** WS `playback.state` (fields with defaults may be omitted) → the REST shape with defaults filled. */
export function normalizePlayback(p: WsPlaybackState | PlaybackState): PlaybackState {
  return {
    ...IDLE_PLAYBACK,
    ...Object.fromEntries(Object.entries(p).filter(([, v]) => v !== undefined)),
  } as PlaybackState
}

export function isActive(p: PlaybackState | null): boolean {
  return !!p && (p.state === 'playing' || p.state === 'loading')
}
