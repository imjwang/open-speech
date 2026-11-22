import {
  AlertTriangle,
  BookOpen,
  Check,
  CircleAlert,
  Clipboard,
  CornerDownLeft,
  DollarSign,
  FileText,
  FolderOpen,
  History,
  Image as ImageIcon,
  Info,
  KeyRound,
  Link2,
  Loader2,
  Pause,
  Play,
  ShieldQuestion,
  Sparkles,
  Type,
  User,
  Volume2,
  X,
} from 'lucide-react'
import type { ReactNode } from 'react'

import type { CardRow } from '@shared/api/ws.gen'

import { Badge } from '@/components/ui/badge'
import { CommandItem, CommandShortcut } from '@/components/ui/command'
import { Kbd } from '@/components/ui/kbd'
import { Progress } from '@/components/ui/progress'
import { Switch } from '@/components/ui/switch'
import { formatPercent, formatSpeed, formatTimeLeft } from '@/format'
import { cn } from '@/lib/utils'
import { describeDropped, URL_RE } from '../dropped'
import { cardRowEnabled, type Row } from '../rows'
import type { ActionModel, CardModel } from '../store'
import { SpeechDiffExcerpt } from './SpeechDiffExcerpt'
import { UsageCard } from './UsageCard'

export interface RowHandlers {
  onSelect(row: Row): void
  onSecondary(row: Row): void
  onTertiary(row: Row): void
  onCancelAction(action: ActionModel): void
}

/** Key hints are shown and also read (visually hidden prefix) so screen readers get them too. */
function Hints({ items }: { items: Array<[string, string]> }) {
  return (
    <CommandShortcut className="hidden group-data-[selected=true]/item:flex">
      {items.map(([key, label]) => (
        <span key={key + label} className="flex items-center gap-1">
          <Kbd>{key}</Kbd>
          <span className="tracking-normal">{label}</span>
        </span>
      ))}
    </CommandShortcut>
  )
}

function SrHint({ children }: { children: ReactNode }) {
  return <span className="sr-only">{children}</span>
}

function Item({ row, handlers, className, children }: { row: Row; handlers: RowHandlers; className?: string; children: ReactNode }) {
  return (
    <CommandItem
      value={row.value}
      onSelect={() => handlers.onSelect(row)}
      disabled={row.kind === 'pending'}
      className={cn('group/item hover:bg-muted/50', className)}
      data-row-kind={row.kind}
    >
      {children}
    </CommandItem>
  )
}

function droppedIcon(row: Extract<Row, { kind: 'dropped' }>) {
  if (row.action === 'describe') return <Sparkles aria-hidden="true" />
  if (row.action === 'summarize') return <FileText aria-hidden="true" />
  if (row.action === 'open') return <BookOpen aria-hidden="true" />
  switch (row.item.kind) {
    case 'file':
      return row.item.isImage ? <ImageIcon aria-hidden="true" /> : <FileText aria-hidden="true" />
    case 'url':
      return <Link2 aria-hidden="true" />
    case 'text':
      return <Type aria-hidden="true" />
  }
}

function ActionRow({ action, row, handlers }: { action: ActionModel; row: Row; handlers: RowHandlers }) {
  const pending = action.status === 'pending' || action.status === 'confirming' || action.status === 'cancelling'
  return (
    <Item row={row} handlers={handlers} className="items-start py-2">
      <div className={cn('w-full rounded-lg border px-3 py-2', pending ? 'border-listen/60 bg-background/60' : 'border-border opacity-80')}>
        <div className="flex items-center gap-2">
          {action.status === 'confirmed' ? (
            <Check aria-hidden="true" className="text-emerald-600 dark:text-emerald-400" />
          ) : action.status === 'cancelled' || action.status === 'failed' ? (
            <X aria-hidden="true" />
          ) : action.status === 'pending' ? (
            <ShieldQuestion aria-hidden="true" className="text-listen" />
          ) : (
            <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" />
          )}
          <span className="font-medium">{action.summary}</span>
          {action.status === 'pending' ? (
            <>
              <SrHint>. Press Enter to confirm, or Escape to cancel.</SrHint>
              <span className="ml-auto flex items-center gap-2">
                <Hints items={[['↵', 'confirm']]} />
                <button
                  type="button"
                  tabIndex={-1}
                  aria-label="Cancel"
                  className="rounded-sm p-0.5 text-muted-foreground hover:bg-muted"
                  onClick={(e) => {
                    e.stopPropagation()
                    handlers.onCancelAction(action)
                  }}
                >
                  <span className="flex items-center gap-1 text-xs">
                    <Kbd>esc</Kbd>
                    <X className="size-3.5" aria-hidden="true" />
                  </span>
                </button>
              </span>
            </>
          ) : (
            <span className="ml-auto text-xs text-muted-foreground">
              {action.message ?? (action.status === 'confirming' ? 'Saving…' : action.status === 'cancelling' ? 'Cancelling…' : action.status)}
            </span>
          )}
        </div>
        {action.preview && <SpeechDiffExcerpt diff={action.preview} />}
      </div>
    </Item>
  )
}

