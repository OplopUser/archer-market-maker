#!/usr/bin/env python3
"""Analyze Archer dashboard samples for fill quality and fee drag."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable


def as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def load_samples(path: Path) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            samples.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return samples


def sample_at_or_after(samples: list[dict[str, Any]], start_index: int, horizon_seconds: float) -> dict[str, Any]:
    start_time = parse_time(samples[start_index].get("time"))
    if start_time is None:
        return samples[-1]
    target = start_time + horizon_seconds
    for sample in samples[start_index + 1 :]:
        ts = parse_time(sample.get("time"))
        if ts is not None and ts >= target:
            return sample
    return samples[-1]


def parse_time(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def infer_fills(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fills: list[dict[str, Any]] = []
    previous = samples[0]
    for index, sample in enumerate(samples[1:], start=1):
        base_delta = as_float(sample.get("base_total")) - as_float(previous.get("base_total"))
        quote_delta = as_float(sample.get("quote_total")) - as_float(previous.get("quote_total"))
        previous = sample
        if abs(base_delta) <= 1e-6 or abs(quote_delta) <= 1e-4:
            continue
        side = "bid" if base_delta > 0.0 and quote_delta < 0.0 else "ask" if base_delta < 0.0 and quote_delta > 0.0 else "other"
        if side == "other":
            continue
        price = abs(quote_delta / base_delta)
        mid_before = as_float(samples[index - 1].get("mid_price"))
        actual_edge_bps = fill_edge_bps(side, price, mid_before)
        fill = {
            "index": index,
            "time": sample.get("time"),
            "side": side,
            "base_delta": base_delta,
            "quote_delta": quote_delta,
            "price": price,
            "mid_before": mid_before,
            "mid_after": as_float(sample.get("mid_price")),
            "actual_edge_bps": actual_edge_bps,
        }
        for horizon in (300, 900):
            future = sample_at_or_after(samples, index, horizon)
            future_mid = as_float(future.get("mid_price"))
            if future_mid > 0.0:
                if side == "bid":
                    markout = (future_mid - price) / price * 10_000.0
                else:
                    markout = (price - future_mid) / price * 10_000.0
                fill[f"markout_{horizon}s_bps"] = markout
                fill[f"mid_{horizon}s"] = future_mid
        fills.append(fill)
    return fills


def fill_edge_bps(side: str, price: float, mid_before: float) -> float:
    if not (math.isfinite(price) and math.isfinite(mid_before)) or price <= 0.0 or mid_before <= 0.0:
        return math.nan
    if side == "bid":
        return (mid_before - price) / mid_before * 10_000.0
    if side == "ask":
        return (price - mid_before) / mid_before * 10_000.0
    return math.nan


def avg_edge(fills: list[dict[str, Any]]) -> float | None:
    values = [as_float(fill.get("actual_edge_bps")) for fill in fills]
    values = [value for value in values if math.isfinite(value)]
    return sum(values) / len(values) if values else None


def cooldown_replay(
    fills: list[dict[str, Any]],
    end_mid: float,
    fee_usdc: float,
    cooldown_seconds: float,
) -> dict[str, Any]:
    """Replay observed fills, skipping same-side fills during cooldown.

    This is not a full market simulator. It answers a narrower question: if the
    bot had stopped quoting the same side after a fill, how much of the observed
    fill PnL would still have happened, using the same end mark.
    """
    cooldown_until: dict[str, float] = {"bid": 0.0, "ask": 0.0}
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for fill in fills:
        side = fill["side"]
        ts = parse_time(fill.get("time"))
        if ts is not None and ts < cooldown_until.get(side, 0.0):
            skipped.append(fill)
            continue
        accepted.append(fill)
        if ts is not None:
            cooldown_until[side] = ts + cooldown_seconds

    base_delta = sum(as_float(fill.get("base_delta")) for fill in accepted)
    quote_delta = sum(as_float(fill.get("quote_delta")) for fill in accepted)
    gross_vs_hold = quote_delta + base_delta * end_mid if math.isfinite(end_mid) else math.nan
    conservative_net = gross_vs_hold - fee_usdc if math.isfinite(fee_usdc) else math.nan
    return {
        "accepted": accepted,
        "skipped": skipped,
        "base_delta": base_delta,
        "quote_delta": quote_delta,
        "gross_vs_hold": gross_vs_hold,
        "conservative_net_vs_hold": conservative_net,
    }


def edge_adjusted_net(summary: dict[str, Any], edge_bps: float) -> float:
    net = as_float(summary["net_trading_vs_hold_usdc"])
    if not math.isfinite(net):
        net = as_float(summary["trading_vs_hold_usdc"])
    if not math.isfinite(net):
        return math.nan
    return net + as_float(summary["fill_notional"]) * edge_bps / 10_000.0


def spread_floor_replay(summary: dict[str, Any], floor_bps: float) -> float:
    """Replay fills with a minimum absolute spread floor.

    This keeps the observed fill sizes and assumes fills would still happen at
    the wider floor price. It is conservative for transaction-count analysis and
    optimistic for fill probability, so use it as a guardrail sizing check, not a
    complete simulator.
    """
    end_mid = as_float(summary["end_mid"])
    if not math.isfinite(end_mid):
        return math.nan

    base_delta_total = 0.0
    quote_delta_total = 0.0
    for fill in summary["fills"]:
        base_delta = as_float(fill.get("base_delta"))
        quote_delta = as_float(fill.get("quote_delta"))
        mid_before = as_float(fill.get("mid_before"))
        edge_bps = as_float(fill.get("actual_edge_bps"))
        side = fill.get("side")
        if (
            math.isfinite(base_delta)
            and math.isfinite(mid_before)
            and math.isfinite(edge_bps)
            and mid_before > 0.0
            and edge_bps < floor_bps
            and side in {"bid", "ask"}
        ):
            size = abs(base_delta)
            floor_price = mid_before * (
                1.0 - floor_bps / 10_000.0
                if side == "bid"
                else 1.0 + floor_bps / 10_000.0
            )
            quote_delta = -size * floor_price if side == "bid" else size * floor_price
        base_delta_total += base_delta
        quote_delta_total += quote_delta

    fee_usdc = as_float(summary["fee_usdc"] or 0.0)
    gross_vs_hold = quote_delta_total + base_delta_total * end_mid
    return gross_vs_hold - fee_usdc if math.isfinite(fee_usdc) else gross_vs_hold


def retained_fill_replay(summary: dict[str, Any], floor_bps: float) -> dict[str, Any]:
    """Replay only fills that already cleared at or wider than the candidate floor.

    This is intentionally stricter than `spread_floor_replay`: it does not assume
    sub-floor fills would still happen after widening. It is therefore a better
    calibration signal for avoiding a no-fill spread recommendation.
    """
    end_mid = as_float(summary["end_mid"])
    if not math.isfinite(end_mid):
        return {
            "fills": [],
            "fill_notional": 0.0,
            "net_vs_hold": math.nan,
        }

    retained: list[dict[str, Any]] = []
    for fill in summary["fills"]:
        edge_bps = as_float(fill.get("actual_edge_bps"))
        if math.isfinite(edge_bps) and edge_bps >= floor_bps:
            retained.append(fill)

    base_delta = sum(as_float(fill.get("base_delta")) for fill in retained)
    quote_delta = sum(as_float(fill.get("quote_delta")) for fill in retained)
    fee_usdc = as_float(summary["fee_usdc"] or 0.0)
    gross_vs_hold = quote_delta + base_delta * end_mid
    net_vs_hold = gross_vs_hold - fee_usdc if math.isfinite(fee_usdc) else gross_vs_hold
    return {
        "fills": retained,
        "fill_notional": sum(abs(as_float(fill.get("quote_delta"))) for fill in retained),
        "net_vs_hold": net_vs_hold,
    }


def calibrate_spread_floors(
    summaries: list[dict[str, Any]],
    floors: list[float],
    *,
    min_retained_runs: int,
    min_retained_notional: float,
    min_retained_net_usdc: float,
) -> list[dict[str, Any]]:
    calibrations: list[dict[str, Any]] = []
    for floor in floors:
        retained_runs = 0
        retained_fills = 0
        retained_notional = 0.0
        retained_net_sum = 0.0
        retained_positive_runs = 0
        optimistic_net_sum = 0.0
        optimistic_positive_runs = 0
        finite_retained_runs = 0
        finite_optimistic_runs = 0

        for summary in summaries:
            retained = retained_fill_replay(summary, floor)
            retained_net = as_float(retained["net_vs_hold"])
            if math.isfinite(retained_net):
                finite_retained_runs += 1
                retained_net_sum += retained_net
                if retained_net > 0.0:
                    retained_positive_runs += 1
            if retained["fills"]:
                retained_runs += 1
                retained_fills += len(retained["fills"])
                retained_notional += as_float(retained["fill_notional"])

            optimistic_net = spread_floor_replay(summary, floor)
            if math.isfinite(optimistic_net):
                finite_optimistic_runs += 1
                optimistic_net_sum += optimistic_net
                if optimistic_net > 0.0:
                    optimistic_positive_runs += 1

        passes = (
            retained_runs >= min_retained_runs
            and retained_notional >= min_retained_notional
            and retained_net_sum >= min_retained_net_usdc
        )
        calibrations.append(
            {
                "floor_bps": floor,
                "retained_runs": retained_runs,
                "retained_fills": retained_fills,
                "retained_notional": retained_notional,
                "retained_net_sum": retained_net_sum,
                "retained_positive_runs": retained_positive_runs,
                "finite_retained_runs": finite_retained_runs,
                "optimistic_net_sum": optimistic_net_sum,
                "optimistic_positive_runs": optimistic_positive_runs,
                "finite_optimistic_runs": finite_optimistic_runs,
                "passes": passes,
            }
        )
    return calibrations


def choose_calibrated_floor(calibrations: list[dict[str, Any]]) -> dict[str, Any] | None:
    passing = [row for row in calibrations if row["passes"]]
    if not passing:
        return None
    return min(passing, key=lambda row: (as_float(row["floor_bps"]), -as_float(row["retained_net_sum"])))


def break_even_spread_floor_bps(summary: dict[str, Any]) -> float | None:
    current_net = as_float(summary["net_trading_vs_hold_usdc"])
    if not math.isfinite(current_net):
        current_net = as_float(summary["trading_vs_hold_usdc"])
    if not math.isfinite(current_net) or not summary["fills"]:
        return None
    if current_net >= 0.0:
        return 0.0

    lo = 0.0
    hi = 500.0
    if spread_floor_replay(summary, hi) < 0.0:
        return None
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if spread_floor_replay(summary, mid) >= 0.0:
            hi = mid
        else:
            lo = mid
    return hi


def count_log_patterns(run_dir: Path) -> dict[str, int]:
    patterns = {
        "price_feed_stale": re.compile(r"Price feed stale"),
        "tx_send_failed": re.compile(r"TX send failed|err=\\{"),
        "rpc_429": re.compile(r"HTTP[^\n]*\b429\b|\b429 Too Many Requests\b|Too Many Requests"),
        "priority_fee_sampling_failed": re.compile(r"priority fee sampling failed"),
        "tx_circuit_breaker": re.compile(r"TX circuit breaker opened|TX circuit breaker open"),
    }
    counts = {key: 0 for key in patterns}
    for filename in ("bot.log", "commands.log", "controller.out", "summary.log"):
        path = run_dir / filename
        if not path.exists():
            continue
        text = path.read_text(errors="replace")
        for key, pattern in patterns.items():
            counts[key] += len(pattern.findall(text))
    return counts


def summarize_run(path: Path) -> dict[str, Any] | None:
    samples = load_samples(path)
    if len(samples) < 2:
        return None
    first = samples[0]
    last = samples[-1]
    fills = infer_fills(samples)
    by_side: dict[str, list[dict[str, Any]]] = {"bid": [], "ask": []}
    for fill in fills:
        by_side[fill["side"]].append(fill)

    def avg(side: str, key: str) -> float | None:
        values = [as_float(fill.get(key)) for fill in by_side[side]]
        values = [value for value in values if math.isfinite(value)]
        return sum(values) / len(values) if values else None

    fill_notional = sum(abs(as_float(fill.get("quote_delta"))) for fill in fills)
    first_time = parse_time(first.get("time"))
    last_time = parse_time(last.get("time"))
    duration_seconds = (
        max(0.0, last_time - first_time)
        if first_time is not None and last_time is not None
        else math.nan
    )
    tx_count = as_float(last.get("tx_count"))
    tx_per_hour = (
        tx_count / duration_seconds * 3600.0
        if math.isfinite(tx_count) and duration_seconds > 0.0
        else math.nan
    )
    net_vs_hold = as_float(last.get("net_trading_vs_hold_usdc"))
    trading_vs_hold = as_float(last.get("trading_vs_hold_usdc"))
    pnl_for_edge = net_vs_hold if math.isfinite(net_vs_hold) else trading_vs_hold
    required_edge_bps = (
        max(0.0, -pnl_for_edge) / fill_notional * 10_000.0
        if fill_notional > 0.0 and math.isfinite(pnl_for_edge)
        else None
    )

    return {
        "run": path.parent.name.replace("adaptive-12h-", ""),
        "samples": len(samples),
        "duration_seconds": duration_seconds,
        "start_mid": as_float(first.get("mid_price")),
        "end_mid": as_float(last.get("mid_price")),
        "base_delta": as_float(last.get("base_total")) - as_float(first.get("base_total")),
        "quote_delta": as_float(last.get("quote_total")) - as_float(first.get("quote_total")),
        "tx_count": last.get("tx_count"),
        "tx_per_hour": tx_per_hour,
        "tx_failed": last.get("tx_failed"),
        "fee_usdc": last.get("fee_usdc"),
        "trading_vs_hold_usdc": trading_vs_hold,
        "net_trading_vs_hold_usdc": net_vs_hold,
        "fill_notional": fill_notional,
        "required_edge_bps": required_edge_bps,
        "avg_actual_edge_bps": avg_edge(fills),
        "break_even_spread_floor_bps": break_even_spread_floor_bps(
            {
                "end_mid": as_float(last.get("mid_price")),
                "fee_usdc": last.get("fee_usdc"),
                "fills": fills,
                "net_trading_vs_hold_usdc": net_vs_hold,
                "trading_vs_hold_usdc": trading_vs_hold,
            }
        ),
        "fills": fills,
        "fill_count": len(fills),
        "bid_fill_count": len(by_side["bid"]),
        "ask_fill_count": len(by_side["ask"]),
        "bid_markout_300s_bps": avg("bid", "markout_300s_bps"),
        "ask_markout_300s_bps": avg("ask", "markout_300s_bps"),
        "bid_markout_900s_bps": avg("bid", "markout_900s_bps"),
        "ask_markout_900s_bps": avg("ask", "markout_900s_bps"),
        "log_counts": count_log_patterns(path.parent),
    }


def iter_sample_paths(inputs: Iterable[str]) -> Iterable[Path]:
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            sample = path / "dashboard-samples.jsonl"
            if sample.exists():
                yield sample
            continue
        yield path


def fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        return "--" if not math.isfinite(value) else f"{value:.{digits}f}"
    return str(value)


def gate_failures(
    summary: dict[str, Any],
    *,
    spread_floor_bps: float | None,
    max_break_even_floor_bps: float | None,
    min_spread_floor_net_usdc: float | None,
    max_tx_per_hour: float | None,
    max_failed_tx: float | None,
    max_priority_fee_sampling_failures: float | None,
    max_rpc_429: float | None,
    max_price_feed_stale: float | None,
) -> list[str]:
    failures: list[str] = []
    run = summary["run"]

    break_even = summary.get("break_even_spread_floor_bps")
    if max_break_even_floor_bps is not None:
        if break_even is None or not math.isfinite(as_float(break_even)):
            failures.append(f"{run}: missing break-even floor")
        elif as_float(break_even) > max_break_even_floor_bps:
            failures.append(
                f"{run}: break-even floor {as_float(break_even):.2f}bps > {max_break_even_floor_bps:.2f}bps"
            )

    if spread_floor_bps is not None and min_spread_floor_net_usdc is not None:
        replay_net = spread_floor_replay(summary, spread_floor_bps)
        if not math.isfinite(replay_net):
            failures.append(f"{run}: missing spread-floor replay net")
        elif replay_net < min_spread_floor_net_usdc:
            failures.append(
                f"{run}: spread floor {spread_floor_bps:.2f}bps replay net "
                f"{replay_net:.4f} < {min_spread_floor_net_usdc:.4f}"
            )

    if max_tx_per_hour is not None:
        tx_per_hour = as_float(summary.get("tx_per_hour"))
        if math.isfinite(tx_per_hour) and tx_per_hour > max_tx_per_hour:
            failures.append(f"{run}: tx/hour {tx_per_hour:.2f} > {max_tx_per_hour:.2f}")

    if max_failed_tx is not None:
        failed = as_float(summary.get("tx_failed"))
        if math.isfinite(failed) and failed > max_failed_tx:
            failures.append(f"{run}: failed tx {failed:.0f} > {max_failed_tx:.0f}")

    log_counts = summary.get("log_counts", {})
    for key, limit in (
        ("priority_fee_sampling_failed", max_priority_fee_sampling_failures),
        ("rpc_429", max_rpc_429),
        ("price_feed_stale", max_price_feed_stale),
    ):
        if limit is None:
            continue
        value = as_float(log_counts.get(key))
        if math.isfinite(value) and value > limit:
            failures.append(f"{run}: {key} {value:.0f} > {limit:.0f}")

    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="dashboard-samples.jsonl files or run directories")
    parser.add_argument("--fills", action="store_true", help="print inferred fills")
    parser.add_argument(
        "--cooldowns",
        default="",
        help="comma-separated same-side cooldown seconds to replay against observed fills",
    )
    parser.add_argument(
        "--edge-bps",
        default="",
        help="comma-separated additional edge bps to apply as a simple spread counterfactual",
    )
    parser.add_argument(
        "--spread-floors",
        default="",
        help="comma-separated absolute minimum spread floors to replay against observed fills",
    )
    parser.add_argument(
        "--calibrate-spread-floors",
        default="",
        help="comma-separated candidate spread floors to score with retained-fill replay",
    )
    parser.add_argument(
        "--calibration-min-retained-runs",
        type=int,
        default=3,
        help="minimum runs with at least one retained fill for calibration pass",
    )
    parser.add_argument(
        "--calibration-min-retained-notional",
        type=float,
        default=50.0,
        help="minimum retained fill notional for calibration pass",
    )
    parser.add_argument(
        "--calibration-min-retained-net-usdc",
        type=float,
        default=0.0,
        help="minimum summed retained-fill net versus hold for calibration pass",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="exit non-zero when any configured validation threshold fails",
    )
    parser.add_argument(
        "--gate-spread-floor-bps",
        type=float,
        default=None,
        help="spread floor to use for the replay net gate",
    )
    parser.add_argument(
        "--min-spread-floor-net-usdc",
        type=float,
        default=None,
        help="minimum accepted replay net at --gate-spread-floor-bps",
    )
    parser.add_argument(
        "--max-break-even-floor-bps",
        type=float,
        default=None,
        help="maximum acceptable break-even absolute spread floor",
    )
    parser.add_argument(
        "--max-tx-per-hour",
        type=float,
        default=None,
        help="maximum acceptable submitted transaction rate",
    )
    parser.add_argument(
        "--max-failed-tx",
        type=float,
        default=None,
        help="maximum acceptable failed transaction count",
    )
    parser.add_argument(
        "--max-priority-fee-sampling-failures",
        type=float,
        default=None,
        help="maximum acceptable priority fee sampling failure count",
    )
    parser.add_argument(
        "--max-rpc-429",
        type=float,
        default=None,
        help="maximum acceptable RPC 429 count",
    )
    parser.add_argument(
        "--max-price-feed-stale",
        type=float,
        default=None,
        help="maximum acceptable stale price feed clear/hold count",
    )
    args = parser.parse_args()
    cooldowns = [
        float(raw)
        for raw in args.cooldowns.split(",")
        if raw.strip()
    ]
    edge_bps_values = [
        float(raw)
        for raw in args.edge_bps.split(",")
        if raw.strip()
    ]
    spread_floor_values = [
        float(raw)
        for raw in args.spread_floors.split(",")
        if raw.strip()
    ]
    calibration_floor_values = [
        float(raw)
        for raw in args.calibrate_spread_floors.split(",")
        if raw.strip()
    ]

    summaries = [summarize_run(path) for path in iter_sample_paths(args.paths)]
    summaries = [summary for summary in summaries if summary]
    if calibration_floor_values:
        calibrations = calibrate_spread_floors(
            summaries,
            calibration_floor_values,
            min_retained_runs=args.calibration_min_retained_runs,
            min_retained_notional=args.calibration_min_retained_notional,
            min_retained_net_usdc=args.calibration_min_retained_net_usdc,
        )
        recommendation = choose_calibrated_floor(calibrations)
        print(
            "calibration_floor_bps,passes,retained_runs,retained_fills,retained_notional,"
            "retained_net_sum,retained_positive_runs,optimistic_net_sum,optimistic_positive_runs"
        )
        for row in calibrations:
            print(
                ",".join(
                    [
                        fmt(row["floor_bps"], 2),
                        "true" if row["passes"] else "false",
                        str(row["retained_runs"]),
                        str(row["retained_fills"]),
                        fmt(row["retained_notional"]),
                        fmt(row["retained_net_sum"]),
                        str(row["retained_positive_runs"]),
                        fmt(row["optimistic_net_sum"]),
                        str(row["optimistic_positive_runs"]),
                    ]
                )
            )
        if recommendation:
            print(
                "RECOMMEND "
                f"spread_floor_bps={fmt(recommendation['floor_bps'], 2)} "
                f"retained_runs={recommendation['retained_runs']} "
                f"retained_notional={fmt(recommendation['retained_notional'])} "
                f"retained_net_sum={fmt(recommendation['retained_net_sum'])}"
            )
        else:
            print("RECOMMEND none")
        return

    cooldown_columns = []
    for seconds in cooldowns:
        label = int(seconds)
        cooldown_columns.extend(
            [
                f"cooldown_{label}s_net",
                f"cooldown_{label}s_skipped",
            ]
        )
    edge_columns = [f"edge_{int(edge)}bps_net" for edge in edge_bps_values]
    spread_floor_columns = [f"spread_floor_{int(edge)}bps_net" for edge in spread_floor_values]
    print(
        "run,samples,duration_min,tx,tx_per_hour,failed,fee_usdc,net_vs_hold,base_delta,quote_delta,"
        "fills,fill_notional,required_edge_bps,avg_actual_edge_bps,break_even_floor_bps,bid_fills,ask_fills,"
        "bid_mo_300,ask_mo_300,bid_mo_900,ask_mo_900,priority_fee_sampling_failed,rpc_429,price_feed_stale,tx_circuit_breaker"
        + ("," + ",".join(edge_columns) if edge_columns else "")
        + ("," + ",".join(spread_floor_columns) if spread_floor_columns else "")
        + ("," + ",".join(cooldown_columns) if cooldown_columns else "")
    )
    all_failures: list[str] = []
    for summary in summaries:
        edge_values = [fmt(edge_adjusted_net(summary, edge)) for edge in edge_bps_values]
        spread_floor_values_out = [
            fmt(spread_floor_replay(summary, floor)) for floor in spread_floor_values
        ]
        cooldown_values: list[str] = []
        for seconds in cooldowns:
            replay = cooldown_replay(
                summary["fills"],
                as_float(summary["end_mid"]),
                as_float(summary["fee_usdc"] or 0.0),
                seconds,
            )
            cooldown_values.extend(
                [
                    fmt(replay["conservative_net_vs_hold"]),
                    str(len(replay["skipped"])),
                ]
            )
        print(
            ",".join(
                [
                    str(summary["run"]),
                    str(summary["samples"]),
                    fmt(as_float(summary.get("duration_seconds")) / 60.0),
                    fmt(summary["tx_count"], 0),
                    fmt(summary["tx_per_hour"], 2),
                    fmt(summary["tx_failed"], 0),
                    fmt(summary["fee_usdc"]),
                    fmt(summary["net_trading_vs_hold_usdc"]),
                    fmt(summary["base_delta"], 6),
                    fmt(summary["quote_delta"]),
                    str(summary["fill_count"]),
                    fmt(summary["fill_notional"]),
                    fmt(summary["required_edge_bps"], 2),
                    fmt(summary["avg_actual_edge_bps"], 2),
                    fmt(summary["break_even_spread_floor_bps"], 2),
                    str(summary["bid_fill_count"]),
                    str(summary["ask_fill_count"]),
                    fmt(summary["bid_markout_300s_bps"], 2),
                    fmt(summary["ask_markout_300s_bps"], 2),
                    fmt(summary["bid_markout_900s_bps"], 2),
                    fmt(summary["ask_markout_900s_bps"], 2),
                    str(summary.get("log_counts", {}).get("priority_fee_sampling_failed", 0)),
                    str(summary.get("log_counts", {}).get("rpc_429", 0)),
                    str(summary.get("log_counts", {}).get("price_feed_stale", 0)),
                    str(summary.get("log_counts", {}).get("tx_circuit_breaker", 0)),
                ]
                + edge_values
                + spread_floor_values_out
                + cooldown_values
            )
        )
        if args.fills:
            for fill in summary["fills"]:
                print(
                    "  "
                    f"{fill['time']} {fill['side']} price={fmt(fill['price'])} "
                    f"edge={fmt(fill.get('actual_edge_bps'), 2)} "
                    f"base_delta={fmt(fill['base_delta'], 6)} quote_delta={fmt(fill['quote_delta'])} "
                    f"mo300={fmt(fill.get('markout_300s_bps'), 2)} "
                    f"mo900={fmt(fill.get('markout_900s_bps'), 2)}"
                )
        if args.gate:
            all_failures.extend(
                gate_failures(
                    summary,
                    spread_floor_bps=args.gate_spread_floor_bps,
                    max_break_even_floor_bps=args.max_break_even_floor_bps,
                    min_spread_floor_net_usdc=args.min_spread_floor_net_usdc,
                    max_tx_per_hour=args.max_tx_per_hour,
                    max_failed_tx=args.max_failed_tx,
                    max_priority_fee_sampling_failures=args.max_priority_fee_sampling_failures,
                    max_rpc_429=args.max_rpc_429,
                    max_price_feed_stale=args.max_price_feed_stale,
                )
            )
    if args.gate:
        if all_failures:
            print("GATE FAIL")
            for failure in all_failures:
                print(f"  {failure}")
            raise SystemExit(1)
        print("GATE PASS")


if __name__ == "__main__":
    main()
