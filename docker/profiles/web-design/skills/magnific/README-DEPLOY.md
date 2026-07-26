# Mise à jour du skill `magnific` dans le repo

Skill **v3.0.0** — testé en réel contre l'API Magnific le 26/07/2026.

## Pourquoi cette mise à jour est nécessaire

`docker/entrypoint.sh` (lignes ~195-205) fait, à **chaque** boot Railway :

```bash
if [ -n "$RAILWAY_ENVIRONMENT" ] || [ ! -d "$_pdir/skills/$_skname" ]; then
    rm -rf "$_pdir/skills/$_skname"
    cp -a "$_sk" "$_pdir/skills/$_skname"
fi
```

`RAILWAY_ENVIRONMENT=production` → la condition est toujours vraie. Tout skill
présent dans `docker/profiles/<profil>/skills/` est **supprimé puis restauré
depuis l'image**, sans test de version.

Conséquence : le template committé est la **seule** source de vérité. Éditer le
volume ne sert à rien — c'est écrasé au redéploiement suivant (constaté en
séance : le md5 du template `3a38098e8fd211a353c7c90b5aad8270` a remplacé une
édition du volume au boot de 09:55).

## Fichiers à déposer

Cible dans le repo :

```
docker/profiles/web-design/skills/magnific/
├── SKILL.md                 (13 892 o — remplace la v1.0.0 de 2 377 o)
└── scripts/
    ├── upscale.py           (5 201 o — corrigé)
    ├── generate.py          (6 581 o — NOUVEAU)
    ├── nano_banana.py       (5 453 o — NOUVEAU)
    └── credits.py           (3 207 o — NOUVEAU)
```

Le template actuel ne contient que `SKILL.md` (v1.0.0) et `scripts/upscale.py`.
Les trois autres scripts sont à ajouter.

### Checksums (après dépôt, à vérifier)

```
90bd3f214bd2af940acd8e86f5402d5e  magnific/SKILL.md
692a362e2ba20a783b23f902680471cc  magnific/scripts/upscale.py
75455381fe2515a1955bd5ab37532a34  magnific/scripts/generate.py
d69b712d436f02ed6f98d2ad580f8112  magnific/scripts/nano_banana.py
699d0c09163c90bb00e89083b7c44eef  magnific/scripts/credits.py
```

## Procédure

```bash
# depuis la racine du repo Hermes
tar xzf magnific-skill-v3.0.0.tar.gz -C docker/profiles/web-design/skills/
# écrase magnific/ ; vérifier :
grep '^version:' docker/profiles/web-design/skills/magnific/SKILL.md   # -> 3.0.0
ls docker/profiles/web-design/skills/magnific/scripts/                 # -> 4 fichiers

git add docker/profiles/web-design/skills/magnific
git commit -m "skill(magnific): v3.0.0 — route nano-banana-pro, analytics crédits, garde-fous"
git push
```

Au redéploiement, l'entrypoint recopie le template → volume, et les deux sont
alignés.

## Ce que la v3.0.0 apporte

**Corrections de faits faux dans la v1.0.0 :**

- `image` n'accepte **pas** d'URL (la v1 documentait `"<URL ou base64>"`).
  Une URL renvoie `400 "Unable to resolve image from <url>"`. C'est du base64 nu,
  sans préfixe `data:`.
- Deux endpoints d'upscale, pas un : `/ai/image-upscaler` (creative) et
  `/ai/image-upscaler-precision`. Precision est le bon défaut pour un asset de
  marque — Creative invente du détail.
- Réponses enveloppées dans `data`, statuts `CREATED`/`IN_PROGRESS`/`COMPLETED`/`FAILED`,
  résultat dans `generated[]` (URLs CDN signées qui **expirent**).

**Ajouts :**

- **Route `nano-banana-pro`** (`POST /v1/ai/text-to-image/nano-banana-pro`) —
  absente de `llms.txt` mais active. Seul modèle qui rend du texte lisible ;
  Mystic produit du pseudo-texte. Vérifié sur un même prompt d'affiche.
- Arbre de décision texte → Nano Banana Pro / sinon Mystic.
- **Analytics API** (`/v1/analytics/team-credit-usage`) pour auditer la conso
  réelle par jour/outil/utilisateur/projet, sans consommer de crédit.
- Section REST vs MCP : le MCP est en OAuth navigateur, donc **incompatible
  Railway** (pas de client_credentials). Il donne accès à Nano Banana 2 /
  Imagen 4 / SVG / 3D, mais n'a pas l'Analytics API.
- Repères de coût en crédits par modèle + limite dure de 25,3 MP en upscale.

**Garde-fous dans les scripts** (bloquent avant tout appel payant) :

- `upscale.py` : refuse une sortie > 25,3 MP.
- `generate.py` : refuse un `aspect_ratio` incompatible avec le modèle `fluid`
  (l'API l'ignorerait silencieusement en facturant).
- `nano_banana.py` : refuse un chemin local en `reference_images` (l'API exige
  des URL publiques — contrat **inverse** de Mystic qui exige du base64), valide
  la longueur du prompt et le plafond de 14 références.

## Pièges à retenir

1. Contrats opposés : Mystic → base64 ; Nano Banana `reference_images` → URL publiques.
2. Ratios : Mystic `widescreen_16_9` ; Nano Banana `16:9`.
3. Formats de sortie : Mystic → JPEG ; Nano Banana Pro → PNG ; upscale Precision → PNG.
4. `llms.txt` est **incomplet** (`nano-banana-pro`, `text-to-icon` en sont absents).
   Pour sonder une route sans dépenser : `POST` avec `{}` → `400` si elle existe,
   `404` sinon. Un `GET` sur la collection n'est pas fiable (404 sur les routes POST-only).
5. L'API **n'erreure pas** sur les combinaisons incompatibles — elle ignore le
   paramètre et facture quand même.

## Correctif d'infra optionnel (non appliqué)

Le `rm -rf` inconditionnel piégera n'importe quel skill édité côté volume, pas
seulement `magnific`. Pour préserver les éditions locales tout en suivant le
template, la boucle pourrait comparer un hash comme le fait déjà
`tools/skills_sync.py` (manifeste `.bundled_manifest`, qui SKIP les skills
modifiés par l'utilisateur). Décision laissée à l'appréciation du mainteneur :
le comportement actuel est peut-être volontaire pour garantir des profils
reproductibles.
