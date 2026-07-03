# Référentiel design systems — vocabulaire et conventions

## shadcn/ui + Radix (défaut produit)
- Composants headless Radix + styling Tailwind
- Spacing scale Tailwind standard : base 4px (increments 0.25rem)
- Un seul token `--radius`, cohérent sur tout le système
- À utiliser par défaut sur tout livrable "outil interne / dashboard / produit" — cohérent avec la stack WMH existante (Vite + React + TS + shadcn/ui + Tailwind + Supabase + Netlify)

## Apple HIG (si univers iOS/produit mobile)
- Hiérarchie typographique SF Pro, densité d'info faible
- Touch target minimum 44×44pt

## Material 3 (si univers Android/Google-adjacent)
- Elevation via ombres tokenisées, pas de bordures dures
- Dynamic color (Material You) : non pertinent hors écosystème Google, ne pas appliquer par défaut

## Règle d'arbitrage pour l'agent
| Type de livrable | Design system | Fichier à charger en complément |
|---|---|---|
| Outil interne / dashboard WMH | shadcn/ui + Tailwind | `wmh-brand.md` si front-office client |
| Site vitrine / manifeste éditorial | Aucun (composition libre) | `design-principles.md` seul, sans contrainte de composants |
| Landing page client tiers (charte imposée) | Extraction forensique de la charte fournie | brief client — pas de fichier statique, analyse au cas par cas |

Ne jamais imposer shadcn/ui sur un site éditorial/créatif : le design system produit brise l'exigence d'unicité de `design-principles.md` §2.
