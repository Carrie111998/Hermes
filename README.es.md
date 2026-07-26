<p align="center">
  <img src="assets/iyari-logo-completo.png" alt="IYARI — tu aliado en cada decisión" width="420">
</p>

<p align="center">
  <a href="https://iyari.io"><img src="https://img.shields.io/badge/web-iyari.io-orange" alt="iyari.io"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/licencia-MIT-green" alt="Licencia MIT"></a>
  <img src="https://img.shields.io/badge/creado%20por-Digital%20Services%20LLC-blue" alt="Creado por Digital Services LLC">
</p>

# IYARI

**Tu aliado en cada decisión.**

IYARI es un agente de IA que mejora a sí mismo, creado por **Digital Services LLC**. Crea habilidades a partir de su experiencia, las mejora con el uso, recuerda lo que aprende entre sesiones, busca en sus propias conversaciones pasadas y construye un modelo cada vez más profundo de quién eres.

Funciona en un VPS de 5 dólares, en un clúster de GPUs o en infraestructura serverless que apenas cuesta nada cuando está en reposo. No está atado a tu portátil: háblale desde Telegram mientras trabaja en una VM en la nube.

IYARI es un fork de [Hermes Agent](https://github.com/NousResearch/hermes-agent), creado originalmente por Nous Research, y desarrollado y mantenido de forma independiente por Digital Services LLC bajo la licencia MIT.

## Usa el modelo que quieras

Nous Portal, OpenRouter, OpenAI, DeepSeek, Kimi, tu propio endpoint y muchos más. Cambia con `hermes model` — sin tocar código, sin ataduras.

## Inicio rápido

```bash
git clone https://github.com/digital-services-llc/iyari.git
cd iyari
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv && uv pip install -e ".[all]"
hermes setup
```

> **Nota:** durante la transición de marca, el comando CLI sigue siendo `hermes`. Todo lo demás — documentación, web, marca — es IYARI.

## Documentación

📖 Documentación completa: **[iyari.io](https://iyari.io)**

## Comunidad

- 🐛 [Issues](https://github.com/digital-services-llc/iyari/issues) — errores y peticiones de funciones, aquí en nuestro repo
- 🌐 [iyari.io](https://iyari.io)
- ✉️ team@iyari.io

## Licencia

MIT — ver [LICENSE](LICENSE).

Creado originalmente por [Nous Research](https://nousresearch.com/). Fork mantenido por **Digital Services LLC** — © 2026.

*Iyari — contigo cada día.*
