---
name: geo-test-tanorient
description: "Test GEO mensuel de tanorient.com : poser la question de référence à 4 moteurs IA avec recherche web (Perplexity, GPT en ligne, Gemini via OpenRouter + Claude via API Anthropic), noter si Tan Services est cité, à quel rang, avec les bonnes marques, et consigner la grille dans le journal wiki. Déclencher pour : test GEO, visibilité IA, 'que disent les IA de Tan Services', citation dans ChatGPT/Perplexity/Gemini, suivi GEO mensuel."
version: 1.0.0
author: WMH Project
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [geo, ai-visibility, tanorient, monthly, openrouter, seo-geo]
---

# geo-test-tanorient

Test GEO mensuel : mesurer comment les assistants IA décrivent **Tan Services**
sur le marché émirati. Avant de commencer : `git -C /opt/data/wiki pull --rebase`
et relis la dernière grille GEO de `tan-seo/journal.md` (baseline 03/08/2026 :
Gemini cite Tan **en premier** sans erreur ; ChatGPT ne le cite pas).

## La question — EXACTE, en anglais, sans variation

> I live in Dubai and I'm looking to buy a sailing catamaran. Who are the main
> dealers and brokers in the UAE, and which brands does each one represent?

Chaque appel est **sans historique ni system prompt orienté** (un seul message
user) — on simule un acheteur, pas notre agent.

## Les 4 moteurs (approximés par API, recherche web active)

⚠️ **Clés** : `OPENROUTER_API_KEY`/`ANTHROPIC_API_KEY` sont volontairement
invisibles dans le shell (blocklist des subprocess terminal, sécurité Hermes).
Ce skill utilise les **miroirs dédiés** `$GEO_TEST_OPENROUTER_KEY` et
`$GEO_TEST_ANTHROPIC_KEY` (vars Railway, référencées sur les originales).
S'ils sont absents : ne simule RIEN, rapporte « clés GEO manquantes » et stop.

3 via OpenRouter (`$GEO_TEST_OPENROUTER_KEY`, `POST https://openrouter.ai/api/v1/chat/completions`) :

| Moteur simulé | `model` |
|---|---|
| Perplexity | `perplexity/sonar-pro` |
| ChatGPT + browsing | le modèle OpenAI courant avec suffixe `:online` (ex. `openai/gpt-5.2:online` — vérifie l'id du jour sur https://openrouter.ai/models, prends le flagship chat) |
| Gemini | le flagship `google/gemini-*` courant avec suffixe `:online` |

Demande dans chaque cas les **sources/citations** (OpenRouter renvoie les
annotations de recherche web ; si un moteur répond sans citations, note-le :
mode dégradé, résultat moins fiable).

Claude via l'API Anthropic (`$GEO_TEST_ANTHROPIC_KEY`) avec l'outil serveur
`web_search` :

```bash
curl -s https://api.anthropic.com/v1/messages \
  -H "x-api-key: $GEO_TEST_ANTHROPIC_KEY" -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" -d '{
  "model": "claude-opus-5", "max_tokens": 2048,
  "tools": [{"type": "web_search_20250305", "name": "web_search"}],
  "messages": [{"role": "user", "content": "<LA QUESTION>"}]}'
```

## Grille de notation — par moteur

| Critère | Valeur |
|---|---|
| Tan Services cité ? | oui/non |
| Rang de citation | 1er / 2e / … / absent |
| Les 5 marques exactes ? (Lagoon, Beneteau, Dragonfly, Y Yachts, Omikron) | exact / partiel (lesquelles manquent) / erroné |
| **Neel ou Fountaine Pajot attribués à Tan ?** (erreurs héritées YachtWorld) | oui 🔴 / non |
| Adresse correcte ? (Ras Al Khaimah — pas Dubaï) | oui/non |
| tanorient.com en source ? | oui/non |
| Concurrents cités (Eden Yachting, Royal Yachting…) | liste |

## Sortie

- **Journal wiki** : nouvelle section `## Baseline GEO (JJ/MM/AAAA)` dans
  `tan-seo/journal.md` avec la grille complète + 2-3 verbatims courts par
  moteur, puis commit/push. Sans point de comparaison daté, le test suivant ne
  veut rien dire.
- **Rapport Discord** : synthèse par moteur (cité ? rang ? erreurs ?) +
  l'évolution vs le mois précédent. **Alerte explicite 🔴 si régression** (Tan
  disparaît d'un moteur où il était cité, ou Neel/Fountaine Pajot réapparaît).
- Précise toujours dans le rapport que c'est une **approximation par API** des
  produits grand public — les interfaces réelles peuvent différer ; le test
  manuel de Gilles reste la référence en cas de doute.
