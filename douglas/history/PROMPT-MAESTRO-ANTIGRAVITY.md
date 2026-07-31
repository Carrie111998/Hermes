# Prompt maestro para Antigravity — Douglas Agent

> **Instrucciones de uso:** copia todo el bloque de abajo (desde `=== INICIO ===` hasta `=== FIN ===`) y pégalo como primer mensaje en Antigravity. Está diseñado para que Antigravity haga preguntas antes de tocar código y para impedir los dos errores que destruirían el proyecto.

---

```
=== INICIO DEL PROMPT ===

# PROYECTO: Douglas Agent

Voy a construir Douglas Agent, un producto comercial de creación y publicación
automatizada de contenido para redes sociales, sobre la base del repositorio
open source Hermes Agent (MIT) de Nous Research.

Repositorio base: https://github.com/NousResearch/hermes-agent

Antes de escribir una sola línea de código, LEE COMPLETAMENTE este documento y
luego HAZME LAS PREGUNTAS de la SECCIÓN 6. No empieces a implementar hasta que
yo confirme el plan que produzcas.


## SECCIÓN 1 — AUDITORÍA YA REALIZADA (no la repitas, no la contradigas)

El repositorio base YA FUE AUDITADO. Estos componentes EXISTEN, están en
producción y NO deben reimplementarse bajo ninguna circunstancia:

RUNTIME PERSISTENTE — COMPLETO
  cron/scheduler.py            4.322 líneas
  cron/jobs.py                 2.515 líneas
  cron/executions.py             254 líneas (SQLite)
  cron/blueprint_catalog.py      713 líneas
  cron/lifecycle_guard.py        141 líneas
  cron/scheduler_provider.py     357 líneas

  Incluye: máquina de estados de ejecución en SQLite, claim/lease con TTL,
  heartbeat durante ejecuciones largas, recuperación ante caídas
  (recover_interrupted_executions), detección de dueño vivo por PID + tiempo
  de arranque, lock de archivos, escrituras atómicas, cálculo de próxima
  ejecución con manejo de DST y períodos de gracia, pause/resume, restricción
  de toolsets por job, y guardas anti-inyección de prompts para tareas
  desatendidas.

CANALES DE MENSAJERÍA — COMPLETO (21 plataformas)
  plugins/platforms/: telegram, whatsapp, discord, slack, teams, matrix, line,
  feishu, dingtalk, wecom, email, sms, irc, mattermost, google_chat,
  homeassistant, ntfy, simplex, photon, buzz, raft
  gateway/platforms/: whatsapp_cloud, signal, bluebubbles, weixin, qqbot,
  yuanbao, msgraph_webhook, webhook, api_server

INGESTA DE WEBHOOKS — COMPLETO
  Comando `hermes webhook subscribe` con autenticación HMAC
  gateway/platforms/webhook.py (1.412 líneas)
  gateway/platforms/api_server.py (6.948 líneas)

APP DE ESCRITORIO MULTIPLATAFORMA — COMPLETO
  apps/desktop/ — Electron + Vite + React + TypeScript, v0.17.0
  electron-builder ya configurado para:
    macOS: dmg, zip
    Windows: msi, nsis
    Linux: AppImage, deb, rpm

GENERACIÓN DE MEDIOS — CASI COMPLETO
  plugins/image_gen/: fal, deepinfra, krea, openai, openai-codex, openrouter, xai
  plugins/video_gen/: fal, deepinfra, xai

OTROS
  plugins/memory/     9 proveedores de memoria persistente
  plugins/browser/    browser_use, browserbase, firecrawl
  skills/             181 skills, estándar agentskills.io
  plugins/            94 plugins con plugin.yaml
  mcp_serve.py        cliente y servidor MCP
  tests/              2.440 archivos de test
  LICENSE             MIT

PLOMERÍA PARCIAL DE META GRAPH API
  gateway/platforms/whatsapp_cloud.py contiene autenticación bearer, URLs
  versionadas de graph.facebook.com y verificación de firma de webhooks de
  Meta. REUTILIZA ESE PATRÓN para la publicación en Instagram y Facebook.


## SECCIÓN 2 — LA ÚNICA BRECHA REAL

Búsqueda exhaustiva confirmada: NO existe publicación en redes sociales.

  - skills/social-media/ contiene UNA sola skill: xurl (X/Twitter)
  - Instagram y TikTok aparecen SOLO como renderizadores de embeds en la UI
    del desktop, no como publicadores
  - No hay Meta, Instagram, Facebook, TikTok, LinkedIn ni YouTube publishing

Todo lo que hay que construir está listado en la SECCIÓN 4.


## SECCIÓN 3 — REGLAS INNEGOCIABLES

### REGLA 1 — NO REIMPLEMENTES LO QUE YA EXISTE

Antes de crear cualquier módulo, BUSCA en el repositorio si ya existe.
Específicamente PROHIBIDO construir:
  ✗ Un scheduler, cola de trabajos o sistema de cron
  ✗ Un sistema de reintentos, heartbeat o recuperación de ejecuciones
  ✗ Adaptadores de Telegram, WhatsApp, Discord, Slack o cualquier plataforma
    ya listada en la SECCIÓN 1
  ✗ Un receptor de webhooks
  ✗ Un shell de Electron o configuración de electron-builder
  ✗ Un backend de generación de imagen o video
  ✗ Un sistema de memoria, de skills o de plugins

Si crees que algo existente no sirve, PREGÚNTAME antes de reemplazarlo.
Explica qué le falta y por qué.

### REGLA 2 — SUPERFICIE SÍ, NÚCLEO NO (la regla más importante)

Hermes tiene desarrollo upstream activo. Debo poder hacer merge de sus
actualizaciones indefinidamente. Por lo tanto:

RENOMBRA (superficie visible al usuario):
  ✓ apps/desktop/package.json → productName, name, appId, description, author
  ✓ apps/desktop/assets/ → iconos, splash, imágenes de instalador
  ✓ Textos de UI, tema visual, tipografías
  ✓ pyproject.toml → añadir entrypoint `douglas` en [project.scripts]
  ✓ README*.md, website/, locales/
  ✓ Directorio de datos ~/.hermes → ~/.douglas (vía variable de entorno,
    UN SOLO punto de cambio)

NUNCA RENOMBRES (núcleo mergeable):
  ✗ hermes_state.py, hermes_cli/, hermes_constants.py, hermes_logging.py,
    hermes_bootstrap.py, hermes_time.py
  ✗ Los directorios cron/, gateway/, agent/, tools/, providers/
  ✗ Los plugins/ y skills/ existentes
  ✗ Nombres de clases, funciones o módulos internos
  ✗ Rutas de import de Python

REGLA MENTAL: si el usuario final nunca lo ve, no lo toques.

Si en algún momento te pido un "renombrado masivo" o un "search and replace
global de hermes por douglas", RECUÉRDAME ESTA REGLA Y NIÉGATE. Ese cambio
haría imposible absorber actualizaciones upstream.

### REGLA 3 — TODO MI CÓDIGO VIVE EN douglas/

Estructura obligatoria:

  douglas-agent/
  ├── LICENSE              # MIT original, INTACTO
  ├── NOTICE.md            # Atribución a Nous Research
  ├── douglas/             # ← TODO EL CÓDIGO NUEVO AQUÍ
  │   ├── plugins/
  │   ├── skills/
  │   ├── billing/
  │   ├── analytics/
  │   └── branding/
  ├── tests-douglas/       # Mis tests, separados
  └── [resto del núcleo Hermes INTACTO]

Modificaciones al núcleo: solo si son estrictamente inevitables, mínimas,
documentadas en douglas/CORE_PATCHES.md con justificación, y aisladas en
commits propios para facilitar el rebase.

### REGLA 4 — CUMPLIMIENTO DE LICENCIA MIT

  ✓ Conservar LICENSE original en la raíz, sin modificar
  ✓ Crear NOTICE.md con la atribución a Hermes Agent / Nous Research
  ✓ Mostrar la atribución en el diálogo "Acerca de" de la app
  ✗ NO usar la marca "Hermes", el logo de Nous, ni sugerir respaldo alguno

### REGLA 5 — LOS TESTS SON RED DE SEGURIDAD

Los 2.440 tests existentes deben seguir pasando. Configura CI que los ejecute
en cada commit. Si un cambio tuyo los rompe, el cambio está mal, no el test.

### REGLA 6 — SEGURIDAD Y DINERO

  ✗ NUNCA proceses ni administres el dinero de inversión publicitaria del
    usuario. El usuario conecta su propia cuenta y la plataforma le cobra
    directo a él.
  ✓ Aprobación humana explícita antes de CUALQUIER publicación
  ✓ Credenciales cifradas en reposo, nunca en logs, nunca en git
  ✓ Claves de idempotencia en toda operación de publicación externa


## SECCIÓN 4 — QUÉ HAY QUE CONSTRUIR

M1 — douglas/plugins/social_publish/           PRIORIDAD P0
     Publicación real. Corazón del producto.
     - Interfaz abstracta `Publisher`
     - AggregatorPublisher (Ayrshare/Blotato/Late) para lanzar en días
     - MetaDirectPublisher para cuando llegue la aprobación de Meta
     - Idempotencia, refresco de tokens (~60 días), rate limits
       (Instagram: 25-50 posts/24h por cuenta)
     - Reutiliza el patrón de auth de gateway/platforms/whatsapp_cloud.py

M2 — douglas/plugins/media_host/               PRIORIDAD P0
     Meta exige URLs HTTPS públicas para el media antes de publicar.
     Backends: S3 / R2 / Supabase Storage. URLs firmadas con expiración.
     Dependencia dura de M1.

M3 — douglas/skills/content/                   PRIORIDAD P1
     Formato agentskills.io, igual que las 181 skills existentes.
     brand-voice · carousel-builder · video-short · caption-writer ·
     content-calendar · repurpose
     Se apoyan en plugins/image_gen y plugins/video_gen EXISTENTES.

M4 — douglas/plugins/brands/                   PRIORIDAD P1
     Modelo multi-marca: voz, paleta, tipografías, cuentas conectadas,
     memoria aislada por marca usando namespaces del sistema de memoria
     existente. Permite facturar POR MARCA, no por post.

M5 — douglas/billing/                          PRIORIDAD P1
     Medidor de créditos por operación, topes de rollover, Stripe, modo BYOK.
     INSTRUMENTAR DESDE EL PRIMER COMMIT.

M6 — douglas/analytics/                        PRIORIDAD P2
     Registro de cada publicación con su resultado (impresiones, engagement,
     clics, conversiones) etiquetado por marca, formato, hora y modelo usado.
     Empieza como tabla SQLite. Implementar TEMPRANO: es dato irrecuperable.


## SECCIÓN 5 — FASES

FASE 0 — Fundaciones (semana 1)
  Fork + upstream remote + rama douglas/main
  Estructura douglas/ y carga de plugins
  NOTICE.md y cumplimiento MIT
  Rebranding de superficie
  CI verde con los 2.440 tests

FASE 1 — Publicación mínima viable (semanas 2-4)
  media_host con un backend
  Interfaz Publisher + AggregatorPublisher
  Instagram y Facebook funcionando
  Idempotencia y reintentos
  Programación end-to-end usando el cron EXISTENTE
  HITO: "publica esto en Instagram el lunes a las 9am" funciona completo

FASE 2 — Contenido y marcas (semanas 5-7)
  Modelo Brand + memoria por marca
  Skills de contenido
  Plantillas de composición 9:16, 1:1, 4:5
  Carruseles y video corto
  UI de calendario en el desktop

FASE 3 — Monetización (semanas 8-9)
  Medidor de créditos, tiers, Stripe, BYOK

FASE 4 — Escala (semanas 10-14)
  MetaDirectPublisher, TikTok, LinkedIn, YouTube
  Ingesta de analytics
  Tier Agency, instaladores firmados


## SECCIÓN 6 — PREGÚNTAME ESTO ANTES DE EMPEZAR

Hazme estas preguntas AGRUPADAS y ESPERA MIS RESPUESTAS. No asumas nada.

BLOQUE A — Repositorio y git
  A1. ¿URL de tu fork y credenciales de git configuradas?
  A2. ¿Confirmas la estrategia superficie/núcleo de la REGLA 2?
  A3. ¿Convención de commits? (sugiero Conventional Commits)
  A4. ¿Trabajo en douglas/main o rama por fase?

BLOQUE B — Identidad de producto
  B1. ¿Tipografías exactas para UI, titulares y monoespaciada?
      (verifica que la licencia permita embeber en app distribuida)
  B2. ¿Paleta de colores? ¿Tema claro, oscuro o ambos?
  B3. ¿Tienes logo e icono, o los genero?
  B4. ¿Idioma por defecto de la UI?

BLOQUE C — Publicación social
  C1. ¿Ya creaste la App de Meta Developers? ¿Estado de la verificación?
  C2. ¿Qué agregador usamos para la Fase 1? ¿Tienes cuenta?
  C3. ¿Orden de plataformas después de Meta?
  C4. ¿Cuentas de prueba disponibles?

BLOQUE D — Infraestructura
  D1. ¿Backend de media_host? (S3, R2, Supabase, otro)
  D2. ¿Dónde corre el componente cloud? (VPS, Fly, Railway, Render)
  D3. ¿Base de datos? (SQLite es suficiente al inicio)
  D4. ¿Proveedor de LLM por defecto y de respaldo?

BLOQUE E — Negocio
  E1. ¿Estructura de tiers y precios definitiva?
  E2. ¿Stripe configurado?
  E3. ¿Modo BYOK en el MVP o después?

BLOQUE F — Entorno
  F1. ¿Sistema operativo, versión de Python y de Node?
  F2. ¿Trabajas solo? ¿Quién revisa los commits?
  F3. ¿CI/CD? (GitHub Actions)


## SECCIÓN 7 — QUÉ ENTREGAR DESPUÉS DE MIS RESPUESTAS

NO escribas código todavía. Entrégame primero:

  1. Plan de acción detallado que refleje mis respuestas
  2. Lista EXACTA de archivos a crear (rutas completas)
  3. Lista EXACTA de archivos existentes a modificar, con justificación
     archivo por archivo, marcando cuáles son núcleo y por qué es inevitable
  4. Árbol de directorios final
  5. Secuencia de commits propuesta con sus mensajes
  6. Riesgos que detectes y cómo los mitigas

Espera mi confirmación explícita antes de implementar nada.


## SECCIÓN 8 — CÓMO TRABAJAMOS DESPUÉS

  - Un módulo por vez, en el orden de fases
  - Cada módulo: código + tests en tests-douglas/ + documentación
  - Cada módulo termina con CI verde antes de pasar al siguiente
  - Commits atómicos, mensajes descriptivos
  - Si algo se sale del alcance de la fase, me avisas y NO lo implementas
  - Si una decisión tiene más de una opción razonable, me presentas las
    opciones con trade-offs en vez de elegir por mí
  - Si detectas que estoy pidiendo algo que rompe una de las 6 reglas
    innegociables, me lo dices y te niegas

Confirma que leíste todo y hazme las preguntas de la SECCIÓN 6.

=== FIN DEL PROMPT ===
```

