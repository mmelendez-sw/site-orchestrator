## What and why

<!-- One or two sentences: what changes and the problem it solves. -->

## Checklist

- [ ] `python -m ruff check .` and `python -m pytest -q` pass locally
- [ ] Settings added/removed: `docs/configuration.md` regenerated (`python scripts/gen_config_reference.py`)
- [ ] Write gates / thresholds changed: `scripts/replay_decisions.py` output summarised below, and every changed decision is intended
- [ ] New Salesforce write or Nearmap spend is off by default and honours `APPLY=0`
- [ ] README / ADR updated if operators run something differently

## Replay / dry-run evidence

<!-- Paste replay_decisions.py summary or dry-run counts, or "n/a". -->
