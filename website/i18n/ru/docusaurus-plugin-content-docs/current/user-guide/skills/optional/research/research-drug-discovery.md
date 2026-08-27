---
title: 'Открытие лекарств. Открытие лекарств: поиск ChEMBL, сходство лекарств, взаимодействие.'
sidebar_label: Drug Discovery
description: 'Открытие лекарств: поиск ChEMBL, сходство лекарств, взаимодействия'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Открытие наркотиков

Открытие лекарств: поиск ChEMBL, сходство лекарств, взаимодействие.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/research/drug-discovery` |
| Путь | `optional-skills/research/drug-discovery` |
| Версия | `1.0.0` |
| Автор | Беннитимз |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `science`, `chemistry`, `pharmacology`, `research`, `health` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Открытие лекарств и фармацевтические исследования

Вы опытный ученый-фармацевт и химик-медик с глубоким
знания в области открытия лекарств, хемоинформатики и клинической фармакологии.
Используйте этот навык для всех исследовательских задач в области фармацевтики и химии.

## Основные рабочие процессы

### 1 — Поиск биологически активных соединений (ChEMBL)

Поиск соединений в ChEMBL (крупнейшая в мире открытая база данных о биологической активности)
по цели, активности или названию молекулы. Ключ API не требуется.

```bash
# Search compounds by target name (e.g. "EGFR", "COX-2", "ACE")
TARGET="$1"
ENCODED=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$TARGET")
curl -s "https://www.ebi.ac.uk/chembl/api/data/target/search?q=${ENCODED}&format=json" \
  | python3 -c "
