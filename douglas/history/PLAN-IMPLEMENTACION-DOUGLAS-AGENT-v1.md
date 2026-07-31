# Plan de Implementación — Douglas Agent

**Basado en auditoría directa del repositorio `NousResearch/hermes-agent` (clonado y analizado).**
Fecha del análisis: julio 2026 · Versión desktop auditada: 0.17.0

---

## 0. Veredicto ejecutivo

Te dije en el mensaje anterior que necesitabas construir un runtime persistente de 3 a 5 semanas.

**Me equivoqué. Ya está construido, y es mejor de lo que te describí.**

Hermes trae un scheduler de 8.758 líneas con persistencia SQLite, claim/lease con TTL, heartbeat, recuperación ante caídas, detección de procesos zombie por PID + tiempo de arranque, locking de archivos, escrituras atómicas, manejo de zonas horarias con períodos de gracia, y guardas anti-inyección de prompts específicas para tareas programadas.

Esto cambia la naturaleza de tu proyecto por completo:

> **No estás construyendo una plataforma de automatización. Estás construyendo ~6 plugins y un paquete de skills sobre un agente maduro.**

El trabajo real ya no es infraestructura. Es **vertical de negocio**: publicación en Meta, multi-marca, créditos y facturación, y el flywheel de datos de rendimiento.

---

## 1. Auditoría: qué ya existe

### 1.1 Runtime persistente — ✅ COMPLETO

| Componente | Ubicación | Líneas | Estado |
|---|---|---|---|
| Scheduler | `cron/scheduler.py` | 4.322 | Producción |
| Modelo de jobs | `cron/jobs.py` | 2.515 | Producción |
| Ejecuciones (SQLite) | `cron/executions.py` | 254 | Producción |
| Catálogo de blueprints | `cron/blueprint_catalog.py` | 713 | Producción |
| Guarda de ciclo de vida | `cron/lifecycle_guard.py` | 141 | Producción |
| Proveedor pluggable | `cron/scheduler_provider.py` | 357 | Experimental |
| Proveedor Chronos (scale-to-zero) | `plugins/cron_providers/chronos/` | — | Fase 4 |

**Capacidades verificadas en el código:**

- `create_execution` / `mark_execution_running` / `finish_execution` — máquina de estados persistida en SQLite
- `recover_interrupted_executions()` — recuperación tras caída del proceso
- `mark_running_jobs_interrupted()` — marcado de trabajos huérfanos
- `_owner_is_live(pid, started_at)` — detección de dueño vivo por PID **y** tiempo de arranque (evita colisiones por reutilización de PID)
- `_oneshot_run_claim_ttl_seconds()` — claim con expiración
- `_run_job_script_with_claim_heartbeat()` — heartbeat durante ejecuciones largas
- `_jobs_lock()` — lock de archivo para acceso concurrente
- `record_ticker_heartbeat` / `get_ticker_heartbeat_age` / `record_ticker_error` — observabilidad del ticker
- `compute_next_run` con `_compute_grace_seconds`, `_timezone_offset_mismatch`, `_recoverable_oneshot_run_at` — manejo correcto de DST y relojes desfasados
- `pause_job` / `resume_job` / `update_job` — control de ciclo de vida
- `CronPromptInjectionBlocked`, `_scan_assembled_cron_prompt`, `_guard_job_credential_exfil` — seguridad específica para automatizaciones desatendidas

**Lo único que falta para tu caso:** idempotencia a nivel de *publicación externa*. El scheduler garantiza que un job no corre dos veces, pero si el job publica en Instagram y falla después del POST, un reintento podría duplicar el post. Necesitas una clave de idempotencia en tu capa de publicación. Es media jornada de trabajo, no cinco semanas.

### 1.2 Canales de comunicación — ✅ COMPLETO (21 plataformas)

`plugins/platforms/`: telegram · whatsapp · discord · slack · teams · matrix · line · feishu · dingtalk · wecom · email · sms · irc · mattermost · google_chat · homeassistant · ntfy · simplex · photon · buzz · raft

`gateway/platforms/`: whatsapp_cloud · signal · bluebubbles · weixin · qqbot · yuanbao · msgraph_webhook · webhook · api_server

