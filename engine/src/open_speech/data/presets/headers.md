---
name: headers
description: "Don't read running page headers."
scope: {}
enabled: true
tier: filter
compiled:
  drop_blocks: [header]
  drop_marks: []
  replace: []
  keep_blocks_only: null
created_by: preset
---
Skip running page headers: the journal name, short title or chapter name repeated at the top of pages.
In AI Enhanced mode, also catch running heads the layout heuristic missed, such as a repeated title
fused into the first line of a page.
