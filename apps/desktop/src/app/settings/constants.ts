import {
  Box,
  Brain,
  type IconComponent,
  Lock,
  MessageCircle,
  Mic,
  Monitor,
  Moon,
  Palette,
  Sun,
  Wrench
} from '@/lib/icons'
import { REASONING_EFFORTS } from '@/lib/reasoning-effort'
import type { ThemeMode } from '@/themes/context'

// Single source of truth for built-in personality names lives in
// lib/personalities (mirrors hermes_cli/personality.py BUILTIN_PERSONALITIES).
export { BUILTIN_PERSONALITIES } from '@/lib/personalities'

import { defineFieldCopy } from './field-copy'
import type { DesktopConfigSection } from './types'

// Provider group definitions used to fold raw env-var names like
// ``XAI_API_KEY`` into a single "xAI" card with a friendly label, short
// description, and signup URL. Membership is determined by longest
// prefix match (see ``providerGroup`` in helpers.ts) so more specific
// prefixes (``MINIMAX_CN_``) correctly beat their general parents
// (``MINIMAX_``). New providers should be added here so they get their
// own card in Settings → Keys instead of being lumped into "Other".
interface ProviderPrefix {
  prefix: string
  name: string
  /** Optional one-line tagline shown beneath the group name. */
  description?: string
  /** Optional canonical signup/console URL surfaced from the card header. */
  docsUrl?: string
  /** Lower numbers float to the top of the providers list. */
  priority: number
}

export const EMPTY_SELECT_VALUE = '__hermes_empty__'
export const CONTROL_TEXT = 'text-xs'

export const PROVIDER_GROUPS: ProviderPrefix[] = [
  {
    prefix: 'NOUS_',
    name: 'Nous Portal',
    description: 'Hosted Hermes & Nous-trained models',
    docsUrl: 'https://portal.nousresearch.com',
    priority: 0
  },
  {
    prefix: 'FIREWORKS_',
    name: 'Fireworks AI',
    description: 'OpenAI-compatible direct model API',
    docsUrl: 'https://app.fireworks.ai/settings/users/api-keys',
    // Slot #2 — mirrors CANONICAL_PROVIDERS (after Nous, ahead of OpenRouter).
    // Same numeric priority as OpenRouter; name sort puts Fireworks first.
    priority: 1
  },
  {
    prefix: 'OPENROUTER_',
    name: 'OpenRouter',
    description: 'Aggregator for hundreds of frontier models',
    docsUrl: 'https://openrouter.ai/keys',
    priority: 1
  },
  {
    prefix: 'ANTHROPIC_',
    name: 'Anthropic',
    description: 'Claude API access (Sonnet, Opus, Haiku)',
    docsUrl: 'https://console.anthropic.com/settings/keys',
    priority: 2
  },
  {
    prefix: 'XAI_',
    name: 'xAI',
    description: 'Grok models (use OAuth for SuperGrok / Premium+)',
    docsUrl: 'https://console.x.ai/',
    priority: 3
  },
  {
    prefix: 'GOOGLE_',
    name: 'Gemini',
    description: 'Google AI Studio (Gemini 1.5 / 2.0 / 2.5)',
    docsUrl: 'https://aistudio.google.com/app/apikey',
    priority: 4
  },
  { prefix: 'GEMINI_', name: 'Gemini', priority: 4 },
  {
    prefix: 'DEEPSEEK_',
    name: 'DeepSeek',
    description: 'Direct DeepSeek API (V3.x, R1)',
    docsUrl: 'https://platform.deepseek.com/api_keys',
    priority: 5
  },
  {
    prefix: 'DASHSCOPE_',
    name: 'DashScope (Qwen)',
    description: 'Alibaba Cloud DashScope — Qwen and multi-vendor models',
    docsUrl: 'https://modelstudio.console.alibabacloud.com/',
    priority: 6
  },
  { prefix: 'HERMES_QWEN_', name: 'DashScope (Qwen)', priority: 6 },
  {
    prefix: 'GLM_',
    name: 'GLM / Z.AI',
    description: 'Zhipu GLM-4.6 and Z.AI hosted endpoints',
    docsUrl: 'https://z.ai/',
    priority: 7
  },
  { prefix: 'ZAI_', name: 'GLM / Z.AI', priority: 7 },
  { prefix: 'Z_AI_', name: 'GLM / Z.AI', priority: 7 },
  {
    prefix: 'KIMI_',
    name: 'Kimi / Moonshot',
    description: 'Moonshot Kimi K2 / coding endpoints',
    docsUrl: 'https://platform.moonshot.cn/',
    priority: 8
  },
  {
    prefix: 'KIMI_CN_',
    name: 'Kimi (China)',
    description: 'Moonshot China endpoint',
    docsUrl: 'https://platform.moonshot.cn/',
    priority: 9
  },
  {
    prefix: 'MINIMAX_',
    name: 'MiniMax',
    description: 'MiniMax-M2 and Hailuo international endpoints',
    docsUrl: 'https://www.minimax.io/',
    priority: 10
  },
  {
    prefix: 'MINIMAX_CN_',
    name: 'MiniMax (China)',
    description: 'MiniMax mainland China endpoint',
    docsUrl: 'https://www.minimaxi.com/',
    priority: 11
  },
  {
    prefix: 'HF_',
    name: 'Hugging Face',
    description: 'Inference Providers — 20+ open models via router.huggingface.co',
    docsUrl: 'https://huggingface.co/settings/tokens',
    priority: 12
  },
  {
    prefix: 'OPENCODE_ZEN_',
    name: 'OpenCode Zen',
    description: 'Pay-as-you-go access to curated coding models',
    docsUrl: 'https://opencode.ai/auth',
    priority: 13
  },
  {
    prefix: 'OPENCODE_GO_',
    name: 'OpenCode Go',
    description: '$10/month subscription for open coding models',
    docsUrl: 'https://opencode.ai/auth',
    priority: 14
  },
  {
    prefix: 'NVIDIA_',
    name: 'NVIDIA NIM',
    description: 'build.nvidia.com or your own local NIM endpoint',
    docsUrl: 'https://build.nvidia.com/',
    priority: 15
  },
  {
    prefix: 'OLLAMA_',
    name: 'Ollama Cloud',
    description: 'Cloud-hosted open models from ollama.com',
    docsUrl: 'https://ollama.com/settings',
    priority: 16
  },
  {
    prefix: 'LM_',
    name: 'LM Studio',
    description: 'Local LM Studio server (OpenAI-compatible)',
    docsUrl: 'https://lmstudio.ai/docs/local-server',
    priority: 17
  },
  {
    prefix: 'STEPFUN_',
    name: 'StepFun',
    description: 'StepFun Step Plan coding models',
    docsUrl: 'https://platform.stepfun.com/',
    priority: 18
  },
  {
    prefix: 'XIAOMI_',
    name: 'Xiaomi MiMo',
    description: 'MiMo-V2.5 and Xiaomi proprietary models',
    docsUrl: 'https://platform.xiaomimimo.com',
    priority: 19
  },
  {
    prefix: 'ARCEEAI_',
    name: 'Arcee AI',
    description: 'Arcee-hosted small + medium models',
    docsUrl: 'https://chat.arcee.ai/',
    priority: 20
  },
  { prefix: 'ARCEE_', name: 'Arcee AI', priority: 20 },
  {
    prefix: 'GMI_',
    name: 'GMI Cloud',
    description: 'GMI Cloud GPU + model serving',
    docsUrl: 'https://www.gmicloud.ai/',
    priority: 21
  },
  {
    prefix: 'AZURE_FOUNDRY_',
    name: 'Azure Foundry',
    description: 'Azure AI Foundry custom endpoints (OpenAI / Anthropic-compatible)',
    docsUrl: 'https://ai.azure.com/',
    priority: 22
  },
  {
    prefix: 'AWS_',
    name: 'AWS Bedrock',
    description: 'Authenticate via AWS profile + region',
    docsUrl: 'https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-regions.html',
    priority: 23
  }
]

