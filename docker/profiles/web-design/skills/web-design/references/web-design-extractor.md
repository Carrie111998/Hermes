# Web Design Extractor — charte client (méthodologie principale)

Transposition du pattern `pptx-design-extractor` (skill Claude interne) au web. Usage principal
de l'agent : le client fournit une charte à respecter (site existant, Figma, PDF de brand
guidelines) — l'agent doit en extraire le VRAI système de design, pas se contenter du déclaratif.

## Le principe : theme ≠ brand

Un site déclare des design tokens (`:root { --color-primary: ... }`, config Tailwind, Figma
Styles publiés). Dans la majorité des cas réels, ces tokens déclarés **ne recoupent pas** les
valeurs réellement utilisées dans le CSS de production — la marque vraie vit dans l'usage
fréquent, pas dans le nommage propre du design system.

> Toujours réconcilier le thème déclaré avec l'usage de-facto, synthétisé par fréquence.
> En cas de divergence, **l'usage de-facto fait foi**. Générer depuis le thème déclaré seul
> produit un livrable qui ne ressemble pas au site source.

Validé empiriquement sur mozilla.org (test du 01/07/2026) : les couleurs de marque les plus
fréquentes dans le CSS réel (`#0060df`, `#d70022`) n'apparaissaient dans AUCUN token `:root`
déclaré — seuls noir/blanc/gris génériques étaient couverts par le thème officiel.

## Phase 1 — Analyse (source = site live)

```bash
python3 scripts/extract_web_design.py <url> [<url2> ...] --out <stem>-charte
```

Fetch statique (HTML + `<link rel=stylesheet>` + `<style>` inline), parsing CSS via `tinycss2`.
Aucune dépendance navigateur — fonctionne dans le conteneur Hermes sans Playwright.

Produit dans `<stem>-charte/` :

| Fichier | Contenu |
|---|---|
| `CHARTE.md` | Réconciliation déclaré/de-facto, palette de-facto, typo de-facto, tailles de titres observées, limites méthodologiques |
| `design-tokens.json` | Version machine-readable, y compris `reconciliation.brand_colors_missing_from_declared_theme` |
| `palette.svg` | Swatch visuel des couleurs de-facto |

**Lire `CHARTE.md` en premier.** Ancrer toute décision de génération sur la palette **de-facto**
(`defacto.colors`), jamais sur `declared_theme` seul.

### Limite connue — sites JS-heavy / CSS-in-JS dynamique
L'extraction statique ne voit pas les styles injectés à l'exécution (styled-components avec
props dynamiques, CSS-in-JS runtime). Si `defacto.colors` semble pauvre (<5 couleurs distinctes
sur un site visuellement riche), c'est le signal : prévoir une passe complémentaire par capture
d'écran + lecture visuelle directe (Playwright screenshot → analyse par le sous-agent vision)
plutôt que de considérer la charte CSS comme définitive.

## Phase 1 bis — Analyse (source = Figma)

Pas de MCP Figma côté Hermes → appel REST direct avec token perso :

```
GET https://api.figma.com/v1/files/:file_key                → styles publiés (le "déclaré")
GET https://api.figma.com/v1/files/:file_key/nodes?ids=...   → fills/strokes réels par node
```

Même logique de réconciliation : compter la fréquence des `fills[].color` réels sur l'ensemble
des nodes vs. les `styles` publiés référencés. Si un node utilise un fill littéral au lieu d'un
style publié de façon récurrente, c'est la même divergence théorique que sur du CSS.

## Phase 1 ter — Analyse (source = PDF brand guidelines)

Cas le plus simple : le PDF déclare généralement fidèlement la charte (pas de divergence
déclaré/de-facto possible, il n'y a pas d'"usage réel" à observer). Extraire directement via le
skill `pdf` (public) : couleurs des swatches, polices nommées, règles d'usage du logo. Traiter
ce cas comme fiable sans réconciliation — mais si un site web du même client existe, croiser
quand même avec Phase 1 pour vérifier que le site respecte son propre PDF (souvent faux).

## Phase 2 — Génération "à la charte"

| Situation | Stratégie |
|---|---|
| Site source accessible, structure de page réutilisable | **Cloner les pages/composants sources** comme gabarits (récupérer markup + classes CSS réelles) |
| Pas de source clonable (PDF seul, ou nouveau microsite) | **Reconstruire depuis `design-tokens.json`** (palette + typo de-facto) — documenter ce qui ne sera pas transféré (gradients, effets complexes) |

Charger `design-principles.md` en complément pour le jugement esthétique (structure, motion,
restraint) — la charte extraite donne les tokens, pas le goût.

## QA avant livraison
- Comparer visuellement le rendu généré au site source (screenshot side-by-side si Playwright
  disponible)
- Passer `a11y-checklist.md` sur le résultat final, indépendamment de la charte client
