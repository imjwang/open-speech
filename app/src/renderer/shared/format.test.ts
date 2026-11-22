import { describe, expect, it } from 'vitest'

import {
  formatClock,
  formatDuration,
  formatPercent,
  formatSpeed,
  formatTimeLeft,
  formatUsd,
  voiceDetail,
  voiceTitle,
} from './format'

describe('voiceTitle', () => {
  it('drops the parenthesised detail', () => {
    expect(voiceTitle('Heart (Kokoro, American English)')).toBe('Heart')
    expect(voiceTitle('Alloy')).toBe('Alloy')
  })
})

describe('voiceDetail', () => {
  it('local Kokoro voice', () => {
    expect(voiceDetail({ name: 'Heart (Kokoro, American English)', provider: 'kokoro', local: true })).toBe(
      'Kokoro · American English · free, offline',
    )
  })

  it('adds the provider label when the name lacks it', () => {
    expect(voiceDetail({ name: 'Emma (British English)', provider: 'kokoro', local: true })).toBe('Kokoro · British English · free, offline')
  })

  it('cloud voices', () => {
    expect(voiceDetail({ name: 'Alloy', provider: 'openai', local: false })).toBe('OpenAI · cloud')
    expect(voiceDetail({ name: 'Alloy (OpenAI)', provider: 'openai' })).toBe('OpenAI · cloud')
    expect(voiceDetail({ name: 'Kore (Gemini, multilingual)', provider: 'gemini' })).toBe('Gemini · multilingual · cloud')
  })

  it('falls back to the raw provider id', () => {
    expect(voiceDetail({ name: 'X', provider: 'acme' })).toBe('acme · cloud')
  })
})

describe('durations', () => {
  it('formatDuration', () => {
    expect(formatDuration(3725)).toBe('1h 2m')
    expect(formatDuration(125)).toBe('2m')
    expect(formatDuration(42)).toBe('42s')
    expect(formatDuration(0)).toBe('0s')
    expect(formatDuration(null)).toBe('')
    expect(formatDuration(-1)).toBe('')
    expect(formatDuration(Number.NaN)).toBe('')
  })

  it('formatTimeLeft', () => {
    expect(formatTimeLeft(600)).toBe('10m left')
    expect(formatTimeLeft(undefined)).toBe('')
  })

  it('formatClock', () => {
    expect(formatClock(65)).toBe('1:05')
    expect(formatClock(0)).toBe('0:00')
    expect(formatClock(3600)).toBe('60:00')
    expect(formatClock(null)).toBe('')
  })
})

describe('formatSpeed', () => {
  it('always shows one or two decimals', () => {
    expect(formatSpeed(1)).toBe('1.0x')
    expect(formatSpeed(1.5)).toBe('1.5x')
    expect(formatSpeed(1.25)).toBe('1.25x')
    expect(formatSpeed(null)).toBe('1.0x')
  })
})

describe('formatUsd', () => {
  it('formats decimal strings and numbers as an estimate', () => {
    expect(formatUsd('3.2')).toBe('$3.20')
    expect(formatUsd(0)).toBe('$0.00')
    expect(formatUsd(0.004)).toBe('<$0.01')
    expect(formatUsd(null)).toBe('$0.00')
    expect(formatUsd('')).toBe('$0.00')
    expect(formatUsd('n/a')).toBe('$n/a')
  })
})

describe('formatPercent', () => {
  it('clamps and rounds', () => {
    expect(formatPercent(0.456)).toBe('46%')
    expect(formatPercent(1.5)).toBe('100%')
    expect(formatPercent(-0.2)).toBe('0%')
    expect(formatPercent(null)).toBe('')
  })
})
