"""Scope boundary enforcement. See spec Section 3.9."""
from pathlib import Path
from typing import List, Set


class ScopeEnforcer:
    """Enforces file-scope boundaries from spec.json."""

    def __init__(self, allowed_files: List[str], project_root: Path):
        self.project_root = Path(project_root).resolve()
        self.allowed_files: Set[Path] = {
            (self.project_root / f).resolve() for f in allowed_files
        }

    def _under_root(self, target: Path) -> bool:
        """True only if target is genuinely inside project_root.

        Uses Path.is_relative_to to avoid sibling-prefix bypass
        (e.g. /a/proj vs /a/proj-evil that str.startswith would allow).
        """
        return target == self.project_root or target.is_relative_to(self.project_root)

    def check_write(self, file_path: str) -> bool:
        """Check if writing to this file is within scope."""
        target = (self.project_root / file_path).resolve()
        if not self._under_root(target):
            return False
        if target not in self.allowed_files:
            return False
        return True

    def check_create(self, file_path: str) -> bool:
        """Check if creating this file is within scope."""
        target = (self.project_root / file_path).resolve()
        return self._under_root(target)

    def block_reason(self, file_path: str) -> str:
        """Explain why a file access was blocked."""
        target = (self.project_root / file_path).resolve()
        if not self._under_root(target):
            return f"File {file_path} is outside project root"
        return f"File {file_path} is not in declared scope boundaries"
