# WMH Project — Design Tokens (Charte 2026)

Source : skill Claude `wmh-html-presentations` (usage interne uniquement — ne pas exposer hors WMH).

## Identité
- Nom : **WMH Project** (jamais "Groupe WMH Project")
- Positionnement : Global Experience Agency
- Tagline : We Make It Happen
- Valeurs : Audace. Impact. Responsabilité.

## Couleurs — noir / blanc / gris STRICT, aucun bleu

| Nom | Variable | Hex | Usage |
|---|---|---|---|
| Noir | `--wmh-black` | #000000 | Fonds titre, texte principal |
| Blanc | `--wmh-white` | #FFFFFF | Texte sur fond sombre, fonds contenu |
| Gris foncé | `--wmh-gray-dark` | #4f4e4d | Body text, titres secondaires |
| Gris moyen | `--wmh-gray-medium` | #c1c0bc | Footer, texte secondaire, séparateurs |
| Gris clair | `--wmh-gray-light` | #f6f5f4 | Fond style Corporate 2026 |

**Anti-patterns à refuser automatiquement :**
- Toute nuance de bleu (le bleu n'apparaît que dans le schéma de construction du SKWARE, jamais en usage réel)
- Anciennes palettes obsolètes : Electric (#8D50FF, #FFED00), Corporate legacy (#F56154, #86C3C7, #EAD3E8), RSE (#79AA76, #92C7D1, #BEC8C2), Bleu Électrique (#0000BA)

## Typographie — Aptos (fallback system-ui, Segoe UI)

| Élément | Taille | Graisse | Style |
|---|---|---|---|
| H1 | 96px | Light (300) | Hero uniquement |
| H2 | 60px | Bold (700) | Titres de section, UPPERCASE |
| H3 | 48px | SemiBold (600) | Sous-titres |
| Body | 18px | Regular (400) | Texte courant |
| Emphasis | 2.5rem | Black (900) | Messages forts style Manifesto |
| Tagline | 24px | Light (300) | Accroches, letter-spacing 0.05em |

**Règle interdite :** ne jamais utiliser Roboto Condensed (ancienne police, obsolète).

## Règles structurelles
- Titres en **MAJUSCULES**
- Séparateur signature : trait noir 80px × 4px sous chaque titre (blanc sur fond sombre)
- Emphase par le **gras** (700–900), jamais par la couleur
- Logo SVG **inline**, jamais en image externe ou URL

## Deux styles disponibles
- **Manifesto** (défaut client) : minimaliste, noir & blanc, centré, fort impact
- **Corporate 2026** (interne/formel) : fond gris clair, aligné gauche, accent box noire

Si le brief ne précise pas le style → Manifesto par défaut.
