# douglas/ — capa de producto de Douglas Agent

Este directorio es la única superficie donde vive código **nuevo** de Douglas
Agent. Todo lo demás en este repositorio es Hermes Agent, sin tocar, para
que `git merge upstream/main` siga siendo posible indefinidamente.

## El contrato de compatibilidad

Douglas Agent es Hermes Agent con otra cara. Por fuera, todo dice Douglas.
Por dentro, todo sigue siendo Hermes y sigue funcionando.

| Elemento | Acción | Regla |
|---|---|---|
| Módulos Python del núcleo | Nada | `hermes_state.py`, `hermes_constants.py`, `hermes_cli/`… intactos |
| Rutas de import | Nada | `import hermes_state` sigue igual |
| Directorios del núcleo | Nada | `cron/`, `gateway/`, `agent/`, `tools/`, `plugins/`, `skills/`, `apps/`, `tui_gateway/`… |
| Paquetes npm internos | Nada | `@hermes/shared`, `@hermes/ink`… intactos |
| Nombres de clases/funciones | Nada | intactos |
| Comando CLI | Añadir alias | `douglas` nuevo, `hermes` sigue funcionando |
| Variables de entorno | Añadir alias | `DOUGLAS_*` primero, `HERMES_*` como respaldo |
| Directorio de datos | Añadir alias | `~/.douglas` primero, `~/.hermes` si ya existe |
| Archivo de config | Añadir alias | `douglas-config.yaml` o `hermes-config.yaml` |
| Textos visibles en UI | Cambiar todo | → "Douglas Agent" |
| Identidad de la app | Cambiar todo | `productName`, `appId`, iconos, fuentes |
| README y docs | Cambiar | Douglas + atribución MIT |

**Regla mental:** si el usuario final lo ve, cámbialo. Si solo lo ve el
intérprete de Python, no lo toques.

## Las 8 reglas

1. **Consulta `CAPABILITIES.md`** (raíz del repo) antes de construir
   cualquier cosa. Si ya existe, úsalo o extiéndelo. Si crees que no sirve,
   pregunta primero.
2. **No renombres** módulos, directorios ni rutas de import del núcleo. No
   crees módulos `douglas_*.py` que dupliquen los existentes. No crees
   shims de compatibilidad entre módulos del núcleo.
3. Todo código **nuevo** vive en `douglas/`. Cada toque al núcleo se anota
   en [`CORE_PATCHES.md`](./CORE_PATCHES.md) con ruta, motivo y alternativa
   descartada.
4. Commits atómicos, agrupados por intención, con mensajes que describan lo
   que el commit realmente hace.
5. **Compatibilidad hacia atrás obligatoria**: quien tenga `~/.hermes`,
   `HERMES_*` o use el comando `hermes` debe seguir funcionando igual.
6. Los tests existentes deben seguir pasando. Si un cambio rompe tests,
   el cambio está mal.
7. **Licencia MIT**: `LICENSE` intacto, `NOTICE` con atribución a Nous
   Research, atribución visible en la pantalla "Acerca de". Nunca usar la
   marca "Hermes" ni el logo de Nous en superficies de producto.
8. Si una decisión admite más de una opción razonable, se presentan las
   opciones con sus trade-offs. No se elige unilateralmente.

## Limitaciones conocidas

Cosas explícitamente pendientes — no silenciadas, documentadas aquí a propósito
para que ningún agente futuro las redescubra desde cero.

### Wake word sigue diciendo "hey hermes"

`tools/wake_word.py` usa por defecto el motor **openWakeWord** con un modelo
ONNX/tflite **ya entrenado** específicamente para el patrón acústico de
"hey hermes" (`tools/wakewords/hey_hermes.onnx`). El texto de configuración
(`wake_word.phrase`) es **cosmético para este motor** — cambiarlo a
"hey douglas" sin retrenar el modelo no cambiaría lo que el motor realmente
escucha, y dejaría la UI diciendo algo que no activa la función. Por eso se
mantiene "hey hermes" tal cual: es la opción honesta, no un descuido.

**Lo que haría falta para tener "hey douglas" de verdad:**
1. **Opción rápida, sin entrenar nada**: cambiar el proveedor por defecto a
   **`sherpa`** (`wake_word.provider: sherpa`) — es open-vocabulary, tokeniza
   cualquier frase escrita en tiempo real contra un modelo genérico. Funciona
   con "hey douglas" de inmediato. Contras: descarga única de ~13MB la primera
   vez, y puede tener precisión/tasa de falsos positivos distinta al modelo
   `hey_hermes` hecho a medida — no se ha medido esa diferencia todavía.
