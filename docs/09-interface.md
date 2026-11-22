# 09 — Interface

Status: Draft v0.1 · 2026-11-22

**As little UI as possible, and as little text as possible.** The interface is a voice-first command bar,
a menu-bar icon, a reader window that appears only when there's something to look at, and a small editor
for rule files. What is on screen is icons, a few controls and the words the user or the app actually
said. There are no settings screens, sidebars, dashboards or dialogs. Everything else happens by voice,
typed commands, or plain files.

Every control is an icon with a name: shown on hover, read by screen readers, in the interface language
(English or Spanish). Nothing has to be read to be used.

## 1. Command bar

Summoned by a global hotkey from anywhere. It opens **already listening**.

```
┌──────────────────────────────────────────────────────────────┐
│ ◉  [▤ report.pdf] [🌐 example.org/post] read these▌           │  ← orb · composer (badges + text)
├──────────────────────────────────────────────────────────────┤
│                        ⌃ 3                                    │  ← earlier turns, folded away
│ 🎙 read these   [▤ report.pdf] [🌐 example.org]               │  ← what you said (replay)
│ 🔊 Reading 2 items, one after another.                    ↵  │  ← reply (spoken; replay)
├━━━━━━━━━━━━━━━━━━━━━━━━━━──────────────────────────────────────┤  ← progress
│ ⏮  ⏯  ⏭   ▤ report.pdf · 1 more queued     12m left  − 1.8x + │  ← transport strip
├──────────────────────────────────────────────────────────────┤
│ 📎  📋  ⟲  │  ♪  ☑  👛  ⚙                               ⌥Space │  ← toolbar
└──────────────────────────────────────────────────────────────┘
```

**The orb** is the microphone and the state of the conversation in one control: idle (press to talk),
listening (it moves with the voice), working, speaking. When this Mac can't listen yet it says so, and
offers the one-press download of the speech model if that is what is missing.

