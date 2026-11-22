# 06 — Rules and skills

Status: Draft v0.1 · 2026-11-21

The design borrows from Claude Code: standing preferences in one Markdown file (like `CLAUDE.md`),
scoped rule files (like `.claude/rules/*.md` with path scopes), and on-demand **skills** in the open
Agent Skills format. Everything is a plain file under `rules/` and `skills/` in app data. Users can
version, share and hand-edit them, and the engine reloads them on change.

## Three kinds of file

| File | What it is | Loaded |
|---|---|---|
| `rules/SPEECH.md` | Free-form standing preferences ("I'm a nurse; read drug names carefully; never read legal disclaimers") | Every Claude plan call, as part of the cached prefix |
| `rules/*.md` | One rule each: scope + instruction (+ compiled filter) | When the scope matches the document |
| `rules/lexicon.csv` | Pronunciations: `term,spoken[,scope]` | Always; applied as a deterministic filter |
| `skills/<name>/SKILL.md` (P1) | A pack of instructions, rules and lexicon for a document genre | Only name + description in context until relevant |

## Rule file format

```markdown
---
name: skip-author-year-citations
description: Don't read author-year citations like "(Smith et al., 2020)"; keep the names when they're the sentence subject.
scope:
  sites: [arxiv.org, "*.acm.org"]
  types: [pdf]
enabled: true
tier: annotate            # filter | annotate | transform — set by the compiler, user may override
compiled: null            # machine-managed filter spec when tier = filter
created_by: assistant     # assistant | user | preset | skill:<name>
---
Skip parenthetical author-year citations. If the author name is the grammatical
subject ("Smith et al. (2020) showed…"), keep the name and drop only the year.
```

**Scope keys** (all optional; an empty scope means global):
- `sites`: hostnames with `*` wildcards (web sources, and PDFs downloaded from them)
- `types`: `pdf | web | docx | text | image | video | audio`
- `docs`: document IDs (created when the user says "for this document only")
- `languages`: BCP-47 codes

**Precedence:** `docs` > `sites` > `types` > global. Within the same level, the most recently edited
rule wins. If two active rules conflict ("read footnotes" vs "skip footnotes"), the engine reports the
conflict on preview, and the assistant asks which one to keep. Rules are never silently merged.

## Compiled filters (tier 1)

When the compiler can express a rule deterministically, it writes `compiled` and sets `tier: filter`, so
it costs nothing at read time:

```yaml
compiled:
  drop_blocks: [footnote]             # block types
  drop_marks: [citation_ref, url]     # mark kinds
  replace:                            # regex → replacement, applied to remaining text
    - {pattern: "\\bet al\\.", with: "and colleagues"}
  keep_blocks_only: null              # e.g. [heading, paragraph] for "only read the body"
```

## Built-in presets

Presets are ordinary rule files shipped in `engine/data/presets/` and copied to `rules/` on first run.
They carry the same names as Speechify's toggles, so switchers find them, plus three additions.

| Preset | Classic (filter) | AI Enhanced adds |
|---|---|---|
| Headers | `drop_blocks: [header]` | Catches running heads the heuristic missed |
| Footers | `drop_blocks: [footer]` | — |
| Page numbers | `drop_blocks: [page_number]` | Inline "Page 4 of 18" fragments |
| Footnotes | `drop_blocks: [footnote]`, `drop_marks: [footnote_ref]` | Endnote markers in odd formats |
| Citations | `drop_marks: [citation_ref]` | Author-year forms, keeping names that are subjects |
| URLs | `drop_marks: [url]` | Reads a short domain when the link text *is* the URL ("see example.org") |
| Parentheses | `drop_marks: [parenthetical]` | Keeps parentheses that carry meaning |
| Brackets | `drop_marks: [bracket]` | — |
| Braces | `drop_marks: [brace]` | — |
| Tables | `drop_blocks: [table]` | Optional one-sentence table summary (transform) |
| Formulas | `drop_blocks: [equation]`, `drop_marks: [formula]` | Optional "explain in words" (transform) |
| Captions | `drop_blocks: [caption]` | — |
| Code | `drop_blocks: [code]` | Optional one-line summary of each code block |

**Mode** (`classic | ai_enhanced`) is set globally and can be overridden per scope (e.g. AI Enhanced
only for `sites: [arxiv.org]`). Classic never calls an API.

