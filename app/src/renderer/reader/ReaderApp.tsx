/**
 * The reader (09 § 3): content plus a thin title strip. PDFs render the original with pdf.js; text
 * sources (DOCX/TXT/MD/paste/headless web/image transcripts) render blocks; live web pages render in a
 * WebContentsView owned by the main process. Highlights follow `playback.word`; follow mode keeps the
 * sentence in view until the user scrolls; click a sentence to start there; Script view shows exactly
 * what will be spoken.
 */
import { ArrowDownToLine } from 'lucide-react'
import { useEffect, useRef, useState, type CSSProperties } from 'react'

import { fetchAllBlocks, fetchSource, unwrap, type Block, type DocumentOut } from '@shared/api/client'
import type { ReaderTarget } from '@shared/ipc'

import type { Bridge } from '@/bridge'
import { useEngine, useEngineStore, useServerEvent } from '@/engine'
import { useT } from '@/i18n'
import { usePrefs } from '@/prefs'
import { prefersReducedMotion } from '@/theme'
import { PdfView } from './components/PdfView'
import { ScriptView } from './components/ScriptView'
import { TextView } from './components/TextView'
import { TitleStrip } from './components/TitleStrip'
import {
  EMPTY_MARKS,
  outlinedBlocks,
  removedMarks,
  sentenceRanges,
  utteranceRanges,
  utteranceReplacements,
  wordRange,
  type BlockMarks,
} from './highlight'
import { useReaderStore } from './store'

const LIVE_REGION_MIN_MS = 4000
const FONT_FAMILIES: Record<string, string> = {
  system: 'inherit',
  atkinson: "'Atkinson Hyperlegible', system-ui, sans-serif",
  opendyslexic: "'OpenDyslexic', system-ui, sans-serif",
}

function readTargetFromLocation(): ReaderTarget | null {
  const p = new URLSearchParams(window.location.search)
  const documentId = p.get('doc') ?? undefined
  const url = p.get('url') ?? undefined
  if (!documentId && !url) return null
  return { ...(documentId ? { documentId } : {}), ...(url ? { url } : {}), ...(p.get('autoplay') === '1' ? { autoplay: true } : {}) }
}

/** Static per-block marks (rule removals, replacements, utterance ranges); recomputed only with the script. */
function staticMarks(blocks: readonly Block[], script: ReturnType<typeof useReaderStore.getState>['script']): Map<string, BlockMarks> {
  const map = new Map<string, BlockMarks>()
  if (!script) return map
  for (const b of blocks) {
    const len = [...b.text].length
    const removed = removedMarks(b.id, script.removed, len)
    map.set(b.id, {
      sentence: [],
      word: null,
      skipped: removed.skipped,
      replaced: [...removed.replaced, ...utteranceReplacements(b.id, script.utterances, len)],
      utterances: utteranceRanges(b.id, script.utterances, len),
    })
  }
  return map
}