**The composer** is a small text editor (Lexical), not a text field.
- **Badges.** Files, links and long pasted text sit inline as badges with an icon for their kind (document, image, link, text). A badge is removed like a character, or with its ✕.
- **Getting things in.** Drop files, links or text anywhere on the bar (several at once become several badges, placed at the caret). Paste: a link becomes a link badge, long or multi-line text a text badge, an image or screenshot an image badge. Type a link and keep typing: it becomes a badge by itself. Press the paperclip to pick files.
- **`@` finds files.** Type `@` for a menu of recently changed files and the usual places (Desktop, Documents, Downloads, Home). A name searches the home folder by file name (Spotlight's index); a path (`@~/Papers/`) walks folders. ↑ ↓ move, ↵ or Tab picks (a file becomes a badge, a folder is entered), Esc closes. Only folders and things Open Speech can read are listed (PDF, DOCX, text and Markdown, HTML, images), only names and paths, never anything outside the home folder and mounted volumes, never hidden files or the Library.
- **Sending.** ↵ sends what is in the composer; ⇧↵ adds a line. Badges alone mean "read these". Words around badges make a command about them ("summarize [report.pdf]").
- **Never locked.** The composer stays editable while an utterance is being recognized or a turn is being answered. What is typed meanwhile is kept for the next command.

**Speaking.**
- **Open:** ⌥Space (configurable). The bar appears centered in ≤ 150 ms with the mic live. Reading pauses while it listens.
- **A command:** with nothing typed, what you say streams into the composer and is sent when you stop (800 ms of silence, releasing a held hotkey, or ↵), together with any badges.
- **Dictation:** with text already typed, what you say is added to the text, and nothing is sent until ↵.
- **Type instead:** any keystroke stops the mic and leaves the transcript as editable text. Tab toggles back to listening.
- The microphone turns itself off after 15 s without speech; press the orb, Tab or the hotkey to talk again.

**Results** are rows (shadcn `Command` / cmdk), newest exchange showing and earlier ones folded behind a
single "earlier" row:
- **What you said**, with its badges; ↵ replays your recording.
- **The reply**, spoken and shown; ↵ replays it. "Not part of this build" reads as information, not as an error.
- **A confirmation card** for anything that lasts: one line saying what will change and where, a speech-diff excerpt for rules, and two named buttons. ↵, ⌘↵ or saying "yes" confirms; Esc or "no" cancels. While listening, the card is read aloud and the bar waits for the answer.
- **Cards** (below).
- With something typed or attached, nothing is selected: ↵ always sends the composer unless you moved to a row yourself.

**The transport strip** appears while something is loaded: back a sentence, play/pause, skip a sentence,
the document (press to show it in the reader), how many more are queued, time left, and speed with − and +.

**The toolbar** is everything the bar can do without a command, as icons: add a file, read the clipboard,
recent documents · voices, rules, spending, settings. Each can also be asked for by voice. A toolbar press
shows its result without a "you said" line.

**Rows, not screens.** Things that are normally settings pages are cards here, and the cards are controls:
- **Voices** → one row per voice with a play button to hear it, its language, where it runs (on this Mac / cloud), and a mark on the current one; ↵ uses it for this document, ⌘↵ makes it the default.
- **Settings** → the few that matter, each changed in place: theme (three icons: follow the Mac, Lavender, Ember), language (follow the Mac, EN, ES), voice, speed (− +), cleanup mode, text size (− +). Changes apply at once in every window.
- **Auto-skip** → the presets as chips with icons; a lit chip is being skipped; press to toggle.
- **Rules** → the active rules, each with an on/off switch; ⌘↵ opens the rule in the rules editor.
- **Spending** → one figure for the month with a budget ring, always marked "estimated", plus what the cache saved.
- **Queue** and **Recent** → documents; ↵ plays, ⌘↵ shows in the reader.
- **Local models** → size and whether it is installed; ↵ asks before downloading.
- **Where am I** → document, position and time left.

**Esc** cancels a pending card, else closes the bar; reading resumes.
**Empty bar:** recent documents, so the last thing can be resumed with ↵.

## 2. Menu bar

- **Icon states:** idle (outline), listening (filled + pulse), playing (waveform), attention (dot: budget warning or error). Respects reduced motion.
- **Click** opens the command bar.
- **Menu:** Open Open Speech · Play / Pause · Recent ▸ · Rules… · Quit, in the interface language.
- Playback continues with every window closed.

## 3. Reader

Opens only for content worth seeing: PDFs, web pages, images, and long text.

```
┌────────────────────────────────────────────────────────────────┐
│ ⏯  Attention Is All You Need   1.8x · 12m             ▤  🎙   │  ← thin title strip
├────────────────────────────────────────────────────────────────┤
│                                                                │
│   The dominant sequence transduction models are based on       │
│   complex recurrent or convolutional neural networks ███████   │  ← current sentence (tint)
│   ████████ that include an encoder and a decoder.              │     current word (stronger)
│   ~~[12]~~ The best performing models also connect …           │  ← skipped span, dimmed
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

- **Content only.** No sidebars or toolbars. The title strip has play/pause, the title, speed and time left (and the page for PDFs), and two icons: **what is spoken** (Script view) and **the command bar**.
- **Highlight:** the current sentence has a soft tint; the current word is stronger (exact on Kokoro, estimated on cloud voices). Lower-fidelity utterances outline the block, section or figure instead.
- **Follow mode:** auto-scroll keeps the sentence in view. A manual scroll releases it and shows a small "back to where it is reading" chip.
- **Click** any sentence to start there.
- **Script view:** shows exactly what will be spoken. Skipped spans are dimmed in the source view; hovering (or asking "why didn't it read that?") names the rule.
- **Web pages:** the live site renders in a `WebContentsView` with persistent logins; highlights are painted in-page. A toggle shows the extracted reader text instead.
- **⌘K** opens the command bar in the context of this document.

## 4. Rules editor

One window for the text files behind the rules (`06`). It opens from the tray menu, the bar's Rules
button, "open the rules" / "abre las reglas", ⌘↵ on a rule row, or when the assistant is asked to show it.

```
┌──────────────┬───────────────────────────────────────────────┐
│ ✎ Preferences│ ⚡ skip-footnotes-arxiv-org   🌐 arxiv.org ◉ {} 🗑 │
│ A Pronunc.   ├───────────────────────────────────────────────┤
│ YOURS        │                                               │
│ ⚡ Skip foot… ◉│  Skip the footnotes on arxiv.org              │
│ ＋ New rule   │                                               │
│ BUILT IN     │                                               │
│ ❞ Citations ◉│                                               │
│ ▭ Footers   ◉│                                               │
│ ∑ Formulas  ○├───────────────────────────────────────────────┤
│ …            │ ✓ Saved                                    🎙 │
└──────────────┴───────────────────────────────────────────────┘
```

- **Left:** the standing preferences, the pronunciation list, your rules (and "New rule"), then the built-in presets, each with its icon and an on/off switch. Built-in names follow the interface language.
- **Right:** the open file as plain text. For a rule that is what it says, in your words; the engine works out how to apply it when it is saved (free and instant when it can, AI Enhanced when it needs judgment). `{ }` shows the whole file, header included, for those who want it.
- **A rule's header strip:** how it is applied (instant, or by AI Enhanced), its name (editable: renames the file), where it applies, its switch, the whole-file toggle, delete (asks once more).
- **Saving is automatic,** shortly after typing stops, on ⌘S, and when the window loses focus. The status line says Saved, Saving, Not saved yet, or the reason a save was refused; the text stays in the editor either way.
- **Dictation:** the microphone button adds what you say at the caret.
- **New rule:** ＋ or ⌘N, then write or say what it should do ("skip the author names under each headline"). An empty rule creates nothing.
- **Preferences** start empty, with a line explaining what they are for instead of a template.
- **Changes from elsewhere** (a voice command, the assistant, a hand edit in Finder) appear at once, unless that would overwrite what is being typed.
- The folder icon shows the files in Finder: they are ordinary files under `rules/`.

## 5. Shortcuts

Global shortcuts are remappable in `settings.toml` under `[shortcuts]` and take effect when the file changes.

| Action | Default | Scope |
|---|---|---|
| Open command bar (listening) | ⌥Space | Global |
| Push-to-talk | Hold ⌥Space | Global |
| Play / pause | ⌥⇧Space, media keys | Global |
| Read clipboard (text or image) | ⌥⇧R | Global |
| Read selection in any app (P1) | ⌥A | Global (Speechify-compatible) |
| Screenshot to listen (P1) | ⌥X | Global (Speechify-compatible) |
| Send / new line | ↵ / ⇧↵ | Bar |
| Toggle typing / listening | Tab | Bar |
| Find a file | @ | Bar |
| Confirm / cancel a card | ↵ or ⌘↵ / Esc | Bar |
| Second action on a row (make default, show in reader, open in editor) | ⌘↵ | Bar |
| Play / pause | Space | Reader |
| Previous / next sentence | ← / → | Reader |
| Previous / next paragraph | ⇧← / ⇧→ | Reader |
| Slower / faster (0.1x) | ↓ / ↑ | Reader |
| Script view | ⌘⇧S | Reader |
| Command bar in context | ⌘K | Reader |
| Save now / new rule | ⌘S / ⌘N | Rules editor |

## 6. Visual design

- shadcn/ui with Tailwind v4 tokens; system font (SF Pro on macOS).
- **Two themes**, set by `appearance.theme`:
  - **Lavender**: pastel and light. Soft violet surfaces, a violet accent for listening and highlight.
  - **Ember**: dark with orange. Near-black warm surfaces, an orange accent.
  - **System** (the default) follows the Mac: Lavender by day, Ember in dark mode.
- A theme change applies at once to every open window and the bar's vibrancy, whether it came from a voice command ("dark theme", "tema claro", "match the system"), the settings card, the assistant, or an edit to `settings.toml`.
- The command bar uses native macOS vibrancy. Radius 12 px, generous spacing, one accent color for listening and highlight.
- Icons are lucide, one per thing: each preset, each kind of badge, each setting has its own.
- Highlight colors meet 3:1 contrast against both themes. The current word is never conveyed by color alone (it's also underlined).
- Reader options (by voice or `settings.toml`): font size, line spacing, a dyslexia-friendly font (e.g. OpenDyslexic or Atkinson Hyperlegible), high contrast.
- Motion is minimal and disabled under reduced motion.

## 7. Accessibility

- **Voice parity:** every P0 flow can be completed by voice alone (NFR-5). Tested by scripted e2e runs that drive the bar only with transcripts, and hands-on with recordings fed to the microphone.
- **Screen readers:** every icon control has a name. The bar's rows have ARIA roles; the composer is a labelled text box. The transcript and replies are `aria-live="polite"`; action cards are announced, with their key hints. Segmented controls are radio groups; switches say what they switch. The reader exposes the current sentence via a live region (rate-limited) and keeps DOM order equal to reading order.
- **Keyboard:** everything reachable; visible focus rings; no keyboard traps; Esc always backs out.
- **Captions:** the reply text is always shown alongside the speech.
- **Language:** the whole interface, the tray menu and the spoken replies follow the `language` setting, and switch at once when it changes.
- **AI-voice disclosure:** first run and the voices list state that voices are AI-generated (provider policy).
- **No time limits** on confirmations; spoken prompts repeat on "say that again".

## 8. First run (onboarding: ≤ 3 steps, spoken by Kokoro, skippable)

Runs the first time the bar opens. The voices and the speech recognizer are in the app, so it speaks and
listens at once. There is no step about keys or accounts.

1. **Voice by ear:** up to three sample voices in the interface language, each a big play button. "Pick a voice. Say one, two or three." With only one voice to offer, this step is skipped.
2. **Cleanup:** "Should I skip headers, footers, page numbers and citations? Say yes or no." Builds with AI Enhanced also offer "smart".

Then: "All set. Press Option-Space any time and tell me what to read." and the bar is listening for the
first real command.

Each question can be answered by voice (in either language: "two", "la segunda", "sí", "no"), by a key
(1–3, Y / S / N, ↵) or by pressing a tile. "Skip setup" leaves at any point. If the microphone times out
while the user is thinking, the tiles and keys still work and the orb turns it back on.
