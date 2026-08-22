import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'

import { getElevenLabsVoices, getHermesConfigSchema, saveHermesConfig } from '@/hermes'
import { useI18n } from '@/i18n'
import { notifyError } from '@/store/notifications'
import type { HermesConfigRecord } from '@/types/hermes'

import { setHermesConfigCache, useHermesConfigRecord } from '../hooks/use-config-record'

import { ConfigField } from './config-field'
import { SECTIONS } from './constants'
import { enumOptionsFor, getNested, inferFieldSchema, setNested } from './helpers'

// The curated voice keys (Settings → Voice) are the single source of which
// per-provider fields exist; both the Voice settings page and the
// Capabilities TTS panel derive from it so the two surfaces never drift.
const VOICE_KEYS = SECTIONS.find(s => s.id === 'voice')?.keys ?? []

export function voiceProviderKeys(section: 'tts' | 'stt', providerKey: string): string[] {
  const prefix = `${section}.${providerKey}.`

  return VOICE_KEYS.filter(key => key.startsWith(prefix))
}

/**
 * Inline voice/model settings for one TTS (or STT) provider, rendered inside
 * the Capabilities → toolset config panel underneath the provider's API-key
 * fields. Reads and writes the same `tts.<provider>.*` config keys as
 * Settings → Voice (shared ConfigField renderer + enum/free-input rules), with
 * the same debounced autosave through the shared config cache.
 */
export function VoiceProviderFields({ section, providerKey }: { section: 'tts' | 'stt'; providerKey: string }) {
  const { t } = useI18n()
  const keys = useMemo(() => voiceProviderKeys(section, providerKey), [section, providerKey])
  const { data: loadedConfig } = useHermesConfigRecord()

  const { data: schemaResponse } = useQuery({
    queryKey: ['hermes-config-schema'],
    queryFn: () => getHermesConfigSchema(),
    staleTime: 5 * 60 * 1000
  })

  // Local editable draft, seeded once from the shared cache (background
  // refetches must not clobber in-progress edits) — the same shape as
  // config-settings.tsx's autosave loop.
  const [config, setConfig] = useState<HermesConfigRecord | null>(null)
  const seeded = useRef(false)
  const lastRev = useRef<string | undefined>(undefined)
  const saveVersionRef = useRef(0)
  const [saveVersion, setSaveVersion] = useState(0)

  // eslint-disable-next-line no-restricted-syntax -- one-shot config seed flag, not an atom mirror
  useEffect(() => {
    if (loadedConfig) {
      const rev = (loadedConfig as Record<string, unknown>)._revision as string | undefined
      if (!seeded.current || (saveVersionRef.current === 0 && rev && rev !== lastRev.current)) {
        seeded.current = true
        lastRev.current = rev
        setConfig(loadedConfig)
      }
    }
  }, [loadedConfig])

  // lastRev is a plain mutable ref carrying the save-request's revision
  // forward, not a reactive-value mirror; it's written once the async save
  // resolves, not synced from a store.
  // eslint-disable-next-line no-restricted-syntax
  useEffect(() => {
    if (!config || saveVersion === 0) {
      return
    }

    const timeout = window.setTimeout(() => {
      void saveHermesConfig(config)
        .then(result => {
          // Carry the server's new revision forward so the next autosave's
          // expected_revision matches disk instead of replaying this draft's
          // load-time revision and false-409ing against its own prior save.
          const saved = result._revision ? { ...config, _revision: result._revision } : config

          lastRev.current = result._revision ?? lastRev.current
          setConfig(saved)
          setHermesConfigCache(saved)
        })
        .catch(err => notifyError(err, t.settings.config.autosaveFailed))
    }, 550)

    return () => window.clearTimeout(timeout)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- config intentionally excluded: updateConfig
    // always bumps saveVersion in the same tick, so config is already fresh whenever this effect
    // re-runs for a real edit. Keeping config out of the deps also means the post-save
    // revision-carry-forward setConfig() above doesn't re-trigger this effect and loop.
  }, [saveVersion])

  // ElevenLabs cloned/library voices from the live account, when available —
  // mirrors the Settings → Voice dynamic voice list.
  const [elVoices, setElVoices] = useState<string[] | null>(null)
  const [elVoiceLabels, setElVoiceLabels] = useState<Record<string, string>>({})
  const wantsElevenLabs = keys.includes('tts.elevenlabs.voice_id')

  useEffect(() => {
    if (!wantsElevenLabs) {
      return
    }

    let cancelled = false

    getElevenLabsVoices()
      .then(result => {
        if (cancelled || !result.available) {
          return
        }

        setElVoices(result.voices.map(voice => voice.voice_id))
        setElVoiceLabels(Object.fromEntries(result.voices.map(voice => [voice.voice_id, voice.label])))
      })
      .catch(() => {
        if (!cancelled) {
          setElVoices(null)
          setElVoiceLabels({})
        }
      })

    return () => void (cancelled = true)
  }, [wantsElevenLabs])

  if (keys.length === 0 || !config) {
    return null
  }

  const schema = schemaResponse?.fields ?? {}

  const updateConfig = (next: HermesConfigRecord) => {
    saveVersionRef.current += 1
    setConfig(next)
    setSaveVersion(saveVersionRef.current)
  }

  return (
    <div className="grid gap-0.5 rounded-lg bg-background/55 px-2.5">
      {keys.map(key => {
        const value = getNested(config, key)
        const field = schema[key] ?? inferFieldSchema(value)
        const isElVoice = key === 'tts.elevenlabs.voice_id'

        return (
          <ConfigField
            enumOptions={enumOptionsFor(key, value, config, isElVoice ? (elVoices ?? undefined) : undefined)}
            key={key}
            onChange={next => updateConfig(setNested(config, key, next))}
            optionLabels={isElVoice ? elVoiceLabels : undefined}
            schema={field}
            schemaKey={key}
            value={value}
          />
        )
      })}
    </div>
  )
}
