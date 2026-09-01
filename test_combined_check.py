# Kombinierter toolset check_fn: Funktionsnachweis
calls = []
def check_a():
    calls.append('a'); return False
def check_b():
    calls.append('b'); return True

combined_store = {}
def register(toolset, check_fn):
    existing = combined_store.get(toolset)
    if existing is None:
        combined_store[toolset] = check_fn
    elif existing is not check_fn:
        def _combined_check(_a=existing, _b=check_fn):
            return bool(_a()) or bool(_b())
        combined_store[toolset] = _combined_check

register('t', check_a)
register('t', check_b)
result = combined_store['t']()
print('kombinierter Check:', result, '| Aufrufe:', calls)
assert result is True and set(calls) == {'a', 'b'}, 'FEHLER'
print('LOGIK OK: beide checks laufen, OR-Ergebnis korrekt')
