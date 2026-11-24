/**
 * PDF reader (03 § Highlighting paths): pdf.js renders the original file; an overlay draws the current
 * sentence (tint), the current word (outline + underline bar, never colour alone), block outlines for
 * lower-fidelity utterances, and dimmed spans that rules skipped (with the rule name). Pages render
 * lazily as they approach the viewport. Click a sentence to start there.
 *
 * Steady on screen: a page renders into an offscreen canvas and is copied onto the visible one in a single
 * step, so a page on screen is never blank while it re-renders; a page already drawn at this size is not drawn
 * again when it scrolls back into view; and as the word moves only the overlay of the page it is on changes.
 * Pages far away give their pixels back (they are drawn again, offscreen, well before they come back).
 */
import * as pdfjs from 'pdfjs-dist'
import type { PDFDocumentProxy, PDFPageProxy, PageViewport, RenderTask } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { memo, useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'

import type { Block, RuleHit } from '@shared/api/client'
import type { Anchor } from '@shared/api/ws.gen'

import { useRenderCount } from '@/debug'
import { anchorRects, bboxToCss, cssToTopLeft, hitTest, wordRects, type PageRect } from '../geometry'
import { needsRender, renderKey } from '../pdf-render'
import { useReaderStore } from '../store'

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl

/** Served by the Vite plugin in config/vite-shared.ts (copied from pdfjs-dist). */
const PDFJS_ASSETS = {
  cMapUrl: '/pdfjs/cmaps/',
  cMapPacked: true,
  standardFontDataUrl: '/pdfjs/standard_fonts/',
  wasmUrl: '/pdfjs/wasm/',
  iccUrl: '/pdfjs/iccs/',
}

/** Start drawing a page this far before it scrolls into view… */
const RENDER_MARGIN_PX = 1200
/**
 * …and let it give its pixels back only once it is very far away (~15 screens): pages near where the reader has
 * been stay drawn, so scrolling back never shows a blank page, while a long PDF read to the end doesn't hold
 * every page's bitmap (~17 MB each on a Retina screen).
 */
const KEEP_MARGIN_PX = 16_000

interface Overlay {
  sentence: PageRect[]
  word: PageRect[]
  outline: PageRect[]
  skipped: Array<PageRect & { rule: string }>
}

const EMPTY_OVERLAY: Overlay = { sentence: [], word: [], outline: [], skipped: [] }

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

/** The live part of the overlay: where the current sentence and word are. */
function liveRects(word: { anchor: Anchor; sentenceAnchors: Anchor[] } | null, blocks: ReadonlyMap<string, Block>): Omit<Overlay, 'skipped'> {
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
  return { sentence, word: wordRectsOut, outline }
}

export function buildOverlay(
  word: { anchor: Anchor; sentenceAnchors: Anchor[] } | null,
  removed: readonly RuleHit[],
  blocks: ReadonlyMap<string, Block>,
): Overlay {
  return { ...liveRects(word, blocks), skipped: skippedRects(removed, blocks) }
}

function byPage<T extends PageRect>(rects: readonly T[]): Map<number, T[]> {
  const out = new Map<number, T[]>()
  for (const r of rects) {
    const list = out.get(r.page)
    if (list) list.push(r)
    else out.set(r.page, [r])
  }
  return out
}

const PdfPage = memo(function PdfPage({
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
  /** This page's part of the overlay (the same object while it doesn't change). */
  overlay: Overlay
  currentPage: boolean
  onClickPoint(page: number, x: number, y: number): void
}) {
  useRenderCount('PdfPage')
  const ref = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const drawnKey = useRef<string | null>(null)
  /** A render under way. It finishes even if the page scrolls out of range meanwhile (it is drawn by then). */
  const inflight = useRef<{ key: string; task: RenderTask } | null>(null)
  const [near, setNear] = useState(pageNumber <= 2)
  const [keep, setKeep] = useState(true)
  const [page, setPage] = useState<PDFPageProxy | null>(null)
  const [viewport, setViewport] = useState<PageViewport | null>(null)

  useEffect(() => {
    const el = ref.current
    if (!el || typeof IntersectionObserver === 'undefined') {
      setNear(true)
      return
    }
    // The reader's scroll container is the root: with the window as root, the margins would not reach past
    // the container's clip, and a page would only start drawing once it was already on screen.
    const root = el.closest<HTMLElement>('[data-scroll-root]')
    const nearIo = new IntersectionObserver((entries) => setNear(entries.some((e) => e.isIntersecting)), {
      root,
      rootMargin: `${RENDER_MARGIN_PX}px 0px`,
    })
    const keepIo = new IntersectionObserver((entries) => setKeep(entries.some((e) => e.isIntersecting)), {
      root,
      rootMargin: `${KEEP_MARGIN_PX}px 0px`,
    })
    nearIo.observe(el)
    keepIo.observe(el)
    return () => {
      nearIo.disconnect()
      keepIo.disconnect()
    }
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
    // Far away: give the pixels back. It is drawn again (offscreen) long before it comes back into view.
    const canvas = canvasRef.current
    if (keep || !canvas || drawnKey.current === null) return
    canvas.width = 0
    canvas.height = 0
    drawnKey.current = null
  }, [keep])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!page || !viewport || !canvas) return
    const dpr = window.devicePixelRatio || 1
    const key = renderKey(scale, dpr)
    if (!needsRender(near, drawnKey.current, key) || inflight.current?.key === key) return
    inflight.current?.task.cancel() // drawn at another size: that one is no use now
    // Draw offscreen, then copy in one step: resizing the visible canvas clears it, and the copy follows in the
    // same task, so no frame ever shows this page blank (or half drawn) while it re-renders.
    const offscreen = document.createElement('canvas')
    offscreen.width = Math.floor(viewport.width * dpr)
    offscreen.height = Math.floor(viewport.height * dpr)
    const task = page.render({
      canvas: offscreen,
      viewport,
      transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
    })
    inflight.current = { key, task }
    task.promise.then(
      () => {
        if (inflight.current?.task !== task) return
        inflight.current = null
        if (canvas.width !== offscreen.width || canvas.height !== offscreen.height) {
          canvas.width = offscreen.width
          canvas.height = offscreen.height
        }
        const ctx = canvas.getContext('2d')
        ctx?.clearRect(0, 0, canvas.width, canvas.height)
        ctx?.drawImage(offscreen, 0, 0)
        drawnKey.current = key
      },
      () => {
        if (inflight.current?.task === task) inflight.current = null
      },
    )
  }, [page, viewport, near, scale])

  useEffect(() => () => inflight.current?.task.cancel(), [])

  const width = viewport?.width ?? fallbackSize.width * scale
  const height = viewport?.height ?? fallbackSize.height * scale
  const firstSentence = overlay.sentence[0] ?? overlay.outline[0]

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
          {overlay.skipped.map((r, i) => {
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
          {overlay.sentence.map((r, i) => (
            <div
              key={`t${i}`}
              className="absolute rounded-[2px] bg-hl-sentence-page mix-blend-multiply"
              style={bboxToCss(r.box, viewport)}
              data-current-sentence={r === firstSentence ? 'true' : undefined}
            />
          ))}
          {overlay.outline.map((r, i) => (
            <div
              key={`o${i}`}
              className="absolute rounded-sm outline-2 outline-offset-2 outline-hl-outline outline-dashed"
              style={bboxToCss(r.box, viewport)}
              data-current-sentence={r === firstSentence ? 'true' : undefined}
            />
          ))}
          {overlay.word.map((r, i) => {
            const css = bboxToCss(r.box, viewport)
            return (
              <div
                key={`w${i}`}
                className="absolute rounded-[2px] border-b-[3px] border-hl-word-page outline-2 outline-hl-word-page"
                style={{ ...css, top: css.top - 1, height: css.height + 3 }}
                data-current-word="true"
              />
            )
          })}
        </div>
      )}
    </div>
  )
})

