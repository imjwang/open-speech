/** Small formatting helpers. Money is always shown as an estimate (FR-ASST-6). */
// This month's spending on the spend chip and in the tray ("Free", "<$0.01", "$0.12", "$123"): shared with
// the main process, so it lives in src/shared.
export { formatSpend, spendLabel, spendRing } from '@shared/spend'

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return ''
  const s = Math.round(seconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m`
  return `${s}s`
}

export function formatTimeLeft(seconds: number | null | undefined): string {
  const d = formatDuration(seconds)
  return d ? `${d} left` : ''
}

export function formatClock(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return ''
  const s = Math.round(seconds)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export function formatSpeed(speed: number | null | undefined): string {
  const v = speed ?? 1
  return `${Number.isInteger(v) ? v.toFixed(1) : v.toFixed(2).replace(/0$/, '')}x`
}

/** Decimal-string money → "$3.20". Never parses into arithmetic beyond display rounding. */
export function formatUsd(value: string | number | null | undefined): string {
  if (value == null || value === '') return '$0.00'
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) return `$${value}`
  if (n > 0 && n < 0.01) return '<$0.01'
  return `$${n.toFixed(2)}`
}

export function formatPercent(fraction: number | null | undefined): string {
  if (fraction == null || !Number.isFinite(fraction)) return ''
  return `${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%`
}

const PROVIDER_LABELS: Record<string, string> = { kokoro: 'Kokoro', openai: 'OpenAI', gemini: 'Gemini', fixture: 'Test' }

/** "Heart" from a catalog name like "Heart (Kokoro, American English)". */
export function voiceTitle(name: string): string {
  return name.split(' (')[0] ?? name
}

/** "Kokoro · American English · free, offline" */
export function voiceDetail(v: { name: string; provider: string; local?: boolean }): string {
  const inner = /\((.*)\)\s*$/.exec(v.name)?.[1]
  const parts = inner ? inner.split(',').map((s) => s.trim()).filter(Boolean) : []
  const label = PROVIDER_LABELS[v.provider] ?? v.provider
  if (parts[0]?.toLowerCase() !== label.toLowerCase()) parts.unshift(label)
  parts.push(v.local ? 'free, offline' : 'cloud')
  return [...new Set(parts)].join(' · ')
}
