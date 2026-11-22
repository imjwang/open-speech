import { Check, Play } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useStore } from 'zustand'

import { unwrap, type KeysState, type Voice } from '@shared/api/client'

import { Button } from '@/components/ui/button'
import { Kbd } from '@/components/ui/kbd'
import { requireClient, useEngine } from '@/engine'
import { voiceDetail, voiceTitle } from '@/format'
import type { BarActions } from '../actions'
import type { BarController } from '../controller'
import { KeyEntry } from './KeyEntry'

type Step = 'keys' | 'voice' | 'cleanup' | 'done'
const STEPS: Step[] = ['keys', 'voice', 'cleanup', 'done']
const CLEANUP_PRESETS = ['Headers', 'Footers', 'Page numbers', 'Citations']
const PROVIDERS = [
  { id: 'anthropic', name: 'Anthropic', use: 'smart cleanup, image descriptions, the assistant' },
  { id: 'openai', name: 'OpenAI', use: 'cloud voices, talking to the bar' },
  { id: 'gemini', name: 'Gemini', use: 'cloud voices, talking to the bar' },
] as const

/** What each step says aloud (09 § Onboarding: spoken by Kokoro, so it works before any key exists). */
export function stepPrompt(step: Step, o: { canListen: boolean; hasClaude: boolean; shortcut: string }): string {
  switch (step) {
    case 'keys':
      return 'Welcome to Open Speech. If you have an Anthropic, OpenAI or Gemini key, paste it now. Or continue without one: the local voice is free.'
    case 'voice':
      return o.canListen ? 'Pick a voice by ear. Say one, two or three.' : 'Pick a voice by ear. Press play to hear each one.'
    case 'cleanup':
      return o.hasClaude
        ? 'Should I skip headers, footers, page numbers and citations? Say yes, smart for AI cleanup, or no.'
        : 'Should I skip headers, footers, page numbers and citations? Yes or no?'
    case 'done':
      return o.canListen
        ? `All set. Press ${o.shortcut} any time and tell me what to read.`
        : `All set. Press ${o.shortcut} any time and type what to read, or copy some text and press Option-Shift-R.`
  }
}

/** Kokoro first, then one available voice per keyed provider, then more local voices (09 § Onboarding). */
export function pickSampleVoices(voices: Voice[], defaultVoice: string | null): Voice[] {
  const available = voices.filter((v) => v.available !== false)
  const picks: Voice[] = []
  const add = (v: Voice | undefined) => {
    if (v && !picks.some((p) => p.id === v.id)) picks.push(v)
  }
  add(available.find((v) => v.id === defaultVoice) ?? available.find((v) => v.provider === 'kokoro'))
  for (const provider of ['openai', 'gemini']) add(available.find((v) => v.provider === provider))
  // Then contrast, so choosing by ear means something: a different accent/gender each (Kokoro ids
  // start with a/b = American/British, f/m = female/male).
  const kind = (v: Voice) => (v.provider === 'kokoro' ? (v.id.split(':').pop() ?? '').slice(0, 2) : v.provider)
  for (const preferred of ['am_michael', 'bf_emma', 'bm_george'])
    if (picks.length < 3 && !picks.some((p) => kind(p) === preferred.slice(0, 2)))
      add(available.find((v) => v.id.endsWith(`:${preferred}`)))
  for (const v of available) if (picks.length < 3 && !picks.some((p) => kind(p) === kind(v))) add(v)
  for (const v of available) if (picks.length < 3) add(v)
  return picks.slice(0, 3)
}

// Ordinals before cardinals: "the second one" means two, not one.
const WORDS: Array<[string, number]> = [
  ['first', 0], ['second', 1], ['third', 2],
  ['one', 0], ['1', 0], ['two', 1], ['2', 1], ['three', 2], ['3', 2],
]

