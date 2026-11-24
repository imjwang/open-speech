import { AudioLines, CircleSlash, EyeOff, Sparkles } from 'lucide-react'
import { memo, type CSSProperties } from 'react'

import type { Block, ScriptOut, Utterance } from '@shared/api/client'
import { codePointIndexTable, cpToUtf16 } from '@shared/text'

import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useT } from '@/i18n'
import { cn } from '@/lib/utils'
import { scriptEntries } from '../highlight'
import { useReaderStore } from '../store'

function UtteranceText({ text, word }: { text: string; word: { start: number; end: number } | null }) {
  if (!word || word.end <= word.start) return <>{text}</>
  const table = codePointIndexTable(text)
  const s = cpToUtf16(text, word.start, table)
  const e = cpToUtf16(text, word.end, table)
  return (
    <>
      {text.slice(0, s)}
      <span className="rounded-sm bg-hl-word text-hl-word-foreground underline decoration-2 underline-offset-4" data-current-word="true">
        {text.slice(s, e)}
      </span>
      {text.slice(e)}
    </>
  )
}

/** One spoken utterance. It follows the playing word itself, so only the current one re-renders per word. */
const ScriptUtterance = memo(function ScriptUtterance({ u, onStart }: { u: Utterance; onStart(utteranceId: string): void }) {
  const word = useReaderStore((s) => (s.word?.utteranceId === u.id ? s.word : null))
  const current = word !== null
  const replacements = (u.rule_trace ?? []).filter((h) => h.action === 'replace')
  return (
    <p
      className={cn('cursor-pointer rounded-sm', u.is_heading && 'mt-4 font-semibold', current && 'bg-hl-sentence text-hl-sentence-foreground')}
      data-current-sentence={current ? 'true' : undefined}
      data-utterance-id={u.id}
      onClick={() => onStart(u.id)}
    >
      <UtteranceText text={u.text} word={word ? { start: word.spokenStart, end: word.spokenEnd } : null} />
      {replacements.map((h, i) => (
        <span key={i} className="ml-2 rounded border px-1 text-[0.75em] text-muted-foreground" title={h.rule_name}>
          {h.original} → {h.replacement}
        </span>
      ))}
    </p>
  )
})

/**
 * Script view (FR-RULE-5, 09 § Reader): exactly what will be spoken, in order, with the spans rules
 * removed shown dimmed; each removal names its rule on hover or keyboard focus.
 */
export function ScriptView({
  script,
  blocks,
  onStart,
  style,
}: {
  script: ScriptOut
  blocks: readonly Block[]
  onStart(utteranceId: string): void
  style?: CSSProperties
}) {
  const t = useT()
  const order = new Map(blocks.map((b) => [b.id, b.order]))
  const entries = scriptEntries(script, order)
  // `mode` is "ai_enhanced" when Claude cleaned the text, "classic" when it couldn't run; the warnings say why.
  const aiCleanup = script.mode === 'ai_enhanced'
  const cleanupLabel = t(aiCleanup ? 'cleanup.ai' : 'cleanup.unavailable')
  const why = aiCleanup ? undefined : script.warnings?.[0]
  return (
    <article className="mx-auto max-w-[72ch] px-6 py-8" style={style} aria-label={t('reader.script')} data-testid="script-view">
      <p className="mb-4 flex items-center gap-3 text-xs text-muted-foreground" data-testid="script-summary">
        <span className="inline-flex items-center gap-1" title={why ? `${cleanupLabel}\n${why}` : cleanupLabel} data-cleanup={aiCleanup ? 'ai' : 'unavailable'}>
          {aiCleanup ? <Sparkles aria-hidden="true" className="size-3.5" /> : <CircleSlash aria-hidden="true" className="size-3.5 opacity-60" />}
          <span className="sr-only">{cleanupLabel}</span>
        </span>
        <span className="inline-flex items-center gap-1 tabular-nums">
          <AudioLines aria-hidden="true" className="size-3.5" />
          {script.utterances.length}
        </span>
        <span className="inline-flex items-center gap-1 tabular-nums">
          <EyeOff aria-hidden="true" className="size-3.5" />
          {script.removed.length}
        </span>
      </p>
      {(script.warnings ?? []).length > 0 && (
        <ul className="mb-4 text-xs text-muted-foreground">
          {(script.warnings ?? []).map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      <div className="space-y-[0.6em]">
        {entries.map((entry) => {
          if (entry.kind === 'utterance' && entry.utterance) return <ScriptUtterance key={entry.key} u={entry.utterance} onStart={onStart} />
          const hit = entry.hit
          if (!hit) return null
          const text = hit.original || blocks.find((b) => b.id === hit.block_id)?.text || ''
          return (
            <Tooltip key={entry.key}>
              <TooltipTrigger asChild>
                <p
                  tabIndex={0}
                  className="rounded-sm text-[0.9em] text-skipped line-through opacity-70 focus-visible:no-underline"
                  data-skipped={hit.rule_name}
                  aria-label={`${t('reader.skipped_by', { rule: hit.rule_name })}: ${text}`}
                >
                  {text.length > 280 ? `${text.slice(0, 277)}…` : text}
                </p>
              </TooltipTrigger>
              <TooltipContent>Skipped by {hit.rule_name}</TooltipContent>
            </Tooltip>
          )
        })}
      </div>
    </article>
  )
}
