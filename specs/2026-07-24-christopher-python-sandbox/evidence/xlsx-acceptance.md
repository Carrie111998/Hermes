# Jailed XLSX acceptance

## Acceptance population

The fixture creates 120 synthetic, tenant-shaped case rows in SQLite, copies
the database to `tenant-copy.db`, and attaches that copy as a sandbox dataset.
Eighty rows are open. No client data is present in the fixture or artifact.

Inside the jailed child, `openpyxl` reads the snapshot-backed dataset and writes
`/work/open-cases.xlsx`. The CI workflow copies that exact retained run artifact
only after `python_sandbox` returns, then uploads it.

## Result

- Branch commit: `693a980889`
- Pull request: <https://github.com/teren-papercutlabs/hermes-pcl/pull/4>
- CI run: <https://github.com/teren-papercutlabs/hermes-pcl/actions/runs/30237111519>
- CI artifact: `python-sandbox-open-cases`, artifact ID `8641990271`
- Artifact URL: <https://github.com/teren-papercutlabs/hermes-pcl/actions/runs/30237111519/artifacts/8641990271>
- Unit population: 18 tests, 18 passed
- Jailed E2E population: 9 tests, 9 passed, 0 skipped
- Workbook: `open-cases-acceptance.xlsx`
- SHA-256: `66d2333ee82d7cb71d002b739e15443547d9464d2be9465e4d29db7360bfdba4`
- Sheet: `Open Cases`
- Shape: 81 rows including header, 5 columns
- Exported population: 80 open cases; every data row has state `open`

This is acceptance evidence for the real jail and XLSX writer. It is not a
claim that a live TGG database was read.

## Return-path verdict

Native chat XLSX return is not ready. WB `65b4eac4` reviews an image-only
`MEDIA:` implementation that remains on a worker branch; it neither ships on
`main` nor covers XLSX. The existing portal download route serves registered
report artifacts, not sandbox run files.

The driver runbook therefore records the authenticated portal-download
fallback contract without claiming that route is live. Until a consumer is
built and verified, XLSX output remains a retained operator artifact while the
client receives the computed summary in WhatsApp.

## Safety

No deploy was performed. `tgg-app-1` was not mutated or used for this
acceptance run.