export function matchVoiceAnswer(step: Step, transcript: string): string | null {
  const t = transcript.toLowerCase()
  if (/\bskip (setup|all|onboarding|everything)\b/.test(t)) return 'skip-all'
  if (step === 'keys' && /\b(skip|later|no key|none|continue|without)\b/.test(t)) return 'skip'
  if (step === 'voice') {
    for (const [word, index] of WORDS) if (new RegExp(`\\b${word}\\b`).test(t)) return `voice:${index}`
  }
  if (step === 'cleanup') {
    if (/\b(smart|ai|enhanced)\b/.test(t)) return 'smart'
    if (/\b(no|nope|don't)\b/.test(t)) return 'no'
    if (/\b(yes|yeah|yep|sure)\b/.test(t)) return 'yes'
  }
  if (step === 'done' && t.trim()) return 'finish'
  if (/\bskip (setup|all|onboarding)\b/.test(t)) return 'skip-all'
  return null
}

/**
 * First-run onboarding inside the bar (09 § Onboarding, FR-SETUP-4): ≤ 3 steps, skippable, works with
 * no keys. Answers can be spoken ("skip", "one/two/three", "yes/smart/no"): the bar keeps listening and
 * the transcript is matched here instead of being sent to the assistant.
 */
export function Onboarding({
  controller,
  actions,
  openBarShortcut,
  openBarSpoken,
  onFinished,
}: {
  controller: BarController
  actions: BarActions
  openBarShortcut: string
  openBarSpoken: string
  onFinished(): void
}) {
  const engine = useEngine()
  const [step, setStep] = useState<Step>('keys')
  const [keys, setKeys] = useState<KeysState | null>(null)
  const [voices, setVoices] = useState<Voice[]>([])
  const [chosenVoice, setChosenVoice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const canListen = useStore(controller.store, (s) => s.canListen)

  useEffect(() => {
    const client = engine.client
    if (!client) return
    void unwrap(client.GET('/api/v1/keys')).then(setKeys, () => undefined)
  }, [engine, step])

  useEffect(() => {
    if (step !== 'voice') return
    const client = engine.client
    if (!client) return
    void unwrap(client.GET('/api/v1/voices', { params: { query: {} } })).then(
      (list) => setVoices(list.items),
      () => setError('Could not load voices.'),
    )
  }, [engine, step])

  const hasClaude = keys?.anthropic === 'configured'

  // Each step is read aloud once; the mic is off while it speaks so it can't answer itself.
  useEffect(() => {
    const client = engine.client
    if (!client || (step === 'keys' && !keys)) return
    const text = stepPrompt(step, { canListen, hasClaude, shortcut: openBarSpoken })
    // A voice dialogue: after each prompt, listen for the answer (when speech input is set up). The last
    // step needs no answer: once it has been said, the bar is ready for the first real command.
    const said = controller.withMicPaused(() => unwrap(client.POST('/api/v1/assistant/say', { body: { text } })), {
      listenAfter: step !== 'done',
    })
    if (step === 'done') void said.then(() => finish({ listen: true }))
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per step
  }, [step, keys === null])

  const samples = pickSampleVoices(voices, controller.store.getState().settings?.defaults?.voice ?? null)

  const finishing = useRef(false)
  const finish = async (opts: { listen?: boolean } = {}) => {
    if (finishing.current) return
    finishing.current = true
    try {
      await unwrap(requireClient(engine).PATCH('/api/v1/settings', { body: { onboarding_completed: true } }))
    } catch {
      // Not fatal: onboarding shows again next time.
    }
    controller.setInterceptor(null)
    onFinished()
    const s = controller.store.getState()
    if (opts.listen && s.open && s.canListen && s.mode === 'typing' && !s.text) controller.startListening()
  }

  const chooseVoice = async (index: number) => {
    const voice = samples[index]
    if (!voice) return
    setChosenVoice(voice.id)
    await actions.setVoice(voice.id, 'default', { quiet: true })
    setStep('cleanup')
  }

  const cleanup = async (answer: 'yes' | 'smart' | 'no') => {
    const enabled = answer !== 'no'
    try {
      await unwrap(
        requireClient(engine).PUT('/api/v1/rules/presets', {
          body: {
            mode: answer === 'smart' ? 'ai_enhanced' : 'classic',
            presets: Object.fromEntries(CLEANUP_PRESETS.map((p) => [p, enabled])),
          },
        }),
      )
    } catch (err) {
      setError((err as Error).message)
    }
    setStep('done')
  }

  // Spoken answers: matched locally, never sent to the assistant.
  useEffect(() => {
    controller.setInterceptor((transcript) => {
      const answer = matchVoiceAnswer(step, transcript)
      if (answer === 'skip') setStep('voice')
      else if (answer?.startsWith('voice:')) void chooseVoice(Number(answer.slice(6)))
      else if (answer === 'smart' && !hasClaude) void cleanup('yes')
      else if (answer === 'yes' || answer === 'smart' || answer === 'no') void cleanup(answer)
      else if (answer === 'finish' || answer === 'skip-all') void finish()
      // Unrecognised: keep listening. Recognised: the next step's prompt speaks, then listens itself.
      return answer === null ? 'listen-again' : 'stop'
    })
    return () => controller.setInterceptor(null)
  })

  const index = STEPS.indexOf(step)
  const anyKey = keys && Object.values(keys).some((s) => s === 'configured')

  return (
    <section aria-labelledby="onboarding-title" className="space-y-3 px-4 pb-4" data-testid="onboarding">
      <div className="flex items-center justify-between">
        <h2 id="onboarding-title" className="text-sm font-semibold">
          {step === 'done' ? 'All set' : `Set up · step ${index + 1} of 3`}
        </h2>
        {step !== 'done' && (
          <Button variant="link" size="sm" onClick={() => void finish()}>
            Skip setup
          </Button>
        )}
      </div>

      {step === 'keys' && (
        <div className="space-y-3">
          <p className="text-sm">
            Keys are optional. Without one, Open Speech reads with a free voice that runs on this Mac.
          </p>
          {keys && (
            <ul className="space-y-1 text-xs" aria-label="Keys">
              {PROVIDERS.map((p) => (
                <li key={p.id} className="flex items-center gap-2">
                  {keys[p.id] === 'configured' ? (
                    <Check aria-hidden="true" className="size-3.5 text-listen" />
                  ) : (
                    <span aria-hidden="true" className="size-3.5 rounded-full border" />
                  )}
                  <span className="font-medium">{p.name}</span>
                  <span className="text-muted-foreground">
                    {keys[p.id] === 'configured' ? 'added' : keys[p.id] === 'invalid' ? 'key rejected' : p.use}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <KeyEntry
            compact
            autoFocus={false}
            onSave={async (key) => {
              const r = await actions.saveKey(key)
              const client = engine.client
              if (client) void unwrap(client.GET('/api/v1/keys')).then(setKeys, () => undefined)
              return r
            }}
          />
          <div className="flex gap-2">
            <Button onClick={() => setStep('voice')}>{anyKey ? 'Continue' : 'Continue without a key'}</Button>
          </div>
        </div>
      )}

      {step === 'voice' && (
        <div className="space-y-3">
          <p className="text-sm">
            Pick a voice by ear.{canListen ? ' Say “one”, “two” or “three”.' : ''}{' '}
            <span className="text-muted-foreground">Voices are AI-generated.</span>
          </p>
          <ol className="space-y-1.5">
            {samples.map((v, i) => (
              <li key={v.id} className="flex items-center gap-2 rounded-md border px-2 py-1.5">
                <Kbd>{i + 1}</Kbd>
                <Button variant="ghost" size="icon-sm" aria-label={`Play a sample of ${v.name}`} onClick={() => void actions.previewVoice(v.id)}>
                  <Play aria-hidden="true" />
                </Button>
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate text-sm">{voiceTitle(v.name)}</span>
                  <span className="text-xs text-muted-foreground">{voiceDetail(v)}</span>
                </span>
                <Button size="sm" variant={chosenVoice === v.id ? 'default' : 'outline'} onClick={() => void chooseVoice(i)}>
                  {chosenVoice === v.id ? <Check aria-hidden="true" /> : null}
                  Use
                </Button>
              </li>
            ))}
            {samples.length === 0 && <li className="text-sm text-muted-foreground">Loading voices…</li>}
          </ol>
          <Button variant="outline" onClick={() => setStep('cleanup')}>
            Keep the current voice
          </Button>
        </div>
      )}

      {step === 'cleanup' && (
        <div className="space-y-3">
          <p className="text-sm">
            Should I skip headers, footers, page numbers and citations?
            {hasClaude && <span className="text-muted-foreground"> “Smart” also lets Claude catch what rules miss.</span>}
          </p>
          <div className="flex gap-2">
            <Button onClick={() => void cleanup('yes')}>Yes</Button>
            {hasClaude && (
              <Button variant="outline" onClick={() => void cleanup('smart')}>
                Smart
              </Button>
            )}
            <Button variant="outline" onClick={() => void cleanup('no')}>
              No
            </Button>
          </div>
        </div>
      )}

      {step === 'done' && (
        <div className="space-y-3">
          <p className="text-sm">
            Press <Kbd>{openBarShortcut}</Kbd> any time and {canListen ? 'tell me' : 'type'} what to read.
          </p>
          {!canListen && (
            <p className="text-xs text-muted-foreground">
              Or copy text anywhere and press <Kbd>⌥⇧R</Kbd> to hear it.
            </p>
          )}
          <Button autoFocus onClick={() => void finish({ listen: true })}>
            Start
          </Button>
        </div>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </section>
  )
}