// Schema-side select overrides for desktop-relevant enum fields whose
// backend schema only declares a string type.
export const ENUM_OPTIONS: Record<string, string[]> = {
  'agent.image_input_mode': ['auto', 'native', 'text'],
  'approvals.mode': ['manual', 'smart', 'off'],
  'code_execution.mode': ['project', 'strict'],
  'context.engine': ['compressor', 'default', 'custom'],
  // '' = inherit the agent's own effort; the rest is the shared scale.
  'delegation.reasoning_effort': ['', ...REASONING_EFFORTS],
  // NOTE: memory.provider is intentionally NOT listed here. Its options are
  // discovery-driven and served by the backend config schema (merged
  // per-request in web_server._schema_with_dynamic_provider_options), so
  // config-field consumes schema.options directly — a static list here would
  // shadow that and hide user-installed/pip providers (#49513).
  // Terminal execution backends — kept in sync with the dispatch ladder in
  // tools/terminal_tool.py::_create_environment (local/docker/singularity/
  // modal/daytona/ssh). Remote backends need extra env (image, tokens, host).
  'terminal.backend': ['local', 'docker', 'singularity', 'modal', 'daytona', 'ssh'],
  'stt.elevenlabs.model_id': ['scribe_v2', 'scribe_v1'],
  'stt.local.model': ['tiny', 'base', 'small', 'medium', 'large-v3'],
  // Speech-to-text backends — kept in sync with the stt block in
  // hermes_cli/config.py (local/groq/openai/mistral/elevenlabs).
  'stt.provider': ['local', 'groq', 'openai', 'mistral', 'xai', 'elevenlabs'],
  // OpenAI TTS voices — the union across models (per the OpenAI TTS API
  // docs). Model-specific narrowing happens in enumOptionsFor():
  // tts-1 / tts-1-hd support 9 voices; gpt-4o-mini-tts supports all 13.
  // Free-input field — the list is suggestions, not a gate (FREE_INPUT_KEYS).
  'tts.openai.voice': [
    'alloy',
    'ash',
    'ballad',
    'cedar',
    'coral',
    'echo',
    'fable',
    'marin',
    'nova',
    'onyx',
    'sage',
    'shimmer',
    'verse'
  ],
  // Popular Edge neural voices (the full catalog is 400+ — free input).
  'tts.edge.voice': [
    'en-US-AriaNeural',
    'en-US-JennyNeural',
    'en-US-AndrewNeural',
    'en-US-BrianNeural',
    'en-US-GuyNeural',
    'en-GB-SoniaNeural'
  ],
  'tts.gemini.model': ['gemini-2.5-flash-preview-tts', 'gemini-2.5-pro-preview-tts'],
  // Gemini TTS prebuilt voice set.
  'tts.gemini.voice': [
    'Zephyr',
    'Puck',
    'Charon',
    'Kore',
    'Fenrir',
    'Leda',
    'Orus',
    'Aoede',
    'Callirrhoe',
    'Autonoe',
    'Enceladus',
    'Iapetus',
    'Umbriel',
    'Algieba',
    'Despina',
    'Erinome',
    'Algenib',
    'Rasalgethi',
    'Laomedeia',
    'Achernar',
    'Alnilam',
    'Schedar',
    'Gacrux',
    'Pulcherrima',
    'Achird',
    'Zubenelgenubi',
    'Vindemiatrix',
    'Sadachbia',
    'Sadaltager',
    'Sulafat'
  ],
  'tts.xai.voice_id': ['eve'],
  'tts.minimax.model': ['speech-02-hd', 'speech-02-turbo'],
  'tts.mistral.model': ['voxtral-mini-tts-2603'],
  'tts.kittentts.model': [
    'KittenML/kitten-tts-nano-0.8-int8',
    'KittenML/kitten-tts-micro-0.8-int8',
    'KittenML/kitten-tts-mini-0.8-int8'
  ],
  'tts.kittentts.voice': ['Jasper'],
  'tts.piper.voice': ['en_US-lessac-medium', 'en_US-amy-medium', 'en_US-ryan-high', 'en_GB-alan-medium'],
  'tts.neutts.model': ['neuphonic/neutts-air-q4-gguf', 'neuphonic/neutts-air-q8-gguf', 'neuphonic/neutts-air'],
  // Text-to-speech backends — kept in sync with the built-in source of truth
  // (agent/tts_registry.py::_BUILTIN_NAMES / tools/tts_tool.py::
  // BUILTIN_TTS_PROVIDERS). 'xai' is Grok TTS.
  'tts.provider': [
    'edge',
    'elevenlabs',
    'openai',
    'xai',
    'minimax',
    'mistral',
    'gemini',
    'neutts',
    'kittentts',
    'piper'
  ],
  'stt.openai.model': ['whisper-1', 'gpt-4o-mini-transcribe', 'gpt-4o-transcribe', 'gpt-transcribe'],
  'stt.mistral.model': ['voxtral-mini-latest', 'voxtral-mini-2602'],
  'tts.openai.model': ['gpt-4o-mini-tts', 'tts-1', 'tts-1-hd'],
  'tts.elevenlabs.model_id': ['eleven_multilingual_v2', 'eleven_turbo_v2_5', 'eleven_flash_v2_5'],
  // NeuTTS local inference device.
  'tts.neutts.device': ['cpu', 'cuda', 'mps'],
  'updates.non_interactive_local_changes': ['stash', 'discard']
}

