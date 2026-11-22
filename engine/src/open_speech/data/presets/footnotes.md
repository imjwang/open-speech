---
name: footnotes
description: "Don't read footnotes or footnote markers."
scope: {}
enabled: true
tier: filter
compiled:
  drop_blocks: [footnote]
  drop_marks: [footnote_ref]
  replace: []
  keep_blocks_only: null
created_by: preset
---
Skip footnotes and the small markers that point to them. In AI Enhanced mode, also skip endnote
markers in unusual formats (superscript letters, symbols, bracketed numbers that point to notes).
