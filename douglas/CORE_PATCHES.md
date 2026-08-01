# CORE_PATCHES.md — Registro de toques al núcleo de Hermes

Cada vez que un cambio de Douglas Agent toca un archivo fuera de `douglas/`
(fuera de la capa de producto), se anota aquí. El objetivo es poder revisar
de un vistazo toda la superficie de fricción con `upstream/main` antes de
cada intento de `git merge upstream/main`.

Formato por entrada:

```
## <ruta del archivo>
- **Motivo**: por qué fue necesario tocar el núcleo en vez de extenderlo
  desde douglas/.
- **Alternativa descartada**: qué otra forma se consideró (plugin, hook,
  wrapper) y por qué no alcanzaba.
- **Commit**: hash del commit que lo introdujo.
```

## hermes_bootstrap.py

- **Qué:** añade `normalize_douglas_env()` (y sus helpers
  `_douglas_home_candidates()` / `_resolve_default_douglas_home()`),
  llamada como la primera línea del bloque de import-time del módulo.
  Copia `DOUGLAS_<X>` → `HERMES_<X>` en `os.environ` para toda variable
  (ganando sobre una `HERMES_<X>` ya presente), y si `HERMES_HOME` sigue
  sin definir tras eso, resuelve el directorio por defecto según la cadena
  documentada en `douglas/README.md`.
- **Por qué:** es el único módulo importado primero por *todos* los entry
  points Python (`hermes`, `hermes-agent`, `hermes-acp`,
  `python -m gateway.run`, `batch_runner.py`, `cron/scheduler.py` — según
  su propio docstring), así que normalizar aquí cubre los 284 llamadores de
  `get_hermes_home()` y los ~35 sitios con fallback hardcodeado sin tocar
  ninguno de ellos — todos leen `HERMES_HOME` de `os.environ`.
- **Alternativa descartada:** (a) wrapper de shell externo — no cubre el
  desktop (Electron spawnea Python directamente, sin pasar por ningún
  wrapper) ni instalaciones vía `python -m gateway.run` directo; (b)
  `douglas/compat.py` importado desde `hermes_constants.py` — invierte la
  dirección de dependencia núcleo→capa de producto, y falla si `douglas/`
  no está en el PYTHONPATH (instalación como paquete, Docker, tests
  aislados con `sys.path` recortado).
- **Riesgo de merge:** bajo — función nueva, ~75 líneas, no modifica
  ninguna función ni línea existente del archivo, solo antepone una
  llamada nueva al bloque de efectos de import ya existente.
- **Commit:** `feat(compat): add Douglas/Hermes home and env resolution`

## apps/desktop/electron/main.ts

- **Qué:** `resolveHermesHome()` gana una rama nueva antes de cada paso
  existente: `DOUGLAS_HOME` (env), lectura de registro de
  `DOUGLAS_HOME` en Windows, y comprobación de existencia de
  `%LOCALAPPDATA%\douglas` / `~/.douglas` antes de caer en la lógica
  original de Hermes (que queda sin modificar).