**Tu requisito de "enviar instrucciones desde Telegram, WhatsApp o cualquier otro canal" está 100% cubierto y no requiere trabajo.**

### 1.3 Ingesta de webhooks — ✅ COMPLETO

- `hermes webhook subscribe` con autenticación HMAC
- `gateway/platforms/webhook.py` (1.412 líneas)
- `gateway/platforms/api_server.py` (6.948 líneas)
- Scripts Python de pre-procesamiento que inyectan contexto antes del agente

### 1.4 App de escritorio multiplataforma — ✅ COMPLETO

`apps/desktop/` — Electron + Vite + React + TypeScript, v0.17.0

Targets de `electron-builder` ya configurados:
- macOS: `dmg`, `zip`
- Windows: `msi`, `nsis`
- Linux: `AppImage`, `deb`, `rpm`

**Tu requisito de "aplicación de escritorio para Linux, Mac y Windows" está resuelto. Solo falta rebranding visual.**

### 1.5 Generación de medios — ✅ CASI COMPLETO

| Tipo | Backends disponibles |
|---|---|
| Imagen | `fal`, `deepinfra`, `krea`, `openai`, `openai-codex`, `openrouter`, `xai` |
| Video | `fal`, `deepinfra`, `xai` |

FAL cubre FLUX, nano-banana, gpt-image, recraft. Es el backend que usarás por defecto.

**Falta:** plantillas de composición para formatos sociales (9:16, 1:1, 4:5), superposición de texto/marca, y generación de carruseles multi-imagen.

### 1.6 Otras capacidades relevantes

| Capacidad | Estado |
|---|---|
| Memoria persistente | ✅ 9 proveedores (`mem0`, `honcho`, `supermemory`, `holographic`, `hindsight`, `openviking`, `byterover`, `retaindb`) |
| Sistema de skills | ✅ 181 skills, compatible con estándar `agentskills.io` |
| Sistema de plugins | ✅ 94 plugins con `plugin.yaml` |
| Cliente + servidor MCP | ✅ `mcp_serve.py` + configuración en `~/.hermes/config.yaml` |
| Subagentes aislados | ✅ Paralelización de workstreams |
| Navegador | ✅ `browser_use`, `browserbase`, `firecrawl` |
| Backends de terminal | ✅ local, Docker, SSH, Singularity, Modal, Daytona |
| Suite de tests | ✅ 2.440 archivos de test |
| Licencia | ✅ MIT |
| Plomería de Meta Graph API | ⚠️ Parcial — existe en `gateway/platforms/whatsapp_cloud.py` (bearer auth, URLs versionadas de graph, verificación de firma de webhook) |

### 1.7 La brecha real

Busqué exhaustivamente publicación en redes sociales. Esto es lo que encontré:

- `skills/social-media/` contiene **una sola skill**: `xurl` (X/Twitter vía CLI oficial)
- Instagram y TikTok aparecen **únicamente** como renderizadores de embeds en la UI del desktop (`apps/desktop/src/components/assistant-ui/embeds/providers/`)
- No existe publicación en Meta, Instagram, Facebook, TikTok, LinkedIn ni YouTube

**Esa es tu brecha completa. Todo lo demás está construido.**

---

## 2. La decisión arquitectónica más importante

En tu primer mensaje pediste "renombrado de archivos y carpetas para mantener coherencia con Douglas Agent".

**No hagas eso. Te va a costar el proyecto.**

Hermes tiene desarrollo activo intenso. Si renombras `hermes_state.py` → `douglas_state.py`, `cron/` → `douglas_cron/`, y los ~4.000 archivos internos, cada actualización upstream se convierte en cientos de conflictos de merge irresolubles. En seis meses estarás atrapado en una versión congelada de 2026, sin parches de seguridad, mientras Nous publica mejoras que no puedes absorber.

### La estrategia correcta: superficie vs. núcleo

