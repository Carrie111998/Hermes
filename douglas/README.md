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

## Procedimiento seguro para worktrees de verificación

Incidente (2026-08-01): al comparar el comportamiento de un commit base
contra `HEAD` en un `git worktree` temporal, 44 archivos rastreados de
`apps/bootstrap-installer/` y `ui-tui/packages/hermes-ink/` aparecieron
como borrados en el checkout **principal** tras limpiar el worktree
(`git worktree remove --force` + `rm -rf`). Recuperados sin pérdida
(`git restore`), verificado con `git diff --stat HEAD` completo contra
todo el repo.

**Causa raíz: no confirmada.** Se investigó activamente, dos intentos de
reproducción fiel y aislada (clon desechable, misma secuencia exacta de
comandos) en un directorio disponible no reprodujeron el borrado. Lo que
sí se confirmó, de forma reproducible:

- `ln -s` en este entorno (Windows + Git Bash/MSYS) **no crea symlinks
  POSIX reales** — crea junctions/reparse points de NTFS. `stat` los
  reporta como `directory`, no como `symlink`; `rm -f` se niega a
  tocarlos ("Is a directory"); el comportamiento de `git worktree
  remove` sobre ellos fue inconsistente entre intentos (un error
  "Directory not empty" en la ejecución real, éxito silencioso en la
  reproducción aislada).
- El propio checkout estaba siendo usado activamente en paralelo
  (`npm ci`, pruebas de arranque) mientras ocurrió el incidente — una
  causa externa concurrente no puede descartarse y es tan plausible
  como el mecanismo de symlinks.

Dado que no se pudo aislar la causa exacta pero el mecanismo de symlink
es demostrablemente poco fiable (comportamiento distinto según la
herramienta que lo toque) independientemente de si fue la causa real
aquí:

**Prohibición:** no crear symlinks (`ln -s`) para compartir
`node_modules` — ni entre worktrees, ni entre clones, ni de ningún
tipo — en este repositorio en Windows. Si dos checkouts necesitan el
mismo `node_modules`, cada uno instala el suyo (`npm ci`), o se usa
`NODE_PATH` para apuntar Node a una instalación externa sin crear
ningún enlace en el sistema de archivos.

**Procedimiento para verificar un commit base (p. ej. antes/después de
un rebrand) sin worktree, cuando alcanza:**
1. `git show <commit>:<ruta>` / `git diff <base> <head> -- <ruta>` para
   comparar código fuente línea por línea — cubre la mayoría de
   preguntas de "¿esto cambió?" sin tocar el filesystem en absoluto.
2. Si hace falta *ejecutar* código del commit base (no solo leerlo):
   `git clone` a un directorio nuevo y desechable (no un worktree del
   mismo repo) e instalar dependencias ahí de forma independiente
   (`npm ci` completo, sin symlinks). Es más lento pero no comparte
   nada con el checkout principal — un error de limpieza ahí no puede
   tocar el repo real.
3. Verificar `git status --short` y `git diff --stat HEAD` en el
   checkout **principal** inmediatamente después de cualquier operación
   de worktree o clon temporal, antes de continuar con cualquier otra
   cosa — para detectar un problema como este de inmediato, no varios
   pasos después.
4. `git worktree add`/`remove` siguen permitidos para uso de **solo
   lectura** (inspeccionar archivos, `git diff` dentro del worktree) —
   la prohibición es específicamente sobre symlinks hacia
   `node_modules` y sobre ejecutar builds/tests dentro de un worktree
   compartiendo estado con el checkout principal.

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

### Si la app arranca y muere: `dist/` desfasado

`apps/desktop/dist/` (el build de producción del renderer) ya está en
`.gitignore` — nunca fue un problema de git. El problema real: **`npm run
dev` nunca lee `dist/`** (el renderer se sirve en vivo desde Vite), así que
un `dist/` reconstruido una vez y luego abandonado puede quedarse desfasado
días sin que nada lo note — hasta que alguien arranca en modo empaquetado
(un build real, o `HERMES_DESKTOP_IS_PACKAGED=1 electron .` para probar ese
código a mano, como en la sesión que persiguió esto) y el bundle viejo
referencia algo que el código fuente actual ya no tiene. El síntoma es
engañoso: el backend arranca bien, el renderer es el que revienta —
"parece" que el gateway no arranca.

