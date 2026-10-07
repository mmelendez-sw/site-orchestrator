"""Score enrichment runs against a labelled eval set (offline).

  python scripts/eval_report.py --eval <data>/eval/eval_set_2026-10-07.csv
  python scripts/eval_report.py --eval eval.csv --runs "2026-10-0*" --csv joined.csv

For every eval Id, the LATEST row across the runs' enrichment_detail.csv
files (run folders in name order, i.e. by timestamp) is the prediction.
Reported, each with precision / recall / coverage against the label
(positive = real cell site):

a. bucket = potential_update as "predicted asset"
b. audit_verdict: confirmed = asset, no_asset = none, inconclusive = no call
c. the NAIP model's cell_equipment answer, by imagery_used and (when the
   column exists) supplemental_sources

Coverage = eval Ids the method made a call on / all eval Ids. Precision and
recall are over the called Ids only.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DETAIL_CSV = "enrichment_detail.csv"
POTENTIAL_UPDATE = "potential_update"
TRUE_LABELS = frozenset({"positive", "1", "true", "yes", "asset"})
FALSE_LABELS = frozenset({"negative", "0", "false", "no", "none"})
NAIP_CELL_FIELDS = ("naip_cell_equipment", "naip_screen_cell_equipment", "cell_equipment")
JOINED_COLUMNS = (
    "Id", "label", "stage", "unqualified_reason", "run", "bucket", "audit_verdict",
    "audit_reason", "naip_cell_equipment", "imagery_used", "supplemental_sources",
    "pred_bucket", "pred_audit", "pred_naip_cell",
)

Prediction = Callable[[dict[str, Any]], "bool | None"]


def id15(value: Any) -> str:
    return str(value or "").strip()[:15]


def parse_label(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text in TRUE_LABELS:
        return True
    if text in FALSE_LABELS:
        return False
    return None


def parse_bool(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


# ---------------------------------------------------------------- predictions


def pred_bucket(row: dict[str, Any] | None) -> bool | None:
    if not row:
        return None
    bucket = str(row.get("bucket") or "").strip()
    if not bucket:
        return None
    return bucket == POTENTIAL_UPDATE


def pred_audit(row: dict[str, Any] | None) -> bool | None:
    verdict = str((row or {}).get("audit_verdict") or "").strip().lower()
    if verdict == "confirmed":
        return True
    if verdict == "no_asset":
        return False
    return None  # inconclusive or not an audit row


def pred_naip_cell(row: dict[str, Any] | None) -> bool | None:
    for name in NAIP_CELL_FIELDS:
        value = parse_bool((row or {}).get(name))
        if value is not None:
            return value
    return None


# -------------------------------------------------------------------- metrics


def _ratio(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def binary_metrics(pairs: Iterable[tuple[bool, bool | None]]) -> dict[str, Any]:
    """(truth, prediction) pairs; prediction None = no call (not covered)."""
    tp = fp = fn = tn = total = 0
    for truth, pred in pairs:
        total += 1
        if pred is None:
            continue
        if pred and truth:
            tp += 1
        elif pred:
            fp += 1
        elif truth:
            fn += 1
        else:
            tn += 1
    called = tp + fp + fn + tn
    return {
        "n": total,
        "called": called,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "coverage": _ratio(called, total),
        "accuracy": _ratio(tp + tn, called),
    }


def grouped_metrics(
    records: Sequence[dict[str, Any]],
    predict: Prediction,
    key: Callable[[dict[str, Any]], str],
) -> dict[str, dict[str, Any]]:
    """binary_metrics per group; records carry 'truth' and 'row'."""
    groups: dict[str, list[tuple[bool, bool | None]]] = defaultdict(list)
    for rec in records:
        groups[key(rec)].append((rec["truth"], predict(rec["row"])))
    return {k: binary_metrics(v) for k, v in sorted(groups.items())}


def verdict_table(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """audit_verdict x label counts (rows without a verdict = 'no audit')."""
    table: dict[str, dict[str, int]] = defaultdict(lambda: {"positive": 0, "negative": 0})
    for rec in records:
        verdict = str((rec["row"] or {}).get("audit_verdict") or "").strip() or "(no audit)"
        table[verdict]["positive" if rec["truth"] else "negative"] += 1
    return dict(table)


# ---------------------------------------------------------------- run reading


def select_latest(
    rows: Iterable[tuple[str, dict[str, Any]]], wanted: set[str] | None = None
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Id15 -> (run name, row) from the latest run (by run name) holding it."""
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for run, row in rows:
        key = id15(row.get("Id"))
        if not key or (wanted is not None and key not in wanted):
            continue
        prev = out.get(key)
        if prev is None or run >= prev[0]:
            out[key] = (run, row)
    return out


def iter_run_rows(run_dirs: Iterable[Path]):
    for run in sorted(run_dirs, key=lambda p: p.name):
        path = run / DETAIL_CSV
        if not path.is_file():
            continue
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                yield run.name, row