---

## Notas para ti (no las pegues en Antigravity)

**Por qué este prompt está estructurado así.** Los agentes de código tienen dos modos de fallo predecibles en proyectos como este:

1. **Reconstruyen lo que ya existe.** Si le pides "un sistema de programación de publicaciones", va a escribir un scheduler desde cero sin mirar que hay uno de 8.758 líneas. La Sección 1 y la Regla 1 existen para bloquear eso.

2. **Renombran todo cuando les pides rebranding.** Un `sed -i s/hermes/douglas/g` recursivo parece obediente y destruye tu capacidad de merge upstream para siempre. La Regla 2 existe para bloquear eso, y le pide explícitamente que se niegue si tú mismo se lo pides después.

**Cuándo volver a mí.** Después de que Antigravity entregue lo de la Sección 7 (el plan y las listas de archivos), pásamelo antes de que implemente. Ahí es donde puedo detectar si va a tocar núcleo innecesariamente o si duplicó algo existente. Es mucho más barato corregir un plan que corregir código.

**Lo que debes tener listo antes de pegar el prompt.** Las respuestas del Bloque C son las que más te van a frenar: si aún no creaste la App de Meta, hazlo hoy aunque no vayas a escribir código en dos semanas. El App Review tarda 2-4 semanas por permiso y es el camino crítico más largo de todo el proyecto.