```
┌─────────────────────────────────────────────────┐
│  CAPA DOUGLAS  (tuya, se renombra todo)         │
│  ├── douglas/plugins/    ← publicación social   │
│  ├── douglas/skills/     ← skills de contenido  │
│  ├── douglas/branding/   ← logos, colores, tipo │
│  └── douglas/billing/    ← créditos, tiers      │
├─────────────────────────────────────────────────┤
│  NÚCLEO HERMES  (intacto, mergeable con upstream)│
│  cron/ · gateway/ · agent/ · plugins/ · skills/ │
└─────────────────────────────────────────────────┘
```

**Renombra la superficie que ve el usuario:**
- Nombre del producto, logo, icono, splash, título de ventana
- Comando CLI: `hermes` → `douglas` (vía alias/entrypoint en `pyproject.toml`, sin tocar módulos)
- `apps/desktop/package.json`: `productName`, `appId`, `name`
- Textos de UI, README, documentación, `locales/`
- Directorio de datos: `~/.hermes` → `~/.douglas` (variable de entorno, un solo punto de cambio)

**NO renombres:**
- Módulos Python internos (`hermes_state`, `hermes_cli`, `hermes_constants`, `hermes_logging`)
- Estructura de directorios del núcleo (`cron/`, `gateway/`, `agent/`, `tools/`)
- Nombres de clases y funciones internas

**Regla mental:** si el usuario final nunca lo ve, no lo toques.

### Flujo de git recomendado

```bash
git remote add upstream https://github.com/NousResearch/hermes-agent.git
git checkout -b douglas/main

# Todo tu código vive en douglas/ y en overrides mínimos
# Actualización mensual:
git fetch upstream
git merge upstream/main   # conflictos mínimos si respetaste la regla
```

### Cumplimiento MIT

La licencia MIT te permite todo esto, pero exige conservar el aviso de copyright original:

1. Mantén `LICENSE` original en la raíz
2. Crea `NOTICE.md`: *"Douglas Agent está construido sobre Hermes Agent (MIT), de Nous Research."*
3. Muestra la atribución en el diálogo "Acerca de" de la app
4. No uses la marca "Hermes" ni el logo de Nous ni sugieras respaldo

---

## 3. Qué hay que construir: 6 módulos

### M1 — `douglas/plugins/social_publish/` 🔴 P0

Publicación real en redes. **Es el corazón del producto.**

```
social_publish/
├── meta/          # Instagram + Facebook Graph API
├── tiktok/
├── linkedin/
├── youtube/
├── x/             # envolver la skill xurl existente
├── core/
│   ├── idempotency.py   # clave por (marca, contenido_hash, ventana)
│   ├── token_refresh.py # tokens Meta caducan ~60 días
│   ├── rate_limits.py   # IG: 25-50 posts/24h por cuenta
│   └── media_host.py    # Meta exige URLs HTTPS públicas
└── plugin.yaml
```

**Reutiliza:** el patrón de autenticación y verificación de firma de `gateway/platforms/whatsapp_cloud.py`. Ya resuelve bearer tokens, URLs versionadas de graph y validación de webhooks de Meta.

**Bloqueante externo:** App Review de Meta, 2-4 semanas por permiso. **Arranca esto el día 1**, en paralelo con todo lo demás. Para validar antes, usa un agregador (Ayrshare, Blotato, Late) como backend intercambiable detrás de la misma interfaz.

**Decisión de diseño:** define una interfaz `Publisher` abstracta con implementaciones `MetaDirectPublisher` y `AggregatorPublisher`. Empiezas con la segunda, migras a la primera sin tocar nada más.

### M2 — `douglas/plugins/media_host/` 🔴 P0

Meta requiere que el video/imagen esté en una URL HTTPS pública antes de publicar.

- Backends: S3, R2, Supabase Storage, o almacenamiento propio
- URLs firmadas con expiración corta
- Limpieza automática tras publicación exitosa

Sin esto, M1 no funciona. Es dependencia dura.

### M3 — `douglas/skills/content/` 🟠 P1

Paquete de skills en formato `agentskills.io` (mismo formato que las 181 existentes):

- `brand-voice` — memoria de tono, paleta, público objetivo por marca
- `carousel-builder` — carruseles multi-imagen coherentes
- `video-short` — guion → escenas → render → subtítulos
- `caption-writer` — copy por plataforma con hashtags
- `content-calendar` — planificación mensual
- `repurpose` — un contenido → N formatos

