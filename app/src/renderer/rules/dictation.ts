/**
 * Dictation for the rules editor: press the microphone, speak, and the words are typed where the caret is.
 * It uses the same speech-to-text as the command bar (`transcribe_only`, so nothing is sent to the assistant)
 * and stops by itself after a pause, or when the button is pressed again.
 */
import { useEffect, useRef, useState } from 'react'

import { BrowserMic } from '@/audio/mic'
import { EnergyVad, toDb } from '@/audio/vad'
import type { LiveEngine } from '@/engine'
import { newSessionId } from '@/ids'
import { usePrefs } from '@/prefs'

export interface Dictation {
  /** Speech input exists in this build and is ready. */
  available: boolean
  active: boolean
  /** 0..1, for the orb. */
  level: number
  /** What has been heard so far in this utterance. */
  transcript: string
  toggle(): void
}

export function useDictation(engine: LiveEngine, onText: (text: string) => void): Dictation {
  const available = usePrefs((s) => s.capabilities?.speech_input.available === true)
  const [active, setActive] = useState(false)
  const [level, setLevel] = useState(0)
  const [transcript, setTranscript] = useState('')
  const session = useRef<string | null>(null)
  const mic = useRef<BrowserMic | null>(null)
  const deliver = useRef(onText)
  deliver.current = onText

  const stop = (submit: boolean) => {
    const id = session.current
    mic.current?.stop()
    setActive(false)
    setLevel(0)
    if (id) engine.socket.send({ type: 'mic.stop', session_id: id, submit, attachments: [] })
    if (!submit) {
      session.current = null
      setTranscript('')
    }
  }

  const start = () => {
    const id = newSessionId()
    const vad = new EnergyVad()
    vad.endOfSpeechMs = Math.max(1200, usePrefs.getState().settings?.voice_input?.end_of_speech_ms ?? 800) // prose has pauses
    const ok = engine.socket.send({ type: 'mic.start', session_id: id, mode: 'auto', context: {}, transcribe_only: true })
    if (!ok) return
    session.current = id
    setActive(true)
    setTranscript('')
    mic.current ??= new BrowserMic()
    void mic.current.start({
      onPcm: (pcm) => {
        if (session.current === id) engine.socket.sendAudio(pcm)
      },
      onLevel: (rms, ms) => {
        if (session.current !== id) return
        setLevel(Math.max(0, Math.min(1, (toDb(rms) + 60) / 48)))
        if (vad.push(rms, ms) === 'speech-end') stop(true)
      },
      onError: () => stop(false),
    })
  }

  useEffect(() => {
    const offs = [
      engine.socket.on('stt.partial', (msg) => {
        if (msg.session_id === session.current) setTranscript(msg.text)
      }),
      engine.socket.on('stt.final', (msg) => {
        if (msg.session_id !== session.current) return
        session.current = null
        mic.current?.stop()
        setActive(false)
        setLevel(0)
        setTranscript('')
        if (msg.text.trim()) deliver.current(msg.text.trim())
      }),
    ]
    return () => {
      offs.forEach((off) => off())
      mic.current?.stop()
    }
  }, [engine])

  return { available, active, level, transcript, toggle: () => (active ? stop(true) : start()) }
}