export function ReaderApp({ bridge }: { bridge: Bridge }) {
  const t = useT()
  const engine = useEngine()
  const endpoint = useEngineStore((s) => s.endpoint)
  const playback = useEngineStore((s) => s.playback)
  const st = useReaderStore()
  const scrollRef = useRef<HTMLElement>(null)
  const programmaticUntil = useRef(0)
  const [liveText, setLiveText] = useState('')
  const lastLive = useRef(0)
  const mac = bridge.platform === 'darwin'

  const docId = st.documentId
  const isPlayingThis = !!docId && playback.document_id === docId
  const isWeb = st.doc?.kind === 'web' && !!st.doc.url && bridge.capabilities.webView
  const showLivePage = isWeb && st.webMode === 'page' && !st.scriptView

  // ---------------------------------------------------------------- target + document selection

  useEffect(() => {
    const initial = readTargetFromLocation()
    if (initial) useReaderStore.setState({ target: initial, documentId: initial.documentId ?? null })
    const offShow = bridge.onReaderShow((target) =>
      useReaderStore.setState({ target, documentId: target.documentId ?? null, webMode: 'page', follow: true }),
    )
    const offWeb = bridge.onWebState((web) => {
      useReaderStore.setState({ web })
      if (web.documentId && web.documentId !== useReaderStore.getState().documentId) {
        useReaderStore.setState({ documentId: web.documentId })
      }
    })
    return () => {
      offShow()
      offWeb()
    }
  }, [bridge])

  useEffect(() => {
    // Without an explicit target, the reader shows (and follows) whatever is playing.
    const t = useReaderStore.getState().target
    const pinnedUrl = !!t?.url
    if (!pinnedUrl && playback.document_id && playback.document_id !== docId && (playback.state === 'playing' || playback.state === 'loading' || !docId)) {
      useReaderStore.setState({ documentId: playback.document_id, follow: true })
    }
  }, [playback.document_id, playback.state, docId])

  useEffect(() => {
    // A URL target: the live page (Electron) posts its blocks and reports the new document id; in a
    // plain browser the engine extracts the page headlessly.
    const target = st.target
    if (!target?.url || docId || !endpoint) return
    if (bridge.capabilities.webView) {
      bridge.setWebMode({ mode: 'page', url: target.url, ...(target.autoplay ? { autoplay: true } : {}) })
      return
    }
    const client = engine.client
    if (!client) return
    void unwrap(
      client.POST('/api/v1/documents', {
        body: { kind: 'url', url: target.url, play: !!target.autoplay, confirm_estimate: false, image_mode: 'auto' },
      }),
    ).then(
      (res) => useReaderStore.setState({ documentId: res.document.id }),
      (err: Error) => useReaderStore.setState({ error: err.message }),
    )
  }, [st.target, docId, endpoint, bridge, engine])

  // ---------------------------------------------------------------- loading

  const loadScript = async (id: string) => {
    const client = engine.client
    if (!client) return
    try {
      const script = await unwrap(client.GET('/api/v1/documents/{document_id}/script', { params: { path: { document_id: id } } }))
      if (useReaderStore.getState().documentId === id) useReaderStore.setState({ script })
    } catch {
      // Script view and dimming need the plan; the reader still works without it.
    }
  }

  useEffect(() => {
    if (!docId || !endpoint) return
    const client = engine.client
    if (!client) return
    let cancelled = false
    let objectUrl: string | null = null
    useReaderStore.setState({ loading: true, error: null, doc: null, blocks: [], script: null, source: null, imageUrl: null, word: null })
    const run = async () => {
      const doc: DocumentOut = await unwrap(client.GET('/api/v1/documents/{document_id}', { params: { path: { document_id: docId } } }))
      const blocks = await fetchAllBlocks(client, docId)
      if (cancelled) return
      useReaderStore.setState({ doc, blocks })
      document.title = doc.title ? `${doc.title} · Open Speech` : 'Open Speech Reader'
      if (doc.has_source_file && (doc.kind === 'pdf' || doc.kind === 'image')) {
        const bytes = await fetchSource(client, docId)
        if (cancelled) return
        if (doc.kind === 'pdf') useReaderStore.setState({ source: bytes })
        else {
          objectUrl = URL.createObjectURL(new Blob([bytes]))
          useReaderStore.setState({ imageUrl: objectUrl })
        }
      }
      await loadScript(docId)
    }
    run()
      .catch((err: Error) => !cancelled && useReaderStore.setState({ error: err.message || 'Could not open this document.' }))
      .finally(() => !cancelled && useReaderStore.setState({ loading: false }))
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [docId, endpoint, engine])

  useEffect(() => {
    // Live page for web documents, hidden for text mode / Script view, destroyed for other kinds.
    const doc = st.doc
    if (!bridge.capabilities.webView || !doc) return
    if (doc.kind !== 'web' || !doc.url) {
      bridge.setWebMode({ mode: 'off' })
      return
    }
    bridge.setWebMode(showLivePage ? { mode: 'page', url: doc.url, documentId: doc.id } : { mode: 'text' })
  }, [st.doc, showLivePage, bridge])

  // ---------------------------------------------------------------- engine events

  useServerEvent('playback.word', (msg) => {
    if (msg.document_id !== useReaderStore.getState().documentId) return
    useReaderStore.setState({
      word: {
        documentId: msg.document_id,
        utteranceId: msg.utterance_id,
        utteranceIndex: msg.utterance_index,
        anchor: msg.anchor,
        sentenceAnchors: msg.sentence_anchors ?? [],
        fidelity: msg.fidelity,
        spokenStart: msg.spoken_start ?? 0,
        spokenEnd: msg.spoken_end ?? 0,
      },
    })
  })

  useServerEvent('document.updated', (msg) => {
    const id = useReaderStore.getState().documentId
    if (msg.document_id !== id || !id) return
    const client = engine.client
    if (!client) return
    void loadScript(id)
    void fetchAllBlocks(client, id).then((blocks) => useReaderStore.getState().documentId === id && useReaderStore.setState({ blocks }))
  })

  // ---------------------------------------------------------------- follow mode

  const word = st.word
  useEffect(() => {
    if (!st.follow || !word || showLivePage) return
    const root = scrollRef.current
    if (!root) return
    const el =
      root.querySelector<HTMLElement>('[data-current-sentence="true"]') ??
      root.querySelector<HTMLElement>('[data-current-word="true"]') ??
      root.querySelector<HTMLElement>('[data-current-page="true"]')
    if (!el) return
    const r = el.getBoundingClientRect()
    const v = root.getBoundingClientRect()
    const margin = Math.min(120, v.height * 0.15)
    if (r.top >= v.top + margin && r.bottom <= v.bottom - margin) return
    programmaticUntil.current = Date.now() + 900
    el.scrollIntoView({ block: 'center', behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
  }, [word?.utteranceId, word?.anchor.char_start, st.follow, st.scriptView, showLivePage, word])

  const onScroll = () => {
    if (Date.now() > programmaticUntil.current && useReaderStore.getState().follow) useReaderStore.setState({ follow: false })
  }

  const backToPlayhead = () => {
    if (showLivePage) bridge.webBackToPlayhead()
    useReaderStore.setState({ follow: true })
  }

  // ---------------------------------------------------------------- live region (rate-limited)

  useEffect(() => {
    const utt = st.script?.utterances.find((u) => u.id === word?.utteranceId)
    if (!utt) return
    const wait = Math.max(0, lastLive.current + LIVE_REGION_MIN_MS - Date.now())
    const t = setTimeout(() => {
      lastLive.current = Date.now()
      setLiveText(utt.text)
    }, wait)
    return () => clearTimeout(t)
  }, [word?.utteranceId, st.script])

  // ---------------------------------------------------------------- playback controls

  const call = async (fn: () => Promise<unknown>) => {
    try {
      await fn()
    } catch (err) {
      useReaderStore.setState({ error: (err as Error).message })
    }
  }

  const playPause = () => {
    const client = engine.client
    if (!client) return
    const p = useEngineStore.getState().playback
    const thisDoc = !!docId && p.document_id === docId
    if (thisDoc && (p.state === 'playing' || p.state === 'loading')) void call(() => unwrap(client.POST('/api/v1/playback/pause')))
    else if (thisDoc && p.state === 'paused') void call(() => unwrap(client.POST('/api/v1/playback/resume')))
    else if (docId) void call(() => unwrap(client.POST('/api/v1/playback/play', { body: { document_id: docId, confirm_estimate: false } })))
  }
  const playPauseRef = useRef(playPause)
  playPauseRef.current = playPause

  const playFrom = (from: { utterance_id?: string; block_id?: string; char?: number }) => {
    const client = engine.client
    if (!client || !docId) return
    useReaderStore.setState({ follow: true })
    void call(() => unwrap(client.POST('/api/v1/playback/play', { body: { document_id: docId, from, confirm_estimate: false } })))
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const client = engine.client
      const target = e.target as HTMLElement | null
      const inControl = !!target?.closest('button, input, textarea, select, [contenteditable="true"], [role="switch"]')
      const mod = mac ? e.metaKey : e.ctrlKey
      if (mod && e.shiftKey && e.key.toLowerCase() === 's') {
        e.preventDefault()
        useReaderStore.setState((s) => ({ scriptView: !s.scriptView }))
        return
      }
      if (mod && !e.shiftKey && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        bridge.openBar({ ...(docId ? { documentId: docId } : {}) })
        return
      }
      if (!client || inControl || e.altKey || mod) return
      const p = useEngineStore.getState().playback
      const thisDoc = !!docId && p.document_id === docId
      if (e.key === ' ') {
        e.preventDefault()
        playPauseRef.current()
      } else if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && thisDoc) {
        e.preventDefault()
        const count = e.key === 'ArrowLeft' ? -1 : 1
        useReaderStore.setState({ follow: true })
        void call(() => unwrap(client.POST('/api/v1/playback/skip', { body: { unit: e.shiftKey ? 'paragraph' : 'sentence', count } })))
      } else if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && thisDoc) {
        e.preventDefault()
        const next = Math.round(Math.min(5, Math.max(0.5, (p.speed ?? 1) + (e.key === 'ArrowUp' ? 0.1 : -0.1))) * 10) / 10
        void call(() => unwrap(client.PUT('/api/v1/playback/speed', { body: { speed: next } })))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [engine, bridge, docId, mac])

  // ---------------------------------------------------------------- render

  // Reader options live in the shared preferences, so "bigger text" by voice shows here at once.
  const settings = usePrefs((s) => s.settings?.reader ?? null)
  const contentStyle: CSSProperties = {
    fontSize: `${settings?.font_size ?? 18}px`,
    lineHeight: settings?.line_spacing ?? 1.6,
    fontFamily: FONT_FAMILIES[settings?.font ?? 'system'] ?? 'inherit',
  }

  const statics = staticMarks(st.blocks, st.script)
  const liveIds = new Set<string>()
  for (const a of word?.sentenceAnchors ?? []) if (a.block_id) liveIds.add(a.block_id)
  if (word?.anchor.block_id) liveIds.add(word.anchor.block_id)
  const marksFor = (blockId: string): BlockMarks => {
    const base = statics.get(blockId) ?? EMPTY_MARKS
    if (!liveIds.has(blockId) || !word) return base
    const len = [...(st.blocks.find((b) => b.id === blockId)?.text ?? '')].length
    return {
      ...base,
      sentence: sentenceRanges(blockId, word.sentenceAnchors.filter((a) => a.char_start != null), len),
      word: wordRange(blockId, word.anchor, len),
    }
  }
  const outlined = outlinedBlocks(word?.sentenceAnchors ?? [])
  const followReleased = showLivePage ? !!st.web?.followReleased : !st.follow
  const title = st.doc?.title ?? st.web?.title ?? (st.loading ? 'Opening…' : 'Open Speech Reader')

  let content
  if (st.error && !st.doc) {
    content = (
      <p role="alert" className="mx-auto max-w-prose px-6 py-10 text-sm text-destructive">
        {st.error}
      </p>
    )
  } else if (!docId && !st.target?.url) {
    content = <p className="mx-auto max-w-prose px-6 py-10 text-muted-foreground">Nothing is playing. Press ⌘K and tell me what to read.</p>
  } else if (st.scriptView && st.script) {
    content = <ScriptView script={st.script} blocks={st.blocks} word={word} onStart={(id) => playFrom({ utterance_id: id })} style={contentStyle} />
  } else if (showLivePage) {
    content = null // the WebContentsView covers the content area
  } else if (st.doc?.kind === 'pdf' && st.source) {
    content = (
      <>
        <PdfView
          data={st.source}
          blocks={st.blocks}
          removed={st.script?.removed ?? []}
          word={word}
          onPlayFrom={(blockId, char) => playFrom({ block_id: blockId, char })}
        />
        {/* Reading-order text for screen readers; the canvas pages are images. */}
        <div className="sr-only">
          <TextView blocks={st.blocks} marksFor={marksFor} outlined={outlined} onStart={(id) => playFrom({ utterance_id: id })} />
        </div>
      </>
    )
  } else {
    content = (
      <>
        {st.imageUrl && (
          <figure className="mx-auto max-w-[72ch] px-6 pt-8">
            <img src={st.imageUrl} alt={st.doc?.title ?? 'Image'} className="max-h-[60vh] w-auto rounded-md border" />
          </figure>
        )}
        <TextView blocks={st.blocks} marksFor={marksFor} outlined={outlined} onStart={(id) => playFrom({ utterance_id: id })} style={contentStyle} />
        {st.loading && <p className="px-6 text-sm text-muted-foreground">{t('reader.loading')}</p>}
      </>
    )
  }

  return (
    <div className="flex h-screen flex-col bg-page text-page-foreground">
      <TitleStrip
        title={title}
        playback={playback}
        isPlayingThis={isPlayingThis}
        pageCount={st.doc?.page_count ?? null}
        scriptView={st.scriptView}
        onToggleScript={() => useReaderStore.setState((s) => ({ scriptView: !s.scriptView }))}
        web={isWeb}
        webMode={st.webMode}
        onToggleWebMode={() => useReaderStore.setState((s) => ({ webMode: s.webMode === 'page' ? 'text' : 'page' }))}
        followReleased={followReleased && (isPlayingThis || showLivePage)}
        onBackToPlayhead={backToPlayhead}
        onPlayPause={() => playPauseRef.current()}
        onCommand={() => bridge.openBar(docId ? { documentId: docId } : {})}
        mac={mac}
      />
      <main ref={scrollRef} className="relative flex-1 overflow-y-auto" onScroll={onScroll} tabIndex={0} aria-label={title}>
        {content}
        {!showLivePage && followReleased && isPlayingThis && (
          <button
            type="button"
            aria-label={t('reader.back_to_playhead')}
            title={t('reader.back_to_playhead')}
            className="fixed bottom-5 left-1/2 grid size-10 -translate-x-1/2 place-items-center rounded-full bg-primary text-primary-foreground shadow-lg focus-visible:outline-2"
            onClick={backToPlayhead}
          >
            <ArrowDownToLine aria-hidden="true" className="size-5" />
          </button>
        )}
      </main>
      <div className="sr-only" aria-live="polite" aria-atomic="true" data-testid="reader-live">
        {liveText}
      </div>
      {st.error && st.doc && (
        <p role="status" className="border-t px-4 py-1 text-xs text-destructive">
          {st.error}
        </p>
      )}
    </div>
  )
}