// Voice/model name fields render as a free-input combobox (Input + datalist)
// instead of a closed Select: providers accept custom voice IDs (ElevenLabs
// cloned voices, xAI custom voices, Edge's 400+ catalog) and ship new model
// names faster than this list updates. The ENUM_OPTIONS above become
// suggestions rather than a gate for these keys.
export const FREE_INPUT_KEYS = new Set([
  'tts.edge.voice',
  'tts.openai.model',
  'tts.openai.voice',
  'tts.elevenlabs.voice_id',
  'tts.gemini.model',
  'tts.gemini.voice',
  'tts.xai.voice_id',
  'tts.minimax.model',
  'tts.minimax.voice_id',
  'tts.mistral.model',
  'tts.mistral.voice_id',
  'tts.neutts.model',
  'tts.kittentts.model',
  'tts.kittentts.voice',
  'tts.piper.voice',
  'tts.deepinfra.model',
  'tts.deepinfra.voice'
])

export const FIELD_LABELS: Record<string, string> = defineFieldCopy({
  model: 'Default Model',
  modelContextLength: 'Context Window',
  fallbackProviders: 'Fallback Models',
  toolsets: 'Enabled Toolsets',
  timezone: 'Timezone',
  display: {
    personality: 'Personality',
    showReasoning: 'Reasoning Blocks'
  },
  desktop: {
    repoScanEnabled: 'Automatic Repository Discovery',
    repoScanRoots: 'Repository Discovery Roots',
    repoScanExcludePaths: 'Excluded Repository Paths'
  },
  agent: {
    maxTurns: 'Max Agent Steps',
    imageInputMode: 'Image Attachments',
    apiMaxRetries: 'API Retries',
    serviceTier: 'Service Tier',
    toolUseEnforcement: 'Tool-Use Enforcement'
  },
  terminal: {
    cwd: 'Working Directory',
    backend: 'Execution Backend',
    timeout: 'Command Timeout',
    persistentShell: 'Persistent Shell',
    envPassthrough: 'Environment Passthrough',
    dockerImage: 'Docker Image',
    singularityImage: 'Singularity Image',
    modalImage: 'Modal Image',
    daytonaImage: 'Daytona Image'
  },
  fileReadMaxChars: 'File Read Limit',
  toolOutput: {
    maxBytes: 'Terminal Output Limit',
    maxLines: 'File Page Limit',
    maxLineLength: 'Line Length Limit'
  },
  codeExecution: {
    mode: 'Code Execution Mode'
  },
  approvals: {
    mode: 'Approval Mode',
    timeout: 'Approval Timeout',
    mcpReloadConfirm: 'Confirm MCP Reloads'
  },
  commandAllowlist: 'Command Allowlist',
  security: {
    redactSecrets: 'Redact Secrets',
    allowPrivateUrls: 'Allow Private URLs'
  },
  browser: {
    allowPrivateUrls: 'Browser Private URLs',
    autoLocalForPrivateUrls: 'Local Browser For Private URLs'
  },
  checkpoints: {
    enabled: 'File Checkpoints',
    maxSnapshots: 'Checkpoint Limit'
  },
  voice: {
    recordKey: 'Voice Shortcut',
    maxRecordingSeconds: 'Max Recording Length',
    autoTts: 'Read Responses Aloud'
  },
  stt: {
    enabled: 'Speech To Text',
    echoTranscripts: 'Echo Transcripts',
    provider: 'Speech-To-Text Provider',
    local: {
      model: 'Local Transcription Model',
      language: 'Transcription Language'
    },
    openai: {
      model: 'OpenAI STT Model'
    },
    groq: {
      model: 'Groq STT Model'
    },
    mistral: {
      model: 'Mistral STT Model'
    },
    elevenlabs: {
      modelId: 'ElevenLabs STT Model',
      languageCode: 'ElevenLabs Language',
      tagAudioEvents: 'Tag Audio Events',
      diarize: 'Speaker Diarization'
    }
  },
  tts: {
    provider: 'Text-To-Speech Provider',
    edge: {
      voice: 'Edge Voice'
    },
    openai: {
      model: 'OpenAI TTS Model',
      voice: 'OpenAI Voice'
    },
    elevenlabs: {
      voiceId: 'ElevenLabs Voice',
      modelId: 'ElevenLabs Model'
    },
    xai: {
      voiceId: 'xAI (Grok) Voice',
      language: 'xAI Language',
      speed: 'xAI Playback Speed',
      autoSpeechTags: 'xAI Auto Speech Tags',
      optimizeStreamingLatency: 'xAI Streaming Latency Optimization',
      sampleRate: 'xAI Sample Rate',
      bitRate: 'xAI Bit Rate'
    },
    minimax: {
      model: 'MiniMax TTS Model',
      voiceId: 'MiniMax Voice'
    },
    mistral: {
      model: 'Mistral TTS Model',
      voiceId: 'Mistral Voice'
    },
    gemini: {
      model: 'Gemini TTS Model',
      voice: 'Gemini Voice'
    },
    neutts: {
      model: 'NeuTTS Model',
      device: 'NeuTTS Device'
    },
    kittentts: {
      model: 'KittenTTS Model',
      voice: 'KittenTTS Voice'
    },
    piper: {
      voice: 'Piper Voice'
    },
    deepinfra: {
      model: 'DeepInfra TTS Model',
      voice: 'DeepInfra Voice'
    }
  },
  memory: {
    memoryEnabled: 'Persistent Memory',
    userProfileEnabled: 'User Profile',
    memoryCharLimit: 'Memory Budget',
    userCharLimit: 'Profile Budget',
    provider: 'Memory Provider'
  },
  context: {
    engine: 'Context Engine'
  },
  compression: {
    enabled: 'Auto-Compression',
    threshold: 'Compression Threshold',
    targetRatio: 'Compression Target',
    protectLastN: 'Protected Recent Messages'
  },
  delegation: {
    model: 'Subagent Model',
    provider: 'Subagent Provider',
    maxIterations: 'Subagent Turn Limit',
    maxConcurrentChildren: 'Parallel Subagents',
    childTimeoutSeconds: 'Subagent Timeout',
    reasoningEffort: 'Subagent Reasoning Effort'
  },
  updates: {
    nonInteractiveLocalChanges: 'In-App Update Local Changes'
  }
})

