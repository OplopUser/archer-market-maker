#!/usr/bin/env python3
"""Run a no-live Archer shadow observation review from retained artifacts."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from scripts.archer_shadow_metrics_capture import capture_once


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))
    return events


def load_policy(path: Optional[Path]) -> Dict[str, Any]:
    if not path:
        return {}
    return json.loads(path.read_text(errors="replace"))


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first_value(events: Iterable[Dict[str, Any]], key: str, default: Any = None) -> Any:
    for event in events:
        if event.get(key) is not None:
            return event.get(key)
    return default


def _status(has_failures: bool, has_warnings: bool) -> str:
    if has_failures:
        return "fail"
    if has_warnings:
        return "warn"
    return "pass"


def evaluate_shadow_events(
    events: List[Dict[str, Any]],
    *,
    policy: Optional[Dict[str, Any]] = None,
    observation_seconds: int = 0,
    capture_path: Optional[Path] = None,
) -> Dict[str, Any]:
    policy = policy or {}
    failures: List[str] = []
    warnings: List[str] = []
    evaluations: List[Dict[str, Any]] = []

    if not events:
        failures.append("no_capture_events")

    max_failed_tx = _as_int(policy.get("max_failed_tx"), 0)
    allow_transactions = bool(policy.get("allow_transactions", False))
    require_market_intel_ok = bool(policy.get("require_market_intel_ok", False))
    makerbook_policy = policy.get("makerbook", {}) if isinstance(policy.get("makerbook"), dict) else {}

    for index, event in enumerate(events):
        event_codes: List[str] = []
        tx = event.get("tx_summary", {}) if isinstance(event.get("tx_summary"), dict) else {}
        status = (
            event.get("makerbook_status", {})
            if isinstance(event.get("makerbook_status"), dict)
            else {}
        )
        intel = (
            event.get("market_intel_snapshot", {})
            if isinstance(event.get("market_intel_snapshot"), dict)
            else {}
        )
        simulated = (
            event.get("simulated_makerbook_update", {})
            if isinstance(event.get("simulated_makerbook_update"), dict)
            else {}
        )

        if event.get("mode") != "shadow":
            failures.append("mode_not_shadow")
            event_codes.append("mode_not_shadow")

        tx_count = _as_int(tx.get("since_start_count"), 0)
        failed_tx = _as_int(tx.get("failed_count"), 0)
        if tx_count > 0 and not allow_transactions:
            failures.append("transactions_observed_in_shadow")
            event_codes.append("transactions_observed_in_shadow")
        if failed_tx > max_failed_tx:
            failures.append("failed_tx_over_policy")
            event_codes.append("failed_tx_over_policy")

        if status.get("command_ok") is False:
            failures.append("makerbook_read_unhealthy")
            event_codes.append("makerbook_read_unhealthy")
        if status.get("stale"):
            warnings.append("makerbook_status_stale")
            event_codes.append("makerbook_status_stale")

        if require_market_intel_ok and intel.get("ok") is not True:
            failures.append("market_intel_not_ok")
            event_codes.append("market_intel_not_ok")

        max_bid_levels = makerbook_policy.get("max_bid_levels")
        max_ask_levels = makerbook_policy.get("max_ask_levels")
        max_quote_notional = makerbook_policy.get("max_quote_notional")
        simulated_violations: List[str] = []
        if max_bid_levels is not None and _as_int(simulated.get("bid_levels")) > _as_int(max_bid_levels):
            simulated_violations.append("bid_levels")
        if max_ask_levels is not None and _as_int(simulated.get("ask_levels")) > _as_int(max_ask_levels):
            simulated_violations.append("ask_levels")
        if max_quote_notional is not None and _as_float(simulated.get("quote_notional")) > _as_float(max_quote_notional):
            simulated_violations.append("quote_notional")
        if simulated_violations:
            failures.append("simulated_update_policy_violation")
            event_codes.append("simulated_update_policy_violation")

        evaluations.append(
            {
                "index": index,
                "timestamp": event.get("timestamp"),
                "source": event.get("source"),
                "reason_codes": event_codes,
                "simulated_policy_violations": simulated_violations,
            }
        )

    reason_codes = sorted(set(failures + warnings))
    policy_version = policy.get("version") or _first_value(events, "policy_version")
    return {
        "run_id": _first_value(events, "run_id", "unknown"),
        "started_at": events[0].get("timestamp") if events else None,
        "completed_at": events[-1].get("timestamp") if events else None,
        "mode": "shadow",
        "venue": "archer",
        "market": _first_value(events, "market", "unknown"),
        "observation_window_seconds": observation_seconds,
        "policy_version": policy_version,
        "status": _status(bool(failures), bool(warnings)),
        "reason_codes": reason_codes,
        "event_count": len(events),
        "artifact_paths": {
            "capture": str(capture_path) if capture_path else None,
        },
        "sources": [event.get("source") for event in events],
        "evaluations": evaluations,
    }


def review_capture_file(
    capture_path: Path,
    *,
    output_path: Optional[Path] = None,
    policy_path: Optional[Path] = None,
    observation_seconds: int = 0,
) -> Dict[str, Any]:
    events = load_jsonl(capture_path)
    review = evaluate_shadow_events(
        events,
        policy=load_policy(policy_path),
        observation_seconds=observation_seconds,
        capture_path=capture_path,
    )
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(review, indent=2, sort_keys=True) + "\n")
    return review


def run_shadow_observation(
    sources: List[str],
    output_dir: Path,
    *,
    run_id: str,
    observation_seconds: int,
    interval_seconds: float,
    policy_path: Optional[Path] = None,
    market: Optional[str] = None,
) -> Dict[str, Any]:
    """Collect fixture/URL metrics for a fixed no-live window, then review them."""
    if not sources:
        raise ValueError("at least one metrics source is required")
    output_dir.mkdir(parents=True, exist_ok=True)
    capture_path = output_dir / f"archer-shadow-{run_id}.jsonl"
    review_path = output_dir / f"archer-shadow-{run_id}-review.json"

    start = time.monotonic()
    source_index = 0
    all_sources_are_files = all(
        not (source.startswith("http://") or source.startswith("https://"))
        for source in sources
    )
    while True:
        source = sources[source_index % len(sources)]
        capture_once(source, capture_path, run_id=run_id, market=market)
        source_index += 1
        elapsed = time.monotonic() - start
        if elapsed >= observation_seconds:
            break
        if all_sources_are_files and source_index >= len(sources):
            break
        time.sleep(max(0.0, min(interval_seconds, observation_seconds - elapsed)))

    return review_capture_file(
        capture_path,
        output_path=review_path,
        policy_path=policy_path,
        observation_seconds=observation_seconds,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-file", action="append", default=[], help="dashboard metrics fixture/path")
    parser.add_argument("--metrics-url", action="append", default=[], help="dashboard /api/metrics URL")
    parser.add_argument("--capture", type=Path, help="existing retained capture JSONL to review")
    parser.add_argument("--output-dir", type=Path, default=Path("logs/archer-shadow"))
    parser.add_argument("--output", type=Path, help="review JSON output for --capture mode")
    parser.add_argument("--run-id", default=str(int(time.time())))
    parser.add_argument("--observation-seconds", type=int, default=60)
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument("--policy", type=Path, default=None)
    parser.add_argument("--market", default=None)
    args = parser.parse_args()

    if args.capture:
        review = review_capture_file(
            args.capture,
            output_path=args.output,
            policy_path=args.policy,
            observation_seconds=args.observation_seconds,
        )
    else:
        sources = args.metrics_file + args.metrics_url
        review = run_shadow_observation(
            sources,
            args.output_dir,
            run_id=args.run_id,
            observation_seconds=args.observation_seconds,
            interval_seconds=args.interval_seconds,
            policy_path=args.policy,
            market=args.market,
        )
    print(json.dumps(review, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