## Lexicon

```csv
term,spoken,scope
SQL,sequel,
Nguyen,win,
kubectl,cube control,
mg,milligrams,types:pdf
```

Matching is whole-word and case-sensitive unless the term is all lowercase. The lexicon is applied after
filters and before chunking. It uses respelling rather than SSML or phonemes, because neither cloud
provider accepts reliable phoneme markup. A lexicon entry changes the utterance text, so the audio cache
re-synthesizes only the affected utterances.

## The compiler

When a rule is proposed (by the assistant or a hand edit), the compiler makes one structured Claude call:

1. **Classify:** filter (expressible in the `compiled` schema), annotate (needs judgment on spans) or transform (changes what is said).
2. **Compile** filter rules into the `compiled` schema. The engine validates the schema and test-runs the regexes.
3. **Scope:** suggest a scope from context ("you're on arxiv.org: only there, or everywhere?").
4. **Name and describe** the rule.

Hand-written rules with no `tier` are compiled on first load. Users can pin `tier` to override.

## Speech diff (preview before saving)

Every proposed rule is previewed on the current document (first ~2 pages or the current section):
- **Removals** are struck through and dimmed; **replacements** are shown as `old → new`.
- **Transforms** show the generated script next to the source section.
- **Spoken summary** for eyes-free users: "On this page that removes 14 citations and 2 footnotes. Save it for arxiv.org?"
- Confirming writes the file. Cancelling leaves no trace.

## Authoring by voice or text

> "Stop reading the footnotes on this site."
> → `propose_rule(instruction, scope_hint=site)` → compiler → filter rule `drop_blocks: [footnote]`, `sites: [current host]`
> → speech diff + spoken summary → "yes" → saved → current plan re-planned from the playhead.

> "Say kubectl as cube control."
> → lexicon entry (no Claude call needed; the fast path recognizes "say X as Y").

> "For this paper, just read the abstract and the conclusions."
> → transform rule, `docs: [doc_…]` → estimate (small) → confirm → script rebuilt.

## Skills (P1)

Skills follow the **Agent Skills specification** (agentskills.io), the open format Claude Code and other
agent tools read. Validation mirrors `aden/aden/agents/spec.py`: `name` 1–64 chars `[a-z0-9-]` matching the
directory, `description` 1–1024 chars, optional `compatibility` and `metadata`. Open Speech additions live
under `metadata` keys prefixed `open-speech-`, so the file stays valid for every other reader.

```
skills/academic-paper/
  SKILL.md
  rules/skip-citations.md
  rules/describe-figures.md
  lexicon.csv
```

```markdown
---
name: academic-paper
description: Reading research papers aloud. Use for PDFs with an abstract, numbered sections, citations and a references list.
metadata:
  open-speech-types: "pdf"
  open-speech-detect: "abstract|references|doi:"
  open-speech-version: "1"
---
Read the title, authors (first three, then "and colleagues"), abstract, and body.
Skip the references section, citations, and figure numbers. Describe figures in one sentence.
Announce section headings as "Section 3: Methods".
```

- **Progressive disclosure:** only `name` + `description` are in the assistant's context; the body and bundled rules load when the skill is applied.
- **Suggestion:** when a document matches `open-speech-detect`, the bar offers "This looks like a paper — use Academic Paper?" (spoken if listening).
- **Bundled skills:** academic-paper, legal-document, news-article, code-heavy-docs, math-text, slides-and-lectures (video/P1).
- Applying a skill activates its rules with `created_by: skill:<name>`. Removing the skill removes them.

## Worked examples

| Say | Becomes |
|---|---|
| "Skip citations" | Preset *Citations* on (Classic); AI Enhanced handles author-year |
| "Describe figures in one sentence" | Transform rule, `types: [pdf, web]`, figure-anchored utterances |
| "Say SQL as sequel" | Lexicon row |
| "Summarize each section in two sentences" | Transform rule (section-anchored); estimate first |
| "Only read the abstract and conclusions of this paper" | Transform rule, `docs: [this]` |
| "On Wikipedia, skip the infobox and the edit links" | Filter rule `sites: [*.wikipedia.org]`, compiled `drop_blocks` + regex |
| "Never read legal disclaimers" | Added to `SPEECH.md` (preference) and an annotate rule |
