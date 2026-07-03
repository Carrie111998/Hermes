# Principes de design web — jugement autonome

Contexte : agent sans boucle d'itération humaine avant livraison. Ce fichier remplace le rôle qu'un directeur artistique jouerait en review — l'auto-critique doit être exécutée par l'agent lui-même, pas déléguée.

## 1. Ancrage sujet (avant toute décision visuelle)
Identifier explicitement : l'objet concret, l'audience, le job unique de la page. Ne jamais démarrer sur un template neutre — les choix distinctifs viennent du sujet réel (son vocabulaire, ses artefacts, son univers), pas d'une esthétique générique plaquée dessus.

## 2. Trois défauts IA à refuser sauf demande explicite
1. Fond crème (~#F4F1EA) + serif display + accent terracotta
2. Fond quasi-noir + accent unique acid-green/vermillon
3. Layout broadsheet : hairline rules, zéro border-radius, colonnes journal denses

Légitimes uniquement si le brief les demande mot pour mot. Sinon, considérer leur apparition comme un signal d'échec de la phase 1.

## 3. Typographie
Minimum 2 rôles (display + body) pairés de façon délibérée pour ce sujet précis — jamais la paire par défaut (Inter + system-ui) sauf si le brief est neutre/utilitaire (dashboard interne, outil).

## 4. Structure = information
Numérotation, labels, dividers : légitimes seulement s'ils encodent une info réelle (séquence, catégorie). Sinon, les supprimer — la décoration sans fonction est un signal de générique.

## 5. Motion
Un moment orchestré (page-load, scroll-reveal) vaut mieux que des micro-interactions dispersées. Toujours respecter `prefers-reduced-motion`. Dans le doute, moins d'animation plutôt que plus.

## 6. Process obligatoire en 3 passes (sans validation humaine intermédiaire)

**Passe 1 — Token system** (à consigner dans les logs de génération pour audit a posteriori) :
- Couleurs : 4–6 valeurs hex nommées
- Typo : display + body (+ utilitaire si besoin data/caption)
- Layout : concept en 1 phrase + wireframe ASCII
- Signature : l'élément unique dont cette page sera mémorable

**Passe 2 — Auto-critique obligatoire avant build :**
Comparer le plan à ce que produirait un prompt similaire sur un sujet différent. Si le résultat serait interchangeable → révision forcée avant d'écrire le moindre code. Consigner ce qui a été changé et pourquoi.

**Passe 3 — Build to floor (non négociable, indépendant du budget créatif) :**
- Responsive jusqu'au mobile
- Focus clavier visible sur tout élément interactif
- Contraste AA minimum (voir `a11y-checklist.md`)
- `prefers-reduced-motion` respecté

## 7. Restraint
Un seul risque esthétique assumé par livrable. Tout ce qui l'entoure reste sobre et discipliné. Retirer un élément avant livraison si le design semble surchargé (règle Chanel : un accessoire en moins).

## 8. Sortie attendue
Chaque livraison doit inclure une note de justification de 2–3 lignes expliquant le choix de signature — c'est la seule trace de la phase 1/2 que Gilles verra sans relire le raisonnement complet.
