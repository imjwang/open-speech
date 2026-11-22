import { KeyRound, Loader2 } from 'lucide-react'
import { useState, type FormEvent } from 'react'

import type { KeyTestResult } from '@shared/api/client'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { detectKeyProvider } from '../dropped'

const PROVIDER_NAMES = { anthropic: 'Anthropic', openai: 'OpenAI', gemini: 'Gemini' } as const

/**
 * "Paste a key" (09 § Rows, FR-SETUP-2): a password field; the provider is detected from the prefix
 * and the engine tests the key before saving. The key is cleared from the field right after sending
 * and is never shown again.
 */
export function KeyEntry({
  onSave,
  onClose,
  autoFocus = true,
  compact = false,
}: {
  onSave(key: string): Promise<KeyTestResult | null>
  onClose?: () => void
  autoFocus?: boolean
  compact?: boolean
}) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<KeyTestResult | null>(null)
  const provider = detectKeyProvider(value)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    const key = value.trim()
    if (!key || busy) return
    setBusy(true)
    setValue('')
    try {
      setResult(await onSave(key))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      onSubmit={submit}
      className={compact ? 'flex flex-col gap-2' : 'flex flex-col gap-2 border-t px-4 py-3'}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && onClose) {
          e.preventDefault()
          e.stopPropagation()
          onClose()
        }
      }}
    >
      <label htmlFor="open-speech-key" className="flex items-center gap-2 text-sm font-medium">
        <KeyRound className="size-4 text-muted-foreground" aria-hidden="true" />
        Paste a key
      </label>
      <div className="flex gap-2">
        <Input
          id="open-speech-key"
          type="password"
          autoComplete="off"
          spellCheck={false}
          autoFocus={autoFocus}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="sk-ant-…, sk-… or AIza…"
          aria-describedby="open-speech-key-help"
        />
        <Button type="submit" disabled={!value.trim() || busy}>
          {busy ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}
          {busy ? 'Testing' : 'Save'}
        </Button>
      </div>
      <p id="open-speech-key-help" className="text-xs text-muted-foreground" aria-live="polite">
        {busy
          ? 'Testing the key with a small call…'
          : result
            ? result.ok
              ? `Saved: ${result.provider}. ${result.detail}`
              : `Not saved: ${result.detail}`
            : provider
              ? `Looks like an ${PROVIDER_NAMES[provider]} key. It is tested before saving and kept in your keychain.`
              : 'Anthropic, OpenAI or Gemini. It is tested before saving and kept in your keychain.'}
      </p>
    </form>
  )
}
