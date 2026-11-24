import { describe, expect, it } from 'vitest'

import { needsRender, renderKey } from './pdf-render'

describe('PDF pages are drawn once per size', () => {
  it('draws a page that comes near and has not been drawn at this size', () => {
    expect(needsRender(true, null, renderKey(1.25, 2))).toBe(true)
    expect(needsRender(true, renderKey(1, 2), renderKey(1.25, 2))).toBe(true) // the window was resized
    expect(needsRender(true, renderKey(1.25, 1), renderKey(1.25, 2))).toBe(true) // moved to a Retina screen
  })

  it('skips a page scrolling back into view that is already drawn at this size (no clear, no blank frame)', () => {
    expect(needsRender(true, renderKey(1.25, 2), renderKey(1.25, 2))).toBe(false)
  })

  it('does nothing for pages far from the viewport', () => {
    expect(needsRender(false, null, renderKey(1.25, 2))).toBe(false)
  })

  it('treats scales that only differ past the fourth decimal as the same size', () => {
    expect(renderKey(1.2500001, 2)).toBe(renderKey(1.25, 2))
  })
})