Estas se apoyan en los backends de `image_gen` y `video_gen` que ya existen. **Aquí no escribes infraestructura, escribes prompts y lógica de composición.**

### M4 — `douglas/plugins/brands/` 🟠 P1

Modelo multi-marca. Es lo que permite facturar por marca (modelo Metricool) en vez de por post.

```python
Brand:
  id, nombre, industria
  voz: tono, palabras prohibidas, emojis sí/no
  visual: paleta, tipografías, logo, plantillas
  cuentas: [{plataforma, cuenta_id, token_ref}]
  memoria: espacio aislado por marca
```

Se integra con el sistema de memoria existente usando namespaces separados por marca.

### M5 — `douglas/billing/` 🟠 P1

- Medidor de créditos por operación (tokens, imagen, video, publicación)
- Topes de rollover (el mecanismo de breakage que analizamos)
- Integración con Stripe
- Modo BYOK que salta el medidor por completo

**Instrumenta esto desde el primer commit.** Si no puedes medir consumo por usuario desde el día 1, no puedes fijar precios ni detectar al usuario que te cuesta $200/mes.

### M6 — `douglas/analytics/` 🟡 P2

El flywheel de datos. Cada publicación se registra con su resultado:

```
publicacion_id · marca · plataforma · formato · hora
copy_hash · imagen_prompt · modelo_usado
→ impresiones · engagement · clics · conversiones
```

Empieza como una tabla SQLite. En 12 meses es tu foso competitivo. Es media jornada implementarla hoy y es irrecuperable si la implementas tarde.

---

## 4. Roadmap por fases

### Fase 0 — Fundaciones (Semana 1)

| # | Tarea | Notas |
|---|---|---|
| 0.1 | Fork + `git remote add upstream` + rama `douglas/main` | |
| 0.2 | **Crear la App de Meta y arrancar verificación de negocio** | Camino crítico más largo, no depende de código |
| 0.3 | Estructura `douglas/` y sistema de carga de plugins | |
| 0.4 | `NOTICE.md` + cumplimiento MIT | |
| 0.5 | Rebranding de superficie (ver §5) | |
| 0.6 | CI: verificar que los 2.440 tests siguen pasando | Red de seguridad innegociable |

### Fase 1 — Publicación mínima viable (Semanas 2-4)

| # | Tarea |
|---|---|
| 1.1 | `media_host` con un backend (R2 o S3) |
| 1.2 | Interfaz `Publisher` + `AggregatorPublisher` |
| 1.3 | Publicar en Instagram y Facebook vía agregador |
| 1.4 | Idempotencia + reintentos con backoff |
| 1.5 | Programación end-to-end usando el cron existente |
| 1.6 | Registro de ejecuciones visible al usuario |

**Hito:** *"Douglas, publica esto en Instagram el lunes a las 9am"* funciona de extremo a extremo.

### Fase 2 — Contenido y marcas (Semanas 5-7)

| # | Tarea |
|---|---|
| 2.1 | Modelo `Brand` + memoria aislada por marca |
| 2.2 | Skills `brand-voice` y `caption-writer` |
| 2.3 | Plantillas de composición para formatos sociales |
| 2.4 | `carousel-builder` |
| 2.5 | `video-short` sobre los backends existentes |
| 2.6 | UI de calendario de contenido en el desktop |

### Fase 3 — Monetización (Semanas 8-9)

| # | Tarea |
|---|---|
| 3.1 | Medidor de créditos con desglose por operación |
| 3.2 | Tiers + topes de rollover |
| 3.3 | Stripe (suscripción + portal de cliente) |
| 3.4 | Modo BYOK |
| 3.5 | Términos y Condiciones **revisados por abogado** |

### Fase 4 — Publicación directa y escala (Semanas 10-14)

| # | Tarea |
|---|---|
| 4.1 | `MetaDirectPublisher` cuando llegue la aprobación |
| 4.2 | TikTok, LinkedIn, YouTube |
| 4.3 | Ingesta de analytics + flywheel |
| 4.4 | Tier Agency: multi-usuario, white-label |
| 4.5 | Instaladores firmados para las tres plataformas |

