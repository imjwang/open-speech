/**
 * How each row looks. Icons carry the meaning; text is the content itself (what was said, a title, a name).
 * Every icon-only control has a name for assistive technology, and key hints appear only on the selected row.
 */
import {
  Check,
  ChevronUp,
  CircleAlert,
  Cloud,
  Download,
  FileText,
  Globe,
  Image as ImageIcon,
  Info,
  Keyboard,
  Laptop,
  ListMusic,
  Loader2,
  Mic,
  Minus,
  Monitor,
  Moon,
  Pencil,
  Play,
  Plus,
  ShieldQuestion,
  Sparkles,
  Sun,
  TriangleAlert,
  Volume2,
  X,
  Zap,
  type LucideIcon,
} from 'lucide-react'
import type { ReactNode } from 'react'

import type { CardRow } from '@shared/api/ws.gen'

import { CommandItem } from '@/components/ui/command'
import { Kbd } from '@/components/ui/kbd'
import { Progress } from '@/components/ui/progress'
import { Switch } from '@/components/ui/switch'
import { formatPercent } from '@/format'
import { useT, useTMaybe } from '@/i18n'
import { cn } from '@/lib/utils'
import { cardRowEnabled, type Row } from '../rows'
import type { ActionModel, CardModel, TurnAttachment } from '../store'
import { documentIcon, PRESET_ICONS, SETTING_ICONS } from './icons'
import { SpeechDiffExcerpt } from './SpeechDiffExcerpt'
import { UsageCard } from './UsageCard'

export interface RowHandlers {
  onSelect(row: Row): void
  onSecondary(row: Row): void
  onTertiary(row: Row): void
  onCancelAction(action: ActionModel): void
  onTogglePreset(card: CardModel, row: CardRow): void
  onSetSetting(card: CardModel, row: CardRow, value: unknown): void
}

/** Key hints, on the selected row only. Glyphs, not words; the row's own label says what Enter does. */
function Keys({ keys, className }: { keys: string[]; className?: string }) {
  return (
    <span aria-hidden="true" className={cn('ml-auto hidden shrink-0 items-center gap-1 group-data-[selected=true]/item:flex', className)}>
      {keys.map((k) => (
        <Kbd key={k}>{k}</Kbd>
      ))}
    </span>
  )
}

function Sr({ children }: { children: ReactNode }) {
  return <span className="sr-only">{children}</span>
}

function Item({ row, handlers, className, children }: { row: Row; handlers: RowHandlers; className?: string; children: ReactNode }) {
  return (
    <CommandItem
      value={row.value}
      onSelect={() => handlers.onSelect(row)}
      disabled={row.kind === 'pending'}
      className={cn('group/item', className)}
      data-row-kind={row.kind}
    >
      {children}
    </CommandItem>
  )
}

/** A small button inside a row that must not move focus out of the composer. */
function RowButton({ label, onPress, children, className }: { label: string; onPress(): void; children: ReactNode; className?: string }) {
  return (
    <button
      type="button"
      tabIndex={-1}
      aria-label={label}
      title={label}
      className={cn('grid size-7 shrink-0 place-items-center rounded-full text-muted-foreground hover:bg-accent hover:text-accent-foreground', className)}
      onMouseDown={(e) => e.preventDefault()}
      onClick={(e) => {
        e.stopPropagation()
        onPress()
      }}
    >
      {children}
    </button>
  )
}

function attachmentIcon(kind: string): LucideIcon {
  return kind === 'url' ? Globe : kind === 'image' ? ImageIcon : FileText
}

export function AttachmentPills({ items }: { items: TurnAttachment[] }) {
  if (!items.length) return null
  return (
    <span className="flex flex-wrap gap-1">
      {items.map((a, i) => {
        const Icon = attachmentIcon(a.kind)
        return (
          <span key={`${a.title}${i}`} className="badge-chip" data-kind={a.kind === 'url' ? 'url' : a.kind === 'text' ? 'text' : 'file'}>
            <Icon aria-hidden="true" />
            <span>{a.title}</span>
          </span>
        )
      })}
    </span>
  )
}

