#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

RPC_KEY_MARKER = "api" + "-key="
RPC_KEY_MARKER_COMPACT = "api" + "key="
PRIVATE_KEY_MARKER = "PRIVATE" + " KEY"
SKIP_DIRS = {".git", "target", "node_modules", ".venv", "__pycache__"}
ALLOW_MARKERS = {"REDACTED", "REPLACE_ME", "YOUR_KEY", "example.invalid"}
ALLOW_MARKERS.update(
    {
        RPC_KEY_MARKER + "test",
        RPC_KEY_MARKER + "x",
        RPC_KEY_MARKER + "secret",
        RPC_KEY_MARKER + "live-secret",
    }
)


def tracked_files(staged: bool) -> list[Path]:
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"] if staged else ["git", "ls-files"]
    result = subprocess.run(cmd, text=True, capture_output=True, check=True)
    return [Path(line) for line in result.stdout.splitlines() if line.strip()]


def should_skip(path: Path) -> bool:
    return (
        any(part in SKIP_DIRS for part in path.parts)
        or path.is_dir()
        or path.suffix in {".md", ".rst"}
    )


def allowed(line: str) -> bool:
    return any(marker in line for marker in ALLOW_MARKERS)


def scan_file(path: Path) -> list[str]:
    findings: list[str] = []
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return findings
    for line_no, line in enumerate(text.splitlines(), start=1):
        lower = line.lower()
        if (
            (RPC_KEY_MARKER in lower or RPC_KEY_MARKER_COMPACT in lower)
            and "http" in lower
            and not allowed(line)
        ):
            findings.append(f"{path}:{line_no}: rpc_api_key_literal")
        if PRIVATE_KEY_MARKER in line and not allowed(line):
            findings.append(f"{path}:{line_no}: private_key_material")
        if "keypair" in lower and ("/users/" in lower or "/home/" in lower):
            findings.append(f"{path}:{line_no}: wallet_path_literal")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true", help="scan only staged files")
    args = parser.parse_args()
    findings: list[str] = []
    for path in tracked_files(args.staged):
        if not should_skip(path) and path.exists():
            findings.extend(scan_file(path))
    if findings:
        print("Key hygiene findings:", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("Key hygiene scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