### Estimación total

**10-14 semanas** para producto vendible, un desarrollador competente a tiempo completo.

Sin la base de Hermes serían 8-12 meses. El scheduler, los 21 canales, el desktop multiplataforma y la generación de medios representan aproximadamente **el 70% del producto ya construido**.

---

## 5. Guía de rebranding

### Cambiar (superficie visible)

| Archivo / ubicación | Cambio |
|---|---|
| `apps/desktop/package.json` | `productName`, `name`, `appId`, `description`, `author` |
| `apps/desktop/assets/` | Icono, splash, imágenes de instalador |
| `apps/desktop/src/` | Textos de UI, tema, tipografías |
| `pyproject.toml` | `[project.scripts]` → añadir entrypoint `douglas` |
| `README*.md` | Reescritura completa |
| `website/` | Documentación |
| `locales/` | Cadenas traducidas |
| `~/.hermes` | → `~/.douglas` vía variable de entorno |

### Tipografía (mencionaste cambiarla)

Recomendación para un producto de contenido/marketing:
- **UI:** Inter, o Geist si buscas algo más distintivo
- **Titulares:** algo con carácter — Cabinet Grotesk, Satoshi, General Sans
- **Monoespaciada:** JetBrains Mono o Geist Mono para el TUI

Verifica que la licencia permita embeber la fuente en una app de escritorio distribuida.

### NO cambiar

`hermes_state.py` · `hermes_cli/` · `hermes_constants.py` · `hermes_logging.py` · `cron/` · `gateway/` · `agent/` · `tools/` · `plugins/` (los existentes) · `skills/` (las existentes)

---

## 6. Estructura final del repositorio

```
douglas-agent/
├── LICENSE                    # MIT original, intacto
├── NOTICE.md                  # Atribución a Nous Research
├── README.md                  # Reescrito
│
├── douglas/                   # ← TODO TU CÓDIGO VIVE AQUÍ
│   ├── __init__.py
│   ├── plugins/
│   │   ├── social_publish/
│   │   ├── media_host/
│   │   └── brands/
│   ├── skills/
│   │   └── content/
│   ├── billing/
│   ├── analytics/
│   └── branding/
│
├── apps/desktop/              # Rebrandeado, estructura intacta
├── cron/                      # INTACTO
├── gateway/                   # INTACTO
├── agent/                     # INTACTO
├── plugins/                   # INTACTO
├── skills/                    # INTACTO
├── tests/                     # INTACTO + douglas tests aparte
└── tests-douglas/             # Tus tests
```

---

## 7. Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| App Review de Meta rechazado | Alto | Arrancar semana 1; agregador como plan B permanente |
| Divergencia del upstream | Alto | Regla superficie/núcleo; merge mensual disciplinado |
| Costo de video destruye márgenes | Alto | Créditos separados y visibles para video desde el día 1 |
| Límite 25-50 posts/24h de Instagram | Medio | Facturar por marca, nunca por volumen de posts |
| Caducidad de tokens Meta (~60 días) | Medio | Demonio de refresco + alertas al usuario |
| Publicación duplicada por reintento | Medio | Claves de idempotencia en la capa de publicación |
| Los 2.440 tests se rompen | Medio | CI bloqueante desde el commit 1 |
| Exposición legal (claims, cancelación) | Alto | Abogado SaaS antes del primer cobro |

---

## 8. Las cinco cosas que haría esta semana

1. **Crear la App de Meta Developers y arrancar la verificación de negocio.** No depende de nada más y es el camino crítico más largo.
2. **Fork con `upstream` configurado** y la regla superficie/núcleo escrita en `CONTRIBUTING.md` para que Antigravity la respete.
3. **Landing con los tiers y botón de waitlist.** Valida el precio antes de escribir el scheduler de publicación. Si nadie hace clic en $99, lo sabes antes de construirlo.
4. **Instrumentar el medidor de créditos y la tabla de analytics.** Media jornada hoy, irrecuperable después.
5. **Abogado SaaS.** $1.500-3.000. Mejor ROI del proyecto.