**Si la app arranca y muere:**
```bash
cd apps/desktop && npm run build
```
luego reintenta. `npm run pack`/`npm run dist*` ya hacen esto automáticamente
(`"pack": "npm run build && npm run builder -- --dir"`) — un build empaquetado
real nunca puede quedar desfasado así. Solo afecta checkouts locales
arrancados en modo empaquetado sin pasar por esos scripts.

**Guardia añadida:** `apps/desktop/electron/main.ts`
(`warnIfRendererBundleStale()`) compara, cada vez que se resuelve
`resolveRendererIndex()` sin servidor de desarrollo, la fecha de
`dist/index.html` contra el archivo `.ts`/`.tsx` más reciente bajo `src/`. Si
`src/` es más nuevo por más de 5 minutos, escribe una advertencia clara en
consola y en `desktop.log` — nunca bloquea el arranque, es solo una pista.
Deliberadamente gateado en el `app.isPackaged` **real** de Electron, no en el
`IS_PACKAGED` combinado — un build realmente empaquetado nunca necesita el
aviso (`dist/` siempre se reconstruye fresco al empaquetar), pero
`HERMES_DESKTOP_IS_PACKAGED=1` (el override manual) sí debe seguir
disparándolo, porque ese override es exactamente el patrón que causó esto.

### Fuente de marca

El wordmark (`assets/logo.svg`) usa **Dimitri Swank Normal** — los TTF
llegaron después de que se escribiera esta nota; los glifos se extrajeron con
`fontTools` (`SVGPathPen`) y quedaron como paths ya trazados en el SVG, no
como texto en vivo con `@font-face`, así que no dependen de que la fuente
esté instalada en la máquina que renderiza el archivo. El resto de la
cabecera y los títulos de la UI del desktop siguen en **Space Grotesk** (SIL
OFL, gratuita) — Dimitri no se aplicó ahí; sigue siendo trabajo pendiente si
se quiere unificar.

### Iconografía e ilustraciones

- **`BrandMark`** (`apps/desktop/src/components/brand-mark.tsx`,
  `apps/bootstrap-installer/src/components/brand-mark.tsx`): ya no es "DA"
  tipográfico — ambas copias usan el mismo PNG (`logo_white.png`, línea
  blanca, fondo transparente) sobre el mismo tile verde esmeralda que tenía
  el placeholder, importado localmente en cada app
  (`src/assets/brand/logo_white.png`).
- **Ícono real de la app** (`apps/desktop/assets/icon.{ico,icns}`, usado en
  el `.exe`/`.app`/taskbar vía `electron-builder`) y los íconos del
  instalador Tauri (`apps/bootstrap-installer/src-tauri/icons/icon.{ico,icns}`):
  regenerados con la misma marca (ya no "DA"). El `.icns` sigue escrito a
  mano (mismo layout ic07–ic14 basado en PNG, sin `icnsutil`) — ver
  "Verificar en hardware real" más abajo antes de firmar/notarizar para
  macOS.
- **Favicon** (`apps/desktop/public/apple-touch-icon.png`): regenerado igual,
  180×180.
- No hay ícono de bandeja del sistema (`Tray`) ni pantalla de splash como
  imagen separada — el overlay de arranque es React, no un asset — así que
  no hay nada que cablear ahí todavía.
- **Mascota "petdex" de Hermes** (`apps/desktop/public/{hermes.png,
  hermes-sprite.png,hermes-frames/}` — un personaje pixel-art con casco alado
  y caduceo): eliminada — se confirmó que ningún componente la referenciaba.

### Instalador NSIS / identidad `appId` sin resolver

