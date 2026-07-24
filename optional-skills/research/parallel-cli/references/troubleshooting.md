# Parallel CLI — Error Handling & Maintenance

---

## Error handling and exit codes

The CLI documents these exit codes:
- `0` success
- `2` bad input
- `3` auth error
- `4` API error
- `5` timeout

If you hit auth errors:
1. check `parallel-cli auth`
2. confirm `PARALLEL_API_KEY` or run `parallel-cli login` / `parallel-cli login --device`
3. verify `parallel-cli` is on `PATH`

## Maintenance

Check current auth / install state:

```bash
parallel-cli auth
parallel-cli --help
```

Update commands:

```bash
parallel-cli update
pip install --upgrade parallel-web-tools
parallel-cli config auto-update-check off
```
