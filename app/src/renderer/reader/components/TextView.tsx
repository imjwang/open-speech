import type { CSSProperties, ReactNode } from 'react'

import type { Block } from '@shared/api/client'

import { cn } from '@/lib/utils'
import { segmentText, type BlockMarks, type Segment } from '../highlight'

const MUTED_TYPES = new Set(['header', 'footer', 'page_number', 'footnote', 'toc', 'reference_entry'])

function SegmentSpan({ seg, firstSentence, onStart }: { seg: Segment; firstSentence: boolean; onStart(id: string): void }) {
  const title = seg.skippedBy
    ? `Skipped: ${seg.skippedBy}`
    : seg.replaced
      ? `Spoken as “${seg.replaced.replacement}” (${seg.replaced.rule})`
      : undefined
  const plain = !seg.sentence && !seg.word && !seg.skippedBy && !seg.replaced && !seg.utteranceId
  if (plain) return <>{seg.text}</>
  return (
    <span
      className={cn(
        seg.utteranceId && 'cursor-pointer',
        seg.sentence && 'rounded-sm bg-hl-sentence text-hl-sentence-foreground [box-decoration-break:clone]',
        seg.word &&
          'rounded-sm bg-hl-word text-hl-word-foreground underline decoration-2 underline-offset-4 [box-decoration-break:clone]',
        seg.skippedBy && 'text-skipped line-through opacity-70',
        seg.replaced && 'underline decoration-dotted underline-offset-4',
      )}
      data-current-sentence={firstSentence ? 'true' : undefined}
      data-current-word={seg.word ? 'true' : undefined}
      data-skipped={seg.skippedBy ?? undefined}
      title={title}
      onClick={(e) => {
        if (!seg.utteranceId || window.getSelection()?.toString()) return
        e.stopPropagation()
        onStart(seg.utteranceId)
      }}
    >
      {seg.text}
      {seg.skippedBy && <span className="sr-only"> (skipped by rule: {seg.skippedBy})</span>}
      {seg.replaced && <span className="sr-only"> (spoken as {seg.replaced.replacement})</span>}
    </span>
  )
}

export function TextBlock({
  block,
  marks,
  outlined,
  onStart,
}: {
  block: Block
  marks: BlockMarks
  outlined: boolean
  onStart(utteranceId: string): void
}) {
  const segments = segmentText(block.text, marks)
  const firstSentence = segments.findIndex((s) => s.sentence || s.word)
  const children: ReactNode = segments.map((seg, i) => (
    <SegmentSpan key={`${seg.start}-${seg.end}`} seg={seg} firstSentence={i === firstSentence} onStart={onStart} />
  ))
  const className = cn(
    'scroll-mt-24 rounded-sm',
    outlined && 'outline-2 outline-offset-4 outline-hl-outline outline-dashed',
    MUTED_TYPES.has(block.type) && 'text-[0.85em] text-muted-foreground',
  )
  const common = {
    id: `block-${block.id}`,
    'data-block-id': block.id,
    'data-current-sentence': outlined ? 'true' : undefined,
    className,
  }
  switch (block.type) {
    case 'heading': {
      const level = Math.min(6, Math.max(1, block.level ?? 2))
      const size = level <= 1 ? 'text-[1.6em]' : level === 2 ? 'text-[1.35em]' : 'text-[1.15em]'
      const Tag = `h${level}` as 'h2'
      return (
        <Tag {...common} className={cn(common.className, size, 'mt-6 mb-2 font-semibold')}>
          {children}
        </Tag>
      )
    }
    case 'list_item':
      return <li {...common}>{children}</li>
    case 'quote':
      return (
        <blockquote {...common} className={cn(common.className, 'border-l-4 pl-4 italic')}>
          {children}
        </blockquote>
      )
    case 'code':
      return (
        <pre {...common} className={cn(common.className, 'overflow-x-auto rounded-md bg-muted p-3 font-mono text-[0.85em] whitespace-pre-wrap')}>
          {children}
        </pre>
      )
    case 'caption':
      return (
        <p {...common} className={cn(common.className, 'text-[0.9em] text-muted-foreground italic')}>
          {children}
        </p>
      )
    default:
      return <p {...common}>{children}</p>
  }
}

/** Blocks as semantic text in reading order (DOM order = reading order, 09 § Accessibility). */
export function TextView({
  blocks,
  marksFor,
  outlined,
  onStart,
  style,
  className,
}: {
  blocks: readonly Block[]
  marksFor(blockId: string): BlockMarks
  outlined: ReadonlySet<string>
  onStart(utteranceId: string): void
  style?: CSSProperties
  className?: string
}) {
  const out: ReactNode[] = []
  for (let i = 0; i < blocks.length; ) {
    const block = blocks[i]
    if (!block) break
    if (block.type === 'list_item') {
      const items: Block[] = []
      while (blocks[i]?.type === 'list_item') items.push(blocks[i++] as Block)
      out.push(
        <ul key={`list-${block.id}`} className="my-3 list-disc space-y-1 pl-6">
          {items.map((b) => (
            <TextBlock key={b.id} block={b} marks={marksFor(b.id)} outlined={outlined.has(b.id)} onStart={onStart} />
          ))}
        </ul>,
      )
      continue
    }
    out.push(<TextBlock key={block.id} block={block} marks={marksFor(block.id)} outlined={outlined.has(block.id)} onStart={onStart} />)
    i++
  }
  return (
    <article className={cn('mx-auto max-w-[72ch] space-y-[0.8em] px-6 py-8', className)} style={style}>
      {out}
    </article>
  )
}
