#!/usr/bin/env python3
"""Plan and optionally execute operator-safe Archer service commands.

The default behavior is dry-run JSON. Live-capable actions require explicit
confirmation flags and environment gates before this wrapper will execute them.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_FILE = "deploy/docker-compose.archer.yml"
DEFAULT_SHADOW_CONFIG = "config/default.toml"
DEFAULT_CANARY_CONFIG = "config/canary/archer-sol-usdc-first-live.toml"
DEFAULT_ENVELOPE_FILE = "config/canary/archer-sol-usdc-first-live-envelope.toml"
DEFAULT_ROLLBACK_COMMAND = "scripts/archer_ops.py rollback-stopped"


def rel(path: str) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate)
    return str(candidate)


def preflight_command(
    *,
    mode: str,
    run_id: str,
    config_file: str,
    phase: str,
    envelope_file: str = "",
    rollback_command: str = "",
) -> list[str]:
    command = [
        "python3",
        "scripts/archer_preflight_gate.py",
        "--metrics-url",
        "http://127.0.0.1:8787/api/metrics",
        "--expected-run-id",
        run_id,
        "--expected-mode",
        mode,
        "--config-file",
        rel(config_file),
    ]
    if phase == "static":
        command.append("--static-only")
    elif phase == "post-start":
        command.append("--post-start")
    else:
        raise ValueError(f"unknown preflight phase: {phase}")
    if rollback_command:
        command.extend(["--rollback-command", rollback_command])
    if envelope_file:
        command.extend(["--canary-envelope-file", rel(envelope_file), "--require-canary-envelope"])
    return command


def docker_compose(compose_file: str, *args: str) -> list[str]:
    return ["docker", "compose", "-f", rel(compose_file), *args]


def with_env(command: list[str], **env: str) -> list[str]:
    return ["env", *[f"{key}={value}" for key, value in env.items()], *command]


def require_canary_gates(env: dict[str, str], confirm_live_canary: bool) -> None:
    if not confirm_live_canary:
        raise ValueError("canary-start requires --confirm-live-canary")
    if env.get("ARCHER_ENABLE_LIVE_TRADING") != "true":
        raise ValueError("canary-start requires ARCHER_ENABLE_LIVE_TRADING=true")
    if env.get("ARCHER_CANARY_PROFILE") != "first-live-sol-usdc":
        raise ValueError("canary-start requires ARCHER_CANARY_PROFILE=first-live-sol-usdc")
    if not env.get("ARCHER_CANARY_APPROVAL_ID"):
        raise ValueError("canary-start requires ARCHER_CANARY_APPROVAL_ID")


def build_plan(
    action: str,
    *,
    run_id: str,
    env: dict[str, str] | None = None,
    execute: bool = False,
    confirm_live_canary: bool = False,
    confirm_emergency_clear: bool = False,
    compose_file: str = DEFAULT_COMPOSE_FILE,
    shadow_config: str = DEFAULT_SHADOW_CONFIG,
    canary_config: str = DEFAULT_CANARY_CONFIG,
    envelope_file: str = DEFAULT_ENVELOPE_FILE,
    rollback_command: str = DEFAULT_ROLLBACK_COMMAND,
) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    commands: list[list[str]] = []
    executes_live_transactions = False
    mode = "stopped"
    notes: list[str] = []

    if action == "shadow-start":
        mode = "shadow"
        commands.append(
            preflight_command(
                mode="shadow",
                run_id=run_id,
                config_file=shadow_config,
                phase="static",
            )
        )
        commands.append(
            with_env(
                docker_compose(
                    compose_file,
                    "--profile",
                    "shadow",
                    "up",
                    "-d",
                    "archer-dashboard",
                    "archer-shadow-runner",
                ),
                ARCHER_RUN_ID=run_id,
                ARCHER_RUN_MODE="shadow",
                ARCHER_DASHBOARD_CONFIG=rel(shadow_config),
                ARCHER_SHADOW_CONFIG=rel(shadow_config),
            )
        )
        commands.append(
            preflight_command(
                mode="shadow",
                run_id=run_id,
                config_file=shadow_config,
                phase="post-start",
            )
        )
        notes.append("Shadow start does not set ARCHER_ENABLE_LIVE_TRADING and runs --shadow.")
    elif action == "canary-start":
        require_canary_gates(env, confirm_live_canary)
        mode = "canary"
        executes_live_transactions = execute
        commands.append(
            preflight_command(
                mode="canary",
                run_id=run_id,
                config_file=canary_config,
                phase="static",
                envelope_file=envelope_file,
                rollback_command=rollback_command,
            )
        )
        commands.append(
            with_env(
                docker_compose(
                    compose_file,
                    "--profile",
                    "capped-live",
                    "up",
                    "-d",
                    "archer-dashboard",
                    "archer-canary-runner",
                ),
                ARCHER_RUN_ID=run_id,
                ARCHER_RUN_MODE="canary",
                ARCHER_DASHBOARD_CONFIG=rel(canary_config),
                ARCHER_CANARY_CONFIG=rel(canary_config),
                ARCHER_CANARY_ENVELOPE=rel(envelope_file),
                ARCHER_ENABLE_LIVE_TRADING=env["ARCHER_ENABLE_LIVE_TRADING"],
                ARCHER_CANARY_PROFILE=env["ARCHER_CANARY_PROFILE"],
                ARCHER_CANARY_APPROVAL_ID=env["ARCHER_CANARY_APPROVAL_ID"],
            )
        )
        commands.append(
            preflight_command(
                mode="canary",
                run_id=run_id,
                config_file=canary_config,
                phase="post-start",
                envelope_file=envelope_file,
                rollback_command=rollback_command,
            )
        )
        notes.append("Canary start is gated by live env, canary profile, approval id, and envelope.")
    elif action == "graceful-stop":
        commands.append(
            docker_compose(
                compose_file,
                "stop",
                "archer-shadow-runner",
                "archer-canary-runner",
                "archer-controller",
            )
        )
        commands.append(["python3", "scripts/archer_ops.py", "clean-book", "--run-id", run_id])
    elif action == "emergency-clear":
        mode = "emergency-clear"
        if execute or confirm_emergency_clear:
            if not confirm_emergency_clear:
                raise ValueError("emergency-clear execution requires --confirm-emergency-clear")
            if env.get("ARCHER_ENABLE_LIVE_TRADING") != "true":
                raise ValueError("emergency-clear execution requires ARCHER_ENABLE_LIVE_TRADING=true")
            executes_live_transactions = execute
            commands.append(
                [
                    "cargo",
                    "run",
                    "--release",
                    "--",
                    "kill",
                    "--config",
                    rel(canary_config),
                ]
            )
        else:
            notes.append("Dry-run emergency clear: no kill transaction is planned without confirmation.")
        commands.append(["python3", "scripts/archer_ops.py", "clean-book", "--run-id", run_id])
    elif action == "rollback-stopped":
        commands.append(
            docker_compose(
                compose_file,
                "down",
                "--remove-orphans",
            )
        )
        commands.append(["python3", "scripts/archer_ops.py", "emergency-clear", "--run-id", run_id])
        commands.append(["python3", "scripts/archer_ops.py", "clean-book", "--run-id", run_id])
        notes.append("Rollback default is stopped plus dry-run clear verification; live clear needs explicit confirmation.")
    elif action == "clean-book":
        mode = "clean-book"
        commands.append(
            [
                "cargo",
                "run",
                "--release",
                "--",
                "status",
                "--config",
                rel(canary_config),
            ]
        )
        notes.append("Clean-book verification is read-only status collection.")
    else:
        raise ValueError(f"unknown Archer ops action: {action}")

    return {
        "action": action,
        "mode": mode,
        "run_id": run_id,
        "dry_run": not execute,
        "executes_live_transactions": executes_live_transactions,
        "commands": commands,
        "notes": notes,
    }


def write_audit(plan: dict[str, Any]) -> Path:
    audit_dir = ROOT / "logs" / "archer-ops"
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{plan['run_id']}-{plan['action']}.json"
    path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    return path


def execute_plan(plan: dict[str, Any]) -> int:
    for command in plan["commands"]:
        subprocess.run(command, cwd=str(ROOT), check=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "shadow-start",
            "canary-start",
            "graceful-stop",
            "emergency-clear",
            "rollback-stopped",
            "clean-book",
        ],
    )
    parser.add_argument("--run-id", default=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-live-canary", action="store_true")
    parser.add_argument("--confirm-emergency-clear", action="store_true")
    parser.add_argument("--compose-file", default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--shadow-config", default=DEFAULT_SHADOW_CONFIG)
    parser.add_argument("--canary-config", default=DEFAULT_CANARY_CONFIG)
    parser.add_argument("--envelope-file", default=DEFAULT_ENVELOPE_FILE)
    args = parser.parse_args(argv)

    plan = build_plan(
        args.action,
        run_id=args.run_id,
        execute=args.execute,
        confirm_live_canary=args.confirm_live_canary,
        confirm_emergency_clear=args.confirm_emergency_clear,
        compose_file=args.compose_file,
        shadow_config=args.shadow_config,
        canary_config=args.canary_config,
        envelope_file=args.envelope_file,
    )
    audit_path = write_audit(plan)
    print(json.dumps({**plan, "audit_path": str(audit_path)}, indent=2, sort_keys=True))
    if args.execute:
        return execute_plan(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