function CardHeader({ card }: { card: CardModel }) {
  if (card.kind === 'usage') return <UsageCard title={card.title} data={card.data} rows={card.rows} />
  return (
    <div className="flex w-full items-baseline justify-between gap-2 text-xs text-muted-foreground">
      <span className="font-medium text-foreground">{card.title}</span>
      {card.kind === 'voices' && <span>AI-generated</span>}
      {card.kind === 'keys' && <span>Keys stay on this Mac.</span>}
    </div>
  )
}

function fitVariant(fit: unknown): 'success' | 'warning' | 'destructive' | 'outline' {
  if (fit === 'fits') return 'success'
  if (fit === 'slow') return 'warning'
  if (fit === 'unsupported') return 'destructive'
  return 'outline'
}

function CardRowBody({ card, row, handlers, item }: { card: CardModel; row: CardRow; handlers: RowHandlers; item: Row }) {
  const subtitle = row.subtitle ? <span className="truncate text-xs text-muted-foreground">{row.subtitle}</span> : null
  switch (card.kind) {
    case 'voices': {
      if (row.data?.group === true) {
        // A provider without a key: one row; Enter opens the key field.
        return (
          <>
            <KeyRound aria-hidden="true" className="text-muted-foreground" />
            <span className="flex min-w-0 flex-col">
              <span className="truncate text-muted-foreground">{row.title}</span>
              {subtitle}
            </span>
            <SrHint>. Enter adds a key.</SrHint>
            <Hints items={[['↵', 'add key']]} />
          </>
        )
      }
      const unavailable = row.data?.available === false
      const reason = typeof row.data?.unavailable_reason === 'string' ? row.data.unavailable_reason : null
      return (
        <>
          <button
            type="button"
            tabIndex={-1}
            aria-label={`Preview ${row.title}`}
            className="rounded-full p-1 hover:bg-muted"
            onClick={(e) => {
              e.stopPropagation()
              handlers.onTertiary(item)
            }}
          >
            <Play className="size-3.5" aria-hidden="true" />
          </button>
          <span className={cn('flex min-w-0 flex-col', unavailable && 'opacity-60')}>
            <span className="truncate">{row.title}</span>
            {subtitle}
            {unavailable && reason && <span className="text-xs text-muted-foreground">{reason}</span>}
          </span>
          {row.selected && (
            <>
              <Check aria-hidden="true" className="ml-auto text-listen" />
              <SrHint>, current voice</SrHint>
            </>
          )}
          <SrHint>. Enter uses it for this document, Command-Enter makes it the default, Shift-Enter plays a sample.</SrHint>
          <Hints items={[['↵', 'use'], ['⌘↵', 'default'], ['⇧↵', '▶']]} />
        </>
      )
    }
    case 'rules':
    case 'presets': {
      const enabled = cardRowEnabled(row)
      return (
        <>
          <span className="flex min-w-0 flex-col">
            <span className="truncate">{row.title}</span>
            {subtitle}
          </span>
          <SrHint>{enabled ? ', on' : ', off'}. Command-Enter toggles it.</SrHint>
          <span className="ml-auto flex items-center gap-2">
            <Switch
              checked={enabled}
              tabIndex={-1}
              aria-hidden="true"
              onClick={(e) => {
                e.stopPropagation()
                handlers.onSecondary(item)
              }}
            />
            <Hints items={card.kind === 'rules' ? [['↵', 'edit'], ['⌘↵', 'toggle']] : [['⌘↵', 'toggle']]} />
          </span>
        </>
      )
    }
    case 'models': {
      const fit = row.data?.fit
      const installed = row.data?.installed === true
      return (
        <>
          <span className="flex min-w-0 flex-col">
            <span className="truncate">{row.title}</span>
            {subtitle}
          </span>
          <span className="ml-auto flex items-center gap-2">
            {typeof fit === 'string' && <Badge variant={fitVariant(fit)}>{fit}</Badge>}
            {installed ? <Badge variant="outline">installed</Badge> : <Hints items={[['↵', 'install']]} />}
          </span>
        </>
      )
    }
    case 'keys':
      return (
        <>
          <KeyRound aria-hidden="true" />
          <span className="flex min-w-0 flex-col">
            <span>{row.title}</span>
            {subtitle}
          </span>
          {row.value && <Badge variant={row.value === 'configured' ? 'success' : row.value === 'invalid' ? 'destructive' : 'outline'}>{row.value}</Badge>}
        </>
      )
    default:
      return (
        <>
          <span className="flex min-w-0 flex-col">
            <span className="truncate">{row.title}</span>
            {subtitle}
          </span>
          {row.value && <span className="ml-auto text-sm tabular-nums text-muted-foreground">{row.value}</span>}
        </>
      )
  }
}

