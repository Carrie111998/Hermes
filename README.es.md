# Douglas Agent

<p align="center">
  <a href="https://github.com/NousResearch/hermes-agent/blob/main/LICENSE"><img src="https://img.shields.io/badge/Licencia-MIT-green?style=for-the-badge" alt="Licencia: MIT"></a>
  <a href="https://github.com/NousResearch/hermes-agent"><img src="https://img.shields.io/badge/Construido%20sobre-Hermes%20Agent-blueviolet?style=for-the-badge" alt="Construido sobre Hermes Agent"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/Lang-English-orange?style=for-the-badge" alt="English"></a>
</p>

**Douglas Agent** es un agente de IA para creación y publicación de contenido, construido sobre [Hermes Agent](https://github.com/NousResearch/hermes-agent) de [Nous Research](https://nousresearch.com), usado bajo licencia MIT.

> Douglas Agent es un producto construido *sobre* Hermes — no lo reemplaza, y no está afiliado ni respaldado por Nous Research. Todo lo que Hermes ya hace (terminal, scheduler, plataformas de mensajería, memoria, skills, plugins, aislamiento multi-perfil, y más) se hereda tal cual; Douglas añade su propia capa encima para flujos de contenido. Consulta [`CAPABILITIES.md`](CAPABILITIES.md) para el inventario completo de qué se hereda y qué es nuevo.

---

## Instalación

Douglas Agent todavía no tiene instalador propio — se instala a través de la infraestructura real de Hermes, y `douglas` funciona como un alias completo de `hermes` desde el momento en que termina la instalación (mismo binario, misma configuración, todo igual — ver [`douglas/README.md`](douglas/README.md) para cómo funciona la capa de compatibilidad).

### Linux, macOS, WSL2, Termux

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

### Windows (nativo, PowerShell)

```powershell
iex (irm https://hermes-agent.nousresearch.com/install.ps1)
```

Después de instalar:

```bash
source ~/.bashrc    # recargar la shell (o: source ~/.zshrc)
douglas             # empezar a chatear — idéntico a `hermes`
```

Los datos viven en `~/.douglas` (o `%LOCALAPPDATA%\douglas` en Windows) por defecto, con reserva a una instalación existente de `~/.hermes` si ya tienes una — ver [`douglas/README.md`](douglas/README.md) para la cadena de resolución completa. `DOUGLAS_HOME`, las variables `DOUGLAS_*`, y los overrides con forma `douglas-config.yaml` funcionan tal como se documenta ahí.

Para problemas de instalación (falsos positivos de antivirus, particularidades de Windows, notas de Termux), consulta el [README original de Hermes](https://github.com/NousResearch/hermes-agent#readme) — el instalador es idéntico, así que los mismos pasos aplican.

---

## Primeros pasos

```bash
douglas              # CLI interactivo — empezar una conversación
douglas model        # Elegir tu proveedor y modelo de LLM
douglas gateway       # Iniciar el gateway de mensajería (Telegram, Discord, etc.)
douglas setup         # Ejecutar el asistente de configuración completo
douglas doctor         # Diagnosticar problemas
```

Cada comando y flag de `hermes` funciona igual como `douglas` — consulta la [guía de CLI de Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/cli) para la referencia completa de comandos, y [`CAPABILITIES.md`](CAPABILITIES.md) para lo que ya existe antes de construir nada nuevo.

---

## Por qué construir sobre Hermes

Hermes Agent ya trae una terminal UI, un gateway de mensajería (Telegram, Discord, Slack, WhatsApp, Signal, Email), un scheduler de cron persistente, 8 proveedores de memoria, un sistema de skills/plugins compatible con [agentskills.io](https://agentskills.io), aislamiento multi-perfil, y 8 backends de ejecución (local, Docker, SSH, Singularity, Modal, Daytona, Vercel Sandbox). Douglas Agent reutiliza todo esto en vez de reconstruirlo — el directorio [`douglas/`](douglas/) contiene todo lo genuinamente nuevo (hosting de medios, publicación social, skills de contenido/marca, facturación), y [`douglas/CORE_PATCHES.md`](douglas/CORE_PATCHES.md) registra cada lugar donde Douglas tuvo que tocar código propio de Hermes, con el motivo de cada uno.

---

## Desarrollo

Misma configuración que Hermes — consulta la [Guía de Contribución de Hermes](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md) para el entorno de desarrollo completo, estilo de código, y configuración de tests. Las reglas específicas de Douglas (qué es seguro tocar, qué nunca tocar, cómo mantener `git merge upstream/main` funcionando) viven en [`douglas/README.md`](douglas/README.md) y en la sección "DOUGLAS AGENT" al final de [`AGENTS.md`](AGENTS.md).

```bash
uv pip install -e ".[all,dev]"
scripts/run_tests.sh              # suite de tests del núcleo de Hermes
pytest tests-douglas/ -q          # tests de la capa de compatibilidad de Douglas
```

---

## Comunidad y soporte

Douglas Agent todavía no tiene canales de soporte propios. Para cualquier cosa relacionada con el agente subyacente (bugs, preguntas sobre cómo funciona una característica de Hermes), el [Discord de Hermes](https://discord.gg/NousResearch) y el [rastreador de issues de Hermes](https://github.com/NousResearch/hermes-agent/issues) son el lugar correcto — por favor no reportes ahí problemas específicos del producto Douglas.

---

## Licencia

MIT — ver [LICENSE](LICENSE) y [NOTICE](NOTICE) para la atribución completa a Nous Research.
