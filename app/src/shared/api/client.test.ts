/** The hand-written parts of the API client: multipart uploads the generator can't describe. */
import { describe, expect, it } from 'vitest'

import { createEngineClient, uploadClone } from './client'

/** A client whose requests are recorded and answered with `answer` (JSON). */
function recordingClient(answer: unknown) {
  const calls: Array<{ method: string; path: string; body: FormData }> = []
  const fetchImpl = async (input: Request | string | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    calls.push({ method: req.method, path: new URL(req.url).pathname, body: await req.formData() })
    return new Response(JSON.stringify(answer), { status: 202, headers: { 'Content-Type': 'application/json' } })
  }
  const client = createEngineClient({ baseUrl: 'http://127.0.0.1:9999', token: 't'.repeat(32) }, fetchImpl as typeof fetch)
  return { client, calls }
}

describe('uploading a voice to clone', () => {
  it('is multipart with the recording, consent and the conversation it answers in', async () => {
    const { client, calls } = recordingClient({ job_id: 'job_1', slug: 'my-voice', voice_id: 'qwen3:clone:my-voice' })
    const wav = new Blob([new Uint8Array(44 + 48 * 4)], { type: 'audio/wav' })
    const res = await uploadClone(client, wav, 'recording.wav', {
      name: 'My voice',
      language: 'en',
      passageId: 'en-1',
      consent: true,
      source: 'recording',
      keep: false,
      sessionId: 's9',
      conversationId: 'conv',
    })
    expect(res.job_id).toBe('job_1')
    expect(calls[0]?.method).toBe('POST')
    expect(calls[0]?.path).toBe('/api/v1/voices/clones')
    const form = calls[0]!.body
    expect(form.get('consent')).toBe('true')
    expect(form.get('keep')).toBe('false')
    expect(form.get('source')).toBe('recording')
    expect(form.get('passage_id')).toBe('en-1')
    expect(form.get('session_id')).toBe('s9')
    expect(form.get('conversation_id')).toBe('conv')
    expect(form.get('name')).toBe('My voice')
    const file = form.get('file') as File
    expect(file.name).toBe('recording.wav')
    expect(file.size).toBe(44 + 48 * 4)
  })

  it('leaves out what is not given', async () => {
    const { client, calls } = recordingClient({ job_id: 'j', slug: 's', voice_id: 'v' })
    await uploadClone(client, new Blob([new Uint8Array(8)]), 'a.wav', { consent: true, source: 'file', keep: true })
    const form = calls[0]!.body
    expect(form.has('passage_id')).toBe(false)
    expect(form.has('session_id')).toBe(false)
    expect(form.get('keep')).toBe('true')
  })
})
