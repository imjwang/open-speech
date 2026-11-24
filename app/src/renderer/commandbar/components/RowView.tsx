/**
 * How each row looks. Icons carry the meaning; text is the content itself (what was said, a title, a name).
 * Every icon-only control has a name for assistive technology, and key hints appear only on the selected row.
 */
import {
  AudioLines,
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
  Ellipsis,
  Circle,
  ShieldCheck,
  Square,
  Trash2,
  UserRound,
  type LucideIcon,
} from 'lucide-react'
import { memo, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'

import type { CardRow } from '@shared/api/ws.gen'

import { CommandItem } from '@/components/ui/command'
import { Kbd } from '@/components/ui/kbd'
import { Progress } from '@/components/ui/progress'
import { Switch } from '@/components/ui/switch'
import { useRenderCount } from '@/debug'
import { formatPercent } from '@/format'
import { useT, useTMaybe } from '@/i18n'
import { cn } from '@/lib/utils'
import { cardRowEnabled, sameRow, type Row } from '../rows'
import type { ActionModel, CaptureState, CardModel, TurnAttachment } from '../store'
import { documentIcon, SETTING_ICONS } from './icons'
import { SpeechDiffExcerpt } from './SpeechDiffExcerpt'
import { UsageCard } from './UsageCard'

export interface RowHandlers {
  onSelect(row: Row): void
  onSecondary(row: Row): void
  onTertiary(row: Row): void
  onCancelAction(action: ActionModel): void
  onSetSetting(card: CardModel, row: CardRow, value: unknown): void
  /** The passage card's record button (it is the card's confirmation: the consent line is on it). */
  onRecord(action: ActionModel): void
  /** Stop a take now (it is sent as it is). */
  onStopRecording(): void
  /** The microphone level for a take's meter (once per frame at most). */
  subscribeLevel(fn: (level: number) => void): () => void
  /** Play a recording through the engine (a new voice's sample, a designed candidate). */
  onReplay(recordingId: string): void
  /** The trash button on one of the user's own voices. */
  onDeleteVoice(row: CardRow): void
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

const AUDIO_NAME = /\.(wav|wave|m4a|mp3|aiff?|flac|ogg|opus|caf|aac)$/i

function attachmentIcon(kind: string, title = ''): LucideIcon {
  if (kind === 'url') return Globe
  if (kind === 'image') return ImageIcon
  return AUDIO_NAME.test(title) ? AudioLines : FileText
}

export function AttachmentPills({ items }: { items: TurnAttachment[] }) {
  if (!items.length) return null
  return (
    <span className="flex flex-wrap gap-1">
      {items.map((a, i) => {
        const Icon = attachmentIcon(a.kind, a.title)
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

function ActionRow({ action, row, handlers, capture }: { action: ActionModel; row: Row; handlers: RowHandlers; capture: CaptureState | null }) {
  const t = useT()
  if (action.kind === 'clone_record') return <CloneCard action={action} row={row} handlers={handlers} capture={capture} />
  const open = action.status === 'pending'
  const busy = action.status === 'confirming' || action.status === 'cancelling'
  const sample = typeof action.details.recording_id === 'string' ? action.details.recording_id : null
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
          {sample && (
            <RowButton label={t('clone.hear_again')} onPress={() => handlers.onReplay(sample)} className="bg-muted">
              <Play className="size-3.5" aria-hidden="true" />
            </RowButton>
          )}
          {open ? (
            <>
              <Sr>. {t('confirm_hint')}</Sr>
              <RowButton label={t('row.cancel')} onPress={() => handlers.onCancelAction(action)}>
                <X className="size-4" aria-hidden="true" />
              </RowButton>
              {/* Designed voices are chosen on their own rows below: no single "yes" here. */}
              {action.kind !== 'keep_design' && <RowButton
                label={t('row.confirm')}
                onPress={() => handlers.onSelect(row)}
                className="bg-primary text-primary-foreground hover:bg-primary/90 hover:text-primary-foreground"
              >
                <Check className="size-4 text-primary-foreground!" aria-hidden="true" />
              </RowButton>}
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

// ------------------------------------------------------------------ the passage card (cloning a voice)

/** A take's level as a bar that follows the microphone (a CSS variable, no re-render per chunk). */
function LevelMeter({ subscribe, active }: { subscribe: RowHandlers['subscribeLevel']; active: boolean }) {
  const ref = useRef<HTMLSpanElement>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    if (!active) {
      el.style.setProperty('--level', '0')
      return
    }
    return subscribe((level) => el.style.setProperty('--level', Math.min(1, level * 1.15).toFixed(3)))
  }, [subscribe, active])
  return (
    <span aria-hidden="true" className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-muted" data-testid="clone-meter">
      <span ref={ref} className="clone-meter absolute inset-y-0 left-0 w-full origin-left rounded-full bg-destructive" />
    </span>
  )
}

function Elapsed({ running }: { running: boolean }) {
  const [start] = useState(() => Date.now())
  const [now, setNow] = useState(start)
  useEffect(() => {
    if (!running) return
    const timer = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(timer)
  }, [running])
  const s = Math.floor((now - start) / 1000)
  return <span className="w-9 text-right text-xs text-muted-foreground tabular-nums">{`${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`}</span>
}

/**
 * "Clone my voice": the passage to read, the consent line, and one control: record (which is the confirmation, so
 * it sits right above the line it confirms), then stop while recording, with the microphone's level. Saying "ready"
 * does the same as the button.
 */
function CloneCard({ action, row, handlers, capture }: { action: ActionModel; row: Row; handlers: RowHandlers; capture: CaptureState | null }) {
  const t = useT()
  const d = action.details
  const passage = typeof d.passage === 'string' ? d.passage : ''
  const consent = typeof d.consent === 'string' ? d.consent : t('clone.consent')
  const phase = capture?.phase ?? null
  const recording = phase === 'recording'
  const working = phase === 'uploading' || phase === 'processing' || phase === 'armed' || action.status === 'confirming'
  const cancelled = action.status === 'cancelled' || action.status === 'failed'
  return (
    <Item row={row} handlers={handlers} className="items-stretch p-1 data-[selected=true]:bg-transparent data-[selected=true]:ring-0">
      <div
        data-testid="clone-card"
        data-phase={phase ?? action.status}
        className={cn(
          'w-full rounded-xl border px-3 py-2.5 transition-colors',
          cancelled ? 'border-border opacity-75' : 'border-primary/50 bg-card shadow-sm group-data-[selected=true]/item:border-primary',
        )}
      >
        <div className="flex items-center gap-2.5">
          <UserRound aria-hidden="true" className="text-primary" />
          <span className="min-w-0 flex-1 font-medium">{action.summary}</span>
          {action.status === 'pending' && (
            <RowButton label={t('row.cancel')} onPress={() => handlers.onCancelAction(action)}>
              <X className="size-4" aria-hidden="true" />
            </RowButton>
          )}
        </div>
        {passage && (
          <p className="mt-2 rounded-lg bg-muted/60 px-3 py-2 text-[15px] leading-relaxed" data-testid="clone-passage" lang={typeof d.language === 'string' ? d.language : undefined}>
            {passage}
          </p>
        )}
        {!cancelled && (
          <div className="mt-2 flex items-center gap-2.5">
            {recording ? (
              <RowButton label={t('clone.stop')} onPress={() => handlers.onStopRecording()} className="size-8 bg-destructive text-white hover:bg-destructive/90 hover:text-white">
                <Square className="size-3.5 fill-current text-white!" aria-hidden="true" />
              </RowButton>
            ) : working ? (
              <span className="grid size-8 place-items-center" aria-hidden="true">
                <Loader2 className="animate-spin motion-reduce:animate-none" />
              </span>
            ) : (
              <RowButton
                label={t('clone.record')}
                onPress={() => handlers.onRecord(action)}
                className="size-8 bg-destructive text-white hover:bg-destructive/90 hover:text-white"
              >
                <Circle className="size-3.5 fill-current text-white!" aria-hidden="true" />
              </RowButton>
            )}
            {recording ? (
              <>
                <LevelMeter subscribe={handlers.subscribeLevel} active={recording} />
                <Elapsed running={recording} />
                <Sr>{t('clone.recording')}</Sr>
              </>
            ) : (
              <span className={cn('min-w-0 flex-1 text-sm', capture?.error ? 'text-destructive' : 'text-muted-foreground')} role={capture?.error ? 'alert' : undefined}>
                {capture?.error ??
                  (phase === 'armed'
                    ? t('clone.get_ready')
                    : working
                      ? t('clone.processing')
                      : phase === 'failed'
                        ? t('clone.record_again')
                        : t('clone.record_hint'))}
              </span>
            )}
          </div>
        )}
        <p className="mt-2 flex items-start gap-1.5 text-xs text-muted-foreground" data-testid="clone-consent">
          <ShieldCheck aria-hidden="true" className="mt-px size-3.5! shrink-0" />
          <span>{consent}</span>
        </p>
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

// ------------------------------------------------------------------ list cards, one row per item

type TMaybe = ReturnType<typeof useTMaybe>

const SEGMENT_ICONS: Record<string, LucideIcon> = { system: Monitor, lavender: Sun, ember: Moon }

function optionLabel(tMaybe: TMaybe, key: string, value: unknown): string {
  const v = String(value)
  if (key === 'appearance.theme') return tMaybe(`theme.${v}`, v)
  if (key === 'language') return tMaybe(`language.${v}`, v)
  if (key === 'qwen3.size') return tMaybe(`qwen.size.${v}`, v)
  if (key === 'qwen3.precision') return tMaybe(`qwen.precision.${v}`, v)
  return v
}

/** What switching a setting to `value` would download ("1.7 GB"), if anything. */
function downloadOf(data: Record<string, unknown>, value: unknown): string | null {
  const downloads = data.downloads
  if (!downloads || typeof downloads !== 'object') return null
  const d = (downloads as Record<string, { size?: unknown }>)[String(value)]
  return d && typeof d.size === 'string' ? d.size : null
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
            const download = downloadOf(data, opt)
            const name = optionLabel(tMaybe, key, opt)
            const label = download ? t('setting.download', { name, size: download }) : name
            const short = key.startsWith('qwen3.') ? name : null
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
                {short ? (
                  <span className="flex items-center gap-0.5 normal-case">
                    {short}
                    {download && <Download aria-hidden="true" className="size-3!" />}
                  </span>
                ) : SegIcon ? (
                  <SegIcon aria-hidden="true" className={cn('size-3.5', on && 'text-primary!')} />
                ) : String(opt) === 'auto' ? (
                  <Monitor aria-hidden="true" className="size-3.5" />
                ) : (
                  String(opt)
                )}
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
      if (typeof row.data?.command === 'string') {
        // "More voices", "Clone a voice": command rows, not voices — no sample button, no provider badge.
        const clone = row.data.icon === 'clone'
        return (
          <>
            <span className={cn('grid size-6 shrink-0 place-items-center rounded-md', clone ? 'bg-primary/15 text-primary' : 'bg-muted')} aria-hidden="true">
              {clone ? <Plus className="size-3.5" /> : <Ellipsis className="size-3.5" />}
            </span>
            <span className="min-w-0 truncate">{row.title}</span>
            <Keys keys={['↵']} />
          </>
        )
      }
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
          {row.data?.clone === true ? (
            row.data.source === 'design' ? (
              <Sparkles aria-label={t('row.designed_voice')} className="size-3.5!" />
            ) : (
              <UserRound aria-label={t('row.your_voice')} className="size-3.5!" />
            )
          ) : local ? (
            <Laptop aria-label={t('row.local')} className="size-3.5!" />
          ) : (
            <Cloud aria-label={t('row.cloud')} className="size-3.5!" />
          )}
          {row.data?.clone === true && (
            <RowButton label={t('row.delete_voice', { name: row.title })} onPress={() => handlers.onDeleteVoice(row)}>
              <Trash2 className="size-3.5" aria-hidden="true" />
            </RowButton>
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
    case 'design': {
      // A designed candidate: hear it (⇧↵ or ▶), keep it (↵).
      const recordingId = typeof row.data?.recording_id === 'string' ? row.data.recording_id : null
      return (
        <>
          {recordingId && (
            <RowButton label={t('row.preview', { name: row.title })} onPress={() => handlers.onReplay(recordingId)} className="bg-muted">
              <Play className="size-3.5" aria-hidden="true" />
            </RowButton>
          )}
          <Sparkles aria-hidden="true" className="size-3.5!" />
          <span className="min-w-0 truncate">{row.title}</span>
          <Sr>. {t('design.keep_hint')}</Sr>
          <Keys keys={['↵', '⇧↵']} />
        </>
      )
    }
    case 'models': {
      const installed = row.data?.installed === true
      const family = typeof row.data?.family === 'string' ? row.data.family : null
      const status = typeof row.data?.status === 'string' ? row.data.status : ''
      const FamilyIcon = family === 'base' ? UserRound : family === 'voicedesign' ? Sparkles : null
      const chips = family ? [row.data?.size, row.data?.precision].filter((x): x is string => typeof x === 'string') : []
      return (
        <>
          {row.data?.kind === 'stt' ? <Mic aria-hidden="true" /> : FamilyIcon ? <FamilyIcon aria-hidden="true" /> : <Volume2 aria-hidden="true" />}
          <span className="flex min-w-0 flex-col">
            <span className="flex min-w-0 items-center gap-1.5">
              <span className="truncate">{row.title}</span>
              {chips.map((c, i) => (
                <span key={c} className="rounded bg-muted px-1 text-[10px] font-medium text-muted-foreground" data-testid="model-chip">
                  {i === 0 ? c.toUpperCase() : t(`qwen.precision.${c}` as 'qwen.precision.8bit')}
                </span>
              ))}
              {row.data?.chosen === true && <Check aria-label={t('row.chosen')} className="size-3! text-primary" />}
            </span>
            {(status.startsWith('Downloading') ? status : row.subtitle) && (
              <span className="truncate text-xs text-muted-foreground">{status.startsWith('Downloading') ? status : row.subtitle}</span>
            )}
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

interface RowViewProps {
  row: Row
  handlers: RowHandlers
}

/** One row. Memoized: rows are rebuilt on every change, but only a row whose models changed re-renders. */
export const RowView = memo(RowViewInner, (a: RowViewProps, b: RowViewProps) => a.handlers === b.handlers && sameRow(a.row, b.row))

function RowViewInner({ row, handlers }: RowViewProps) {
  useRenderCount('RowView')
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
      return <ActionRow action={row.action} row={row} handlers={handlers} capture={row.capture} />
    case 'card': {
      const card = row.card
      // Blocks are not selectable rows: they just show a figure.
      return (
        <div data-row-kind="card" data-card={card.kind}>
          {card.kind === 'usage' ? (
            <div className="px-2">
              <UsageCard title={card.title} data={card.data} rows={card.rows} />
            </div>
          ) : (
            <WhereCard data={card.data} />
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
          {row.job === 'clone' || row.job === 'design' ? (
            <AudioLines aria-hidden="true" className="text-primary" />
          ) : (
            <Download aria-hidden="true" className="text-primary" />
          )}
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
