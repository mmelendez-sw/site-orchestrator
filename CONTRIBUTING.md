# Contributing to ICEMAN

ICEMAN writes to production Salesforce and spends a metered Nearmap budget. The rules below keep changes safe to ship.

## Workflow

1. Branch from `main` (`feature/…`, `fix/…`, `chore/…`). Never commit to `main` directly.
2. Make the change with tests (see below), then run the local checks:
   ```powershell
   python -m ruff check .
   python -m pytest -q
   python scripts/gen_config_reference.py --check
   ```
3. Open a pull request and fill in the checklist. CI (`.github/workflows/tests.yml`) runs the same three checks on every push.

## Tests

- Tests are offline. `conftest.py` turns off the networked features and blanks API keys, Salesforce credentials and the Azure SQL server before `.env` loads (variables already exported in your shell still win). A test that needs a service must mock it.
- Put tests next to the code: `classifier/tests`, `enrichment/tests`, `salesforce/tests`, `scripts/tests`.
- Changing a write gate or threshold (`enrichment/bucketing.py`, `classifier/asset_classifier.py`, `enrichment/connectx_audit.py`): add or update a case in `enrichment/tests/golden_cases.py` or `classifier/tests/test_tier_gates.py`, and run `python scripts/replay_decisions.py <recent month>`. Paste its summary into the PR; every changed decision should be intended.

## Configuration

- Read settings only through `envutil` (`env_str`, `env_flag`, `env_int`, `env_float`, `env_csv`) with a safe default. A missing or malformed value must never crash a run or turn on a paid path.
- After adding, renaming or removing a setting, run `python scripts/gen_config_reference.py` and commit `docs/configuration.md`. Add it to `.env.example` only if operators normally set it.
- New behaviour that spends money or writes to Salesforce starts **off** by default and is turned on in its own PR after a dry run.

## Salesforce and spend safety

- Every Salesforce write path must honour `APPLY=0` (dry run) and log each record to the run folder.
- Writes go through `enrichment/sf_ops.py` in sObject Collections batches; do not add one-off writes inside classification code.
- Every Nearmap purchase must go through `classifier.imagery` so it is metered and budget-checked.
- Do not run live enrichment, paid APIs or Salesforce writes from a branch without the operator's go-ahead.

## Code style

- `ruff` enforces errors, import order and modern syntax (`pyproject.toml`). Match the surrounding code; keep comments for the *why*.
- No secrets in code, logs or test fixtures. `classifier.sources.base.redact` scrubs tokens from log text.
- Runtime data (runs, chips, caches, CSVs) belongs in `../site-orchestrator-data`, never in the repo.

## Removing things

Delete code that no production path reaches instead of leaving it behind a flag nobody sets. Remove its tests, settings and docs in the same PR, and note removals that change behaviour in the relevant ADR.
