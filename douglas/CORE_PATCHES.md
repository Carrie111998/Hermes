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
