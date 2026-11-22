/**
 * Minimal preload for live web pages in the reader (03 § Security): it can only (1) post the
 * Readability blocks it extracted and (2) paint highlights. It exposes nothing to the page's own
 * scripts (no contextBridge API) and never receives the engine token.
 */
import { Readability } from '@mozilla/readability'
import { ipcRenderer } from 'electron'

import { IPC } from '../shared/ipc-channels'
import type { WebPaint } from '../shared/ipc'
import { extractBlocks, type ReadabilityCtor } from './web/extract'
import { HighlightPainter } from './web/highlight'

let painter: HighlightPainter | null = null
let timer: ReturnType<typeof setTimeout> | null = null

function extractAndPost(): void {
  try {
    const payload = extractBlocks(document, { Readability: Readability as unknown as ReadabilityCtor })
    ipcRenderer.send(IPC.webBlocks, payload)
  } catch (err) {
    ipcRenderer.send(IPC.webBlocks, { url: location.href, title: document.title, blocks: [] })
    console.warn('[open-speech] extraction failed', err)
  }
}

function schedule(delayMs: number): void {
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => {
    timer = null
    painter?.invalidate()
    extractAndPost()
  }, delayMs)
}

function init(): void {
  painter = new HighlightPainter(document, () => ipcRenderer.send(IPC.webFollowReleased))
  // Give client-rendered pages a moment to fill in their content.
  schedule(700)
}

if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', init, { once: true })
else init()

ipcRenderer.on(IPC.webPaint, (_event, paint: WebPaint) => {
  if (paint && Array.isArray(paint.sentence)) painter?.paint(paint)
})
ipcRenderer.on(IPC.webFollow, (_event, value: { follow?: boolean }) => painter?.setFollow(value?.follow === true))
ipcRenderer.on(IPC.webExtract, () => schedule(0))