def resolve_runs(patterns: Sequence[str] | None, runs_root: Path) -> list[Path]:
    found: dict[str, Path] = {}
    for pattern in patterns or ["*"]:
        path = Path(pattern)
        if path.is_absolute():
            matches = list(path.parent.glob(path.name)) if any(c in path.name for c in "*?[") else [path]
        else:
            matches = list(runs_root.glob(pattern))
        for match in matches:
            if match.is_dir():
                found[str(match)] = match
    return sorted(found.values(), key=lambda p: p.name)


def load_eval(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            truth = parse_label(row.get("label"))
            key = id15(row.get("Id"))
            if key and truth is not None:
                out[key] = {**row, "truth": truth}
    return out


def join_records(
    eval_set: dict[str, dict[str, Any]],
    latest: dict[str, tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    records = []
    for key, ev in eval_set.items():
        run, row = latest.get(key, ("", None))
        records.append({"Id": ev.get("Id") or key, "truth": ev["truth"], "eval": ev,
                        "run": run, "row": row})
    return records


# ------------------------------------------------------------------- printing


def _pct(value: float | None) -> str:
    return "  -  " if value is None else f"{value * 100:5.1f}%"


def metrics_line(name: str, m: dict[str, Any]) -> str:
    return (
        f"  {name[:28]:28} {m['n']:6} {m['called']:6} {_pct(m['coverage'])} "
        f"{_pct(m['precision'])} {_pct(m['recall'])}  tp {m['tp']} fp {m['fp']} fn {m['fn']} tn {m['tn']}"
    )


HEADER = f"  {'':28} {'n':>6} {'called':>6} {'cover':>6} {'prec':>6} {'recall':>6}"


def report(records: Sequence[dict[str, Any]]) -> str:
    seen = [r for r in records if r["row"] is not None]
    lines = [
        f"eval Ids: {len(records)} (positive {sum(r['truth'] for r in records)}, "
        f"negative {sum(not r['truth'] for r in records)}); in a run: {len(seen)}",
        "",
        "a. bucket potential_update = predicted asset",
        HEADER,
        metrics_line("all", binary_metrics((r["truth"], pred_bucket(r["row"])) for r in records)),
        "",
        "b. audit_verdict (confirmed = asset, no_asset = none)",
        HEADER,
        metrics_line("all", binary_metrics((r["truth"], pred_audit(r["row"])) for r in records)),
    ]
    for verdict, counts in sorted(verdict_table(seen).items()):
        lines.append(f"    {verdict:26} positive {counts['positive']:5}  negative {counts['negative']:5}")
    lines += [
        "",
        "c. NAIP cell_equipment",
        HEADER,
        metrics_line("all", binary_metrics((r["truth"], pred_naip_cell(r["row"])) for r in records)),
        "  by imagery_used:",
    ]
    for group, m in grouped_metrics(
        seen, pred_naip_cell, lambda r: str(r["row"].get("imagery_used") or "(blank)")
    ).items():
        lines.append(metrics_line(f"  {group}", m))
    if any("supplemental_sources" in r["row"] for r in seen):
        lines.append("  by supplemental_sources:")
        for group, m in grouped_metrics(
            seen, pred_naip_cell, lambda r: str(r["row"].get("supplemental_sources") or "none")
        ).items():
            lines.append(metrics_line(f"  {group}", m))
    return "\n".join(lines)


def _fmt_pred(value: bool | None) -> str:
    return "" if value is None else ("1" if value else "0")


def write_joined(path: Path, records: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=JOINED_COLUMNS)
        writer.writeheader()
        for r in records:
            row = r["row"] or {}
            writer.writerow(
                {
                    "Id": r["Id"],
                    "label": "positive" if r["truth"] else "negative",
                    "stage": r["eval"].get("stage", ""),
                    "unqualified_reason": r["eval"].get("unqualified_reason", ""),
                    "run": r["run"],
                    "bucket": row.get("bucket", ""),
                    "audit_verdict": row.get("audit_verdict", ""),
                    "audit_reason": row.get("audit_reason", ""),
                    "naip_cell_equipment": row.get("naip_cell_equipment", ""),
                    "imagery_used": row.get("imagery_used", ""),
                    "supplemental_sources": row.get("supplemental_sources", ""),
                    "pred_bucket": _fmt_pred(pred_bucket(r["row"])),
                    "pred_audit": _fmt_pred(pred_audit(r["row"])),
                    "pred_naip_cell": _fmt_pred(pred_naip_cell(r["row"])),
                }
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--eval", required=True, type=Path, help="eval CSV from build_eval_set.py")
    parser.add_argument("--runs", action="append", default=None,
                        help="run folder glob under the runs dir, or an absolute path (repeatable; default *)")
    parser.add_argument("--csv", type=Path, default=None, help="write the per-Id joined file here")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from paths import runs_dir

    eval_set = load_eval(args.eval)
    run_dirs = resolve_runs(args.runs, runs_dir())
    print(f"runs: {len(run_dirs)} folder(s) under {runs_dir()}", flush=True)
    latest = select_latest(iter_run_rows(run_dirs), set(eval_set))
    records = join_records(eval_set, latest)
    print(report(records))
    if args.csv:
        write_joined(args.csv, records)
        print(f"wrote {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
