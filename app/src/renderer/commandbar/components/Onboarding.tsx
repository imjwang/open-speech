import { BookOpenText, Check, ListX, Play, Sparkles, X } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useStore } from 'zustand'

import { unwrap, type Voice } from '@shared/api/client'
import type { Lang } from '@shared/i18n'

import { IconButton } from '@/components/ui/icon-button'
import { Kbd } from '@/components/ui/kbd'
import { requireClient, useEngine } from '@/engine'
import { voiceTitle } from '@/format'
import { useLanguage, useT, type MessageKey } from '@/i18n'
import { usePrefs } from '@/prefs'
import { cn } from '@/lib/utils'
import type { BarActions } from '../actions'
import type { BarController } from '../controller'

type Step = 'voice' | 'cleanup' | 'done'
const STEPS: Step[] = ['voice', 'cleanup', 'done']
const CLEANUP_PRESETS = ['Headers', 'Footers', 'Page numbers', 'Citations']

/** The message each step shows and says aloud (09 § Onboarding: spoken by the local voice). */
export function stepPrompt(step: Step, o: { canListen: boolean; smart: boolean; voices?: number }): MessageKey {
  switch (step) {
    case 'voice':
      // Only as many numbers as there are voices to pick from.
      if (o.canListen) return o.voices === 2 ? 'onboard.voice.two' : 'onboard.voice'
      return 'onboard.voice.no_mic'
    case 'cleanup':
      return o.smart ? 'onboard.cleanup.smart' : 'onboard.cleanup'
    case 'done':
      return o.canListen ? 'onboard.done' : 'onboard.done.no_mic'
  }
}

function speaks(v: Voice, lang: Lang): boolean {
  return !v.languages?.length || v.languages.some((l) => l.toLowerCase().split('-')[0] === lang)
}

/**
 * Three voices to choose between by ear, in the user's language: the current default first, then contrast
 * (a different gender or accent), local voices before cloud ones.
 */
export function pickSampleVoices(voices: Voice[], defaultVoice: string | null, lang: Lang = 'en'): Voice[] {
  const available = voices.filter((v) => v.available !== false)
  const inLanguage = available.filter((v) => speaks(v, lang))
  const pool = inLanguage.length ? [...inLanguage.filter((v) => v.languages?.length), ...inLanguage.filter((v) => !v.languages?.length)] : available
  const picks: Voice[] = []
  const add = (v: Voice | undefined) => {
    if (v && !picks.some((p) => p.id === v.id)) picks.push(v)
  }
  // Kokoro ids start with accent + gender (af, am, bf, bm, ef, em): one of each kind before a second of any.
  const kind = (v: Voice) => (v.provider === 'kokoro' ? (v.id.split(':').pop() ?? '').slice(0, 2) : v.provider)
  add(pool.find((v) => v.id === defaultVoice) ?? pool[0])
  for (const preferred of ['am_michael', 'bf_emma', 'em_alex', 'ef_dora'])
    if (picks.length < 3 && !picks.some((p) => kind(p) === preferred.slice(0, 2))) add(pool.find((v) => v.id.endsWith(`:${preferred}`)))
  for (const v of pool) if (picks.length < 3 && !picks.some((p) => kind(p) === kind(v))) add(v)
  for (const v of pool) if (picks.length < 3) add(v)
  return picks.slice(0, 3)
}

// Ordinals before cardinals: "the second one" means two, not one.
const NUMBERS: Array<[RegExp, number]> = [
  [/\b(first|primer[oa]?)\b/, 0],
  [/\b(second|segund[oa])\b/, 1],
  [/\b(third|tercer[oa]?)\b/, 2],
  [/\b(one|uno|una|1)\b/, 0],
  [/\b(two|dos|2)\b/, 1],
  [/\b(three|tres|3)\b/, 2],
]

