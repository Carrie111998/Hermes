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
