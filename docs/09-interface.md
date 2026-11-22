# 09 — Interface

Status: Draft v0.1 · 2026-11-21

**As little UI as possible.** The interface is a voice-first command bar, a menu-bar icon, and a reader
window that appears only when there's something to look at. There are no settings screens, sidebars,
dashboards or dialogs. Everything else happens by voice, typed commands, or plain files.

## 1. Command bar

Summoned by a global hotkey from anywhere. It opens **already listening**.

```
┌──────────────────────────────────────────────────────────────┐
│ ◉ ▁▃▅▂  stop reading the footnotes on this site▌        ⌥␣  │  ← live transcript (partials)
├──────────────────────────────────────────────────────────────┤
│ ▶ 0:03  You                                                  │  ← your recording (replay)
│ ▶       That skips footnotes on arxiv.org — 14 on this page. │  ← reply (spoken; replay)
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ ✓ Save rule · Skip footnotes · arxiv.org     ↵  ·  esc ✕ │ │  ← action card
│ │   − ¹ See appendix B for the full derivation.            │ │    (speech diff excerpt)
│ └──────────────────────────────────────────────────────────┘ │
│ ⏵ Now playing · Attention Is All You Need · 1.8x · 12m left  │
│ ⟲ Recent · Kokoro paper · The Economist — Briefing · …       │
└──────────────────────────────────────────────────────────────┘
```

**Behavior**
- **Open:** ⌥Space (configurable). The bar appears centered in ≤ 150 ms with the mic live and a level meter. Reading pauses.
- **Speak:** partials stream into the input line in place. Silence of 800 ms, releasing a held hotkey, or Enter submits.
- **Type:** any keystroke stops the mic and turns the transcript into editable text. Tab toggles back to listening.
- **Results** are rows (shadcn `Command` / cmdk): answer, action card, list (voices with ▶, rules, recents, local models), usage card, now playing.
- **Confirm** with ↵ or ⌘↵ or by saying "yes"; **cancel** with Esc or "no". While listening, the card is read aloud.
- **Replay:** ▶ on your utterance plays your recording; ▶ on a reply replays it. Scroll up for earlier turns this session.
- **Drop or paste** a file, URL, image or text onto the bar → default row "Read it" (↵), plus Describe (images), Summarize, Open in reader.
- **Esc** with nothing pending closes the bar; reading resumes.
- **Empty bar** (no speech for 5 s): shows Now playing, Recents and a few suggestions ("Read clipboard", "Open file…", "Show voices", "How much have I spent?").

**Rows, not screens.** Things that are normally settings pages are rows here:
- **Voices** → a list with ▶ preview, provider and language, a ✓ on the current one; ↵ sets it for this document, ⌘↵ sets the default.
- **Rules** → active rules for the current document, each toggleable; ↵ opens the file in the default editor.
- **Usage** → a compact card: month-to-date estimate, budget bar, top three providers, cache savings.
- **Keys** → "Paste a key" row: a secure input; the provider is detected from the prefix and tested before saving.
- **Local models** → list with size, RAM and a fit badge (fits / slow / unsupported); ↵ installs after confirming.

## 2. Menu bar

- **Icon states:** idle (outline), listening (filled + pulse), playing (waveform), attention (dot: budget warning or error). Respects reduced motion.
- **Click** opens the command bar.
- **Menu:** Play/Pause · Recent ▸ · Open Rules Folder · Quit.
- Playback continues with every window closed.

## 3. Reader

Opens only for content worth seeing: PDFs, web pages, images, video (P1), and long text.

