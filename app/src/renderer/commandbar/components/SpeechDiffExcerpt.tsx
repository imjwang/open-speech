import type { SpeechDiff } from '@shared/api/ws.gen'

/** Rule preview (06 § Speech diff): removals struck through and dimmed, replacements old → new. */
export function SpeechDiffExcerpt({ diff, max = 3 }: { diff: SpeechDiff; max?: number }) {
  const removals = (diff.removals ?? []).slice(0, max)
  const replacements = (diff.replacements ?? []).slice(0, Math.max(0, max - removals.length))
  const transforms = (diff.transforms ?? []).slice(0, 1)
  const counts = Object.entries(diff.counts ?? {}).filter(([, n]) => (n ?? 0) > 0)
  if (!removals.length && !replacements.length && !transforms.length && !diff.summary) return null
  return (
    <div className="mt-1.5 space-y-1 border-l-2 border-border pl-3 text-[13px] leading-snug" data-testid="speech-diff">
      {removals.map((r, i) => (
        <p key={`r${i}`} className="text-muted-foreground">
          <span aria-hidden="true">− </span>
          <span className="sr-only">Removed by {r.rule_name}: </span>
          <del className="decoration-1 opacity-75">{r.text}</del>
        </p>
      ))}
      {replacements.map((r, i) => (
        <p key={`p${i}`}>
          <span className="sr-only">Replaced by {r.rule_name}: </span>
          <del className="text-muted-foreground opacity-75">{r.old}</del>
          <span aria-hidden="true"> → </span>
          <span className="sr-only"> with </span>
          <ins className="no-underline font-medium">{r.new}</ins>
        </p>
      ))}
      {transforms.map((t, i) => (
        <p key={`t${i}`} className="italic">
          {t.script.slice(0, 2).join(' ')}
        </p>
      ))}
      {counts.length > 0 && (
        <p className="text-xs text-muted-foreground">
          {counts.map(([k, n]) => `${n} ${k.replace(/_/g, ' ')}`).join(' · ')}
        </p>
      )}
      {diff.summary && <p className="text-xs text-muted-foreground">{diff.summary}</p>}
    </div>
  )
}
