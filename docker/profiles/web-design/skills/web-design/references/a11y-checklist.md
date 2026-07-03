# Checklist Accessibilité (WCAG 2.2 AA) — validation avant livraison

Cette checklist doit être exécutée par l'agent avant tout envoi de livrable, idéalement automatisée via `@axe-core/playwright` sur le rendu final (Playwright déjà en prod côté WMH pour l'automation MSC newsletter — réutilisable ici).

## Contraste
- Texte normal ≥ 4.5:1
- Texte large (≥24px, ou ≥19px en gras) ≥ 3:1
- Aucune information portée uniquement par la couleur

## Navigation clavier
- Focus visible sur tout élément interactif (outline réel, pas juste changement de couleur de fond)
- Tab order logique, aucun piège au clavier
- Zone de clic ≥ 24×24px (critère WCAG 2.2 nouveau — target size)

## Structure
- Un seul `h1` par page
- Hiérarchie de titres sans saut de niveau (h2 → h3, jamais h2 → h4)
- Landmarks sémantiques (`header`, `nav`, `main`, `footer`) plutôt que `div` génériques

## Contenu
- `alt` descriptif sur images informatives, `alt=""` sur décoratives
- Formulaires : `label` associé à chaque champ, erreurs liées via `aria-describedby`
- Animations respectant `prefers-reduced-motion`

## Pipeline de vérification suggéré
```
1. Génération du rendu (HTML/React build)
2. Playwright screenshot + axe-core scan
3. Si violations "critical"/"serious" → blocage, retour à design-principles.md passe 3
4. Si violations "minor" → log dans la note de justification, livraison autorisée
```
