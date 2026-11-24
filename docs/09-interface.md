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
│ 📎  📋  ⟲  │  ♪  ☑  ⚙                                  ◔ $0.12 │  ← toolbar · this month's spending
└──────────────────────────────────────────────────────────────┘
```

**The orb** is the microphone and the state of the conversation in one control: listening (it moves with the
voice), working, speaking ("Speaking. Press to stop and talk."), and idle (quiet: the conversation is over and
the microphone is released). Pressing it does what Tab does. Its tooltip also gives the shortcut that opens the
bar from anywhere (⌥Space). When this Mac can't listen it says so, and offers the one-press download of the
speech model if that is what is missing. Short notes (connecting, a microphone error, why listening isn't
possible) are the composer's placeholder, not rows of their own, so the bar doesn't grow and shrink; when idle
the placeholder says how to talk again ("Tab or ⌥Space to talk").

**The composer** is a small text editor (Lexical), not a text field.
- **Badges.** Files, links and long pasted text sit inline as badges with an icon for their kind (document, image, link, text). A badge is removed like a character, or with its ✕.
- **Getting things in.** Drop files, links or text anywhere on the bar (several at once become several badges, placed at the caret). Paste: a link becomes a link badge, long or multi-line text a text badge, an image or screenshot an image badge. Type a link and keep typing: it becomes a badge by itself. Press the paperclip to pick files.
- **`@` finds files.** Type `@` for a menu of recently changed files and the usual places (Desktop, Documents, Downloads, Home). A name searches the home folder by file name (Spotlight's index); a path (`@~/Papers/`) walks folders. ↑ ↓ move, ↵ or Tab picks (a file becomes a badge, a folder is entered), Esc closes. Only folders and things Open Speech can read are listed (PDF, DOCX, text and Markdown, HTML, images), only names and paths, never anything outside the home folder and mounted volumes, never hidden files or the Library.
- **Sending.** ↵ sends what is in the composer; ⇧↵ adds a line. Badges alone mean "read these". Words around badges make a command about them ("summarize [report.pdf]").
- **Never locked.** The composer stays editable while an utterance is being recognized or a turn is being answered. What is typed meanwhile is kept for the next command.

**Speaking: a conversation, one way at a time.**
- **Open:** ⌥Space (configurable). The bar appears centered in ≤ 150 ms already listening: the microphone opens
  the instant the bar opens, unless this Mac is known not to be able to listen. Nothing said in the first instant
  is lost: audio is kept (up to 30 s) until the engine has accepted it, even while the engine is still starting,
  and an utterance that ended before then is sent the moment it is. Reading pauses while a conversation is on.
- **A command:** with nothing typed, what you say streams into the composer and is sent when you stop (800 ms of
  silence, releasing a held hotkey, or ↵), together with any badges. The orb shows it working.
- **The answer is spoken** (typed commands and toolbar presses too), and shown. While the assistant speaks the
  orb says so and nothing from the microphone is sent: the reply plays through the speakers, where the
  microphone's echo cancellation can't hear it (07 § Turn-taking).
- **Then it listens again** for a follow-up, for 7 s (10 s when a confirmation card is waiting for "yes" or
  "no"), from the same open microphone. If nothing comes, the conversation goes **idle**: the orb goes quiet,
  the microphone is released, reading resumes. No note, no row. Tab, the orb or the hotkey talks again.
- **A command whose effect is reading** ("resume", "read my clipboard", "faster" while reading) ends the
  conversation instead, so what it did is heard at once. So do the transport buttons.
- **Interrupting:** Tab, the orb or the hotkey while the assistant speaks stops it at once and listens; Esc stops
  it and ends the conversation. (Speaking over it doesn't interrupt yet: 07 § Turn-taking.)
- **Dictation:** with text already typed, what you say is added to the text, and nothing is sent until ↵.
- **Type instead:** any keystroke stops listening and leaves the transcript as editable text; the assistant
  doesn't listen again until what is typed is sent. Tab toggles back to listening.
- **What the bar says itself** is spoken in the interface language and shown: "Sorry, I didn't catch that"
  (then it listens once more, then goes idle), no answer, the budget warning, why it can't listen.

**Results** are rows (shadcn `Command` / cmdk), newest exchange showing and earlier ones folded behind a
single "earlier" row:
- **What you said**, with its badges; ↵ replays your recording.
- **The reply**, spoken and shown; ↵ replays it. "Not part of this build" reads as information, not as an error.
- **A confirmation card** for anything that lasts: one line saying what will change and where, a speech-diff excerpt for rules, and two named buttons. ↵, ⌘↵ or saying "yes" confirms; Esc or "no" cancels. The question is spoken; once it has been heard the bar listens for the answer.
- **Cards** (below).
- With something typed or attached, nothing is selected: ↵ always sends the composer unless you moved to a row yourself.

**The transport strip** appears while something is loaded: back a sentence, play/pause, skip a sentence,
the document (press to show it in the reader), how many more are queued, time left, and speed with − and +.

**The toolbar** is everything the bar can do without a command, as icons: add a file, read the clipboard,
recent documents · voices, rules, settings. Each can also be asked for by voice. A toolbar press
shows its result without a "you said" line.

**The spend chip** ends the toolbar: this month's spending, always in view. A ring fills with the share of the
budget (amber from the warning level, red at the budget), or, while nothing has been spent, with the share of
the free voice allowance in a muted colour; there is no ring when nothing is used and there is no budget. The
amount reads "Free" (exactly nothing), "<$0.01", or dollars and cents ("$0.12"; whole dollars from $100). Its
name for screen readers says it in words: "Spent this month: about 12 cents of your 10 dollar budget,
estimated. Free voice: 120,000 of 1,000,000 characters." Pressing it shows the spending card. It follows the
engine (`GET /usage/budget` on connect and on every open, `usage.changed` between).

**Steady on screen.** Opening the bar never shows the previous open: the window appears only once the new
state has painted, at its final height. The window is resized synchronously with the content (no band below the
panel, no height bouncing), and a listening orb re-renders nothing (the level is a CSS variable).

**Rows, not screens.** Things that are normally settings pages are cards here, and the cards are controls:
- **Voices** → one row per voice with a play button to hear it, its language, where it runs (on this Mac / cloud), and a mark on the current one; ↵ uses it for this document, ⌘↵ makes it the default. The user's own voices have a person icon (a designed one, sparkles) and a trash button (asked on a card first). The card ends with "More voices" and **＋ Clone a voice** (the same as saying "clone my voice").
- **Settings** → the few that matter, each changed in place: theme (three icons: follow the Mac, Lavender, Ember), language (follow the Mac, EN, ES), voice, speed (− +), cleanup mode, text size (− +). While a Qwen voice is in use or Qwen weights are on the Mac, also **local voice size** (Small 0.6B, Large 1.7B) and **local voice precision** (4-bit … Full); an option that needs a download shows ⤓ and says its size, and choosing it asks to download. Changes apply at once in every window.
- **Rules** → the active rules, each with an on/off switch; ⌘↵ opens the rule in the rules editor.
- **Spending** → one figure for the month with a budget ring, always marked "estimated", free allowances in use ("Free voice: 120,000 of 1,000,000 characters · resets Dec 1"), plus what the cache saved.
- **Queue** and **Recent** → documents; ↵ plays, ⌘↵ shows in the reader.
- **Local models** → size and whether it is installed; ↵ asks before downloading. Qwen weights read as their family ("Qwen voices", "Qwen voice cloning", "Qwen voice design") with size and precision chips and a mark on the one the settings choose.
- **Clone your voice** (the passage card, after "clone my voice") → the two sentences to read, in a larger type; one red record control; the consent line under it ("This is my voice, or I have the speaker's permission to clone it. Clones stay on this Mac."), which the record press (or saying "ready") confirms. While recording the orb turns red and breathes with the level, the card shows a stop button, a level bar and the time; silence after the reading ends the take (or the orb, Tab, Enter). The conversation microphone is closed meanwhile: the take uses its own capture path with echo cancellation, noise suppression and gain control off. Then "Making your voice…" with the job's progress, the new voice saying its test sentence, and a "Keep “My voice”" card with ▶ to hear it again; "yes" keeps it and reads with it, "no" deletes it. A take with no speech says so and the card offers to record again; a recording the engine refuses says why and shows the next passage.
- **Designed voices** → after "make a voice that sounds like …": three rows ("Voice 1–3") with ▶; saying "the second one" (or ↵ on a row) keeps that one.
- **Where am I** → document, position and time left.

**Esc** stops the assistant speaking, cancels a pending card, else closes the bar; reading resumes.
**Empty bar:** recent documents, so the last thing can be resumed with ↵.

## 2. Menu bar

- **Icon states:** idle (outline), listening (filled + pulse), speaking (a speech bubble, while the assistant talks), playing (waveform), attention (dot: budget warning or error). Respects reduced motion. The icon and tooltip change only when what they show changes.
- **Click** opens the command bar.
- **Menu:** a first line with this month's spending ("This month: $0.12 · estimated", also in the tooltip) · Open Open Speech · Play / Pause · Recent ▸ · Rules… · Show spending in the menu bar · Quit, in the interface language. "Show spending in the menu bar" (off by default) puts the amount beside the icon.
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

- **Steady while reading.** Only the text block (or PDF page) the word is in redraws as reading moves; a PDF page already drawn is never blanked when it scrolls back into view; opening another document keeps the current one on screen until the new one is ready. While the command bar is up, the reader opens without taking focus (the bar stays).
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

- **Left:** the reading instructions, the pronunciation list, then your rules (and "New rule"), each with its icon and an on/off switch. Built-in names follow the interface language.
- **Right:** the open file as plain text. For a rule that is what it says, in your words; the engine works out how to apply it when it is saved (free and instant when it can, by Claude when it needs judgment). `{ }` shows the whole file, header included, for those who want it.
- **A rule's header strip:** how it is applied (instant, or by Claude), its name (editable: renames the file), where it applies, its switch, the whole-file toggle, delete (asks once more).
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
| Open command bar (listening); while it speaks: stop it and listen; while it listens: close | ⌥Space | Global |
| Push-to-talk | Hold ⌥Space | Global |
| Play / pause | ⌥⇧Space, media keys | Global |
| Read clipboard (text or image) | ⌥⇧R | Global |
| Read selection in any app (P1) | ⌥A | Global (Speechify-compatible) |
| Screenshot to listen (P1) | ⌥X | Global (Speechify-compatible) |
| Send / new line | ↵ / ⇧↵ | Bar |
| Toggle typing / listening; while the assistant speaks: stop it and listen | Tab | Bar |
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
- Icons are lucide, one per thing: each kind of badge and each setting has its own.
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

## 8. First run (no questions)

The first time Open Speech starts, the bar opens by itself, already listening. The voices and the speech
recognizer are in the app, so it can talk and listen at once; defaults stand in for questions (the default voice,
the default reading instructions), and everything can be changed later by voice. There is no step about keys or accounts.

If nothing is said for 4 s it says, once, in the interface language: "Hi, I'm listening. Tell me what to read,
like 'read my clipboard'. Press Option-Space any time to call me." (also shown), then listens for a reply like
after any answer. The first finished turn, or closing the bar, completes the first run (`onboarding_completed`).
The macOS microphone prompt, if it appears, doesn't make the bar disappear.