export const FIELD_DESCRIPTIONS: Record<string, string> = defineFieldCopy({
  model: 'Used for new chats unless you pick a different model in the composer.',
  modelContextLength: "Leave at 0 to use the selected model's detected context window.",
  fallbackProviders: 'Backup provider:model entries to try if the default model fails.',
  display: {
    personality: 'Default assistant style for new sessions.',
    showReasoning: 'Show reasoning sections when the backend provides them.'
  },
  desktop: {
    repoScanEnabled: 'Scan local folders for Git repositories to show in Projects.',
    repoScanRoots: 'Folders to scan. Leave empty to scan your home directory.',
    repoScanExcludePaths: 'Folders and their descendants to skip during repository discovery.'
  },
  timezone: 'IANA timezone identifier. Blank uses the system timezone.',
  agent: {
    imageInputMode: 'Controls how image attachments are sent to the model.',
    maxTurns: 'Upper bound for tool-calling turns before Hermes stops a run.'
  },
  terminal: {
    cwd: 'Default project folder for tool and terminal work.',
    persistentShell: 'Keep shell state between commands when the backend supports it.',
    envPassthrough: 'Environment variables to pass into tool execution.',
    dockerImage: 'Container image used when the execution backend is Docker.',
    singularityImage: 'Image used when the execution backend is Singularity.',
    modalImage: 'Image used when the execution backend is Modal.',
    daytonaImage: 'Image used when the execution backend is Daytona.'
  },
  codeExecution: {
    mode: 'How strictly code execution is scoped to the current project.'
  },
  fileReadMaxChars: 'Maximum characters Hermes can read from one file request.',
  approvals: {
    mode: 'How Hermes handles commands that need explicit approval.',
    timeout: 'How long approval prompts wait before timing out.'
  },
  security: {
    redactSecrets: 'Hide detected secrets from model-visible content when possible.'
  },
  checkpoints: {
    enabled: 'Create rollback snapshots before file edits.'
  },
  memory: {
    memoryEnabled: 'Save durable memories that can help future sessions.',
    userProfileEnabled: 'Maintain a compact profile of user preferences.'
  },
  context: {
    engine: 'Strategy for managing long conversations near the context limit.'
  },
  compression: {
    enabled: 'Summarize older context when conversations get large.'
  },
  voice: {
    autoTts: 'Automatically speak assistant responses.'
  },
  tts: {
    xai: {
      voiceId: 'xAI voice ID (e.g. eve) or a custom voice ID.',
      language: 'Spoken language code (e.g. en, pt-BR) or "auto" for auto-detection.',
      speed: 'Playback speed. 0.7 = slower, 1.0 = normal, 1.5 = faster.',
      autoSpeechTags: 'Let an LLM insert expressive audio tags ([laughing], [sighs]) into the script before synthesis.',
      optimizeStreamingLatency: 'Latency vs. quality trade-off. 0 = best quality, 2 = lowest latency.',
      sampleRate: 'Audio sample rate in Hz. Higher = better quality, larger files.',
      bitRate: 'MP3 bitrate in bps. Only applies when codec is mp3.'
    },
    neutts: {
      device: 'Local inference device for NeuTTS.'
    }
  },
  stt: {
    enabled: 'Enable local or provider-backed speech transcription.',
    echoTranscripts: 'Post the raw 🎙️ transcript of voice messages back to the chat.',
    elevenlabs: {
      languageCode: 'Optional ISO-639-3 language code. Blank lets ElevenLabs auto-detect.'
    }
  },
  updates: {
    nonInteractiveLocalChanges:
      'When Hermes updates itself from the app (no terminal prompt), keep local source edits (stash) or throw them away (discard). Terminal updates always ask.'
  }
})

