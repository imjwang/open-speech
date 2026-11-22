import { Check, Play } from 'lucide-react'
import { useEffect, useState } from 'react'

import { unwrap, type KeysState, type Voice } from '@shared/api/client'

import { Button } from '@/components/ui/button'
import { Kbd } from '@/components/ui/kbd'
import { requireClient, useEngine } from '@/engine'
import { cn } from '@/lib/utils'
import type { BarActions } from '../actions'
import type { BarController } from '../controller'
import { KeyEntry } from './KeyEntry'

type Step = 'keys' | 'voice' | 'cleanup' | 'done'
const STEPS: Step[] = ['keys', 'voice', 'cleanup', 'done']
const CLEANUP_PRESETS = ['Headers', 'Footers', 'Page numbers', 'Citations']

/** Kokoro first, then one available voice per keyed provider, then more local voices (09 § Onboarding). */
export function pickSampleVoices(voices: Voice[], defaultVoice: string | null): Voice[] {
  const available = voices.filter((v) => v.available !== false)
  const picks: Voice[] = []
  const add = (v: Voice | undefined) => {
    if (v && !picks.some((p) => p.id === v.id)) picks.push(v)
  }
  add(available.find((v) => v.id === defaultVoice) ?? available.find((v) => v.provider === 'kokoro'))
  for (const provider of ['openai', 'gemini']) add(available.find((v) => v.provider === provider))
  for (const v of available) if (picks.length < 3) add(v)
  return picks.slice(0, 3)
}

const WORDS: Record<string, number> = { one: 0, '1': 0, first: 0, two: 1, '2': 1, second: 1, three: 2, '3': 2, third: 2 }

export function matchVoiceAnswer(step: Step, transcript: string): string | null {
  const t = transcript.toLowerCase()
  if (step === 'keys' && /\b(skip|later|no key|none)\b/.test(t)) return 'skip'
  if (step === 'voice') {
    for (const [word, index] of Object.entries(WORDS)) if (new RegExp(`\\b${word}\\b`).test(t)) return `voice:${index}`
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
  onFinished,
}: {
  controller: BarController
  actions: BarActions
  openBarShortcut: string
  onFinished(): void
}) {
  const engine = useEngine()
  const [step, setStep] = useState<Step>('keys')
  const [keys, setKeys] = useState<KeysState | null>(null)
  const [voices, setVoices] = useState<Voice[]>([])
  const [chosenVoice, setChosenVoice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

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

  const samples = pickSampleVoices(voices, controller.store.getState().settings?.defaults?.voice ?? null)

  const finish = async () => {
    try {
      await unwrap(requireClient(engine).PATCH('/api/v1/settings', { body: { onboarding_completed: true } }))
    } catch {
      // Not fatal: onboarding shows again next time.
    }
    onFinished()
  }

  const chooseVoice = async (index: number) => {
    const voice = samples[index]
    if (!voice) return
    setChosenVoice(voice.id)
    await actions.setVoice(voice.id, 'default')
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
      else if (answer === 'yes' || answer === 'smart' || answer === 'no') void cleanup(answer)
      else if (answer === 'finish' || answer === 'skip-all') {
        void finish()
        return 'stop'
      }
      return 'listen-again'
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
            If you have an Anthropic, OpenAI or Gemini key, paste it now. Or say “skip” to use the free local voice.
          </p>
          {keys && (
            <ul className="flex flex-wrap gap-2 text-xs" aria-label="Keys">
              {(['anthropic', 'openai', 'gemini'] as const).map((p) => (
                <li key={p} className={cn('rounded-md border px-2 py-0.5', keys[p] === 'configured' && 'border-listen')}>
                  {p}: {keys[p]}
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
            <Button onClick={() => setStep('voice')}>{anyKey ? 'Continue' : 'Skip: use the local voice'}</Button>
          </div>
        </div>
      )}

      {step === 'voice' && (
        <div className="space-y-3">
          <p className="text-sm">Pick a voice by ear. Say “one”, “two” or “three”.</p>
          <p className="text-xs text-muted-foreground">Voices are AI-generated.</p>
          <ol className="space-y-1.5">
            {samples.map((v, i) => (
              <li key={v.id} className="flex items-center gap-2 rounded-md border px-2 py-1.5">
                <Kbd>{i + 1}</Kbd>
                <Button variant="ghost" size="icon-sm" aria-label={`Play a sample of ${v.name}`} onClick={() => void actions.previewVoice(v.id)}>
                  <Play aria-hidden="true" />
                </Button>
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate text-sm">{v.name}</span>
                  <span className="text-xs text-muted-foreground">
                    {v.provider}
                    {v.local ? ' · local, free' : ''}
                  </span>
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
            Should I skip headers, footers, page numbers and citations? “Smart” also uses AI Enhanced cleanup (uses your Anthropic key).
          </p>
          <div className="flex gap-2">
            <Button onClick={() => void cleanup('yes')}>Yes</Button>
            <Button variant="outline" onClick={() => void cleanup('smart')}>
              Smart
            </Button>
            <Button variant="outline" onClick={() => void cleanup('no')}>
              No
            </Button>
          </div>
        </div>
      )}

      {step === 'done' && (
        <div className="space-y-3">
          <p className="text-sm">
            Press <Kbd>{openBarShortcut}</Kbd> any time and just tell me what to read.
          </p>
          <Button autoFocus onClick={() => void finish()}>
            Start
          </Button>
        </div>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </section>
  )
}
