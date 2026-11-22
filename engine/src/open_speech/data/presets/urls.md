---
name: urls
description: "Don't read web addresses."
scope: {}
enabled: true
tier: filter
compiled:
  drop_blocks: []
  drop_marks: [url]
  replace: []
  keep_blocks_only: null
created_by: preset
---
Skip web addresses and DOIs. In AI Enhanced mode, when the link text is the address itself
("see example.org/docs"), read just the short domain ("see example.org").