// Spanish localizations for the config-field copy above. Kept beside the
// English source so a field's label and help text live in one file; es.ts
// imports these instead of the English constants.
export const FIELD_LABELS_ES: Record<string, string> = defineFieldCopy({
  model: 'Modelo por defecto',
  modelContextLength: 'Ventana de contexto',
  fallbackProviders: 'Modelos de reserva',
  toolsets: 'Toolsets habilitados',
  timezone: 'Zona horaria',
  display: {
    personality: 'Personalidad',
    showReasoning: 'Bloques de razonamiento'
  },
  desktop: {
    repoScanEnabled: 'Detección automática de repositorios',
    repoScanRoots: 'Raíces de detección de repositorios',
    repoScanExcludePaths: 'Rutas de repositorio excluidas'
  },
  agent: {
    maxTurns: 'Pasos máximos del agente',
    imageInputMode: 'Adjuntos de imagen',
    apiMaxRetries: 'Reintentos de API',
    serviceTier: 'Nivel de servicio',
    toolUseEnforcement: 'Aplicación de uso de herramientas'
  },
  terminal: {
    cwd: 'Directorio de trabajo',
    backend: 'Backend de ejecución',
    timeout: 'Tiempo de espera de comando',
    persistentShell: 'Shell persistente',
    envPassthrough: 'Paso de variables de entorno',
    dockerImage: 'Imagen Docker',
    singularityImage: 'Imagen Singularity',
    modalImage: 'Imagen Modal',
    daytonaImage: 'Imagen Daytona'
  },
  fileReadMaxChars: 'Límite de lectura de archivos',
  toolOutput: {
    maxBytes: 'Límite de salida de terminal',
    maxLines: 'Límite de páginas de archivo',
    maxLineLength: 'Límite de longitud de línea'
  },
  codeExecution: {
    mode: 'Modo de ejecución de código'
  },
  approvals: {
    mode: 'Modo de aprobación',
    timeout: 'Tiempo de espera de aprobación',
    mcpReloadConfirm: 'Confirmar recargas MCP'
  },
  commandAllowlist: 'Lista blanca de comandos',
  security: {
    redactSecrets: 'Ocultar secretos',
    allowPrivateUrls: 'Permitir URLs privadas'
  },
  browser: {
    allowPrivateUrls: 'URLs privadas del navegador',
    autoLocalForPrivateUrls: 'Navegador local para URLs privadas'
  },
  checkpoints: {
    enabled: 'Puntos de control de archivos',
    maxSnapshots: 'Límite de puntos de control'
  },
  voice: {
    recordKey: 'Atajo de voz',
    maxRecordingSeconds: 'Duración máxima de grabación',
    autoTts: 'Leer respuestas en voz alta'
  },
  stt: {
    enabled: 'Voz a texto',
    echoTranscripts: 'Mostrar transcripciones',
    provider: 'Proveedor de voz a texto',
    local: {
      model: 'Modelo de transcripción local',
      language: 'Idioma de transcripción'
    },
    openai: {
      model: 'Modelo STT de OpenAI'
    },
    groq: {
      model: 'Modelo STT de Groq'
    },
    mistral: {
      model: 'Modelo STT de Mistral'
    },
    elevenlabs: {
      modelId: 'Modelo STT de ElevenLabs',
      languageCode: 'Idioma de ElevenLabs',
      tagAudioEvents: 'Etiquetar eventos de audio',
      diarize: 'Diarización de hablantes'
    }
  },
  tts: {
    provider: 'Proveedor de texto a voz',
    edge: {
      voice: 'Voz de Edge'
    },
    openai: {
      model: 'Modelo TTS de OpenAI',
      voice: 'Voz de OpenAI'
    },
    elevenlabs: {
      voiceId: 'Voz de ElevenLabs',
      modelId: 'Modelo de ElevenLabs'
    },
    xai: {
      voiceId: 'Voz de xAI (Grok)',
      language: 'Idioma de xAI',
      speed: 'Velocidad de reproducción de xAI',
      autoSpeechTags: 'Etiquetas de voz automáticas de xAI',
      optimizeStreamingLatency: 'Optimización de latencia de streaming de xAI',
      sampleRate: 'Frecuencia de muestreo de xAI',
      bitRate: 'Bitrate de xAI'
    },
    minimax: {
      model: 'Modelo TTS de MiniMax',
      voiceId: 'Voz de MiniMax'
    },
    mistral: {
      model: 'Modelo TTS de Mistral',
      voiceId: 'Voz de Mistral'
    },
    gemini: {
      model: 'Modelo TTS de Gemini',
      voice: 'Voz de Gemini'
    },
    neutts: {
      model: 'Modelo de NeuTTS',
      device: 'Dispositivo de NeuTTS'
    },
    kittentts: {
      model: 'Modelo de KittenTTS',
      voice: 'Voz de KittenTTS'
    },
    piper: {
      voice: 'Voz de Piper'
    },
    deepinfra: {
      model: 'Modelo TTS de DeepInfra',
      voice: 'Voz de DeepInfra'
    }
  },
  memory: {
    memoryEnabled: 'Memoria persistente',
    userProfileEnabled: 'Perfil de usuario',
    memoryCharLimit: 'Presupuesto de memoria',
    userCharLimit: 'Presupuesto de perfil',
    provider: 'Proveedor de memoria'
  },
  context: {
    engine: 'Motor de contexto'
  },
  compression: {
    enabled: 'Compresión automática',
    threshold: 'Umbral de compresión',
    targetRatio: 'Objetivo de compresión',
    protectLastN: 'Mensajes recientes protegidos'
  },
  delegation: {
    model: 'Modelo de subagente',
    provider: 'Proveedor de subagente',
    maxIterations: 'Límite de turnos del subagente',
    maxConcurrentChildren: 'Subagentes en paralelo',
    childTimeoutSeconds: 'Tiempo de espera del subagente',
    reasoningEffort: 'Esfuerzo de razonamiento del subagente'
  },
  updates: {
    nonInteractiveLocalChanges: 'Cambios locales en actualización desde la app'
  }
})

