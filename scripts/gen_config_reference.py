"""Generate docs/configuration.md: every environment variable the code reads.

The table is built from the source (AST), so it cannot drift from the code:
CI runs ``--check`` (via scripts/tests/test_config_reference.py) and fails when
a new variable is read but the reference was not regenerated.

    python scripts/gen_config_reference.py           # rewrite docs/configuration.md
    python scripts/gen_config_reference.py --check   # exit 1 if it is stale
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "configuration.md"

# envutil readers plus module-local wrappers named like them.
READERS = {"env_str", "env_flag", "env_float", "env_int", "env_csv", "_env_flag", "_flag", "_csv_env"}
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,}")

HEADER = """\
# Configuration reference

Every environment variable ICEMAN reads, generated from the source by
`python scripts/gen_config_reference.py` (CI fails when this file is stale).
Values come from the process environment or `.env` (see `.env.example` for the
settings operators normally touch; everything else is a tuning knob with a
safe default). `python -m enrichment.lanes` sets its own per-batch defaults
(`LANE_DEFAULT_ENV` in enrichment/lanes.py, e.g. `APPLY_BATCH_SIZE=10`) and
splits `--gemini-rpm-total` (default `GEMINI_RPM` or 60) and
`--claude-rpm-total` (default `CLAUDE_RPM` or 50) across lanes.

Booleans accept `1` / `true` / `yes` (case-insensitive); anything else is off.
Blank or unparsable numbers fall back to the default. "computed" means the
default is an expression or module constant, and "unset" means the read has
no inline default (the code decides what absence means); see the file.

| Variable | Default | Read in |
|---|---|---|
"""


def _source_files() -> list[Path]:
    # Tracked plus new (not yet added) files, as they are on disk now.
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"], cwd=ROOT, text=True
    ).split()
    return sorted({ROOT / n for n in names
                   if "/tests/" not in n and n != "conftest.py" and (ROOT / n).is_file()})


def _env_name(node: ast.Call) -> str | None:
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        name = node.args[0].value
        if ENV_NAME.fullmatch(name):
            return name
    return None


def _is_reader(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id in READERS
    if isinstance(func, ast.Attribute):
        # os.getenv(...), os.environ.get(...)
        if func.attr == "getenv":
            return True
        if func.attr == "get" and isinstance(func.value, ast.Attribute) and func.value.attr == "environ":
            return True
        return func.attr in READERS
    return False


def _default(node: ast.Call) -> str:
    arg = node.args[1] if len(node.args) > 1 else next(
        (kw.value for kw in node.keywords if kw.arg == "default"), None)
    if arg is None:
        return "off" if getattr(node.func, "id", getattr(node.func, "attr", "")).endswith("flag") else "unset"
    if isinstance(arg, ast.Constant):
        value = arg.value
        if isinstance(value, bool):
            return "on" if value else "off"
        if value == "":
            return "unset"
        if isinstance(value, str) and getattr(node.func, "id", getattr(node.func, "attr", "")).endswith("flag"):
            return "on" if value.strip().lower() in {"1", "true", "yes"} else "off"
        return f"`{value}`"
    return "computed"


def _or_defaults(tree: ast.AST) -> dict[int, str]:
    """``reader("X") or <literal>``: map the reader call's id to that literal."""
    out: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and len(node.values) >= 2:
            first, fallback = node.values[0], node.values[-1]
            if isinstance(first, ast.Call) and isinstance(fallback, ast.Constant) and fallback.value not in ("", None):
                out[id(first)] = f"`{fallback.value}`"
    return out


def collect() -> dict[str, dict[str, set[str]]]:
    found: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"defaults": set(), "files": set()})
    for path in _source_files():
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        or_defaults = _or_defaults(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) \
                    and node.value.attr == "environ" and isinstance(node.slice, ast.Constant) \
                    and isinstance(node.slice.value, str):
                found[node.slice.value]["files"].add(rel)
                found[node.slice.value]["defaults"].add("required")
                continue
            if not (isinstance(node, ast.Call) and _is_reader(node.func)):
                continue
            name = _env_name(node)
            if name:
                found[name]["files"].add(rel)
                default = _default(node)
                found[name]["defaults"].add(or_defaults.get(id(node), default) if default == "unset" else default)
    return found


def render() -> str:
    rows = []
    for name, info in sorted(collect().items()):
        defaults = sorted(info["defaults"] - {"required"}) or ["required"]
        if len(defaults) > 1 and "unset" in defaults:
            defaults.remove("unset")
        rows.append(f"| `{name}` | {' / '.join(defaults)} | {', '.join(sorted(info['files']))} |")
    return HEADER + "\n".join(rows) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if docs/configuration.md is stale")
    args = parser.parse_args()
    text = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run python scripts/gen_config_reference.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({text.count(chr(10)) - HEADER.count(chr(10))} variables)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
