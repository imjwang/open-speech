---
name: citations
description: "Don't read citation markers like [12] or (Smith et al., 2020)."
scope: {}
enabled: true
tier: filter
compiled:
  drop_blocks: []
  drop_marks: [citation_ref]
  replace: []
  keep_blocks_only: null
created_by: preset
---
Skip in-text citations such as [12], [3-5] and (Smith et al., 2020). In AI Enhanced mode, also skip
other author-year forms, but keep the author names when they are the grammatical subject
("Smith et al. (2020) showed..." becomes "Smith et al. showed...").
