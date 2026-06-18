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


def _average(values: Iterable[float]) -> Optional[float]:
    values = [value for value in values if isinstance(value, (int, float))]
    if not values:
        return None
    return sum(values) / len(values)


def _status(has_failures: bool, has_warnings: bool) -> str:
    if has_failures:
        return "fail"
    if has_warnings:
        return "warn"
    return "pass"


def _control_validation(events: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    for event in events:
        control = event.get(key)
        if isinstance(control, dict):
            return {
                "status": str(control.get("status") or "warn"),
                "details": control,
            }
    return {"status": "warn", "details": {}, "reason": "missing"}


def _route_quality(events: List[Dict[str, Any]], policy: Dict[str, Any]) -> Dict[str, Any]:
    scores = [
        _as_float(event.get("route_quality", {}).get("score"), default=float("nan"))
        for event in events
        if isinstance(event.get("route_quality"), dict)
    ]
    scores = [score for score in scores if score == score]
    min_score = min(scores) if scores else None
    threshold = policy.get("min_route_quality_score")
    if min_score is None:
        status = "warn"
    elif threshold is not None and min_score < _as_float(threshold):
        status = "fail"
    else:
        status = "pass"
    return {
        "status": status,
        "min_score": min_score,
        "threshold": threshold,
        "samples": len(scores),
    }


def _no_fill_exposure(events: List[Dict[str, Any]], policy: Dict[str, Any]) -> Dict[str, Any]:
    quote_notionals: List[float] = []
    for event in events:
        exposure = event.get("no_fill_exposure")
        if not isinstance(exposure, dict):
            continue
        quote_notionals.append(
            _as_float(exposure.get("bid_notional")) + _as_float(exposure.get("ask_notional"))
        )
    max_quote_notional = max(quote_notionals) if quote_notionals else 0.0
    threshold = policy.get("max_no_fill_quote_notional")
    status = (
        "fail"
        if threshold is not None and max_quote_notional > _as_float(threshold)
        else "pass"
    )
    return {
        "status": status,
        "max_quote_notional": max_quote_notional,
        "threshold": threshold,
        "samples": len(quote_notionals),
    }


def _expected_fill_edge(events: List[Dict[str, Any]], policy: Dict[str, Any]) -> Dict[str, Any]:
    fill_probabilities = []
    edge_bps = []
    for event in events:
        expected_fill = event.get("expected_fill")
        expected_edge = event.get("expected_edge")
        if isinstance(expected_fill, dict):
            fill_probabilities.append(_as_float(expected_fill.get("probability"), default=float("nan")))
        if isinstance(expected_edge, dict):
            edge_bps.append(_as_float(expected_edge.get("bps"), default=float("nan")))
    fill_probabilities = [value for value in fill_probabilities if value == value]
    edge_bps = [value for value in edge_bps if value == value]
    avg_edge = _average(edge_bps)
    threshold = policy.get("min_expected_edge_bps")
    status = (
        "fail"
        if avg_edge is not None and threshold is not None and avg_edge < _as_float(threshold)
        else "pass"
    )
    return {
        "status": status,
        "avg_expected_fill_probability": _average(fill_probabilities),
        "avg_expected_edge_bps": avg_edge,
        "min_expected_edge_bps": threshold,
        "samples": max(len(fill_probabilities), len(edge_bps)),
    }


def _promotion_gate(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    inputs: Dict[str, Any] = {}
    for event in events:
        candidate = event.get("promotion_inputs")
        if isinstance(candidate, dict):
            inputs = candidate
            break
    shadow_passed = bool(inputs.get("shadow_passed", False))
    live_canary_passed = bool(inputs.get("live_canary_passed", False))
    after_cost_edge = _as_float(inputs.get("after_cost_edge_bps"))
    min_after_cost_edge = _as_float(inputs.get("min_after_cost_edge_bps"))
    after_cost_edge_pass = after_cost_edge >= min_after_cost_edge
    cross_venue_safe = bool(inputs.get("cross_venue_safe", False))
    requested_capital = _as_float(inputs.get("requested_capital_usdc"))
    approved_capital = _as_float(inputs.get("approved_capital_usdc"))
    capital_increase_allowed = (
        shadow_passed
        and live_canary_passed
        and after_cost_edge_pass
        and cross_venue_safe
        and requested_capital <= approved_capital
    )
    multi_venue_allowed = capital_increase_allowed and cross_venue_safe
    reason_codes: List[str] = []
    if not shadow_passed:
        reason_codes.append("shadow_not_passed")
    if not live_canary_passed:
        reason_codes.append("live_canary_not_passed")
    if not after_cost_edge_pass:
        reason_codes.append("after_cost_edge_below_floor")
    if not cross_venue_safe:
        reason_codes.append("cross_venue_not_safe")
    if requested_capital > approved_capital:
        reason_codes.append("capital_request_above_limit")
    return {
        "shadow_passed": shadow_passed,
        "live_canary_passed": live_canary_passed,
        "after_cost_edge_pass": after_cost_edge_pass,
        "cross_venue_safe": cross_venue_safe,
        "capital_increase_allowed": capital_increase_allowed,
        "multi_venue_allowed": multi_venue_allowed,
        "reason_codes": reason_codes,
    }


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
    route_quality = _route_quality(events, policy)
    no_fill_exposure = _no_fill_exposure(events, policy)
    expected_fill_edge = _expected_fill_edge(events, policy)
    promotion_gate = _promotion_gate(events)
    blockers: List[str] = []
    if route_quality["status"] == "fail":
        blockers.append("route_quality_below_floor")
    if no_fill_exposure["status"] == "fail":
        blockers.append("no_fill_exposure_over_policy")
    if expected_fill_edge["status"] == "fail":
        blockers.append("expected_edge_below_floor")
    blockers.extend(promotion_gate["reason_codes"])
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
        "config_checksum": policy.get("config_checksum") or _first_value(events, "config_checksum"),
        "control_validation": {
            "static_config": _control_validation(events, "static_config"),
            "signal_multipliers": _control_validation(events, "signal_multipliers"),
            "quote_policy": _control_validation(events, "quote_policy_control"),
        },
        "route_quality": route_quality,
        "no_fill_exposure": no_fill_exposure,
        "expected_fill_edge": expected_fill_edge,
        "blockers": sorted(set(blockers)),
        "promotion_gate": promotion_gate,
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
    policy = load_policy(policy_path)
    policy_version = policy.get("version")

    start = time.monotonic()
    source_index = 0
    while True:
        source = sources[source_index % len(sources)]
        capture_once(
            source,
            capture_path,
            run_id=run_id,
            market=market,
            policy_version=policy_version,
        )
        source_index += 1
        elapsed = time.monotonic() - start
        if elapsed >= observation_seconds:
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