/** What a spoken answer means at this step, in either language (matched here, never sent to the assistant). */
export function matchVoiceAnswer(step: Step, transcript: string): string | null {
  const t = transcript
    .toLowerCase()
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
  if (/\b(skip|saltar?|salta|omitir?|omite) (the |la |el )?(setup|all|onboarding|everything|configuracion|todo)\b/.test(t)) return 'skip-all'
  if (step === 'voice') {
    // "Keep this one" is about the voice just heard, not voice number one.
    if (/\b(this|that) one\b|\b(esta|esa|la misma)\b/.test(t)) return 'keep'
    for (const [word, index] of NUMBERS) if (word.test(t)) return `voice:${index}`
    if (/\b(keep|same|skip|next|siguiente|sigue)\b/.test(t)) return 'keep'
  }
  if (step === 'cleanup') {
    if (/\b(smart|ai|enhanced|inteligente|ia|lista)\b/.test(t)) return 'smart'
    if (/\b(no|nope|don't|nada)\b/.test(t)) return 'no'
    // "see" is how a recognizer often hears a lone "sí".
    if (/\b(yes|yeah|yep|sure|si|see|claro|vale|de acuerdo)\b/.test(t)) return 'yes'
  }
  if (step === 'done' && t.trim()) return 'finish'
  return null
}

/**
 * First-run setup inside the bar (09 § Onboarding, FR-SETUP-4): two questions, spoken aloud and answered by
 * voice, a key, or a press. Nothing to read beyond the question itself, nothing to sign in to, skippable.
 */
export function Onboarding({ controller, actions, orb, heard, openBarShortcut, openBarSpoken, onFinished }: {
  controller: BarController
  actions: BarActions
  /** The voice orb, drawn beside the question. */
  orb: ReactNode
  /** What is being heard right now (the answer, as it is spoken). */
  heard: string
  openBarShortcut: string
  openBarSpoken: string
  onFinished(): void
}) {
  const t = useT()
  const lang = useLanguage()
  const engine = useEngine()
  const [step, setStep] = useState<Step>('voice')
  // null until the list has arrived: the first question is only asked once we know what to offer.
  const [voices, setVoices] = useState<Voice[] | null>(null)
  const [chosen, setChosen] = useState<string | null>(null)
  const canListen = useStore(controller.store, (s) => s.canListen === true)
  const smart = usePrefs((s) => s.capabilities?.ai_cleanup === true)
  const defaultVoice = usePrefs((s) => s.settings?.defaults?.voice ?? null)

  useEffect(() => {
    const client = engine.client
    if (!client) return
    void unwrap(client.GET('/api/v1/voices', { params: { query: {} } })).then(
      (list) => setVoices(list.items),
      () => setVoices([]),
    )
  }, [engine])

  const samples = pickSampleVoices(voices ?? [], defaultVoice, lang)
  const promptOptions = { canListen, smart, voices: samples.length }
  const prompt = t(stepPrompt(step, promptOptions), { shortcut: openBarShortcut })
  // With one voice (or none) there is nothing to choose: go straight to the next question.
  const nothingToPick = voices !== null && samples.length < 2
  useEffect(() => {
    if (step === 'voice' && nothingToPick) setStep('cleanup')
  }, [step, nothingToPick])

  // Each step is read aloud once; the mic is off while it speaks so it can't answer itself.
  useEffect(() => {
    const client = engine.client
    if (!client) return
    if (step === 'voice' && (voices === null || nothingToPick)) return
    const text = t(stepPrompt(step, promptOptions), { shortcut: openBarSpoken })
    // A voice dialogue: after each prompt, listen for the answer. The last step needs no answer: once it
    // has been said, the bar is ready for the first real command.
    const said = controller.withMicPaused(() => unwrap(client.POST('/api/v1/assistant/say', { body: { text } })), {
      listenAfter: step !== 'done',
    })
    if (step === 'done') void said.then(() => finish({ listen: true }))
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per step (and when the language changes)
  }, [step, lang, voices === null])

  const finishing = useRef(false)
  const finish = async (opts: { listen?: boolean } = {}) => {
    if (finishing.current) return
    finishing.current = true
    try {
      await unwrap(requireClient(engine).PATCH('/api/v1/settings', { body: { onboarding_completed: true } }))
    } catch {
      // Not fatal: setup shows again next time.
    }
    controller.setInterceptor(null)
    onFinished()
    const s = controller.store.getState()
    if (opts.listen && s.open && s.canListen && s.mode === 'typing') controller.startListening()
  }

  const chooseVoice = async (index: number) => {
    const voice = samples[index]
    if (!voice) return
    setChosen(voice.id)
    await actions.setVoice(voice.id, 'default')
    setStep('cleanup')
  }

  const cleanup = async (answer: 'yes' | 'smart' | 'no') => {
    try {
      await unwrap(
        requireClient(engine).PUT('/api/v1/rules/presets', {
          body: {
            mode: answer === 'smart' && smart ? 'ai_enhanced' : 'classic',
            presets: Object.fromEntries(CLEANUP_PRESETS.map((p) => [p, answer !== 'no'])),
          },
        }),
      )
    } catch (err) {
      controller.notice('error', (err as Error).message)
    }
    setStep('done')
  }

  // Spoken answers: matched locally, never sent to the assistant.
  useEffect(() => {
    controller.setInterceptor((transcript) => {
      const answer = matchVoiceAnswer(step, transcript)
      if (answer?.startsWith('voice:')) void chooseVoice(Number(answer.slice(6)))
      else if (answer === 'keep') setStep('cleanup')
      else if (answer === 'yes' || answer === 'smart' || answer === 'no') void cleanup(answer)
      else if (answer === 'finish' || answer === 'skip-all') void finish()
      // Unrecognised: keep listening. Recognised: the next step's prompt speaks, then listens itself.
      return answer === null ? 'listen-again' : 'stop'
    })
    return () => controller.setInterceptor(null)
  })

  // Keys answer too: 1–3 pick a voice, Y / S / N answer the cleanup question.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const k = e.key.toLowerCase()
      if (step === 'voice' && ['1', '2', '3'].includes(k)) void chooseVoice(Number(k) - 1)
      else if (step === 'voice' && k === 'enter') setStep('cleanup')
      else if (step === 'cleanup' && (k === 'y' || k === 'enter')) void cleanup('yes')
      else if (step === 'cleanup' && k === 's' && smart) void cleanup('smart')
      else if (step === 'cleanup' && k === 'n') void cleanup('no')
      else if (step === 'done' && k === 'enter') void finish({ listen: true })
      else return
      e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const index = STEPS.indexOf(step)
  const tile = 'group flex flex-1 flex-col items-center gap-2 rounded-xl border bg-card px-2 py-3 text-center transition-colors'

  return (
    <section aria-labelledby="onboarding-prompt" className="px-4 pt-3 pb-4" data-testid="onboarding" data-step={step}>
      <div className="flex items-start gap-3">
        {orb}
        <div className="min-w-0 flex-1 py-0.5">
          <p id="onboarding-prompt" aria-live="polite" className="text-[15px] leading-snug">
            {prompt}
          </p>
          <p className="h-5 truncate text-sm text-muted-foreground" aria-live="polite">
            {heard}
          </p>
        </div>
        <ol aria-label={t('onboard.step', { n: Math.min(index + 1, 2), total: 2 })} className="mt-1.5 flex gap-1">
          {STEPS.slice(0, 2).map((s, i) => (
            <li key={s} className={cn('size-1.5 rounded-full', i <= index ? 'bg-primary' : 'bg-border')} />
          ))}
        </ol>
        {step !== 'done' && (
          <IconButton label={t('onboard.skip')} onClick={() => void finish()} className="-mt-1 size-7">
            <X className="size-4" />
          </IconButton>
        )}
      </div>

      {step === 'voice' && (
        <>
          <ol className="mt-3 flex gap-2">
            {samples.map((v, i) => {
              const name = voiceTitle(v.name)
              return (
                <li key={v.id} className={cn(tile, chosen === v.id && 'border-primary')}>
                  <button
                    type="button"
                    aria-label={t('row.preview', { name })}
                    onClick={() => void actions.previewVoice(v.id)}
                    className="relative grid size-14 place-items-center rounded-full bg-secondary text-secondary-foreground outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <Play aria-hidden="true" className="size-6 fill-current" />
                    <Kbd className="absolute -top-1 -right-1 bg-card">{i + 1}</Kbd>
                  </button>
                  <span className="max-w-full truncate text-sm font-medium">{name}</span>
                  <IconButton label={t('row.use_voice', { name })} onClick={() => void chooseVoice(i)} className="size-7 bg-primary text-primary-foreground hover:bg-primary/90 hover:text-primary-foreground">
                    <Check className="size-4" />
                  </IconButton>
                </li>
              )
            })}
            {samples.length === 0 && <li className="h-32 flex-1 animate-pulse rounded-xl bg-muted motion-reduce:animate-none" aria-hidden="true" />}
          </ol>
          <p className="mt-2 text-center text-xs text-muted-foreground">{t('voices.ai')}</p>
        </>
      )}

      {step === 'cleanup' && (
        <div className="mt-3 flex gap-2" role="group" aria-labelledby="onboarding-prompt">
          <button type="button" autoFocus className={cn(tile, 'hover:border-primary focus-visible:border-primary')} onClick={() => void cleanup('yes')}>
            <ListX aria-hidden="true" className="size-7 text-primary" />
            <span className="text-sm font-medium">{t('onboard.yes')}</span>
            <Kbd>Y</Kbd>
          </button>
          {smart && (
            <button type="button" className={cn(tile, 'hover:border-primary focus-visible:border-primary')} onClick={() => void cleanup('smart')}>
              <Sparkles aria-hidden="true" className="size-7 text-primary" />
              <span className="text-sm font-medium">{t('onboard.smart')}</span>
              <Kbd>S</Kbd>
            </button>
          )}
          <button type="button" className={cn(tile, 'hover:border-primary focus-visible:border-primary')} onClick={() => void cleanup('no')}>
            <BookOpenText aria-hidden="true" className="size-7 text-muted-foreground" />
            <span className="text-sm font-medium">{t('onboard.no')}</span>
            <Kbd>N</Kbd>
          </button>
        </div>
      )}

      {step === 'done' && (
        <div className="mt-3 flex items-center justify-center gap-3">
          <Kbd className="h-8 px-3 text-sm">{openBarShortcut}</Kbd>
          <IconButton label={t('onboard.start')} autoFocus onClick={() => void finish({ listen: true })} className="size-10 bg-primary text-primary-foreground hover:bg-primary/90 hover:text-primary-foreground">
            <Check className="size-5" />
          </IconButton>
        </div>
      )}
    </section>
  )
}