```
┌────────────────────────────────────────────────────────────────┐
│ Attention Is All You Need · p. 3/15 · 1.8x            Script ◐ │  ← thin title strip
├────────────────────────────────────────────────────────────────┤
│                                                                │
│   The dominant sequence transduction models are based on       │
│   complex recurrent or convolutional neural networks ███████   │  ← current sentence (tint)
│   ████████ that include an encoder and a decoder.              │     current word (stronger)
│   ~~[12]~~ The best performing models also connect …           │  ← skipped span, dimmed
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

- **Content only.** No sidebars or toolbars. The title strip shows the title, position, speed, and a **Script view** toggle.
- **Highlight:** the current sentence has a soft tint; the current word is stronger (exact on Kokoro, estimated on cloud voices). Lower-fidelity utterances outline the block, section or figure instead.
- **Follow mode:** auto-scroll keeps the sentence in view. A manual scroll releases it and shows a small "↓ Back to playhead" chip.
- **Click** any sentence to start there.
- **Script view:** shows exactly what will be spoken. Skipped spans are dimmed in the source view; hovering (or asking "why didn't it read that?") names the rule.
- **Web pages:** the live site renders in a `WebContentsView` with persistent logins; highlights are painted in-page. A "Reader text" toggle shows the extracted text instead.
- **⌘K** opens the command bar in the context of this document.

## 4. Shortcuts

All are remappable in `settings.toml` under `[shortcuts]`.

| Action | Default | Scope |
|---|---|---|
| Open command bar (listening) | ⌥Space | Global |
| Push-to-talk | Hold ⌥Space | Global |
| Play / pause | ⌥⇧Space, media keys | Global |
| Read clipboard (text or image) | ⌥⇧R | Global |
| Read selection in any app (P1) | ⌥A | Global (Speechify-compatible) |
| Screenshot to listen (P1) | ⌥X | Global (Speechify-compatible) |
| Toggle typing / listening | Tab | Bar |
| Confirm / cancel | ↵ or ⌘↵ / Esc | Bar |
| Play / pause | Space | Reader |
| Previous / next sentence | ← / → | Reader |
| Previous / next paragraph | ⇧← / ⇧→ | Reader |
| Slower / faster (0.1x) | ↓ / ↑ | Reader |
| Script view | ⌘⇧S | Reader |
| Command bar in context | ⌘K | Reader |

## 5. Visual design

- shadcn/ui with Tailwind v4 tokens; follows the OS light/dark setting; system font (SF Pro on macOS).
- The command bar uses native vibrancy on macOS and a solid surface elsewhere. Radius 12 px, generous spacing, one accent color for listening and highlight.
- Highlight colors meet 3:1 contrast against both themes. The current word is never conveyed by color alone (it's also underlined).
- Reader options (by voice or `settings.toml`): font size, line spacing, a dyslexia-friendly font (e.g. OpenDyslexic or Atkinson Hyperlegible), high contrast.
- Motion is minimal and disabled under reduced motion.

## 6. Accessibility

- **Voice parity:** every P0 flow can be completed by voice alone (NFR-5). Tested by scripted e2e runs that drive the bar only with transcripts.
- **Screen readers:** the bar's input and rows have ARIA roles (combobox + listbox). The transcript and replies are `aria-live="polite"`; action cards are announced, with their key hints. The reader exposes the current sentence via a live region (rate-limited) and keeps DOM order equal to reading order.
- **Keyboard:** everything reachable; visible focus rings; no keyboard traps; Esc always backs out.
- **Captions:** the reply text is always shown alongside the speech.
- **AI-voice disclosure:** onboarding and the voices list state that voices are AI-generated (provider policy).
- **No time limits** on confirmations; spoken prompts repeat on "say that again".

## 7. Onboarding (≤ 3 steps, spoken by Kokoro, skippable)

Runs the first time the bar opens. Kokoro is bundled, so it speaks before any key exists.

1. **Keys (optional):** "If you have an Anthropic, OpenAI or Gemini key, paste it now. Or say 'skip' to use the free local voice." The paste row detects the provider and tests the key.
2. **Voice by ear:** three ▶ samples (Kokoro plus one per keyed provider). "Say one, two or three."
3. **Cleanup:** "Should I skip headers, footers, page numbers and citations? Say 'yes', 'smart' for AI Enhanced, or 'no'."

Then: "Press Option-Space any time and just tell me what to read."