- **Por qué:** el desktop no arranca desde el CLI — calcula la ruta en
  TypeScript y se la pasa explícita al proceso Python hijo al spawnearlo
  (`HERMES_HOME` pineada, ver comentario "Explicitly pin HERMES_HOME for
  the child" más abajo en el mismo archivo). Sin este espejo, el desktop
  seguiría resolviendo `~/.hermes`/`%LOCALAPPDATA%\hermes` sin saber que
  `~/.douglas`/`%LOCALAPPDATA%\douglas` existe.
- **Alternativa descartada:** ninguna — es la única forma de cubrir este
  componente, dado que no ejecuta ningún código Python antes de decidir
  la ruta.
- **Riesgo de merge:** bajo-medio — la función original queda intacta
  como fallback exacto (mismas ramas, mismo orden), solo se antepone la
  capa Douglas encima. `readWindowsUserEnvVar()` y `normalizeHermesHomeRoot()`
  ya eran genéricas por parámetro — no requirieron cambios.
- **Commit:** `feat(compat): add Douglas/Hermes home and env resolution`

## apps/bootstrap-installer/src-tauri/src/paths.rs

- **Qué:** `hermes_home()` gana la misma rama `DOUGLAS_HOME`/existencia de
  directorio douglas-nombrado antes de la lógica Hermes original (intacta
  como fallback).
- **Por qué:** el instalador nativo corre *antes* de que exista Python o
  el propio checkout de Hermes — es la tercera pieza que debe conocer la
  cadena Douglas de forma independiente, ya documentada en este archivo
  como "Mirrors hermes_constants.get_hermes_home()" antes de este cambio.
- **Alternativa descartada:** ninguna — mismo razonamiento que `main.ts`.
- **Riesgo de merge:** bajo — misma forma que el cambio en `main.ts`,
  función original preservada como fallback.
- **Commit:** `feat(compat): add Douglas/Hermes home and env resolution`

## apps/desktop/electron/main.ts (2/2 — normalización DOUGLAS_DESKTOP_*)

- **Qué:** un bloque nuevo, insertado justo antes de la resolución de
  `HERMES_HOME`, que copia cualquier `DOUGLAS_DESKTOP_<X>` presente a
  `HERMES_DESKTOP_<X>` en `process.env` (ganando sobre un valor ya
  presente). Cubre `HERMES_DESKTOP_REMOTE_URL`, `_REMOTE_TOKEN`,
  `HERMES_DESKTOP_APP_NAME`, y cualquier variable futura con ese
  prefijo, sin tocar los ~7 sitios que ya leen esas variables
  (`main.ts`, `hardening.ts`).
- **Por qué:** mismo patrón que `hermes_bootstrap.py::normalize_douglas_env()`
  en el lado Python (Paso 2) — normalizar una vez, muy al principio,
  en vez de envolver cada sitio de lectura individualmente.
- **Alternativa descartada:** envolver cada uno de los ~7
  `process.env.HERMES_DESKTOP_*` con un fallback — viola la misma
  regla de "no tocar los llamadores existentes" que motivó el diseño
  del Paso 2.
- **Riesgo de merge:** bajo — bloque nuevo de ~9 líneas, no modifica
  ninguna línea existente.
- **Commit:** `fix(brand): resolve Paso 3 follow-up items`

## apps/desktop/electron/main.ts (3/3 — URI scheme `douglas://`)

- **Qué:** `HERMES_PROTOCOL` (constante única) se reemplaza por
  `DEEP_LINK_PROTOCOLS = ['douglas', 'hermes']` — el registro con el SO
  (`app.setAsDefaultProtocolClient`) ahora ocurre para ambos schemes, y
  la detección de un deep link entrante (`_extractDeepLink`) acepta
  cualquiera de los dos. Al reconstruir un link pendiente
  (`ipcMain.handle('hermes:deep-link-ready', ...)`) se usa
  `CANONICAL_DEEP_LINK_PROTOCOL = 'douglas'` — solo se generan links
  `douglas://` desde ahora, `hermes://` queda como entrada aceptada por
  compatibilidad con enlaces existentes en docs/dashboard.
- **Por qué:** pedido explícito — "se añade, no se sustituye".
- **Alternativa descartada:** ninguna — es la forma directa de añadir
  un scheme sin romper el existente.
- **Riesgo de merge:** bajo — mismo patrón que el archivo ya usaba
  (una constante controla el comportamiento en 3 sitios), solo pasa de
  string a array e itera.
- **Commit:** `feat(protocol): register douglas:// alongside hermes:// deep links`

## apps/desktop/electron/userdata-migration.ts (nuevo) + main.ts (4/4 — migración de userData)

- **Qué:** módulo nuevo, `migrateUserDataFromLegacyHermes()`, más un
  bloque en `main.ts` que lo invoca justo antes de la primera lectura
  de `app.getPath('userData')` (línea del bloque de sandbox de
  Windows). Si el directorio nuevo (`productName` = "Douglas Agent")
  está vacío/no existe y el legado (`productName` = "Hermes", mismo
  padre vía `app.getPath('appData')`) tiene datos, **copia** (nunca
  mueve) el contenido completo — preservando el modo de cada archivo,
  crítico para `native-oauth-tokens.json` — y escribe un marcador
  `.migrated-from-hermes` con qué se copió y cuántos archivos. Si la
  copia falla a mitad de camino, no se escribe el marcador (permite
  reintento en el próximo arranque) y `main.ts` muestra un
  `dialog.showErrorBox` en el primer tick de `app.whenReady()` — antes
  de `createWindow()` — explicando qué pasó y dónde sigue estando el
  dato original (nunca se borra ni se mueve).
- **Por qué:** hallazgo de revisión pre-merge del Paso 3 —
  `productName` cambió de `"Hermes"` a `"Douglas Agent"` en
  `4c8da5049`, y Electron resuelve `userData` por defecto como
  `path.join(app.getPath('appData'), productName)`. Sin este parche,
  cualquier usuario existente pierde silenciosamente
  `connection.json`, `window-state.json`, `active-profile.json`,
  `native-oauth-tokens.json`, etc. — el mismo directorio HERMES_HOME
  ya tenía cadena de compatibilidad (Pasos 2/3 arriba); `userData` de
  Electron (un concepto totalmente distinto — vive bajo
  `appData`/Roaming, no bajo `LOCALAPPDATA`/`HERMES_HOME`) no tenía
  ninguna.
- **Decisión de diseño (pedida explícitamente):** migrar, no fijar
  (`app.setPath` apuntando para siempre al nombre viejo) — anclarse a
  "Hermes" a perpetuidad es deuda permanente. La ruta legado se
  calcula con `path.join(appDataPath, 'Hermes')` donde `appDataPath`
  viene de `app.getPath('appData')` (la propia resolución de
  Electron) — el módulo no contiene un solo literal de ruta específico
  de plataforma (`%APPDATA%`, `Application Support`, `.config`); por
  construcción no tiene ramas condicionadas a la plataforma que
  probar por separado.
- **`apps/bootstrap-installer`:** no tiene un `userData` propio al
  estilo Electron — su equivalente (`hermes_home()` en `paths.rs`) ya
  usa la cadena Douglas/Hermes desde el Paso 3 y no deriva de
  `productName`/`identifier` de Tauri (que, de hecho, siguen sin
  rebrandear: `tauri.conf.json` todavía dice `"productName": "Hermes"`).
  Nada que migrar ahí todavía.
- **Alternativa descartada:** `app.setPath('userData', <ruta vieja>)`
  al arrancar — descartada explícitamente por decisión de producto
  (ver arriba); habría evitado el problema pero fijado el nombre
  "Hermes" en el disco de todo usuario nuevo para siempre.
- **Riesgo de merge:** bajo — archivo nuevo aislado + ~30 líneas
  insertadas en dos puntos de `main.ts` (antes del bloque de sandbox
  de Windows, y al inicio de `whenReady().then()`); no modifica
  ninguna lectura de `userData` existente.
- **Tests:** `apps/desktop/electron/userdata-migration.test.ts` — legado
  con datos + nuevo vacío → migra; ambos con datos → no toca nada;
  ninguno con datos → instalación limpia; marcador ya presente →
  idempotente; fallo de copia a mitad → error reportado, legado
  intacto, sin marcador. Ejecutado y verificado manualmente con
  `node --experimental-strip-types` (el `node_modules` de este
  checkout está roto — ver nota de reinstalación aparte — así que
  `npm run test:desktop:platforms` no pudo confirmarse en esta
  sesión; recomendado correrlo tras la reinstalación limpia).
- **Commit:** *(pendiente — sin commit todavía en esta sesión)*

## Corrección — `DOUGLAS_DESKTOP_REMOTE_URL` (verificación, no bug)

Un reporte previo de esta sesión afirmó que
`process.env.DOUGLAS_DESKTOP_REMOTE_URL` nunca se lee y que el texto
de ayuda en `hardening.ts` (líneas ~68/81) era engañoso. Eso era
**incorrecto** — producto de un `grep` sobre el literal
`DOUGLAS_DESKTOP_REMOTE_URL` que no encontró el normalizador genérico
de `main.ts:517-521` (`key.startsWith('DOUGLAS_DESKTOP_')`), documentado
arriba en "Paso 3 (2/2)". Verificado de nuevo línea por línea: el
bloque corre en el top-level del módulo, antes de cualquiera de los
~7 sitios que leen `process.env.HERMES_DESKTOP_*` (el primero está a
~6900 líneas de distancia) — `DOUGLAS_DESKTOP_REMOTE_URL` sí funciona,
mapeado a `HERMES_DESKTOP_REMOTE_URL` antes de que nada lo consuma. El
texto de ayuda es correcto tal cual está. No se tocó nada aquí.

## apps/desktop/electron/main.ts (5 — AppUserModelId)

- **Qué:** `app.setAppUserModelId('com.nousresearch.hermes')` →
  `'com.douglasdevsec.douglas-agent'`, con comentario explicando por
  qué (alinea con `build.appId`/`executableName`/nombre de acceso
  directo NSIS, que ya habían cambiado).
- **Por qué:** hallazgo de auditoría — dejar el AUMID viejo mientras
  el exe, el `appId` y el nombre del acceso directo ya cambiaron era
  incoherente, no protector: un acceso directo nuevo se crea de todas
  formas sin agrupar con uno viejo, porque apunta a un `.exe` con
  nombre distinto independientemente del AUMID.
- **Qué rompe (decisión consciente, no un bug):** un usuario que
  actualiza desde una instalación Hermes pierde el agrupamiento de la
  barra de tareas / jump list / permisos de notificación del icono
  anclado viejo — se resetean, hay que volver a anclar. No hay pérdida
  de datos (esto no toca `userData` ni `HERMES_HOME`).
- **Riesgo de merge:** trivial — una constante de tipo string.
- **Commit:** *(pendiente)*

## apps/desktop/electron/main.ts (6 — blindaje de `safeStorage.decryptString()`)

- **Qué:** `decryptDesktopSecret()` gana un parámetro `context` y,
  en el `catch` de `safeStorage.decryptString()` (antes silencioso,
  `catch { return '' }`): registra vía `rememberLog` qué secreto
  falló y por qué, y dispara `notifyCredentialDecryptFailure()` — una
  función nueva que muestra, una sola vez por sesión (deduplicada con
  un flag de módulo), `dialog.showErrorBox('Douglas Agent', 'Tus
  credenciales guardadas no pudieron leerse tras la actualización.
  Vuelve a conectar tus cuentas.')`, en cuanto la app está lista
  (inmediato si `app.isReady()`, encolado en `app.whenReady()` si no).
  Los 8 sitios que llaman a `decryptDesktopSecret()` ahora pasan una
  etiqueta de contexto (`'native OAuth tokens (<url>)'`, `'remote
  gateway token'`, `'SSH token (profile <p>)'`, etc.) para que el log
  diga cuál credencial fue.
- **Por qué:** decisión explícita de producto tras el hallazgo de
  `safeStorage`/Keychain (ver `douglas/README.md`, "Verificar en
  hardware real" #1) — **no** intentar migrar la clave del llavero
  (no verificable sin hardware macOS/Linux; adivinar sería peor que no
  hacer nada), sino blindar el fallo: nunca crash, nunca estado
  corrupto, siempre tratado como "no autenticado" (el retorno `''` ya
  hacía esto en los 8 call sites, sin cambios ahí), mensaje explícito
  en vez de un error genérico, y el archivo cifrado nunca se borra —
  puede volver a descifrar si el usuario revierte de versión.
- **Alternativa descartada:** intentar re-encriptar/migrar la clave
  automáticamente — descartada explícitamente por decisión de
  producto: no hay forma de verificar que funcione sin el hardware, y
  un intento de migración fallido silenciosamente sería peor que el
  mensaje explícito.
- **Riesgo de merge:** bajo — el `catch` ya existía y ya devolvía
  `''`; el cambio es puramente aditivo (logging + una notificación
  deduplicada), ningún call site cambia su manejo del valor de
  retorno.
- **Verificación pendiente:** no se puede confirmar en esta sesión
  que el fallo de descifrado realmente ocurre en macOS/Linux tras el
  rebrand — ver `douglas/README.md`, "Verificar en hardware real" #1.
- **Commit:** *(pendiente)*
