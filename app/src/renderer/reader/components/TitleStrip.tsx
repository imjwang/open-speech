import { ArrowDownToLine, FileText, Globe, Mic, Pause, Play, ScrollText } from 'lucide-react'

import type { PlaybackState } from '@shared/api/client'

import { IconButton } from '@/components/ui/icon-button'
import { formatDuration, formatSpeed } from '@/format'
import { useT } from '@/i18n'
import { cn } from '@/lib/utils'

/**
 * Thin title strip (09 § Reader): play/pause, the title, where the reading is, and three icon toggles
 * (follow the reading, live page ↔ extracted text, what is spoken) plus the microphone, which opens the
 * command bar on this document. Draggable on macOS.
 */
export function TitleStrip({
  title,
  playback,
  isPlayingThis,
  pageCount,
  scriptView,
  onToggleScript,
  web,
  webMode,
  onToggleWebMode,
  followReleased,
  onBackToPlayhead,
  onPlayPause,
  onCommand,
  mac,
}: {
  title: string
  playback: PlaybackState
  isPlayingThis: boolean
  pageCount: number | null
  scriptView: boolean
  onToggleScript(): void
  web: boolean
  webMode: 'page' | 'text'
  onToggleWebMode(): void
  followReleased: boolean
  onBackToPlayhead(): void
  onPlayPause(): void
  onCommand(): void
  mac: boolean
}) {
  const t = useT()
  const playingThis = isPlayingThis && (playback.state === 'playing' || playback.state === 'loading')
  const page = isPlayingThis ? playback.page : null
  const pages = (isPlayingThis ? playback.page_count : null) ?? pageCount
  const position = page && pages ? `${page}/${pages}` : pages ? String(pages) : null
  const parts = [
    position,
    isPlayingThis ? formatSpeed(playback.speed) : null,
    isPlayingThis && playback.time_left_s != null ? formatDuration(playback.time_left_s) : null,
  ].filter(Boolean)
  return (
    <header
      className={cn(
        'flex h-10 shrink-0 items-center gap-2 border-b bg-background pr-2 text-sm text-foreground [-webkit-app-region:drag]',
        mac ? 'pl-20' : 'pl-3',
      )}
    >
      <IconButton
        label={playingThis ? t('row.pause') : t('row.play')}
        shortcut="Space"
        side="bottom"
        onClick={onPlayPause}
        className="size-7 bg-primary text-primary-foreground [-webkit-app-region:no-drag] hover:bg-primary/90 hover:text-primary-foreground"
      >
        {playingThis ? <Pause className="size-4 fill-current" /> : <Play className="size-4 fill-current" />}
      </IconButton>
      <h1 className="min-w-0 truncate font-medium">{title}</h1>
      {parts.length > 0 && (
        <p className="shrink-0 text-xs text-muted-foreground tabular-nums" aria-label={page && pages ? t('reader.page', { page, pages }) : undefined}>
          {parts.join(' · ')}
        </p>
      )}
      <div className="ml-auto flex items-center gap-0.5 [-webkit-app-region:no-drag]">
        {followReleased && (
          <IconButton label={t('reader.back_to_playhead')} side="bottom" onClick={onBackToPlayhead} className="text-primary">
            <ArrowDownToLine />
          </IconButton>
        )}
        {web && (
          <IconButton
            label={webMode === 'text' ? t('reader.page_view') : t('reader.text_view')}
            side="bottom"
            active={webMode === 'text'}
            onClick={onToggleWebMode}
          >
            {webMode === 'text' ? <Globe /> : <FileText />}
          </IconButton>
        )}
        <IconButton label={t('reader.script')} shortcut={mac ? '⌘⇧S' : 'Ctrl+Shift+S'} side="bottom" active={scriptView} onClick={onToggleScript}>
          <ScrollText />
        </IconButton>
        <IconButton label={t('reader.command')} shortcut={mac ? '⌘K' : 'Ctrl+K'} side="bottom" onClick={onCommand}>
          <Mic />
        </IconButton>
      </div>
    </header>
  )
}