export function RowView({ row, handlers }: { row: Row; handlers: RowHandlers }) {
  switch (row.kind) {
    case 'local-confirm':
      return (
        <Item row={row} handlers={handlers}>
          <ShieldQuestion aria-hidden="true" className="text-listen" />
          <span className="flex flex-col">
            <span className="font-medium">{row.confirm.title}</span>
            <span className="text-xs text-muted-foreground">{row.confirm.detail}</span>
          </span>
          <SrHint>. Press Enter to confirm, or Escape to cancel.</SrHint>
          <Hints items={[['↵', 'confirm'], ['esc', 'cancel']]} />
        </Item>
      )
    case 'dropped':
      return (
        <Item row={row} handlers={handlers}>
          {droppedIcon(row)}
          <span>{row.label}</span>
          <span className="truncate text-xs text-muted-foreground">{describeDropped(row.item)}</span>
          {row.isDefault && <Hints items={[['↵', '']]} />}
        </Item>
      )
    case 'submit': {
      const link = URL_RE.test(row.text) && !/\s/.test(row.text)
      return (
        <Item row={row} handlers={handlers}>
          {link ? <Link2 aria-hidden="true" /> : <CornerDownLeft aria-hidden="true" />}
          <span className="truncate">{link ? `Read ${row.text}` : row.text}</span>
          <Hints items={[['↵', link ? 'read' : 'ask']]} />
        </Item>
      )
    }
    case 'pending':
      return (
        <Item row={row} handlers={handlers}>
          <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" />
          <span className="truncate text-muted-foreground">{row.text}</span>
        </Item>
      )
    case 'user': {
      const t = row.turn
      return (
        <Item row={row} handlers={handlers}>
          {t.recordingId ? <Play aria-hidden="true" /> : <User aria-hidden="true" />}
          <span className="shrink-0 text-xs font-medium text-muted-foreground">You</span>
          <span className="truncate">{t.text}</span>
          {t.recordingId && (
            <>
              <SrHint>. Press Enter to replay your recording.</SrHint>
              <Hints items={[['↵', '▶']]} />
            </>
          )}
        </Item>
      )
    }
    case 'reply': {
      const t = row.turn
      if (t.error && t.errorCode === 'missing_key') {
        return (
          <Item row={row} handlers={handlers} className="items-start">
            <KeyRound aria-hidden="true" className="mt-0.5 text-muted-foreground" />
            <span className="whitespace-pre-wrap">{t.error}</span>
            <SrHint>. Press Enter to paste a key.</SrHint>
            <Hints items={[['↵', 'paste a key']]} />
          </Item>
        )
      }
      return (
        <Item row={row} handlers={handlers} className="items-start">
          {t.error ? (
            <CircleAlert aria-hidden="true" className="mt-0.5 text-destructive" />
          ) : t.replyRecordingId ? (
            <Volume2 aria-hidden="true" className="mt-0.5" />
          ) : !t.done ? (
            <Loader2 aria-hidden="true" className="mt-0.5 animate-spin motion-reduce:animate-none" />
          ) : (
            <Info aria-hidden="true" className="mt-0.5" />
          )}
          <span className={cn('whitespace-pre-wrap', t.error && 'text-destructive')}>
            {t.error ?? (t.reply || (t.done ? '' : 'Thinking…'))}
          </span>
          {t.replyRecordingId && (
            <>
              <SrHint>. Press Enter to replay.</SrHint>
              <Hints items={[['↵', '▶']]} />
            </>
          )}
        </Item>
      )
    }
    case 'action':
      return <ActionRow action={row.action} row={row} handlers={handlers} />
    case 'card':
      return (
        <Item row={row} handlers={handlers} className="data-[selected=true]:ring-0">
          <CardHeader card={row.card} />
        </Item>
      )
    case 'card-row':
      return (
        <Item row={row} handlers={handlers}>
          <CardRowBody card={row.card} row={row.row} handlers={handlers} item={row} />
        </Item>
      )
    case 'notice':
      return (
        <Item row={row} handlers={handlers}>
          {row.notice.level === 'error' ? (
            <CircleAlert aria-hidden="true" className="text-destructive" />
          ) : row.notice.level === 'warn' ? (
            <AlertTriangle aria-hidden="true" className="text-amber-600 dark:text-amber-400" />
          ) : (
            <Check aria-hidden="true" />
          )}
          <span className={cn(row.notice.level === 'error' && 'text-destructive')}>{row.notice.text}</span>
          <Hints items={[['↵', 'dismiss']]} />
        </Item>
      )
    case 'job':
      return (
        <Item row={row} handlers={handlers}>
          <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" />
          <span className="flex w-full flex-col gap-1">
            <span className="text-sm">{row.label}</span>
            <Progress value={row.pct} aria-label={`${row.label}: ${Math.round(row.pct)}%`} />
          </span>
        </Item>
      )
    case 'now-playing': {
      const p = row.playback
      const playing = p.state === 'playing' || p.state === 'loading'
      const finished = !playing && (p.progress ?? 0) >= 0.999
      return (
        <Item row={row} handlers={handlers}>
          {playing ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}
          <span className="shrink-0 text-xs font-medium text-muted-foreground">
            {playing ? 'Now playing' : finished ? 'Finished' : 'Paused'}
          </span>
          <span className="truncate">{p.document_title ?? 'Untitled'}</span>
          <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
            {[formatSpeed(p.speed), finished ? null : formatTimeLeft(p.time_left_s)].filter(Boolean).join(' · ')}
          </span>
          <SrHint>. Enter {playing ? 'pauses' : 'plays'}, Command-Enter opens the reader.</SrHint>
          <Hints items={[['↵', playing ? 'pause' : finished ? 'replay' : 'play']]} />
        </Item>
      )
    }
    case 'recent':
      return (
        <Item row={row} handlers={handlers}>
          <History aria-hidden="true" />
          <span className="truncate">{row.doc.title}</span>
          {row.doc.progress > 0 && (
            <span className="shrink-0 text-xs text-muted-foreground tabular-nums">{formatPercent(row.doc.progress)}</span>
          )}
          <SrHint>. Enter resumes, Command-Enter opens the reader.</SrHint>
        </Item>
      )
    case 'suggestion': {
      const icon =
        row.id === 'read-clipboard' ? (
          <Clipboard aria-hidden="true" />
        ) : row.id === 'open-file' ? (
          <FolderOpen aria-hidden="true" />
        ) : row.id === 'show-voices' ? (
          <Volume2 aria-hidden="true" />
        ) : (
          <DollarSign aria-hidden="true" />
        )
      return (
        <Item row={row} handlers={handlers}>
          {icon}
          <span>{row.label}</span>
        </Item>
      )
    }
  }
}
