# 01 — Speechify analysis

Status: Draft v0.1 · 2026-11-21 · Research snapshot; claims cite Speechify's help center and 2025–2026 reviews (sources at the end).

## Key findings

1. **Speechify's Mac app is now only a menu-bar utility.** The Dec 2025 app reads selected text (⌥A), reads a screenshot via OCR (⌥X), pauses and resumes (⌥Q), and offers a floating mini player and dictation. It has no documented library or document reader. Full reading, skipping, notes and MP3 export live in the **web app**. A desktop app that combines a real reader with system-wide reading fills a gap.
2. **Their cleanup is a fixed list of toggles.** On the web, "Auto Skip content" offers *Headers, Footers, Footnotes, Tables, Formulas, Citations, URLs, Parentheses, Brackets, Braces*, with a **Classic** vs **AI Enhanced** mode. iOS adds page numbers. The toggles differ by platform, and users still report page numbers, "dot dot dot" and other junk being read.
3. **There is no user pronunciation dictionary for reading.** The official advice is to report mispronunciations to support. The "Dictionary" on Mac and Windows is for dictation only.
4. **Billing drives the complaints.** Trials roll into annual plans; users report charges after cancelling and prices that differ by channel. Premium-voice word quotas (150k/month guaranteed, 1M cap in 2026) can change "without notice". Trustpilot is 4.7 overall, but billing dominates the 1-star reviews.
5. **The product has sprawled.** Podcasts, meeting assistant, AI workspace, Studio, audiobook store, a ChatGPT app and celebrity voices sit around the reader. Reviewers cite upsell nudges, forced sign-in and long onboarding.
6. **The voice assistant doesn't control playback.** It is pitched as removing the need for a keyboard, but there is no documented way to say "go back a paragraph", "skip this table" or "where am I?"
7. **Speed** runs 0.5x–5x (Premium; Free is capped at 1.5x), with an "Increase Speed Automatically" ramp. It is shown as a multiplier. Quality is reported to degrade above 2.5–3x.

## Keep / Simplify / Drop / Later

| Speechify feature | Decision | Our version | Req |
|---|---|---|---|
| Import PDF, DOCX, TXT, HTML, URL, paste | **Keep** | Same, plus clipboard and images | FR-READ-1..4, FR-MM-1 |
| Scanned PDF / OCR | **Keep (P1)** | "AI Enhanced extraction" via Claude vision, estimated first | FR-READ-11 |
| Screenshot to listen (⌥X) | **Keep (P1)** | Screenshot → Claude vision (transcribe or describe) | FR-READ-10 |
| Read selection anywhere (⌥A) | **Keep (P1)** | Global hotkey; P0 ships **read clipboard** (no permissions) | FR-READ-5, FR-READ-9 |
| One-click play on web pages | **Simplify** | Pages open in the in-app reader; the bar's "Read this" works on any open page | FR-READ-2 |
| Synced highlighting, auto-scroll, click-to-jump | **Keep** | Sentence exact; word exact on Kokoro, estimated on cloud voices; follow mode | FR-READ-6, FR-READ-7 |
| Skip forward/back (sentence, 10 s) | **Keep** | ±sentence, ±paragraph; "back 10 seconds" by voice | FR-READ-8 |
| Speed 0.5–5x, auto-ramp | **Keep** | 0.5–5x, pitch-preserved, no re-synthesis; ramp P1 | FR-READ-12, FR-READ-13 |
| Auto-skip toggles + Classic / AI Enhanced | **Keep** | The same names, as **built-in rule presets**; Classic = deterministic filters, AI Enhanced = Claude span ops | FR-RULE-1 |
| Pronunciation fixes (support ticket) | **Replace** | User lexicon (`lexicon.csv`) + "say X as Y" by voice | FR-RULE-4 |
| Voice picker (400–1,000+ voices, diamonds) | **Simplify** | One list across OpenAI, Gemini and local, ▶ preview without switching, no badges | FR-VOICE-1..3 |
| Offline / on-device voices | **Keep** | Kokoro, bundled on first run, works fully offline | FR-VOICE-5 |
| Library, folders, resume | **Simplify** | Recents in the command bar + full-text search; position always resumes. Folders P1 | FR-BAR-8, FR-READ-14 |
| Cross-device sync | **Later** | Optional sync through a user-owned folder | — |
| MP3 export (always 1x) | **Keep (P1)** | Export at any speed, estimate first | FR-READ-15 |
| Mini player with live subtitles | **Simplify** | Menu-bar state + command bar now-playing row; floating pill P1 | FR-BAR-9, FR-BAR-10 |
| Dark mode, dyslexic font | **Keep** | Theme follows the OS; dyslexia-friendly font option in the reader | FR-SETUP-5 |
| Remappable shortcuts | **Keep** | `settings.toml`; Speechify-equivalent defaults | FR-SETUP-6 |
| AI summaries / chat with doc | **Simplify** | Same assistant as everything else ("summarize this section") | FR-ASST-8 |
| Voice AI assistant | **Replace** | Voice operates *everything*: playback, rules, voices, usage, settings | FR-ASST-1..7 |
| Highlights, notes, bookmarks | **Later (P2)** | Bookmarks by voice first | — |
| Sleep timer | **Later (P2)** | By voice ("stop in 20 minutes") | — |
| Gmail / Google Docs / Kindle Cloud play buttons | **Later** | Covered by the in-app browser + clipboard; no extension | — |
| Cloud drives (Drive, Dropbox, OneDrive) | **Later** | Local files first | — |
| EPUB | **Later (P2)** | — | FR-READ-16 |
| Podcasts, meetings, workspace, Studio, audiobooks, ChatGPT app | **Drop** | Out of scope by principle | — |
| Celebrity voices, premium badges, upsells | **Drop** | — | — |
| Accounts, sign-in, trials, subscriptions | **Drop** | No account; provider keys are part of the build; costs shown in dollars | — |
| Tracking SDKs, cloud library | **Drop** | Local-only, no telemetry | — |

