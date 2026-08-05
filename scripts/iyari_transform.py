#!/usr/bin/env python3
"""
Transformador de rebranding Hermes -> IYARI para la documentacion del fork.

Uso:
    # pasada en seco (imprime diff unificado, NO escribe):
    python3 scripts/iyari_transform.py --dry website/docs/reference/*.md

    # aplicar (escribe los archivos in-place):
    python3 scripts/iyari_transform.py website/docs/reference/*.md

Flags:
    --dry                  imprime el diff sin escribir.
    --skip-nous-research   NO aplica la regla "Nous Research" -> "Digital Services LLC"
                           (util cuando esas ocurrencias son casos dudosos a revisar
                           a mano: p.ej. atribucion de Nous Portal o el Discord de Nous).

Reglas (ver CLAUDE.md para el criterio completo). Se aplican EN ESTE ORDEN:
    0.  NousResearch/hermes-agent          -> digital-services-llc/iyari
    0b. Nous Research (con espacio)        -> Digital Services LLC   (salvo --skip-nous-research)
    1.  Hermes Agent                       -> IYARI                  (elimina "Agent")
    2.  \\bHermes\\b (?![- ][34])          -> IYARI                  (protege modelo Hermes-3/4)
    3.  IYARI' + espacio+minuscula (posesivo en prosa) -> IYARI's

Preservado (NO se toca): URLs nousresearch.com, comando/paquete `hermes` minuscula,
paths ~/.hermes/ y env vars HERMES_*, modelo Hermes-3/Hermes-4, LICENSE, y el
servicio Nous Portal / Nous Subscription / Nous Tool / provider=nous.
"""
import re, sys, difflib

def transform(s):
    # 0) repo de GitHub (cubre github.com/..., gh --repo ..., prosa y .git)
    s = re.sub(r'NousResearch/hermes-agent', 'digital-services-llc/iyari', s)
    # 0b) "Nous Research" (con espacio) -> Digital Services LLC.
    #     No toca "nousresearch.com" (minuscula/sin espacio) ni "Nous Portal".
    if not SKIP_NOUS_RESEARCH:
        s = re.sub(r'Nous Research', 'Digital Services LLC', s)
    # 1) marca compuesta primero (elimina "Agent")
    s = re.sub(r'Hermes Agent', 'IYARI', s)
    # 2) Hermes suelto -> IYARI, protegiendo modelo Hermes-3 / Hermes-4 / Hermes 3 / Hermes 4
    s = re.sub(r'\bHermes\b(?![- ][34])', 'IYARI', s)
    # 3) posesivo: "Hermes' algo" (prosa) quedo como "IYARI' algo" -> "IYARI's algo".
    #    Requiere espacio+minuscula despues del apostrofo para no confundir la
    #    comilla de cierre de un string literal ('Hermes' en codigo) con un
    #    posesivo real -- bug real encontrado en Fase 0.4 (corrompia
    #    'Hermes' -> 'IYARI's, sintaxis Python invalida).
    s = re.sub(r"IYARI'(?=\s[a-z])", "IYARI's", s)
    return s

dry = '--dry' in sys.argv
SKIP_NOUS_RESEARCH = '--skip-nous-research' in sys.argv
files = [a for a in sys.argv[1:] if not a.startswith('--')]
for f in files:
    orig = open(f, encoding='utf-8').read()
    new = transform(orig)
    if orig == new:
        continue
    if dry:
        diff = difflib.unified_diff(orig.splitlines(), new.splitlines(),
                                    fromfile=f, tofile=f, lineterm='')
        print('\n'.join(diff))
        print('=' * 80)
    else:
        open(f, 'w', encoding='utf-8').write(new)
        print(f'WROTE {f}')
