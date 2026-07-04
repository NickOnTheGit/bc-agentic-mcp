"""Capability guard tests — decision logic (pure) + audit trail.

The audit hook itself is process-global and cannot be uninstalled, so tests for
the raising path run in a SUBPROCESS; in-process tests cover the pure decision
function and the audit record shape.
"""
import json
import subprocess
import sys

from bc_agentic_mcp import capabilities


def test_check_exec_allows_declared_programs():
    for exe in ("git", "GIT.EXE", r"C:\Program Files\Git\cmd\git.exe",
                "docker", "pwsh", "powershell.exe", "dotnet", "alc.exe"):
        allowed, name = capabilities.check_exec(exe)
        assert allowed, f"{exe} should be allowed (resolved: {name})"


def test_check_exec_allows_python_interpreters():
    for exe in (sys.executable, "python", "python3.13", "py.exe"):
        allowed, _ = capabilities.check_exec(exe)
        assert allowed, f"{exe} should be allowed"


def test_check_exec_refuses_undeclared_programs():
    for exe in ("curl", "certutil.exe", "mshta", "wscript", "regsvr32",
                r"C:\temp\evil.exe", "bash"):
        allowed, name = capabilities.check_exec(exe)
        assert not allowed, f"{exe} must be refused (resolved: {name})"


def test_guard_blocks_disallowed_exec_in_subprocess(tmp_path):
    """End-to-end: install the hook in a child process, try an undeclared exec."""
    script = (
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "from bc_agentic_mcp import capabilities\n"
        f"capabilities.install(audit_dir=Path({str(tmp_path)!r}))\n"
        "try:\n"
        "    subprocess.run(['certutil', '-ping'], capture_output=True)\n"
        "    print('NOT-BLOCKED')\n"
        "except PermissionError as exc:\n"
        "    print('BLOCKED:', str(exc)[:60])\n"
        "out = subprocess.run(['git', '--version'], capture_output=True, text=True)\n"
        "print('GIT-OK' if out.returncode == 0 else 'GIT-FAILED')\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=60)
    assert "BLOCKED: capability guard" in proc.stdout, proc.stdout + proc.stderr
    assert "GIT-OK" in proc.stdout, proc.stdout + proc.stderr
    # Audit trail recorded both decisions.
    audit = (tmp_path / "subprocess.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in audit]
    execs = [r for r in records if r.get("event") == "exec"]
    assert any(r["name"] == "certutil" and r["allowed"] is False for r in execs)
    assert any(r["name"] == "git" and r["allowed"] is True for r in execs)
