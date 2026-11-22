import { ArrowDown, FileText, Globe, ScrollText } from 'lucide-react'

import type { PlaybackState } from '@shared/api/client'

import { Button } from '@/components/ui/button'
import { Kbd } from '@/components/ui/kbd'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { formatSpeed, formatTimeLeft } from '@/format'
import { cn } from '@/lib/utils'

/** Thin title strip (09 § Reader): title · p. 3/15 · 1.8x · Script toggle. Draggable on macOS. */
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
  mac: boolean
}) {
  const page = isPlayingThis ? playback.page : null
  const pages = (isPlayingThis ? playback.page_count : null) ?? pageCount
  const parts = [
    page && pages ? `p. ${page}/${pages}` : pages ? `${pages} pages` : null,
    isPlayingThis ? formatSpeed(playback.speed) : null,
    isPlayingThis ? formatTimeLeft(playback.time_left_s) : null,
  ].filter(Boolean)
  return (
    <header
      className={cn(
        'flex h-10 shrink-0 items-center gap-2 border-b bg-background/95 pr-2 text-sm [-webkit-app-region:drag]',
        mac ? 'pl-20' : 'pl-3',
      )}
    >
      <h1 className="min-w-0 truncate font-medium">{title}</h1>
      {parts.length > 0 && (
        <p className="shrink-0 text-muted-foreground tabular-nums" aria-label="Position">
          <span aria-hidden="true">· </span>
          {parts.join(' · ')}
        </p>
      )}
      <div className="ml-auto flex items-center gap-1 [-webkit-app-region:no-drag]">
        {followReleased && (
          <Button size="sm" variant="secondary" onClick={onBackToPlayhead}>
            <ArrowDown aria-hidden="true" />
            Back to playhead
          </Button>
        )}
        {web && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button size="sm" variant="ghost" aria-pressed={webMode === 'text'} onClick={onToggleWebMode}>
                {webMode === 'text' ? <Globe aria-hidden="true" /> : <FileText aria-hidden="true" />}
                {webMode === 'text' ? 'Live page' : 'Reader text'}
              </Button>
            </TooltipTrigger>
            <TooltipContent>{webMode === 'text' ? 'Show the live site' : 'Show the extracted text'}</TooltipContent>
          </Tooltip>
        )}
        <Tooltip>
          <TooltipTrigger asChild>
            <Button size="sm" variant={scriptView ? 'secondary' : 'ghost'} aria-pressed={scriptView} onClick={onToggleScript}>
              <ScrollText aria-hidden="true" />
              Script
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            Show exactly what will be spoken <Kbd className="ml-1">{mac ? '⌘⇧S' : 'Ctrl+Shift+S'}</Kbd>
          </TooltipContent>
        </Tooltip>
      </div>
    </header>
  )
}
