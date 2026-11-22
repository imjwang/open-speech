/**
 * PDF reader (03 § Highlighting paths): pdf.js renders the original file; an overlay draws the current
 * sentence (tint), the current word (outline + underline bar, never colour alone), block outlines for
 * lower-fidelity utterances, and dimmed spans that rules skipped (with the rule name). Pages render
 * lazily as they approach the viewport. Click a sentence to start there.
 */
import * as pdfjs from 'pdfjs-dist'
import type { PDFDocumentProxy, PDFPageProxy, PageViewport } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { useEffect, useRef, useState, type MouseEvent } from 'react'

import type { Block, RuleHit } from '@shared/api/client'
import type { Anchor } from '@shared/api/ws.gen'

import { anchorRects, bboxToCss, cssToTopLeft, hitTest, wordRects, type PageRect } from '../geometry'

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl

/** Served by the Vite plugin in config/vite-shared.ts (copied from pdfjs-dist). */
const PDFJS_ASSETS = {
  cMapUrl: '/pdfjs/cmaps/',
  cMapPacked: true,
  standardFontDataUrl: '/pdfjs/standard_fonts/',
  wasmUrl: '/pdfjs/wasm/',
  iccUrl: '/pdfjs/iccs/',
}

interface Overlay {
  sentence: PageRect[]
  word: PageRect[]
  outline: PageRect[]
  skipped: Array<PageRect & { rule: string }>
}

function skippedRects(removed: readonly RuleHit[], blocks: ReadonlyMap<string, Block>): Overlay['skipped'] {
  const out: Overlay['skipped'] = []
  for (const hit of removed) {
    if (hit.action === 'replace') continue
    const block = blocks.get(hit.block_id)
    if (!block) continue
    const whole = hit.action === 'drop_block' || hit.action === 'keep_only' || hit.src_start == null || hit.src_end == null
    const rects = whole ? [] : wordRects(block, hit.src_start ?? 0, hit.src_end ?? 0)
    if (rects.length) for (const box of rects) out.push({ page: box.page, box, precision: 'exact', rule: hit.rule_name })
    else if (block.bbox) out.push({ page: block.bbox.page, box: block.bbox, precision: 'block', rule: hit.rule_name })
  }
  return out
}

export function buildOverlay(
  word: { anchor: Anchor; sentenceAnchors: Anchor[] } | null,
  removed: readonly RuleHit[],
  blocks: ReadonlyMap<string, Block>,
): Overlay {
  const sentence: PageRect[] = []
  const outline: PageRect[] = []
  for (const a of word?.sentenceAnchors ?? []) {
    for (const r of anchorRects(a, blocks)) (r.precision === 'block' && a.char_start == null ? outline : sentence).push(r)
  }
  const wordRectsOut = word ? anchorRects(word.anchor, blocks).filter((r) => r.precision === 'exact') : []
  if (word && sentence.length === 0 && outline.length === 0) {
    // No sentence anchors: fall back to outlining the word's block.
    for (const r of anchorRects({ block_id: word.anchor.block_id ?? null }, blocks)) outline.push(r)
  }
  return { sentence, word: wordRectsOut, outline, skipped: skippedRects(removed, blocks) }
}