2. **Opción con calidad equivalente**: entrenar un modelo openWakeWord nuevo
   para "hey douglas". openWakeWord soporta esto con datos sintéticos
   generados por TTS (sin grabar voces reales) vía su propio notebook de
   entrenamiento (ver [su repo](https://github.com/dscripka/openWakeWord)) —
   es un proyecto de ML aparte, no una tarea de branding: requiere tiempo de
   entrenamiento (típicamente horas en una GPU en la nube, el propio proyecto
   documenta el proceso con Google Colab), evaluación de falsos positivos
   contra audio ambiente, y empaquetar el `.onnx`/`.tflite` resultante en
   `tools/wakewords/`.

### Fuente de marca

La cabecera y los títulos usan **Space Grotesk** (SIL OFL, gratuita) en vez
de la fuente "Dimitri" original del brief — los archivos TTF de Dimitri nunca
aparecieron en los assets rescatados. Si se consiguen, sustituyen el
`@font-face` en `apps/desktop/src/styles.css` sin tocar nada más (la variable
`--dt-font-display` es el único punto de cambio).

### Iconografía e ilustraciones

Sin arte propio de Douglas Agent todavía. Estado actual, punto por punto:

- **`BrandMark`** (`apps/desktop/src/components/brand-mark.tsx`,
  `apps/bootstrap-installer/src/components/brand-mark.tsx`): placeholder
  tipográfico "DA" sobre verde esmeralda — ya no usa imagen externa.
- **Ícono real de la app** (`apps/desktop/assets/icon.{png,ico,icns}`,
  usado en el `.exe`/`.app`/`.dmg`/taskbar vía `electron-builder`) y los
  íconos del instalador Tauri (`apps/bootstrap-installer/src-tauri/icons/`):
  **todavía son la mascota ilustrada de Nous Research** ("nous-girl", con
  una etiqueta "N" visible). Es el hueco de marca más visible que queda —
  pendiente de resolución explícita antes de cualquier build pública.
- **Favicon** (`apps/desktop/public/apple-touch-icon.png`): misma mascota de
  Nous, usada en las etiquetas `<link>` de `index.html`.
- **Mascota "petdex" de Hermes** (`apps/desktop/public/{hermes.png,
  hermes-sprite.png,hermes-frames/}` — un personaje pixel-art con casco
  alado y caduceo): confirmado que **no está referenciada por ningún
  componente actual** — son archivos huérfanos, seguros de eliminar sin
  romper nada, pero no eliminados todavía a la espera de confirmación.

## Estructura

| Carpeta | Contenido |
|---|---|
| `plugins/` | Plugins nuevos de Douglas Agent (media hosting, publicación social, etc.) |
| `skills/` | Skills nuevas, formato `agentskills.io`, igual que las 181 ya existentes en `skills/`/`optional-skills/` |
| `billing/` | Cliente de pagos propio (Stripe), desacoplado del Nous Portal |
| `analytics/` | Métricas de rendimiento de publicaciones |
| `branding/` | Assets e identidad visual (fuentes, colores, textos de marca) |
| `history/` | Documentos de planeación/diagnóstico previos, conservados como referencia histórica |
| `compat.py` | Helpers de resolución de home/env/config con alias Douglas→Hermes |
| `CORE_PATCHES.md` | Registro de cada toque al núcleo de Hermes: ruta, motivo, alternativa descartada |

## Cadena canónica de resolución Douglas/Hermes

Fuente única de verdad. `hermes_bootstrap.py` (Python), `apps/desktop/electron/main.ts`
(`resolveHermesHome()`) y `apps/bootstrap-installer/src-tauri/src/paths.rs`
(`hermes_home()`) implementan esta misma cadena por separado — no pueden
compartir código porque corren en tres runtimes distintos (Python, el
proceso principal de Electron en Node, y un instalador nativo en Rust que
se ejecuta *antes* de que exista Python) — y cada uno referencia esta
sección por comentario (`// Mirrors douglas/README.md` / `# Mirrors
douglas/README.md`), siguiendo el mismo patrón que el propio Hermes ya usa
para mantener sincronizados `hermes_constants.py`, `main.ts` y `paths.rs`
entre sí.

**Variables de entorno — regla genérica:** cualquier `DOUGLAS_<X>` presente
y no vacía sobrescribe `HERMES_<X>` en el entorno del proceso, para
cualquier `<X>`. Esto pasa una sola vez, muy al principio del proceso, así
que los ~200 sitios existentes que ya leen `HERMES_<X>` funcionan sin
tocarlos.

**Directorio home:**

| Orden | Windows | macOS / Linux |
|---|---|---|
| 1 | `%DOUGLAS_HOME%` si está seteada | `$DOUGLAS_HOME` si está seteada |
| 2 | `%HERMES_HOME%` si está seteada | `$HERMES_HOME` si está seteada |
| 3 | `%LOCALAPPDATA%\douglas` si el directorio existe | `~/.douglas` si el directorio existe |
| 4 | `%LOCALAPPDATA%\hermes` si el directorio existe (instalación previa de Hermes) | `~/.hermes` si el directorio existe (instalación previa de Hermes) |
| 5 | `~/.hermes` si existe (legado pre-`%LOCALAPPDATA%`, mismo caso que ya maneja `main.ts`) | *(no aplica — no existe un legado equivalente en macOS/Linux)* |
| 6 | `%LOCALAPPDATA%\douglas` (instalación nueva, se crea) | `~/.douglas` (instalación nueva, se crea) |

Los pasos 3-6 solo se evalúan cuando ni `DOUGLAS_HOME` ni `HERMES_HOME`
están seteadas — si cualquiera de las dos lo está, gana y no se mira el
disco.

**Nota de alcance**: en Electron, la resolución también consulta el
registro de Windows (`HKCU`) para `DOUGLAS_HOME`/`HERMES_HOME` antes del
paso 3, porque una app GUI lanzada desde el Explorador hereda el bloque de
entorno capturado en el login y no ve variables seteadas después vía
`setx` (issue #45471 de Hermes). El lado Python **no** replica esta lectura
de registro: un proceso Python siempre se lanza desde una shell con el
entorno vigente, o como hijo de Electron, que ya le pasa `HERMES_HOME`
explícito al spawnearlo (`main.ts`, sección "Explicitly pin HERMES_HOME for
the child") — el problema que la lectura de registro resuelve no existe en
ese caso.

**`ContextVar` de perfiles**: `get_hermes_home_override()` (usado por
`hermes_cli/profiles.py` para aislar perfiles) se comprueba antes que
cualquier variable de entorno y siempre gana cuando está activo. Esta
cadena solo determina el valor *por defecto* del proceso — nunca compite
con un override de perfil activo.