`apps/desktop/package.json`'s `build.appId` cambió de
`com.nousresearch.hermes` a `com.douglasdevsec.douglas-agent`. Windows
"Agregar o quitar programas" y la clave de desinstalación de NSIS están
indexadas por ese `appId` — un instalador Douglas no reconoce una instalación
Hermes previa como "la misma app": queda como una entrada separada (segundo
directorio de instalación, segundo acceso directo de Start Menu) en vez de
actualizar en el lugar. No es pérdida de datos (`HERMES_HOME`/el backend
compartido se resuelven igual desde cualquiera de las dos), es duplicación de
disco y confusión de usuario. **Deliberadamente sin tocar** — va junto con el
renombrado de `hermes-setup` (`installer_dest()` en
`apps/bootstrap-installer/src-tauri/src/paths.rs`, todavía literalmente
`hermes-setup.exe`) en la sesión previa a la primera release pública, no
antes.

## Verificar en hardware real

Cosas que este entorno de desarrollo (Windows, sin macOS/Linux disponibles en
la sesión que las tocó) no puede confirmar por sí mismo. No asumir que
"pasó la revisión de código" equivale a "verificado" para ninguno de estos
cuatro — bloquear la primera release pública hasta correrlos en el hardware
real.

### 1. `safeStorage` en macOS (Keychain) y Linux (libsecret)

`safeStorage.decryptString()` (`apps/desktop/electron/main.ts`,
`decryptDesktopSecret()`) es una llamada nativa al almacén de credenciales
del SO. En Windows usa DPAPI, ligado a la cuenta de usuario — inmune al
rebrand. En macOS/Linux, Electron documenta que el lookup queda ligado a la
identidad de la app (bundle id / nombre de app), que sí cambió
(`com.nousresearch.hermes` → `com.douglasdevsec.douglas-agent`). Hipótesis
sin verificar: un `native-oauth-tokens.json` o un token de gateway remoto
cifrados por un build viejo (identidad Hermes) pueden no descifrar bajo la
identidad nueva.

**El código ya asume que esto puede fallar** — todo fallo de
`decryptString()` se captura, se registra vía `rememberLog` con el contexto
específico (qué secreto, qué perfil/URL), nunca vuelve a lanzar, se trata
como "no autenticado" (mismo camino que "nunca inició sesión"), y dispara una
vez por sesión `dialog.showErrorBox` con el texto exacto: *"Tus credenciales
guardadas no pudieron leerse tras la actualización. Vuelve a conectar tus
cuentas."* El archivo cifrado nunca se borra en el fallo — sigue disponible
si el usuario revierte a una versión anterior.

**Qué falta verificar en hardware real:** instalar un build viejo (identidad
Hermes) en un Mac y en una máquina Linux, iniciar sesión / guardar un token
remoto, actualizar al build Douglas, y confirmar (a) si de verdad no
descifra, y (b) si no descifra, que aparece el diálogo exacto de arriba y la
app sigue arrancando con normalidad (no crashea, no queda en un estado a
medias).

### 2. Validez del `.icns` para firma/notarización de macOS

`apps/desktop/assets/icon.icns` y la copia de
`apps/bootstrap-installer/src-tauri/icons/icon.icns` están escritos a mano
(sin `icnsutil`/`iconutil`) — verificados byte a byte en esta sesión (magic
`icns`, longitud total, framing TLV de cada entrada, CRC de cada PNG interno,
dimensiones correctas para cada OSType `ic07`–`ic14`, todos RGBA de 8 bits).
Eso confirma que el **contenedor** es válido; no confirma que `codesign`/
`notarytool` lo acepten sin quejarse — eso solo se sabe firmando de verdad en
una Mac. Falta también `ic04`/`ic05` (16×16/32×32 legado) — Finder debería
poder reescalar desde `ic07`/`ic11`, pero no se ha visto renderizado en un
Finder real.