// ------------------------------------------------------------------ confirmation card

function ActionRow({ action, row, handlers }: { action: ActionModel; row: Row; handlers: RowHandlers }) {
  const t = useT()
  const open = action.status === 'pending'
  const busy = action.status === 'confirming' || action.status === 'cancelling'
  return (
    <Item row={row} handlers={handlers} className="items-stretch p-1 data-[selected=true]:bg-transparent data-[selected=true]:ring-0">
      <div
        className={cn(
          'w-full rounded-xl border px-3 py-2.5 transition-colors',
          open ? 'border-primary/50 bg-card shadow-sm group-data-[selected=true]/item:border-primary' : 'border-border opacity-75',
        )}
      >
        <div className="flex items-center gap-2.5">
          {action.status === 'confirmed' ? (
            <Check aria-hidden="true" className="text-success" />
          ) : action.status === 'cancelled' || action.status === 'failed' ? (
            <X aria-hidden="true" />
          ) : busy ? (
            <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" />
          ) : (
            <ShieldQuestion aria-hidden="true" className="text-primary" />
          )}
          <span className="min-w-0 flex-1 font-medium">{action.summary}</span>
          {open ? (
            <>
              <Sr>. {t('confirm_hint')}</Sr>
              <RowButton label={t('row.cancel')} onPress={() => handlers.onCancelAction(action)}>
                <X className="size-4" aria-hidden="true" />
              </RowButton>
              <RowButton
                label={t('row.confirm')}
                onPress={() => handlers.onSelect(row)}
                className="bg-primary text-primary-foreground hover:bg-primary/90 hover:text-primary-foreground"
              >
                <Check className="size-4 text-primary-foreground!" aria-hidden="true" />
              </RowButton>
            </>
          ) : (
            action.message && <span className="text-xs text-muted-foreground">{action.message}</span>
          )}
        </div>
        {action.preview && <SpeechDiffExcerpt diff={action.preview} />}
      </div>
    </Item>
  )
}

// ------------------------------------------------------------------ block cards

function WhereCard({ data }: { data: Record<string, unknown> }) {
  const t = useT()
  const percent = typeof data.percent === 'number' ? data.percent : null
  const page = typeof data.page === 'number' && typeof data.page_count === 'number' ? t('reader.page', { page: data.page, pages: data.page_count }) : null
  if (percent === null) return null
  return (
    <div className="flex w-full items-center gap-3 px-2 py-1.5 text-xs text-muted-foreground" data-testid="where-card">
      <Progress value={percent} aria-label={`${percent}%`} className="h-1.5 flex-1" />
      <span className="tabular-nums">{page ?? `${percent}%`}</span>
    </div>
  )
}

/** Auto-skip presets as chips: lit when that kind of content is being skipped. */
function PresetChips({ card, handlers }: { card: CardModel; handlers: RowHandlers }) {
  const t = useT()
  const tMaybe = useTMaybe()
  return (
    <div className="flex w-full flex-wrap gap-1.5 px-2 py-1.5" role="group" aria-label={card.title} data-testid="preset-chips">
      {card.rows.map((row) => {
        const on = cardRowEnabled(row)
        const name = tMaybe(`preset.${row.title}`, row.title)
        const Icon = PRESET_ICONS[row.title] ?? FileText
        return (
          <button
            key={row.id}
            type="button"
            aria-pressed={on}
            aria-label={t(on ? 'preset.skipping' : 'preset.reading', { name })}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => handlers.onTogglePreset(card, row)}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring',
              on ? 'border-transparent bg-primary text-primary-foreground' : 'bg-card text-muted-foreground hover:bg-accent',
            )}
          >
            <Icon aria-hidden="true" className={cn('size-3.5', on ? 'text-primary-foreground!' : '')} />
            <span>{name}</span>
          </button>
        )
      })}
    </div>
  )
}

// ------------------------------------------------------------------ list cards, one row per item

type TMaybe = ReturnType<typeof useTMaybe>