export const FIELD_DESCRIPTIONS_ES: Record<string, string> = defineFieldCopy({
  model: 'Se usa para chats nuevos salvo que elijas otro modelo en el compositor.',
  modelContextLength: 'Déjalo en 0 para usar la ventana de contexto detectada del modelo seleccionado.',
  fallbackProviders: 'Entradas proveedor:modelo de respaldo por si el modelo por defecto falla.',
  display: {
    personality: 'Estilo de asistente por defecto para sesiones nuevas.',
    showReasoning: 'Mostrar secciones de razonamiento cuando el backend las proporcione.'
  },
  desktop: {
    repoScanEnabled: 'Explora carpetas locales en busca de repositorios Git para mostrarlos en Proyectos.',
    repoScanRoots: 'Carpetas a explorar. Déjalo vacío para explorar tu directorio de inicio.',
    repoScanExcludePaths: 'Carpetas (y sus descendientes) que se omiten durante el descubrimiento de repositorios.'
  },
  timezone: 'Identificador de zona horaria IANA. En blanco usa la del sistema.',
  agent: {
    imageInputMode: 'Controla cómo se envían los adjuntos de imagen al modelo.',
    maxTurns: 'Límite superior de turnos con herramientas antes de que Hermes detenga una ejecución.'
  },
  terminal: {
    cwd: 'Carpeta de proyecto por defecto para el trabajo de herramientas y terminal.',
    persistentShell: 'Mantener el estado del shell entre comandos cuando el backend lo admita.',
    envPassthrough: 'Variables de entorno que se pasan a la ejecución de herramientas.',
    dockerImage: 'Imagen de contenedor usada cuando el backend de ejecución es Docker.',
    singularityImage: 'Imagen usada cuando el backend de ejecución es Singularity.',
    modalImage: 'Imagen usada cuando el backend de ejecución es Modal.',
    daytonaImage: 'Imagen usada cuando el backend de ejecución es Daytona.'
  },
  codeExecution: {
    mode: 'Cuán estrictamente se limita la ejecución de código al proyecto actual.'
  },
  fileReadMaxChars: 'Máximo de caracteres que Hermes puede leer en una petición de archivo.',
  approvals: {
    mode: 'Cómo gestiona Hermes los comandos que necesitan aprobación explícita.',
    timeout: 'Cuánto esperan las solicitudes de aprobación antes de agotar el tiempo.'
  },
  security: {
    redactSecrets: 'Oculta los secretos detectados del contenido visible para el modelo cuando es posible.'
  },
  checkpoints: {
    enabled: 'Crea instantáneas de reversión antes de editar archivos.'
  },
  memory: {
    memoryEnabled: 'Guarda recuerdos duraderos que pueden ayudar a futuras sesiones.',
    userProfileEnabled: 'Mantiene un perfil compacto de preferencias del usuario.'
  },
  context: {
    engine: 'Estrategia para gestionar conversaciones largas cerca del límite de contexto.'
  },
  compression: {
    enabled: 'Resume el contexto antiguo cuando las conversaciones crecen.'
  },
  voice: {
    autoTts: 'Leer automáticamente las respuestas del asistente.'
  },
  tts: {
    xai: {
      voiceId: 'ID de voz de xAI (p. ej. eve) o un ID de voz personalizado.',
      language: 'Código de idioma hablado (p. ej. es, pt-BR) o "auto" para detección automática.',
      speed: 'Velocidad de reproducción. 0.7 = más lento, 1.0 = normal, 1.5 = más rápido.',
      autoSpeechTags:
        'Deja que un LLM inserte etiquetas de audio expresivas ([risas], [suspira]) en el guion antes de sintetizar.',
      optimizeStreamingLatency: 'Compromiso entre latencia y calidad. 0 = mejor calidad, 2 = menor latencia.',
      sampleRate: 'Frecuencia de muestreo de audio en Hz. Más alto = mejor calidad, archivos más grandes.',
      bitRate: 'Bitrate MP3 en bps. Solo se aplica cuando el códec es mp3.'
    },
    neutts: {
      device: 'Dispositivo de inferencia local para NeuTTS.'
    }
  },
  stt: {
    enabled: 'Habilita la transcripción de voz local o con proveedor.',
    echoTranscripts: 'Publica la transcripción 🎙️ en bruto de los mensajes de voz de vuelta al chat.',
    elevenlabs: {
      languageCode: 'Código de idioma ISO-639-3 opcional. En blanco deja que ElevenLabs lo detecte.'
    }
  },
  updates: {
    nonInteractiveLocalChanges:
      'Cuando Hermes se actualiza desde la app (sin terminal), conserva las ediciones locales del código fuente (stash) o descártalas (discard). Las actualizaciones por terminal siempre preguntan.'
  }
})

