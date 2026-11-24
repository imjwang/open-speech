/** When a PDF page needs drawing (PdfView.tsx). Pure, so the skip rule is tested without pdf.js. */

/** The size a page was last drawn at. A page drawn at this key is not drawn again. */
export function renderKey(scale: number, dpr: number): string {
  return `${scale.toFixed(4)}:${dpr}`
}

/** Whether a page needs drawing: it is near the viewport and not already drawn at this size. */
export function needsRender(near: boolean, drawnKey: string | null, key: string): boolean {
  return near && drawnKey !== key
}