**Qué falta verificar:** `codesign --verify` y `xcrun notarytool submit` (o
el paso equivalente de `electron-builder`'s `afterSign`) contra un build
real de macOS, y una revisión visual del ícono en Finder/Dock/Launchpad a
varios tamaños.

### 3. Migración de `userData` en las tres plataformas

La lógica de migración (`apps/desktop/electron/userdata-migration.ts`) está
cubierta por tests unitarios que corren contra el filesystem real de
cualquier SO que ejecute la suite — pero esta sesión solo tuvo Windows
disponible. El diseño deliberadamente no tiene ramas condicionadas a la
plataforma (usa `app.getPath('appData')` de Electron en vez de literales por
SO), así que no hay lógica *distinta* por plataforma que pueda estar rota de
forma distinta — pero eso en sí es una suposición que vale la pena confirmar
con una migración real de principio a fin en cada plataforma.

**Qué falta verificar:** en macOS y Linux, instalar un build viejo
(identidad Hermes), generar datos reales de usuario (conexión guardada,
estado de ventana, sesión OAuth nativa), actualizar al build Douglas, y
confirmar que `~/Library/Application Support/Douglas Agent/` (macOS) y
`~/.config/Douglas Agent/` (Linux) terminan con los archivos migrados, el
marcador `.migrated-from-hermes`, y — específicamente — que
`native-oauth-tokens.json` conserva su modo de archivo (`0600`) tras la
copia, no solo el contenido.

### 4. Tests POSIX (`termios`/`tty`/`pty`) que no corren en Windows

No es un bug — `termios`, y las partes de `tty`/`pty` que dependen de él,
son módulos de la librería estándar de Python que no existen en Windows.
Cualquier test que los importe se salta o falla en un checkout Windows por
definición, nunca en verde. La señal local en esta plataforma está
contaminada para esos tests específicamente; no usarla como confirmación de
que pasan. **CI con runner Linux es la fuente de verdad** para estos —
Hermes ya trae 21 workflows en `.github/workflows/`, probablemente solo haga
falta activarlos en el fork antes de confiar en la señal verde/roja de un
PR que los toque.

## Riesgos abiertos

Índice de todo lo pendiente de verificar antes de la primera release
pública, consolidado en un solo lugar. Cada uno tiene su detalle completo
en la sección enlazada — esto es solo el resumen de una línea.

1. **Cadena de resolución (`HERMES_HOME`/`DOUGLAS_HOME`) sin verificar en
   máquina limpia real.** Verificada con una réplica fiel del algoritmo
   (misma función `readWindowsUserEnvVar` real, registro simulado como
   ausente sin tocar `HKCU\Environment`) — las tres ramas
   (solo `~/.douglas`, solo `~/.hermes`, ninguna) pasan. Pero eso es una
   réplica de la lógica, no la app real arrancando en una máquina que de
   verdad no tenga el valor de registro heredado. Sigue sin confirmarse en
   una instalación nueva de verdad.
2. **`safeStorage` en macOS (Keychain) y Linux (libsecret) sin verificar.**
   Ver "Verificar en hardware real" #1. El blindaje (try/catch, mensaje
   explícito, nunca borra el archivo) está en el código; que el fallo de
   descifrado realmente ocurra tras el rebrand no se ha confirmado en
   hardware real.
3. **Causa del incidente del worktree sin confirmar.** Ver "Procedimiento
   seguro para worktrees de verificación". Dos intentos de reproducción
   aislada no lo reprodujeron; el mecanismo de symlink quedó prohibido por
   ser demostrablemente poco fiable, no porque se haya probado que fue la
   causa.
4. **Instalador NSIS / identidad `appId` sin resolver.** Ver
   "Instalador NSIS / identidad appId sin resolver". Deliberadamente sin
   tocar — va junto con el renombrado de `hermes-setup` antes de la
   primera release pública.
5. **Wake word sigue diciendo "hey hermes".** Ver "Wake word sigue diciendo
   'hey hermes'". El texto de configuración es cosmético; el motor
   (`openWakeWord`) tiene un modelo entrenado específicamente para ese
   patrón acústico — cambiarlo sin retrenar no cambiaría lo que el motor
   realmente escucha.
6. ~~La versión muestra v0.19.1 pero package.json dice 0.17.0~~ —
   **investigado, no es un riesgo.** `pyproject.toml` y
   `hermes_cli/__init__.py` dicen `0.19.1` (coincide con lo que muestra la
   barra de estado); `apps/desktop/package.json` dice `0.17.0`
   deliberadamente sin sincronizar — `resolveHermesVersion()` en
   `main.ts` ya documenta por qué: el panel "Acerca de" muestra a
   propósito la versión canónica de Python (la que `release.py` incrementa),
   no la del `package.json` del Electron, que históricamente se
   desincronizaba (se quedó atascada en `0.0.2`). Comportamiento
   intencional, documentado en el propio código, no un hueco de esta
   sesión.

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