// Curated desktop config surface: only fields a user might tune from the app.
export const SECTIONS: DesktopConfigSection[] = [
  {
    id: 'model',
    label: 'Model',
    icon: Box,
    keys: ['model_context_length', 'fallback_providers']
  },
  {
    id: 'chat',
    label: 'Chat',
    icon: MessageCircle,
    keys: ['display.personality', 'timezone', 'display.show_reasoning', 'agent.image_input_mode']
  },
  {
    id: 'appearance',
    label: 'Appearance',
    icon: Palette,
    keys: []
  },
  {
    id: 'workspace',
    label: 'Workspace',
    icon: Monitor,
    keys: [
      'terminal.cwd',
      'desktop.repo_scan_enabled',
      'desktop.repo_scan_roots',
      'desktop.repo_scan_exclude_paths',
      'code_execution.mode',
      'terminal.persistent_shell',
      'terminal.env_passthrough',
      'file_read_max_chars'
    ]
  },
  {
    id: 'safety',
    label: 'Safety',
    icon: Lock,
    keys: [
      'approvals.mode',
      'approvals.timeout',
      'approvals.mcp_reload_confirm',
      'command_allowlist',
      'security.redact_secrets',
      'security.allow_private_urls',
      'browser.allow_private_urls',
      'browser.auto_local_for_private_urls',
      'checkpoints.enabled'
    ]
  },
  {
    id: 'memory',
    label: 'Memory & Context',
    icon: Brain,
    keys: [
      'memory.memory_enabled',
      'memory.user_profile_enabled',
      'memory.memory_char_limit',
      'memory.user_char_limit',
      'memory.provider',
      'context.engine',
      'compression.enabled',
      'compression.threshold',
      'compression.target_ratio',
      'compression.protect_last_n'
    ]
  },
  {
    id: 'voice',
    label: 'Voice',
    icon: Mic,
    keys: [
      'tts.provider',
      'stt.enabled',
      'stt.echo_transcripts',
      'stt.provider',
      'voice.auto_tts',
      'tts.edge.voice',
      'tts.openai.model',
      'tts.openai.voice',
      'tts.elevenlabs.voice_id',
      'tts.elevenlabs.model_id',
      'tts.xai.voice_id',
      'tts.xai.language',
      'tts.xai.speed',
      'tts.xai.auto_speech_tags',
      'tts.xai.optimize_streaming_latency',
      'tts.xai.sample_rate',
      'tts.xai.bit_rate',
      'tts.minimax.model',
      'tts.minimax.voice_id',
      'tts.mistral.model',
      'tts.mistral.voice_id',
      'tts.gemini.model',
      'tts.gemini.voice',
      'tts.neutts.model',
      'tts.neutts.device',
      'tts.kittentts.model',
      'tts.kittentts.voice',
      'tts.piper.voice',
      'tts.deepinfra.model',
      'tts.deepinfra.voice',
      'stt.local.model',
      'stt.local.language',
      'stt.openai.model',
      'stt.groq.model',
      'stt.mistral.model',
      'stt.elevenlabs.model_id',
      'stt.elevenlabs.language_code',
      'stt.elevenlabs.tag_audio_events',
      'stt.elevenlabs.diarize',
      'voice.record_key',
      'voice.max_recording_seconds'
    ]
  },
  {
    id: 'advanced',
    label: 'Advanced',
    icon: Wrench,
    keys: [
      'toolsets',
      'terminal.backend',
      'terminal.timeout',
      'terminal.docker_image',
      'terminal.singularity_image',
      'terminal.modal_image',
      'terminal.daytona_image',
      'tool_output.max_bytes',
      'tool_output.max_lines',
      'tool_output.max_line_length',
      'checkpoints.max_snapshots',
      'agent.max_turns',
      'agent.api_max_retries',
      'agent.service_tier',
      'agent.tool_use_enforcement',
      'delegation.model',
      'delegation.provider',
      'delegation.max_iterations',
      'delegation.max_concurrent_children',
      'delegation.child_timeout_seconds',
      'delegation.reasoning_effort',
      'updates.non_interactive_local_changes'
    ]
  }
]

export interface ModeOption {
  id: ThemeMode
  label: string
  icon: IconComponent
}

export const MODE_OPTIONS: ModeOption[] = [
  { id: 'light', label: 'Light', icon: Sun },
  { id: 'dark', label: 'Dark', icon: Moon },
  { id: 'system', label: 'System', icon: Monitor }
]