import json,sys
data=json.load(sys.stdin)
targets=data.get('targets',[])[:5]
for t in targets:
    print(f\"ChEMBL ID : {t.get('target_chembl_id')}\")
    print(f\"Name      : {t.get('pref_name')}\")
    print(f\"Type      : {t.get('target_type')}\")
    print()
"
```

```bash
# Get bioactivity data for a ChEMBL target ID
TARGET_ID="$1"   # e.g. CHEMBL203
curl -s "https://www.ebi.ac.uk/chembl/api/data/activity?target_chembl_id=${TARGET_ID}&pchembl_value__gte=6&limit=10&format=json" \
  | python3 -c "
import json,sys
data=json.load(sys.stdin)
acts=data.get('activities',[])
print(f'Found {len(acts)} activities (pChEMBL >= 6):')
for a in acts:
    print(f\"  Molecule: {a.get('molecule_chembl_id')}  |  {a.get('standard_type')}: {a.get('standard_value')} {a.get('standard_units')}  |  pChEMBL: {a.get('pchembl_value')}\")
"
```

```bash
# Look up a specific molecule by ChEMBL ID
MOL_ID="$1"   # e.g. CHEMBL25 (aspirin)
curl -s "https://www.ebi.ac.uk/chembl/api/data/molecule/${MOL_ID}?format=json" \
  | python3 -c "
import json,sys
m=json.load(sys.stdin)
props=m.get('molecule_properties',{}) or {}
print(f\"Name       : {m.get('pref_name','N/A')}\")
print(f\"SMILES     : {m.get('molecule_structures',{}).get('canonical_smiles','N/A') if m.get('molecule_structures') else 'N/A'}\")
print(f\"MW         : {props.get('full_mwt','N/A')} Da\")
print(f\"LogP       : {props.get('alogp','N/A')}\")
print(f\"HBD        : {props.get('hbd','N/A')}\")
print(f\"HBA        : {props.get('hba','N/A')}\")
print(f\"TPSA       : {props.get('psa','N/A')} Å²\")
print(f\"Ro5 violations: {props.get('num_ro5_violations','N/A')}\")
print(f\"QED        : {props.get('qed_weighted','N/A')}\")
"
```

### 2 — Расчет сходства с лекарственными средствами (Липински Ro5 + Вебер)

Оцените любую молекулу на предмет соответствия установленным правилам биодоступности при пероральном приеме, используя
Бесплатный API свойств PubChem — установка RDKit не требуется.

```bash
COMPOUND="$1"
ENCODED=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$COMPOUND")
curl -s "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/${ENCODED}/property/MolecularWeight,XLogP,HBondDonorCount,HBondAcceptorCount,RotatableBondCount,TPSA,InChIKey/JSON" \
  | python3 -c "
import json,sys
data=json.load(sys.stdin)
props=data['PropertyTable']['Properties'][0]
mw   = float(props.get('MolecularWeight', 0))
logp = float(props.get('XLogP', 0))
hbd  = int(props.get('HBondDonorCount', 0))
hba  = int(props.get('HBondAcceptorCount', 0))
rot  = int(props.get('RotatableBondCount', 0))
tpsa = float(props.get('TPSA', 0))
print('=== Lipinski Rule of Five (Ro5) ===')
print(f'  MW   {mw:.1f} Da    {\"✓\" if mw<=500 else \"✗ VIOLATION (>500)\"}')
print(f'  LogP {logp:.2f}       {\"✓\" if logp<=5 else \"✗ VIOLATION (>5)\"}')
print(f'  HBD  {hbd}           {\"✓\" if hbd<=5 else \"✗ VIOLATION (>5)\"}')
print(f'  HBA  {hba}           {\"✓\" if hba<=10 else \"✗ VIOLATION (>10)\"}')
viol = sum([mw>500, logp>5, hbd>5, hba>10])
print(f'  Violations: {viol}/4  {\"→ Likely orally bioavailable\" if viol<=1 else \"→ Poor oral bioavailability predicted\"}')
print()
print('=== Veber Oral Bioavailability Rules ===')
print(f'  TPSA         {tpsa:.1f} Å²   {\"✓\" if tpsa<=140 else \"✗ VIOLATION (>140)\"}')
print(f'  Rot. bonds   {rot}           {\"✓\" if rot<=10 else \"✗ VIOLATION (>10)\"}')
print(f'  Both rules met: {\"Yes → good oral absorption predicted\" if tpsa<=140 and rot<=10 else \"No → reduced oral absorption\"}')
"
```

### 3 — Взаимодействие лекарств и поиск по безопасности (OpenFDA)

```bash
DRUG="$1"
ENCODED=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$DRUG")
curl -s "https://api.fda.gov/drug/label.json?search=drug_interactions:\"${ENCODED}\"&limit=3" \
  | python3 -c "
import json,sys
data=json.load(sys.stdin)
results=data.get('results',[])
if not results:
    print('No interaction data found in FDA labels.')
    sys.exit()
for r in results[:2]:
    brand=r.get('openfda',{}).get('brand_name',['Unknown'])[0]
    generic=r.get('openfda',{}).get('generic_name',['Unknown'])[0]
    interactions=r.get('drug_interactions',['N/A'])[0]
    print(f'--- {brand} ({generic}) ---')
    print(interactions[:800])
    print()
"
```

```bash
DRUG="$1"
ENCODED=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$DRUG")
curl -s "https://api.fda.gov/drug/event.json?search=patient.drug.medicinalproduct:\"${ENCODED}\"&count=patient.reaction.reactionmeddrapt.exact&limit=10" \
  | python3 -c "
import json,sys
data=json.load(sys.stdin)
results=data.get('results',[])
if not results:
    print('No adverse event data found.')
    sys.exit()
print(f'Top adverse events reported:')
for r in results[:10]:
    print(f\"  {r['count']:>5}x  {r['term']}\")
"
```

### 4 — Поиск соединений PubChem

```bash
COMPOUND="$1"
ENCODED=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$COMPOUND")
CID=$(curl -s "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/${ENCODED}/cids/TXT" | head -1 | tr -d '[:space:]')
echo "PubChem CID: $CID"
curl -s "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/${CID}/property/IsomericSMILES,InChIKey,IUPACName/JSON" \
  | python3 -c "
import json,sys
p=json.load(sys.stdin)['PropertyTable']['Properties'][0]
print(f\"IUPAC Name : {p.get('IUPACName','N/A')}\")
print(f\"SMILES     : {p.get('IsomericSMILES','N/A')}\")
print(f\"InChIKey   : {p.get('InChIKey','N/A')}\")
"
```

### 5 — Литература по мишеням и заболеваниям (OpenTargets)

```bash
GENE="$1"
curl -s -X POST "https://api.platform.opentargets.org/api/v4/graphql" \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"{ search(queryString: \\\"${GENE}\\\", entityNames: [\\\"target\\\"], page: {index: 0, size: 1}) { hits { id score object { ... on Target { id approvedSymbol approvedName associatedDiseases(page: {index: 0, size: 5}) { count rows { score disease { id name } } } } } } } }\"}" \
  | python3 -c "
import json,sys
data=json.load(sys.stdin)
hits=data.get('data',{}).get('search',{}).get('hits',[])
if not hits:
    print('Target not found.')
    sys.exit()
obj=hits[0]['object']
print(f\"Target: {obj.get('approvedSymbol')} — {obj.get('approvedName')}\")
assoc=obj.get('associatedDiseases',{})
print(f\"Associated with {assoc.get('count',0)} diseases. Top associations:\")
for row in assoc.get('rows',[]):
    print(f\"  Score {row['score']:.3f}  |  {row['disease']['name']}\")
"
```

## Рекомендации по рассуждению

При анализе сходства лекарств или молекулярных свойств всегда:

1. **Сначала укажите необработанные значения** — MW, LogP, HBD, HBA, TPSA, RotBonds
2. **Применить наборы правил** — фильтр Ro5 (Липински), Вебера, Гозе, где это необходимо.
3. **Отметить обязательства** — метаболические горячие точки, риск hERG, высокий уровень TPSA для проникновения в ЦНС.
4. **Предложите варианты оптимизации** — биоизостерические замены, стратегии пролекарств, усечение кольца.
5. **Укажите исходный API** — ChEMBL, PubChem, OpenFDA или OpenTargets.

Для вопросов ADMET систематически рассуждайте через абсорбцию, распределение, метаболизм, выведение и токсичность. См. ссылки/ADMET_REFERENCE.md для получения подробных инструкций.

## Важные примечания

- Все API бесплатны, общедоступны, не требуют аутентификации.
- Ограничения скорости ChEMBL: добавьте сон 1 между пакетными запросами.
- Данные FDA отражают зарегистрированные побочные эффекты, а не обязательно их причинную связь.
- Всегда рекомендуется консультироваться с лицензированным фармацевтом или врачом для принятия клинических решений.

## Краткий справочник

| Задача | API | Конечная точка |
|------|-----|----------|
| Найти цель | ЧЕМБЛ | `/api/data/target/search?q=` |
| Получите биоактивность | ЧЕМБЛ | `/api/data/activity?target_chembl_id=` |
| Свойства молекул | ПабХим | `/rest/pug/compound/name/{name}/property/` |
| Взаимодействие с лекарственными средствами | OpenFDA | `/drug/label.json?search=drug_interactions:` |
| Побочные явления | OpenFDA | `/drug/event.json?search=...&count=reaction` |
| Генная болезнь | ОпенТаргетс | GraphQL POST `/api/v4/graphql` |