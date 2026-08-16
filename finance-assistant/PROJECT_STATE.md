# Project State

## Goal
Yerel Gmail banka ekstrelerini güvenli biçimde işleyip banka bağımsız finansal analiz ve ileride dashboard sunmak.

## Architecture
Parser registry ve banka-özel parserlar transaction üretir; IngestionService atomik olarak statement/transaction/audit kayıtlarını yazar. DuckDB lokal veri deposudur. AnalysisService read-only aggregate, kategori, ücret, karşılaştırma ve completeness sonuçları üretir. Gmail yalnızca document source'tur.

## Decisions
- Gerçek finansal veriler, PDF'ler ve OAuth secret'ları repository dışında kalır.
- Completeness transaction setini audit kaydından üstün tutar; tam fakat eski/auditsiz import `LEGACY_UNVERIFIED` olur.
- Manuel kategori > deterministik kural > opsiyonel LLM > unknown sırası korunur.
- Dashboard'a geçmeden önce doğruluk, bütünlük ve gizlilik kontrolleri tamamlanır.

## Completed
- Üç parser ve Gmail readonly ingestion akışı doğrulandı.
- Gerçek DB'de 3 statement ve 74 transaction doğrulandı.
- Merkezi analiz ve güvenli public JSON oluşturuldu.
- `statement_completeness()` legacy audit eksikliğini ayrı durum olarak ele alacak şekilde uygulandı.

## Current
- Proje ilk tamamlanabilir sürüm kapsamına ulaştı: ingestion, merkezi analiz,
  deterministik kategorizasyon, privacy-safe raporlama ve read-only dashboard
  birlikte çalışıyor.
- Kategori kuralları genişletildi; gerçek DuckDB değiştirilmedi.
- `report --month YYYY-MM --output-dir PATH` deterministik CSV ve HTML üretir.
- `dashboard` komutu Streamlit dashboard'u başlatır. Dashboard yalnızca analiz
  servisini okur; merchant açıklaması, kart bilgisi ve transaction kimliği
  göstermez.

## Next
- İş Bankası ve Axess için gerçek yeni Gmail mesajlarında sender discovery ve
  kontrollü canlı ingest smoke testi.
- Yeni banka formatları veya kullanıcıdan gelen kategori düzeltmeleri olursa
  TDD ile eklenebilir.

## Issues
- İş Bankası ve Axess Gmail sender yapılandırması gerçek yeni e-postalarla discovery bekliyor.
- Eski importlar için doğrulanabilir processing audit kaydı yok; sahte backfill yapılmayacak.

## Validation
- Full tests: PASS — `.venv/bin/pytest -q` → 77 passed.
- Targeted analysis/ingestion/reporting tests: PASS — 22 passed.
- Real DB read-only audit: PASS — 3 statements, 74 linked transactions; isbank_maximum 19, axess 52, enpara 3.
- Processing audit: PASS — enpara has 2 `SKIPPED_DUPLICATE` rows; no successful ingestion audit exists.
- Completeness: PASS — all three banks return `LEGACY_UNVERIFIED`; lower audited counts remain covered by fixture tests as `PARTIAL`.
- Real DB analysis smoke: PASS — August 2026 total spending `875.25`.
- Public JSON privacy smoke: PASS — no `by_card`, card identifiers, raw merchant
  descriptions, fingerprints, or secrets.
- Report smoke: PASS — `/tmp/finance-assistant-report-smoke2/2026-08.csv` and
  `.html` generated; privacy scan clean.
- Dashboard smoke: PASS — Streamlit server reached
  `http://127.0.0.1:18506` and stopped cleanly under bounded test timeout.
- Read-only database regression: PASS — analysis rejects missing DB paths,
  opens existing DBs read-only, and cannot create tables through its connection;
  ingestion remains on the writable database path.
- Public JSON omits transaction-level `top_transactions`; the dashboard shows
  only the uncategorized aggregate count, not individual transactions.
- CSV/HTML report writes are atomic and fail closed on symlink targets.
- Compile: PASS — `.venv/bin/python -m compileall -q app tests dashboard`.
- Diff check: PASS — `git diff --check`.
