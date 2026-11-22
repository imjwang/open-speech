---
name: page-numbers
description: "Don't read page numbers."
scope: {}
enabled: true
tier: filter
compiled:
  drop_blocks: [page_number]
  drop_marks: []
  replace: []
  keep_blocks_only: null
created_by: preset
---
Skip page numbers. In AI Enhanced mode, also skip inline fragments such as "Page 4 of 18" that ended
up inside the body text.