## Where we are structurally better

1. **A user pronunciation dictionary** and free-form rules, identical on every surface, stored as files you can share.
2. **Voice control of playback and navigation**: "back a paragraph", "skip this table", "where am I?", "how much is left?"
3. **No billing disputes**: no subscription, a local ledger in dollars, budgets you set.
4. **Paywalled and logged-in pages** read through your own session in the in-app browser (Speechify's URL import cannot).
5. **Pinned voices** can't drift into another language after a vendor update.
6. **Multimodal sources**: images now, audio files next. Claude turns them into a script worth listening to.
7. **Local voices** at $0, offline, with exact word timing.
8. **An API**: anything the app does is scriptable.

## Switcher guide (goes into the README)

| Speechify | Open Speech |
|---|---|
| Settings → Auto Skip content → Headers / Footers / … | Say "skip headers" or toggle the preset in the bar (`Skip: Headers`) |
| Classic / AI Enhanced | Same names; AI Enhanced uses Claude, in builds that include it |
| ⌥A play selection (Mac) | ⌥⇧R reads the clipboard (P0); ⌥A reads the selection (P1) |
| ⌥X screenshot to listen | ⌥X (P1) |
| ⌥Q pause / resume | ⌥⇧Space (global), media keys, or say "pause" |
| Speed 1.0x–5.0x | Same multipliers; WPM shown on request ("what speed is this?") |
| Report a mispronunciation | "Say *Nguyen* as *win*" adds it to your lexicon |
| Voice picker | "Show voices", then ▶ to preview each |
| Download MP3 | "Export this as audio" (P1) |

## Sources

- Speechify help center: import (7958983), auto-skip on computer (7973999, Aug 2026), iOS skip (15061357), Mac app (15097216, 8715689), miniplayer (15097417), offline on Mac (15097414), click-to-jump (15039232), shortcuts (4865153), dyslexic font (9537289), MP3 export (8724093, 9555732), dictionary (14634465), highlights and notes (12802107) — https://help.speechify.com
- https://speechify.com/pricing/ · https://speechify.com/usage-limits/ · https://speechify.com/news/speechify-launches-new-mac-app-with-voice-typing-dictation/
- https://www.trustpilot.com/review/speechify.com · https://apps.apple.com/us/app/speechify-text-to-speech-voice/id1209815023
- https://techcrunch.com/2025/11/25/speechify-adds-voice-typing-and-voice-assistant-to-its-chrome-extension
- https://dev.to/techsifted/speechify-review-2026-the-best-text-to-speech-app-for-students-and-power-readers-142e · https://www.roborhythms.com/speechify-review-2026/
- https://oae.stanford.edu/accessibility-resources/speechify-tips-tricks
