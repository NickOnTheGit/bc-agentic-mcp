"""CI guard: ensure guideline rule manifest stays in sync with policy implementation.

Usage:
  python -m bc_agentic_mcp.guideline_manifest_check
  python -m bc_agentic_mcp.guideline_manifest_check --project-root <repo>

Exit codes:
  0 -> in sync
  1 -> drift detected (missing/extra codes)
  2 -> invalid inputs (files missing/unreadable)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Set


CODE_RE = re.compile(r"\bGL-[A-Z0-9]{2,}\b")


def collect_codes_from_policy(policy_file: Path) -> Set[str]:
    text = policy_file.read_text(encoding="utf-8", errors="replace")
    return set(CODE_RE.findall(text))


def load_manifest_codes(manifest_file: Path) -> Set[str]:
    data = json.loads(manifest_file.read_text(encoding="utf-8", errors="replace"))
    out: Set[str] = set()
    for entry in data.get("rules", []) or []:
        if isinstance(entry, dict):
            code = str(entry.get("code") or "").strip()
            if code:
                out.add(code)
    return out


def validate_manifest_sync(project_root: Path) -> Dict[str, object]:
    policy_file = project_root / "src" / "bc_agentic_mcp" / "guidelines_policy.py"
    manifest_file = project_root / ".specs" / "policy" / "guideline_rule_manifest.json"

    if not policy_file.exists() or not manifest_file.exists():
        return {
            "ok": False,
            "missing_files": [
                str(p) for p in (policy_file, manifest_file) if not p.exists()
            ],
            "missing_in_manifest": [],
            "extra_in_manifest": [],
        }

    try:
        policy_codes = collect_codes_from_policy(policy_file)
        manifest_codes = load_manifest_codes(manifest_file)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "error": str(exc),
            "missing_in_manifest": [],
            "extra_in_manifest": [],
        }

    missing = sorted(c for c in policy_codes if c not in manifest_codes)
    extra = sorted(c for c in manifest_codes if c not in policy_codes)

    return {
        "ok": not missing and not extra,
        "missing_in_manifest": missing,
        "extra_in_manifest": extra,
        "policy_count": len(policy_codes),
        "manifest_count": len(manifest_codes),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate guideline rule manifest sync")
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args(argv)

    root = Path(args.project_root).resolve()
    result = validate_manifest_sync(root)

    if result.get("missing_files"):
        print("manifest-check: INVALID - missing files:")
        for p in result["missing_files"]:
            print(f"  - {p}")
        return 2

    if result.get("error"):
        print(f"manifest-check: INVALID - {result['error']}")
        return 2

    if result["ok"]:
        print(
            "manifest-check: OK "
            f"(policy={result.get('policy_count', 0)}, manifest={result.get('manifest_count', 0)})"
        )
        return 0

    print("manifest-check: DRIFT detected")
    missing = result.get("missing_in_manifest", [])
    extra = result.get("extra_in_manifest", [])
    if missing:
        print("  missing_in_manifest:")
        for c in missing:
            print(f"    - {c}")
    if extra:
        print("  extra_in_manifest:")
        for c in extra:
            print(f"    - {c}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
