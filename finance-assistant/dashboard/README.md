# Local finance dashboard

The dashboard entrypoint is `app/dashboard.py` and is launched with:

```bash
uv run python -m app.main dashboard
# or: uv run streamlit run app/dashboard.py
```

It opens a read-only view over `AnalysisService`: month selection, spending
KPIs, category and bank charts, a six-month trend, statement completeness
warnings (including `LEGACY_UNVERIFIED`), and an uncategorized table containing
only transaction date, amount, and bank. It never writes merchant rules or
transactions to DuckDB. Do not place raw statements, credentials, or exports
in this directory.
