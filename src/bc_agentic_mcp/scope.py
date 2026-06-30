"""Scope boundary enforcement. See spec Section 3.9."""
from pathlib import Path
from typing import List, Optional, Set


class ScopeEnforcer:
    """Enforces file-scope boundaries from spec.json."""

    def __init__(
        self,
        allowed_files: List[str],
        project_root: Path,
        allowed_extensions: Optional[List[str]] = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.allowed_files: Set[Path] = {
            (self.project_root / f).resolve() for f in allowed_files
        }
        self.allowed_extensions: Set[str] = set(allowed_extensions or [])

    def _under_root(self, target: Path) -> bool:
        """True only if target is genuinely inside project_root.

        Uses Path.is_relative_to to avoid sibling-prefix bypass
        (e.g. /a/proj vs /a/proj-evil that str.startswith would allow).
        """
        return target == self.project_root or target.is_relative_to(self.project_root)

    def _is_in_allowed_extension(self, file_path: str) -> bool:
        """Check if the file's extension matches an allowed extension."""
        if not self.allowed_extensions:
            return True  # permissive when no extensions declared
        target = (self.project_root / file_path).resolve()
        rel = target.relative_to(self.project_root)
        # First path component is the extension directory
        first_part = rel.parts[0] if rel.parts else ""
        return first_part in self.allowed_extensions

    def check_write(self, file_path: str) -> bool:
        """Check if writing to this file is within scope."""
        target = (self.project_root / file_path).resolve()
        if not self._under_root(target):
            return False
        if target not in self.allowed_files:
            return False
        return True

    def check_create(self, file_path: str) -> bool:
        """Check if creating this file is within scope and allowed extension."""
        target = (self.project_root / file_path).resolve()
        if not self._under_root(target):
            return False
        if not self._is_in_allowed_extension(file_path):
            return False
        return True

    def block_reason(self, file_path: str) -> str:
        """Explain why a file access was blocked."""
        target = (self.project_root / file_path).resolve()
        if not self._under_root(target):
            return f"File {file_path} is outside project root"
        if self.allowed_extensions and not self._is_in_allowed_extension(file_path):
            return f"File {file_path} is not in an allowed extension: {', '.join(sorted(self.allowed_extensions))}"
        return f"File {file_path} is not in declared scope boundaries"