function PdfPage({
  doc,
  pageNumber,
  scale,
  fallbackSize,
  overlay,
  currentPage,
  onClickPoint,
}: {
  doc: PDFDocumentProxy
  pageNumber: number
  scale: number
  fallbackSize: { width: number; height: number }
  overlay: Overlay
  currentPage: boolean
  onClickPoint(page: number, x: number, y: number): void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [near, setNear] = useState(pageNumber <= 2)
  const [page, setPage] = useState<PDFPageProxy | null>(null)
  const [viewport, setViewport] = useState<PageViewport | null>(null)

  useEffect(() => {
    const el = ref.current
    if (!el || typeof IntersectionObserver === 'undefined') {
      setNear(true)
      return
    }
    const io = new IntersectionObserver((entries) => setNear(entries.some((e) => e.isIntersecting)), { rootMargin: '800px 0px' })
    io.observe(el)
    return () => io.disconnect()
  }, [])

  useEffect(() => {
    if (!near || page) return
    let cancelled = false
    void doc.getPage(pageNumber).then((p) => !cancelled && setPage(p))
    return () => {
      cancelled = true
    }
  }, [near, page, doc, pageNumber])

  useEffect(() => {
    if (!page) return
    setViewport(page.getViewport({ scale }))
  }, [page, scale])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!page || !viewport || !canvas || !near) return
    const dpr = window.devicePixelRatio || 1
    canvas.width = Math.floor(viewport.width * dpr)
    canvas.height = Math.floor(viewport.height * dpr)
    const task = page.render({
      canvas,
      viewport,
      transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
    })
    task.promise.catch(() => undefined)
    return () => task.cancel()
  }, [page, viewport, near])

  const width = viewport?.width ?? fallbackSize.width * scale
  const height = viewport?.height ?? fallbackSize.height * scale
  const onPage = <T extends PageRect>(rects: readonly T[]) => rects.filter((r) => r.page === pageNumber)
  const firstSentence = onPage(overlay.sentence)[0] ?? onPage(overlay.outline)[0]

  const click = (e: MouseEvent<HTMLDivElement>) => {
    if (!viewport) return
    const rect = e.currentTarget.getBoundingClientRect()
    const { x, y } = cssToTopLeft(e.clientX - rect.left, e.clientY - rect.top, viewport)
    onClickPoint(pageNumber, x, y)
  }

  return (
    <div
      ref={ref}
      className="relative mx-auto mb-4 bg-white shadow-md"
      style={{ width, height }}
      data-page={pageNumber}
      data-current-page={currentPage ? 'true' : undefined}
      onClick={click}
    >
      <canvas ref={canvasRef} className="block" style={{ width, height }} role="img" aria-label={`Page ${pageNumber}`} />
      {viewport && (
        <div className="pointer-events-none absolute inset-0" aria-hidden="true">
          {onPage(overlay.skipped).map((r, i) => {
            const css = bboxToCss(r.box, viewport)
            return (
              <div
                key={`s${i}`}
                className="pointer-events-auto absolute bg-[repeating-linear-gradient(135deg,rgba(107,114,128,.35)_0_4px,rgba(255,255,255,.55)_4px_8px)]"
                style={css}
                title={`Skipped: ${r.rule}`}
                data-skipped={r.rule}
              />
            )
          })}
          {onPage(overlay.sentence).map((r, i) => (
            <div
              key={`t${i}`}
              className="absolute rounded-[2px] bg-hl-sentence mix-blend-multiply"
              style={bboxToCss(r.box, viewport)}
              data-current-sentence={r === firstSentence ? 'true' : undefined}
            />
          ))}
          {onPage(overlay.outline).map((r, i) => (
            <div
              key={`o${i}`}
              className="absolute rounded-sm outline-2 outline-offset-2 outline-hl-outline outline-dashed"
              style={bboxToCss(r.box, viewport)}
              data-current-sentence={r === firstSentence ? 'true' : undefined}
            />
          ))}
          {onPage(overlay.word).map((r, i) => {
            const css = bboxToCss(r.box, viewport)
            return (
              <div
                key={`w${i}`}
                className="absolute rounded-[2px] border-b-[3px] border-hl-word outline-2 outline-hl-word"
                style={{ ...css, top: css.top - 1, height: css.height + 3 }}
                data-current-word="true"
              />
            )
          })}
        </div>
      )}
    </div>
  )
}

export function PdfView({
  data,
  blocks,
  removed,
  word,
  onPlayFrom,
}: {
  data: ArrayBuffer
  blocks: readonly Block[]
  removed: readonly RuleHit[]
  word: { anchor: Anchor; sentenceAnchors: Anchor[] } | null
  onPlayFrom(blockId: string, char: number): void
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null)
  const [firstSize, setFirstSize] = useState<{ width: number; height: number } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [containerWidth, setContainerWidth] = useState(900)

  useEffect(() => {
    let cancelled = false
    // pdf.js transfers the buffer to its worker; keep the caller's copy intact.
    const task = pdfjs.getDocument({ data: new Uint8Array(data.slice(0)), ...PDFJS_ASSETS })
    task.promise.then(
      async (d) => {
        if (cancelled) return
        const p1 = await d.getPage(1)
        const vp = p1.getViewport({ scale: 1 })
        if (cancelled) return
        setFirstSize({ width: vp.width, height: vp.height })
        setDoc(d)
      },
      (err: Error) => !cancelled && setError(err.message || 'This PDF could not be displayed.'),
    )
    return () => {
      cancelled = true
      void task.destroy()
    }
  }, [data])

  useEffect(() => {
    const el = containerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setContainerWidth(el.clientWidth))
    ro.observe(el)
    setContainerWidth(el.clientWidth)
    return () => ro.disconnect()
  }, [])

  const blockMap = new Map(blocks.map((b) => [b.id, b]))
  const overlay = buildOverlay(word, removed, blockMap)
  const currentPage = overlay.sentence[0]?.page ?? overlay.outline[0]?.page ?? overlay.word[0]?.page ?? null
  const scale = firstSize ? Math.max(0.5, Math.min(2.5, (Math.min(containerWidth, 1100) - 48) / firstSize.width)) : 1

  const clickPoint = (page: number, x: number, y: number) => {
    const hit = hitTest(page, x, y, blocks)
    if (hit) onPlayFrom(hit.blockId, hit.char)
  }

  return (
    <div ref={containerRef} className="w-full px-6 py-6" data-testid="pdf-view">
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {doc &&
        firstSize &&
        Array.from({ length: doc.numPages }, (_, i) => (
          <PdfPage
            key={i + 1}
            doc={doc}
            pageNumber={i + 1}
            scale={scale}
            fallbackSize={firstSize}
            overlay={overlay}
            currentPage={currentPage === i + 1}
            onClickPoint={clickPoint}
          />
        ))}
    </div>
  )
}
