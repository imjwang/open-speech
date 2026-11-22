import { BookOpen, ListMusic, Minus, Pause, Play, Plus, SkipBack, SkipForward } from 'lucide-react'

import type { PlaybackState } from '@shared/api/client'

import { IconButton } from '@/components/ui/icon-button'
import { formatDuration, formatSpeed } from '@/format'
import { useT } from '@/i18n'

export interface TransportHandlers {
  toggle(): void
  skip(count: number): void
  speed(delta: number): void
  next(): void
  openReader(): void
}

/**
 * What is being read, with the transport: always there while something is open, so play/pause, a sentence
 * back or forward, the speed and the next document in the queue are one press away without a command.
 */
export function NowPlaying({ playback, handlers }: { playback: PlaybackState; handlers: TransportHandlers }) {
  const t = useT()
  const playing = playback.state === 'playing' || playback.state === 'loading'
  const finished = !playing && (playback.progress ?? 0) >= 0.999
  const queue = playback.queue ?? []
  const left = finished ? t('row.finished') : playback.time_left_s != null ? t('row.left', { time: formatDuration(playback.time_left_s) }) : ''
  return (
    <section aria-label={playback.document_title ?? ''} className="border-t" data-testid="now-playing">
      <div className="h-0.5 bg-muted" aria-hidden="true">
        <div className="h-full bg-primary transition-[width] duration-500" style={{ width: `${Math.round((playback.progress ?? 0) * 100)}%` }} />
      </div>
      <div className="flex items-center gap-1 px-3 py-1.5">
        <IconButton label={t('row.previous_sentence')} onClick={() => handlers.skip(-1)} tabIndex={-1}>
          <SkipBack />
        </IconButton>
        <IconButton
          label={playing ? t('row.pause') : t('row.play')}
          onClick={handlers.toggle}
          tabIndex={-1}
          className="bg-primary text-primary-foreground hover:bg-primary/90 hover:text-primary-foreground"
        >
          {playing ? <Pause className="fill-current" /> : <Play className="fill-current" />}
        </IconButton>
        <IconButton label={t('row.next_sentence')} onClick={() => handlers.skip(1)} tabIndex={-1}>
          <SkipForward />
        </IconButton>
        <button
          type="button"
          tabIndex={-1}
          title={t('row.open_reader')}
          className="mx-1 flex min-w-0 flex-1 items-center gap-2 rounded-md px-1.5 py-1 text-left hover:bg-accent"
          onClick={handlers.openReader}
        >
          <BookOpen aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
          <span className="min-w-0 truncate text-sm">{playback.document_title ?? ''}</span>
          <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">{left}</span>
        </button>
        {queue.length > 0 && (
          <IconButton label={`${t('row.next')} · ${queue[0]?.title ?? ''}`} onClick={handlers.next} tabIndex={-1} className="w-auto gap-1 px-1.5">
            <ListMusic />
            <span className="text-xs tabular-nums" aria-label={t('row.queue_more', { n: queue.length })}>
              {queue.length}
            </span>
          </IconButton>
        )}
        <IconButton label={t('row.slower')} onClick={() => handlers.speed(-0.25)} tabIndex={-1} className="size-6">
          <Minus className="size-3.5" />
        </IconButton>
        <span className="min-w-9 text-center text-xs tabular-nums" aria-live="off">
          {formatSpeed(playback.speed)}
        </span>
        <IconButton label={t('row.faster')} onClick={() => handlers.speed(0.25)} tabIndex={-1} className="size-6">
          <Plus className="size-3.5" />
        </IconButton>
      </div>
    </section>
  )
}
