"""Regenerate the prompt-CI hash manifest — run after any DELIBERATE prompt change:

    python -m tests.evals.regen_prompt_hashes
"""
import json
from pathlib import Path

from tests.evals.test_outcome_evals import MANIFEST, current_prompt_hashes


def main() -> None:
    hashes = current_prompt_hashes()
    MANIFEST.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"pinned {len(hashes)} prompt/agent files -> {MANIFEST}")


if __name__ == "__main__":
    main()