const SEGMENT_ICONS: Record<string, LucideIcon> = { system: Monitor, lavender: Sun, ember: Moon, classic: Zap, ai_enhanced: Sparkles }

function optionLabel(tMaybe: TMaybe, key: string, value: unknown): string {
  const v = String(value)
  if (key === 'appearance.theme') return tMaybe(`theme.${v}`, v)
  if (key === 'language') return tMaybe(`language.${v}`, v)
  if (key === 'defaults.mode') return tMaybe(`mode.${v}`, v)
  return v
}

function SettingRow({ card, row, handlers }: { card: CardModel; row: CardRow; handlers: RowHandlers }) {
  const t = useT()
  const tMaybe = useTMaybe()
  const data = row.data ?? {}
  const key = typeof data.key === 'string' ? data.key : row.id
  const Icon = SETTING_ICONS[key] ?? Info
  const options = Array.isArray(data.options) ? (data.options as unknown[]) : null
  const step = typeof data.step === 'number' ? data.step : null
  const value = data.value
  const set = (v: unknown) => handlers.onSetSetting(card, row, v)
  return (
    <>
      <Icon aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate">{row.title}</span>
      {options ? (
        <span role="radiogroup" aria-label={row.title} className="flex shrink-0 gap-0.5 rounded-lg bg-muted p-0.5">
          {options.map((opt) => {
            const SegIcon = SEGMENT_ICONS[String(opt)]
            const label = optionLabel(tMaybe, key, opt)
            const on = opt === value
            return (
              <button
                key={String(opt)}
                type="button"
                role="radio"
                tabIndex={-1}
                aria-checked={on}
                aria-label={label}
                title={label}
                onMouseDown={(e) => e.preventDefault()}
                onClick={(e) => {
                  e.stopPropagation()
                  set(opt)
                }}
                className={cn(
                  'grid h-6 min-w-7 place-items-center rounded-md px-1.5 text-[11px] font-medium uppercase',
                  on ? 'bg-card text-foreground shadow-xs' : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {SegIcon ? <SegIcon aria-hidden="true" className={cn('size-3.5', on && 'text-primary!')} /> : String(opt) === 'auto' ? <Monitor aria-hidden="true" className="size-3.5" /> : String(opt)}
              </button>
            )
          })}
        </span>
      ) : step !== null && typeof value === 'number' ? (
        <span className="flex shrink-0 items-center gap-1">
          <RowButton label={key === 'reader.font_size' ? t('text.smaller') : t('row.slower')} onPress={() => handlers.onSecondary({ kind: 'card-row', value: '', card, row })}>
            <Minus className="size-3.5" aria-hidden="true" />
          </RowButton>
          <span className="min-w-9 text-center text-sm tabular-nums">{key === 'defaults.speed' ? `${value}x` : value}</span>
          <RowButton label={key === 'reader.font_size' ? t('text.larger') : t('row.faster')} onPress={() => handlers.onSelect({ kind: 'card-row', value: '', card, row })}>
            <Plus className="size-3.5" aria-hidden="true" />
          </RowButton>
        </span>
      ) : (
        <span className="shrink-0 text-sm text-muted-foreground">{row.value}</span>
      )}
    </>
  )
}

function CardRowBody({ card, row, handlers, item }: { card: CardModel; row: CardRow; handlers: RowHandlers; item: Row }) {
  const t = useT()
  switch (card.kind) {
    case 'voices': {
      const local = row.data?.local === true
      const langs = Array.isArray(row.data?.languages) ? (row.data.languages as string[]) : []
      const lang = [...new Set(langs.map((l) => l.split('-')[0]?.toUpperCase()))].filter(Boolean).slice(0, 2)
      return (
        <>
          <RowButton label={t('row.preview', { name: row.title })} onPress={() => handlers.onTertiary(item)} className="bg-muted">
            <Play className="size-3.5" aria-hidden="true" />
          </RowButton>
          <span className="min-w-0 truncate">{row.title}</span>
          {lang.map((l) => (
            <span key={l} className="rounded bg-muted px-1 text-[10px] font-medium text-muted-foreground">
              {l}
            </span>
          ))}
          {local ? (
            <Laptop aria-label={t('row.local')} className="size-3.5!" />
          ) : (
            <Cloud aria-label={t('row.cloud')} className="size-3.5!" />
          )}
          {row.selected ? (
            <>
              <Check aria-hidden="true" className="ml-auto text-primary" />
              <Sr>{t('row.current_voice')}</Sr>
            </>
          ) : (
            <Keys keys={['↵', '⇧↵']} />
          )}
        </>
      )
    }
    case 'rules': {
      const enabled = cardRowEnabled(row)
      const ai = row.data?.tier !== 'filter'
      return (
        <>
          {ai ? <Sparkles aria-label={t('rules.needs_ai')} /> : <Zap aria-label={t('rules.filter')} />}
          <span className="flex min-w-0 flex-col">
            <span className="truncate">{row.title}</span>
            {row.subtitle && <span className="truncate text-xs text-muted-foreground">{row.subtitle}</span>}
          </span>
          <Sr>{enabled ? t('row.on') : t('row.off')}</Sr>
          <span className="ml-auto flex shrink-0 items-center gap-1.5">
            <RowButton label={t('row.edit')} onPress={() => handlers.onSecondary(item)}>
              <Pencil className="size-3.5" aria-hidden="true" />
            </RowButton>
            <Switch checked={enabled} tabIndex={-1} aria-hidden="true" onClick={(e) => (e.stopPropagation(), handlers.onSelect(item))} />
          </span>
        </>
      )
    }
    case 'models': {
      const installed = row.data?.installed === true
      return (
        <>
          {row.data?.kind === 'stt' ? <Mic aria-hidden="true" /> : <Volume2 aria-hidden="true" />}
          <span className="flex min-w-0 flex-col">
            <span className="truncate">{row.title}</span>
            {row.subtitle && <span className="truncate text-xs text-muted-foreground">{row.subtitle}</span>}
          </span>
          <span className="ml-auto flex shrink-0 items-center gap-2">
            {installed ? <Check aria-label={t('row.installed')} className="text-success" /> : <Download aria-label={t('row.install')} />}
          </span>
        </>
      )
    }
    case 'recents':
    case 'queue': {
      const Icon = card.kind === 'queue' ? ListMusic : documentIcon(typeof row.data?.kind === 'string' ? row.data.kind : null)
      return (
        <>
          <Icon aria-hidden="true" />
          <span className="min-w-0 truncate">{row.title}</span>
          {row.value && <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">{row.value}</span>}
        </>
      )
    }
    case 'settings':
      return <SettingRow card={card} row={row} handlers={handlers} />
    default:
      return (
        <>
          <span className="min-w-0 truncate">{row.title}</span>
          {row.value && <span className="ml-auto text-sm text-muted-foreground tabular-nums">{row.value}</span>}
        </>
      )
  }
}

// ------------------------------------------------------------------ every row

export function RowView({ row, handlers }: { row: Row; handlers: RowHandlers }) {
  const t = useT()
  switch (row.kind) {
    case 'local-confirm':
      return (
        <Item row={row} handlers={handlers}>
          <Download aria-hidden="true" className="text-primary" />
          <span className="min-w-0 flex-1 truncate font-medium">{row.confirm.title}</span>
          <span className="text-xs text-muted-foreground">{row.confirm.detail}</span>
          <Sr>. {t('confirm_hint')}</Sr>
          <Keys keys={['↵']} className="ml-2" />
        </Item>
      )
    case 'pending':
      return (
        <Item row={row} handlers={handlers}>
          <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" />
          <span className="min-w-0 truncate text-muted-foreground">{row.text}</span>
          <AttachmentPills items={row.attachments} />
        </Item>
      )
    case 'history':
      return (
        <Item row={row} handlers={handlers} className="justify-center py-0.5 text-xs text-muted-foreground">
          <ChevronUp aria-hidden="true" className="size-3.5!" />
          <span className="tabular-nums" aria-label={t('tool.history')}>
            {row.count}
          </span>
        </Item>
      )
    case 'user': {
      const turn = row.turn
      const Icon = turn.source === 'spoken' ? Mic : Keyboard
      return (
        <Item row={row} handlers={handlers} className="text-muted-foreground">
          <Icon aria-label={t('row.you')} />
          <span className="min-w-0 truncate">{turn.text}</span>
          <AttachmentPills items={turn.attachments} />
          {turn.recordingId && (
            <>
              <Sr>. {t('row.replay')}</Sr>
              <Keys keys={['↵']} />
            </>
          )}
        </Item>
      )
    }
    case 'reply': {
      const turn = row.turn
      const unavailable = turn.errorCode === 'unavailable'
      return (
        <Item row={row} handlers={handlers} className="items-start">
          {turn.error && !unavailable ? (
            <CircleAlert aria-hidden="true" className="mt-0.5 text-destructive" />
          ) : turn.replyRecordingId ? (
            <Volume2 aria-hidden="true" className="mt-0.5 text-primary" />
          ) : !turn.done ? (
            <Loader2 aria-hidden="true" className="mt-0.5 animate-spin motion-reduce:animate-none" />
          ) : (
            <Info aria-hidden="true" className="mt-0.5" />
          )}
          <span className={cn('min-w-0 flex-1 whitespace-pre-wrap', turn.error && !unavailable && 'text-destructive')}>
            {turn.error ?? (turn.reply || (turn.done ? '' : t('row.thinking')))}
          </span>
          {turn.replyRecordingId && (
            <>
              <Sr>. {t('row.replay')}</Sr>
              <Keys keys={['↵']} />
            </>
          )}
        </Item>
      )
    }
    case 'action':
      return <ActionRow action={row.action} row={row} handlers={handlers} />
    case 'card': {
      const card = row.card
      // Blocks are not selectable rows: they hold their own controls (chips) or just show a figure.
      return (
        <div data-row-kind="card" data-card={card.kind}>
          {card.kind === 'usage' ? (
            <div className="px-2">
              <UsageCard title={card.title} data={card.data} rows={card.rows} />
            </div>
          ) : card.kind === 'where' ? (
            <WhereCard data={card.data} />
          ) : (
            <PresetChips card={card} handlers={handlers} />
          )}
        </div>
      )
    }
    case 'card-row':
      return (
        <Item row={row} handlers={handlers}>
          <CardRowBody card={row.card} row={row.row} handlers={handlers} item={row} />
        </Item>
      )
    case 'notice': {
      const level = row.notice.level
      return (
        <Item row={row} handlers={handlers}>
          {level === 'error' ? (
            <CircleAlert aria-hidden="true" className="text-destructive" />
          ) : level === 'warn' ? (
            <TriangleAlert aria-hidden="true" className="text-warning" />
          ) : (
            <Check aria-hidden="true" className="text-success" />
          )}
          <span className={cn('min-w-0 flex-1', level === 'error' && 'text-destructive')}>{row.notice.text}</span>
          <RowButton label={t('row.dismiss')} onPress={() => handlers.onSelect(row)}>
            <X className="size-3.5" aria-hidden="true" />
          </RowButton>
        </Item>
      )
    }
    case 'job':
      return (
        <Item row={row} handlers={handlers}>
          <Download aria-hidden="true" className="text-primary" />
          <span className="flex w-full flex-col gap-1">
            <span className="truncate text-sm">{row.label}</span>
            <Progress value={row.pct} aria-label={`${row.label}: ${Math.round(row.pct)}%`} className="h-1.5" />
          </span>
        </Item>
      )
    case 'recent': {
      const Icon = documentIcon(row.doc.kind)
      return (
        <Item row={row} handlers={handlers}>
          <Icon aria-hidden="true" />
          <span className="min-w-0 truncate">{row.doc.title}</span>
          {row.doc.progress > 0 && (
            <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">{formatPercent(row.doc.progress)}</span>
          )}
          <Sr>. {t('row.play')}</Sr>
        </Item>
      )
    }
  }
}