export function PdfView({
  data,
  blocks,
  removed,
  onPlayFrom,
}: {
  data: ArrayBuffer
  blocks: readonly Block[]
  removed: readonly RuleHit[]
  onPlayFrom(blockId: string, char: number): void
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  // The document on screen. A new one replaces it only once it has loaded, so switching documents never shows
  // an empty reader in between; `generation` re-creates the pages for the new document.
  const [loaded, setLoaded] = useState<{ doc: PDFDocumentProxy; firstSize: { width: number; height: number }; generation: number } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [containerWidth, setContainerWidth] = useState(900)
  const word = useReaderStore((s) => s.word)

  useEffect(() => {
    let cancelled = false
    let shown = false
    // pdf.js transfers the buffer to its worker; keep the caller's copy intact.
    const task = pdfjs.getDocument({ data: new Uint8Array(data.slice(0)), ...PDFJS_ASSETS })
    task.promise.then(
      async (d) => {
        if (cancelled) return
        const p1 = await d.getPage(1)
        const vp = p1.getViewport({ scale: 1 })
        if (cancelled) return
        shown = true
        setError(null)
        setLoaded((prev) => ({ doc: d, firstSize: { width: vp.width, height: vp.height }, generation: (prev?.generation ?? 0) + 1 }))
      },
      (err: Error) => !cancelled && setError(err.message || 'This PDF could not be displayed.'),
    )
    return () => {
      cancelled = true
      // A document that made it on screen stays there until the next one has loaded (see below).
      if (!shown) void task.destroy()
    }
  }, [data])

  // The previous document's worker goes once its replacement is on screen, and the last one on unmount.
  const shownDoc = useRef<PDFDocumentProxy | null>(null)
  useEffect(() => {
    const previous = shownDoc.current
    shownDoc.current = loaded?.doc ?? null
    if (previous && previous !== shownDoc.current) void previous.loadingTask.destroy()
  }, [loaded])
  useEffect(() => () => void shownDoc.current?.loadingTask.destroy(), [])

  useEffect(() => {
    const el = containerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setContainerWidth(el.clientWidth))
    ro.observe(el)
    setContainerWidth(el.clientWidth)
    return () => ro.disconnect()
  }, [])

  const blockMap = useMemo(() => new Map(blocks.map((b) => [b.id, b])), [blocks])
  const skipped = useMemo(() => byPage(skippedRects(removed, blockMap)), [removed, blockMap])
  // Pages with nothing live on them keep the same overlay object, so they don't re-render as the word moves.
  const staticOverlays = useMemo(() => {
    const out = new Map<number, Overlay>()
    for (const [p, rects] of skipped) out.set(p, { ...EMPTY_OVERLAY, skipped: rects })
    return out
  }, [skipped])
  const live = useMemo(() => liveRects(word, blockMap), [word, blockMap])
  const liveOverlays = useMemo(() => {
    const sentence = byPage(live.sentence)
    const wordBy = byPage(live.word)
    const outline = byPage(live.outline)
    const out = new Map<number, Overlay>()
    for (const p of new Set([...sentence.keys(), ...wordBy.keys(), ...outline.keys()])) {
      out.set(p, { sentence: sentence.get(p) ?? [], word: wordBy.get(p) ?? [], outline: outline.get(p) ?? [], skipped: skipped.get(p) ?? [] })
    }
    return out
  }, [live, skipped])
  const currentPage = live.sentence[0]?.page ?? live.outline[0]?.page ?? live.word[0]?.page ?? null
  const firstSize = loaded?.firstSize
  const scale = firstSize ? Math.max(0.5, Math.min(2.5, (Math.min(containerWidth, 1100) - 48) / firstSize.width)) : 1

  const onPlayFromRef = useRef(onPlayFrom)
  onPlayFromRef.current = onPlayFrom
  const blocksRef = useRef(blocks)
  blocksRef.current = blocks
  // Stable, so the memoized pages don't re-render for a new click handler.
  const clickPoint = useMemo(
    () => (page: number, x: number, y: number) => {
      const hit = hitTest(page, x, y, blocksRef.current)
      if (hit) onPlayFromRef.current(hit.blockId, hit.char)
    },
    [],
  )

  return (
    <div ref={containerRef} className="w-full px-6 py-6" data-testid="pdf-view">
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {loaded &&
        firstSize &&
        Array.from({ length: loaded.doc.numPages }, (_, i) => (
          <PdfPage
            key={`${loaded.generation}:${i + 1}`}
            doc={loaded.doc}
            pageNumber={i + 1}
            scale={scale}
            fallbackSize={firstSize}
            overlay={liveOverlays.get(i + 1) ?? staticOverlays.get(i + 1) ?? EMPTY_OVERLAY}
            currentPage={currentPage === i + 1}
            onClickPoint={clickPoint}
          />
        ))}
    </div>
  )
}
