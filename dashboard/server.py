#!/usr/bin/env python3
"""Read-only Archer bot dashboard.

The server intentionally does not call Archer mutating commands. It reads status,
wallet balances, logs, and transaction metadata, then serves a local dashboard.
"""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import functools
import glob
import json
import os
import pathlib
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
DEFAULT_CONFIG = ROOT / "config" / "live-usdc-style-12h.toml"
BIN = ROOT / "target" / "release" / "archer-market-maker"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ARCHER_PROGRAM_ID = "Archer8kgiavM61GyusMzaaS2ft5sALtNsD1HxkUPMhy"
ARCHER_IX_NAMES = {
    6: "initialize_maker_book",
    7: "update_book",
    8: "update_mid_price",
    9: "clear_book",
    11: "maker_deposit",
    12: "maker_withdraw",
    26: "update_sync_spread",
    30: "update_expiry_in_slots",
    31: "close_maker_book",
}
RUN_PREFIXES = ("adaptive-12h-", "usdc-style-12h-")
DASHBOARD_COMMAND_TIMEOUT = float(os.environ.get("ARCHER_DASHBOARD_COMMAND_TIMEOUT", "2"))
DASHBOARD_RPC_TIMEOUT = float(os.environ.get("ARCHER_DASHBOARD_RPC_TIMEOUT", "1.5"))
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: Optional[dt.datetime] = None) -> str:
    return (ts or now_utc()).isoformat().replace("+00:00", "Z")


def run_cmd(args: List[str], timeout: float = 8.0) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": f"timeout after {timeout}s",
        }
    except Exception as exc:  # noqa: BLE001 - API should surface errors as data.
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc)}


def base58_encode(raw: bytes) -> str:
    value = int.from_bytes(raw, "big")
    encoded = ""
    while value:
        value, remainder = divmod(value, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    pad = 0
    for byte in raw:
        if byte == 0:
            pad += 1
        else:
            break
    return ("1" * pad) + (encoded or "")


def base58_decode(text: str) -> Optional[bytes]:
    value = 0
    for char in text:
        if char not in BASE58_ALPHABET:
            return None
        value = value * 58 + BASE58_ALPHABET.index(char)
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
    pad = 0
    for char in text:
        if char == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + raw


def decode_instruction_data(data: Any) -> Optional[bytes]:
    if isinstance(data, str):
        decoded = base58_decode(data)
        if decoded is not None:
            return decoded
        try:
            return base64.b64decode(data)
        except Exception:
            return None
    if isinstance(data, list) and len(data) >= 2 and data[1] == "base64":
        try:
            return base64.b64decode(data[0])
        except Exception:
            return None
    return None


def classify_archer_transaction(tx: Optional[Dict[str, Any]]) -> Tuple[Optional[str], List[str]]:
    message = (((tx or {}).get("transaction") or {}).get("message") or {})
    account_keys = message.get("accountKeys") or []
    kinds: List[str] = []
    for ix in message.get("instructions") or []:
        program_id = ix.get("programId")
        if program_id is None and isinstance(ix.get("programIdIndex"), int):
            try:
                account_key = account_keys[ix["programIdIndex"]]
                program_id = account_key.get("pubkey") if isinstance(account_key, dict) else account_key
            except (IndexError, KeyError, TypeError):
                program_id = None
        if program_id != ARCHER_PROGRAM_ID:
            continue
        raw = decode_instruction_data(ix.get("data"))
        if not raw:
            continue
        kinds.append(ARCHER_IX_NAMES.get(raw[0], f"ix_{raw[0]}"))

    kind_set = set(kinds)
    if "clear_book" in kind_set:
        return "clear", kinds
    if {"update_mid_price", "update_book"} <= kind_set:
        return "mid_book", kinds
    if "update_book" in kind_set:
        return "book_only", kinds
    if "update_mid_price" in kind_set:
        return "mid_only", kinds
    if kinds:
        return "+".join(kinds), kinds
    return None, kinds


def pubkey_from_keypair_json(path: str) -> Optional[str]:
    try:
        values = json.loads(pathlib.Path(path).read_text())
        if not isinstance(values, list) or len(values) < 64:
            return None
        return base58_encode(bytes(int(value) & 0xFF for value in values[32:64]))
    except Exception:
        return None


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(part.strip()) for part in inner.split(",")]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def load_simple_toml(path: pathlib.Path) -> Dict[str, Dict[str, Any]]:
    data: Dict[str, Dict[str, Any]] = {}
    section: Optional[str] = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            data.setdefault(section, {})
            continue
        if section and "=" in line:
            key, value = line.split("=", 1)
            data[section][key.strip()] = parse_scalar(value)
    return data


def effective_spreads_bps(spreads: Any, floor: Any) -> List[float]:
    if not isinstance(spreads, list) or not spreads:
        return []
    try:
        values = [float(spread) for spread in spreads]
        floor_value = float(floor or 0.0)
    except (TypeError, ValueError):
        return []
    shift = max(0.0, floor_value - values[0])
    return [round(spread + shift, 4) for spread in values]


def discover_run_dir(explicit: Optional[str]) -> Optional[pathlib.Path]:
    if explicit:
        path = pathlib.Path(explicit).expanduser()
        return path if path.is_absolute() else ROOT / path
    candidates = []
    for prefix in RUN_PREFIXES:
        candidates.extend(
            pathlib.Path(path)
            for path in glob.glob(str(ROOT / "logs" / f"{prefix}*"))
            if pathlib.Path(path).is_dir()
        )
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def parse_run_start(run_dir: Optional[pathlib.Path]) -> Optional[dt.datetime]:
    if not run_dir:
        return None
    match = re.search(r"(\d{8}T\d{6}Z)", run_dir.name)
    if not match:
        return None
    return dt.datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
        tzinfo=dt.timezone.utc
    )


def run_id_from_dir(run_dir: Optional[pathlib.Path]) -> Optional[str]:
    if not run_dir:
        return None
    for prefix in RUN_PREFIXES:
        if run_dir.name.startswith(prefix):
            return run_dir.name.replace(prefix, "", 1)
    return run_dir.name


def parse_duration_seconds(run_dir: Optional[pathlib.Path], default: int) -> int:
    if not run_dir:
        return default
    summary = run_dir / "summary.log"
    if not summary.exists():
        return default
    match = re.search(r"duration_seconds=(\d+)", summary.read_text(errors="replace"))
    return int(match.group(1)) if match else default


def parse_status_output(text: str) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    keys = {
        "Market": "market",
        "Maker": "maker",
        "Mode": "mode",
        "Mid ticks": "mid_ticks",
        "Bid levels": "bid_levels",
        "Ask levels": "ask_levels",
        "Base free": "base_free",
        "Base locked": "base_locked",
        "Quote free": "quote_free",
        "Quote locked": "quote_locked",
    }
    for line in text.splitlines():
        if ":" not in line:
            continue
        left, right = [part.strip() for part in line.split(":", 1)]
        key = keys.get(left)
        if not key:
            continue
        if key.endswith("_levels") or key == "mid_ticks":
            fields[key] = int(float(right))
        elif key in {"base_free", "base_locked", "quote_free", "quote_locked"}:
            fields[key] = float(right)
        else:
            fields[key] = right
    fields["base_total"] = fields.get("base_free", 0.0) + fields.get("base_locked", 0.0)
    fields["quote_total"] = fields.get("quote_free", 0.0) + fields.get("quote_locked", 0.0)
    return fields


def parse_market_output(text: str) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        left, right = [part.strip() for part in line.split(":", 1)]
        if left == "Tick price increment":
            fields["tick_price_increment"] = float(right)
        elif left == "Maker fee ppm":
            fields["maker_fee_ppm"] = int(right)
        elif left == "Taker fee ppm":
            fields["taker_fee_ppm"] = int(right)
        elif left == "Base mint":
            fields["base_mint"] = right
        elif left == "Quote mint":
            fields["quote_mint"] = right
        elif left == "Owner matches Archer":
            fields["owner_matches_archer"] = right.lower() == "true"
    return fields


def parse_snapshot_status(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    text = path.read_text(errors="replace")
    marker = "--- archer status ---"
    idx = text.find(marker)
    if idx < 0:
        return None
    return parse_status_output(text[idx + len(marker) :])


def parse_first_commands_status(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    text = path.read_text(errors="replace")
    marker = "=== Archer Market Maker Status ==="
    idx = text.find(marker)
    if idx < 0:
        return None
    end = text.find("[exit=", idx)
    block = text[idx:end] if end > idx else text[idx:]
    return parse_status_output(block)


def parse_first_history_baseline(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    for line in path.read_text(errors="replace").splitlines():
        try:
            sample = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            sample.get("mid_price") is not None
            and sample.get("base_total") is not None
            and sample.get("quote_total") is not None
        ):
            return {
                "mid_price": sample["mid_price"],
                "base_total": sample["base_total"],
                "quote_total": sample["quote_total"],
                "source": str(path),
                "time": sample.get("time"),
            }
    return None


def tail_lines(path: pathlib.Path, limit: int = 80) -> List[str]:
    if not path.exists():
        return []
    lines = path.read_text(errors="replace").splitlines()
    return lines[-limit:]


def count_patterns(paths: Iterable[pathlib.Path], patterns: Dict[str, str]) -> Dict[str, int]:
    counts = {key: 0 for key in patterns}
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(errors="replace")
        for key, pattern in patterns.items():
            counts[key] += len(re.findall(pattern, text, flags=re.IGNORECASE))
    return counts


class DashboardState:
    def __init__(
        self,
        config_path: pathlib.Path,
        run_dir: Optional[pathlib.Path],
        sample_interval: int,
        tx_lookback: int,
        fee_fetch_batch: int,
    ) -> None:
        self.config_path = config_path
        self.run_dir = run_dir
        self.sample_interval = sample_interval
        self.tx_lookback = tx_lookback
        self.fee_fetch_batch = fee_fetch_batch
        self.config = load_simple_toml(config_path)
        maker_keypair_path = os.environ.get("ARCHER_MAKER_KEYPAIR_PATH")
        if maker_keypair_path:
            self.config.setdefault("market", {})["maker_keypair_path"] = maker_keypair_path
        self.rpc_url = str(self.config.get("connection", {}).get("rpc_url", ""))
        self.wallet = str(self.config.get("market", {}).get("maker_keypair_path", ""))
        self.wallet_pubkey = self._discover_wallet_pubkey()
        self.run_start = parse_run_start(run_dir)
        self.duration_seconds = parse_duration_seconds(run_dir, 43200)
        self.fee_cache: Dict[str, Dict[str, Any]] = {}
        self.market_cache: Tuple[float, Dict[str, Any]] = (0.0, {})
        self.metrics_cache: Tuple[float, Dict[str, Any]] = (0.0, {})
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.sample_path = run_dir / "dashboard-samples.jsonl" if run_dir else None

    def _discover_wallet_pubkey(self) -> str:
        from_json = pubkey_from_keypair_json(self.wallet)
        if from_json:
            return from_json
        cmd = run_cmd(["solana-keygen", "pubkey", self.wallet], timeout=5)
        if cmd["ok"]:
            return cmd["stdout"].strip()
        return "unknown"

    def rpc_call(self, method: str, params: List[Any], timeout: float = DASHBOARD_RPC_TIMEOUT) -> Any:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": int(time.time() * 1000) % 1_000_000, "method": method, "params": params}
        ).encode("utf-8")
        req = urllib.request.Request(
            self.rpc_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if "error" in body:
            raise RuntimeError(body["error"])
        return body.get("result")

    def get_market_meta(self) -> Dict[str, Any]:
        cached_at, cached = self.market_cache
        if cached and time.time() - cached_at < 300:
            return cached
        cmd = run_cmd(
            [str(BIN), "market", "--config", str(self.config_path)],
            timeout=DASHBOARD_COMMAND_TIMEOUT,
        )
        meta = parse_market_output(cmd["stdout"]) if cmd["ok"] else {}
        meta.setdefault("tick_price_increment", 0.001)
        meta["command_ok"] = cmd["ok"]
        meta["command_error"] = cmd["stderr"].strip()
        self.market_cache = (time.time(), meta)
        return meta

    def get_snapshot_status(self) -> Dict[str, Any]:
        if not self.run_dir:
            return {}
        candidates = sorted(
            self.run_dir.glob("*.snapshot.txt"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            status = parse_snapshot_status(path)
            if status:
                status["source"] = str(path)
                return status
        status = parse_first_commands_status(self.run_dir / "commands.log")
        if status:
            status["source"] = str(self.run_dir / "commands.log")
        return status or {}

    def get_status(self) -> Dict[str, Any]:
        cmd = run_cmd(
            [str(BIN), "status", "--config", str(self.config_path)],
            timeout=DASHBOARD_COMMAND_TIMEOUT,
        )
        parsed = parse_status_output(cmd["stdout"]) if cmd["ok"] else {}
        if not parsed:
            parsed = self.get_snapshot_status()
            if parsed:
                parsed["stale"] = True
        else:
            parsed["stale"] = False
        parsed["command_ok"] = cmd["ok"]
        parsed["command_error"] = cmd["stderr"].strip()
        return parsed

    def get_wallet_balances(self) -> Dict[str, Any]:
        balances = {
            "native_sol": None,
            "wsol": None,
            "usdc": None,
            "errors": [],
        }
        if not self.rpc_url or self.wallet_pubkey == "unknown":
            balances["errors"].append("missing RPC URL or wallet pubkey")
            return balances
        try:
            native = self.rpc_call(
                "getBalance",
                [self.wallet_pubkey, {"commitment": "confirmed"}],
                timeout=DASHBOARD_RPC_TIMEOUT,
            )
            balances["native_sol"] = native["value"] / 1_000_000_000
        except Exception as exc:  # noqa: BLE001
            balances["errors"].append(f"native SOL: {exc}")
        for label, mint in [("wsol", WSOL_MINT), ("usdc", USDC_MINT)]:
            try:
                result = self.rpc_call(
                    "getTokenAccountsByOwner",
                    [
                        self.wallet_pubkey,
                        {"mint": mint},
                        {"encoding": "jsonParsed", "commitment": "confirmed"},
                    ],
                    timeout=DASHBOARD_RPC_TIMEOUT,
                )
                total = 0.0
                for item in result.get("value", []):
                    amount = item["account"]["data"]["parsed"]["info"]["tokenAmount"]
                    total += float(amount.get("uiAmountString") or amount.get("uiAmount") or 0)
                balances[label] = total
            except Exception as exc:  # noqa: BLE001
                balances["errors"].append(f"{label}: {exc}")
        return balances

    def get_market_intel(self) -> Dict[str, Any]:
        url = str(self.config.get("feed", {}).get("market_intel_signal_url") or "").strip()
        if not url:
            return {"enabled": False, "ok": False, "url": None}
        try:
            with urllib.request.urlopen(url, timeout=DASHBOARD_RPC_TIMEOUT) as resp:
                signal = json.loads(resp.read().decode("utf-8"))
            recommendation = signal.get("recommendation") or {}
            return {
                "enabled": True,
                "ok": True,
                "url": url,
                "mode": signal.get("mode"),
                "quote_enabled": recommendation.get("quote_enabled"),
                "fair_value": recommendation.get("fair_value"),
                "spread_add_bps": recommendation.get("spread_add_bps"),
                "size_multiplier": recommendation.get("size_multiplier"),
                "bid_size_multiplier": recommendation.get("bid_size_multiplier"),
                "ask_size_multiplier": recommendation.get("ask_size_multiplier"),
                "summary": recommendation.get("summary"),
                "reasons": recommendation.get("reasons") or [],
            }
        except Exception as exc:  # noqa: BLE001 - dashboard exposes health as data.
            return {
                "enabled": True,
                "ok": False,
                "url": url,
                "error": str(exc),
            }

    def get_process_state(self) -> Dict[str, Any]:
        screen = run_cmd(["screen", "-ls"], timeout=5)
        process_cmd = run_cmd(["ps", "-eo", "pid,args"], timeout=5)
        screen_text = (screen["stdout"] + screen["stderr"]).strip()
        process_lines = []
        for line in process_cmd["stdout"].splitlines():
            if (
                "archer_usdc_style_12h" in line
                or "archer_adaptive_12h" in line
                or "archer-market-maker run --config" in line
                or ("SCREEN" in line and "archer" in line)
            ):
                process_lines.append(line.strip())
        process_text = "\n".join(process_lines)
        return {
            "screen_ok": screen["ok"] or "No Sockets" in screen_text,
            "screens": screen_text.splitlines() if screen_text else [],
            "processes": process_text.splitlines() if process_text else [],
            "controller_running": "archer-usdc-style-12h" in screen_text
            or "archer-adaptive-12h" in screen_text
            or "archer_usdc_style_12h.sh" in process_text
            or "archer_adaptive_12h.py" in process_text,
            "bot_running": "archer-usdc-style-active" in screen_text
            or "archer-adaptive-active" in screen_text
            or "archer-market-maker run --config" in process_text,
        }

    def get_signatures_since_start(self) -> List[Dict[str, Any]]:
        if self.wallet_pubkey == "unknown" or not self.run_start:
            return []
        start_ts = int(self.run_start.timestamp())
        out: List[Dict[str, Any]] = []
        before: Optional[str] = None
        while len(out) < self.tx_lookback:
            opts: Dict[str, Any] = {"limit": min(1000, self.tx_lookback - len(out))}
            if before:
                opts["before"] = before
            page = self.rpc_call(
                "getSignaturesForAddress",
                [self.wallet_pubkey, opts],
                timeout=DASHBOARD_RPC_TIMEOUT,
            )
            if not page:
                break
            out.extend(page)
            before = page[-1]["signature"]
            oldest_ts = page[-1].get("blockTime")
            if oldest_ts and oldest_ts < start_ts:
                break
            if len(page) < opts["limit"]:
                break
        return [item for item in out if item.get("blockTime", 0) >= start_ts]

    def get_tx_fee(self, signature: str) -> Optional[int]:
        cached = self.fee_cache.get(signature)
        if cached is not None:
            return cached.get("fee_lamports")
        tx = self.rpc_call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "json",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
            timeout=DASHBOARD_RPC_TIMEOUT,
        )
        fee = None
        if tx and tx.get("meta"):
            fee = tx["meta"].get("fee")
        tx_kind, ix_kinds = classify_archer_transaction(tx)
        self.fee_cache[signature] = {
            "fee_lamports": fee,
            "archer_tx_kind": tx_kind,
            "archer_ix_kinds": ix_kinds,
            "seen_at": iso(),
        }
        return fee

    def get_transactions(self) -> Dict[str, Any]:
        if self.tx_lookback <= 0:
            return {
                "ok": True,
                "since_start_count": 0,
                "fees_known_count": 0,
                "fees_complete": True,
                "fee_sol_known": 0.0,
                "fee_sol_total": 0.0,
                "avg_fee_sol": 0.0,
                "priority_fee_sol_known": 0.0,
                "priority_fee_sol_estimate": 0.0,
                "failed_count": 0,
                "last": [],
            }
        try:
            signatures = self.get_signatures_since_start()
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": str(exc),
                "since_start_count": 0,
                "fees_known_count": 0,
                "fees_complete": False,
                "fee_sol_total": 0.0,
                "priority_fee_sol_estimate": 0.0,
                "failed_count": 0,
                "last": [],
            }

        # Avoid a slow first page. New signatures are filled over successive polls.
        uncached = [item["signature"] for item in signatures if item["signature"] not in self.fee_cache]
        for signature in uncached[: self.fee_fetch_batch]:
            try:
                self.get_tx_fee(signature)
            except Exception:
                self.fee_cache[signature] = {"fee_lamports": None, "seen_at": iso()}

        total_fee = 0
        priority_fee = 0
        known = 0
        kind_known = 0
        kind_counts: Dict[str, int] = {
            "mid_book": 0,
            "book_only": 0,
            "mid_only": 0,
            "clear": 0,
            "other": 0,
            "unknown": 0,
        }
        recent = []
        for item in signatures:
            signature = item["signature"]
            cached = self.fee_cache.get(signature, {})
            fee = cached.get("fee_lamports")
            if fee is not None:
                known += 1
                total_fee += int(fee)
                priority_fee += max(0, int(fee) - 5000)
            kind = cached.get("archer_tx_kind")
            if kind:
                kind_known += 1
                kind_counts[kind if kind in kind_counts else "other"] += 1
            else:
                kind_counts["unknown"] += 1
            if len(recent) < 12:
                recent.append(
                    {
                        "signature": signature,
                        "block_time": dt.datetime.fromtimestamp(
                            item["blockTime"], tz=dt.timezone.utc
                        ).isoformat().replace("+00:00", "Z")
                        if item.get("blockTime")
                        else None,
                        "fee_sol": fee / 1_000_000_000 if fee is not None else None,
                        "priority_fee_sol_estimate": max(0, fee - 5000) / 1_000_000_000
                        if fee is not None
                        else None,
                        "err": item.get("err"),
                        "kind": kind,
                    }
                )

        avg_fee = total_fee / known if known else 0
        avg_priority_fee = priority_fee / known if known else 0
        estimated_fee = total_fee if known == len(signatures) else avg_fee * len(signatures)
        estimated_priority_fee = (
            priority_fee if known == len(signatures) else avg_priority_fee * len(signatures)
        )

        return {
            "ok": True,
            "since_start_count": len(signatures),
            "fees_known_count": known,
            "fees_complete": known == len(signatures),
            "fee_sol_known": total_fee / 1_000_000_000,
            "fee_sol_total": estimated_fee / 1_000_000_000,
            "avg_fee_sol": avg_fee / 1_000_000_000,
            "priority_fee_sol_known": priority_fee / 1_000_000_000,
            "priority_fee_sol_estimate": estimated_priority_fee / 1_000_000_000,
            "failed_count": sum(1 for item in signatures if item.get("err")),
            "kind_known_count": kind_known,
            "kind_counts": kind_counts,
            "last": recent,
        }

    def get_baseline(self) -> Dict[str, Any]:
        status = None
        if self.run_dir:
            status = parse_first_commands_status(self.run_dir / "commands.log")
            if not status:
                status = parse_snapshot_status(self.run_dir / "00_before.snapshot.txt")
            if not status and self.sample_path:
                baseline = parse_first_history_baseline(self.sample_path)
                if baseline:
                    return {
                        "mid_price": baseline["mid_price"],
                        "base_total": baseline["base_total"],
                        "quote_total": baseline["quote_total"],
                        "snapshot": baseline["source"],
                        "sample_time": baseline.get("time"),
                    }
        if not status:
            status = {
                "mid_ticks": None,
                "base_total": None,
                "quote_total": None,
            }
        tick = self.get_market_meta().get("tick_price_increment", 0.001)
        start_mid = status.get("mid_ticks") * tick if status.get("mid_ticks") else None
        return {
            "mid_price": start_mid,
            "base_total": status.get("base_total"),
            "quote_total": status.get("quote_total"),
            "snapshot": str(self.run_dir / "commands.log") if self.run_dir and status else None,
        }

    def get_logs(self) -> Dict[str, Any]:
        paths = []
        count_paths = []
        if self.run_dir:
            paths = [
                self.run_dir / "bot.log",
                self.run_dir / "controller.out",
                self.run_dir / "summary.log",
                self.run_dir / "commands.log",
            ]
            count_paths = [
                self.run_dir / "bot.log",
                self.run_dir / "commands.log",
            ]
        patterns = {
            "price_feed_stale": r"Price feed stale",
            "tx_send_failed": r"TX send failed|err=\\{",
            "rpc_429": r"HTTP[^\\n]*\\b429\\b|\\b429 Too Many Requests\\b|Too Many Requests",
            "priority_fee_sampling_failed": r"priority fee sampling failed",
            "binance_ws_error": r"Binance WS error|Binance connect failed|Binance WS closed",
            "tx_circuit_breaker": r"TX circuit breaker opened|TX circuit breaker open",
        }
        return {
            "counts": count_patterns(count_paths, patterns),
            "summary_tail": tail_lines(self.run_dir / "summary.log", 40) if self.run_dir else [],
            "bot_tail": tail_lines(self.run_dir / "bot.log", 80) if self.run_dir else [],
            "controller_tail": tail_lines(self.run_dir / "controller.out", 40) if self.run_dir else [],
        }

    def get_strategy_ledger(self) -> Dict[str, Any]:
        if not self.run_dir:
            return {"active": None, "entries": []}
        active = None
        active_path = self.run_dir / "active-strategy.json"
        if active_path.exists():
            try:
                active = json.loads(active_path.read_text(errors="replace"))
            except json.JSONDecodeError:
                active = {"error": "active-strategy.json parse failed"}
        entries = []
        ledger_path = self.run_dir / "strategy-ledger.jsonl"
        if ledger_path.exists():
            for line in ledger_path.read_text(errors="replace").splitlines()[-60:]:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return {"active": active, "entries": entries}

    def compute_pnl(
        self,
        status: Dict[str, Any],
        baseline: Dict[str, Any],
        transactions: Dict[str, Any],
        mid_price: Optional[float],
    ) -> Dict[str, Any]:
        if (
            mid_price is None
            or baseline.get("mid_price") is None
            or baseline.get("base_total") is None
            or baseline.get("quote_total") is None
        ):
            return {
                "available": False,
                "reason": "missing mid price or baseline",
            }
        start_base = float(baseline["base_total"])
        start_quote = float(baseline["quote_total"])
        start_mid = float(baseline["mid_price"])
        current_base = float(status.get("base_total", 0.0))
        current_quote = float(status.get("quote_total", 0.0))
        base_delta = current_base - start_base
        quote_delta = current_quote - start_quote

        start_value = start_base * start_mid + start_quote
        current_value = current_base * mid_price + current_quote
        hold_value = start_base * mid_price + start_quote
        fee_sol = float(transactions.get("fee_sol_total") or 0.0)
        fee_usdc = fee_sol * mid_price

        return {
            "available": True,
            "start_value_usdc": start_value,
            "current_value_usdc": current_value,
            "hold_value_usdc": hold_value,
            "gross_pnl_usdc": current_value - start_value,
            "hold_pnl_usdc": hold_value - start_value,
            "trading_vs_hold_usdc": current_value - hold_value,
            "fee_usdc": fee_usdc,
            "fee_sol": fee_sol,
            "base_delta": base_delta,
            "quote_delta": quote_delta,
            "fill_detected": abs(base_delta) > 0.000001 or abs(quote_delta) > 0.0001,
            "net_trading_vs_hold_usdc": current_value - hold_value - fee_usdc,
            "net_portfolio_pnl_usdc": current_value - start_value - fee_usdc,
        }

    def collect_metrics(self) -> Dict[str, Any]:
        self.config = load_simple_toml(self.config_path)
        self.rpc_url = str(self.config.get("connection", {}).get("rpc_url", self.rpc_url))
        market = self.get_market_meta()
        status = self.get_status()
        rpc_unhealthy = not market.get("command_ok", False) or not status.get("command_ok", False)
        tick = market.get("tick_price_increment", 0.001)
        mid_price = status.get("mid_ticks") * tick if status.get("mid_ticks") else None
        baseline = self.get_baseline()
        if rpc_unhealthy:
            transactions = {
                "ok": False,
                "error": "skipped because live Archer RPC/status is unhealthy",
                "since_start_count": 0,
                "fees_known_count": 0,
                "fees_complete": False,
                "fee_sol_total": 0.0,
                "priority_fee_sol_estimate": 0.0,
                "failed_count": 0,
                "last": [],
            }
            balances = {
                "native_sol": None,
                "wsol": None,
                "usdc": None,
                "errors": ["skipped because live Archer RPC/status is unhealthy"],
            }
        else:
            transactions = self.get_transactions()
            balances = self.get_wallet_balances()
        process = self.get_process_state()
        logs = self.get_logs()
        market_intel = self.get_market_intel()
        run_end = self.run_start + dt.timedelta(seconds=self.duration_seconds) if self.run_start else None
        elapsed = (now_utc() - self.run_start).total_seconds() if self.run_start else None
        progress = (
            max(0.0, min(1.0, elapsed / self.duration_seconds))
            if elapsed is not None and self.duration_seconds > 0
            else None
        )

        strategy_ledger = self.get_strategy_ledger()
        active_settings = (
            strategy_ledger.get("active", {}).get("settings")
            if strategy_ledger.get("active")
            else None
        ) or {}
        metrics = {
            "time": iso(),
            "config_path": str(self.config_path),
            "run": {
                "run_dir": str(self.run_dir) if self.run_dir else None,
                "run_id": run_id_from_dir(self.run_dir),
                "start": iso(self.run_start) if self.run_start else None,
                "expected_end": iso(run_end) if run_end else None,
                "duration_seconds": self.duration_seconds,
                "elapsed_seconds": elapsed,
                "progress": progress,
                "completed_by_time": progress is not None and progress >= 1.0,
            },
            "market": market,
            "status": status,
            "mid_price": mid_price,
            "baseline": baseline,
            "pnl": self.compute_pnl(status, baseline, transactions, mid_price),
            "wallet": {
                "pubkey": self.wallet_pubkey,
                "balances": balances,
            },
            "transactions": transactions,
            "market_intel": market_intel,
            "process": process,
            "logs": logs,
            "strategy": {
                "active_profile": strategy_ledger.get("active", {}).get("profile")
                if strategy_ledger.get("active")
                else None,
                "active_reason": strategy_ledger.get("active", {}).get("reason")
                if strategy_ledger.get("active")
                else None,
                "active_description": strategy_ledger.get("active", {}).get("description")
                if strategy_ledger.get("active")
                else None,
                "spreads_bps": active_settings.get(
                    "spread_levels_bps", self.config.get("strategy", {}).get("spread_levels_bps", [])
                ),
                "min_effective_spread_bps": active_settings.get(
                    "min_effective_spread_bps",
                    self.config.get("strategy", {}).get("min_effective_spread_bps"),
                ),
                "effective_spreads_bps": effective_spreads_bps(
                    active_settings.get(
                        "spread_levels_bps",
                        self.config.get("strategy", {}).get("spread_levels_bps", []),
                    ),
                    active_settings.get(
                        "min_effective_spread_bps",
                        self.config.get("strategy", {}).get("min_effective_spread_bps"),
                    ),
                ),
                "inventory_pct": active_settings.get(
                    "inventory_pct", self.config.get("strategy", {}).get("inventory_pct")
                ),
                "heartbeat_ms": active_settings.get(
                    "heartbeat_interval_ms",
                    self.config.get("execution", {}).get("heartbeat_interval_ms"),
                ),
                "min_mid_update_interval_ms": active_settings.get(
                    "min_mid_update_interval_ms",
                    self.config.get("execution", {}).get("min_mid_update_interval_ms"),
                ),
                "min_mid_update_ticks": active_settings.get(
                    "min_mid_update_ticks", self.config.get("execution", {}).get("min_mid_update_ticks")
                ),
                "priority_fee_mode": self.config.get("execution", {}).get("priority_fee_mode"),
                "priority_fee_cap": self.config.get("execution", {}).get(
                    "priority_fee_max_microlamports"
                ),
            },
            "strategy_ledger": strategy_ledger,
        }
        return metrics

    def sample_from_metrics(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        pnl = metrics.get("pnl", {})
        status = metrics.get("status", {})
        tx = metrics.get("transactions", {})
        market_intel = metrics.get("market_intel", {})
        return {
            "time": metrics.get("time"),
            "mid_price": metrics.get("mid_price"),
            "bid_levels": status.get("bid_levels"),
            "ask_levels": status.get("ask_levels"),
            "status_stale": status.get("stale"),
            "base_total": status.get("base_total"),
            "quote_total": status.get("quote_total"),
            "gross_pnl_usdc": pnl.get("gross_pnl_usdc"),
            "trading_vs_hold_usdc": pnl.get("trading_vs_hold_usdc"),
            "net_trading_vs_hold_usdc": pnl.get("net_trading_vs_hold_usdc"),
            "fee_sol": pnl.get("fee_sol"),
            "fee_usdc": pnl.get("fee_usdc"),
            "tx_count": tx.get("since_start_count"),
            "tx_failed": tx.get("failed_count"),
            "tx_kind_counts": tx.get("kind_counts"),
            "intel_mode": market_intel.get("mode"),
            "intel_fair_value": market_intel.get("fair_value"),
            "intel_spread_add_bps": market_intel.get("spread_add_bps"),
            "intel_size_multiplier": market_intel.get("size_multiplier"),
            "intel_bid_size_multiplier": market_intel.get("bid_size_multiplier"),
            "intel_ask_size_multiplier": market_intel.get("ask_size_multiplier"),
            "intel_reasons": market_intel.get("reasons"),
            "min_effective_spread_bps": metrics.get("strategy", {}).get("min_effective_spread_bps"),
            "effective_spreads_bps": metrics.get("strategy", {}).get("effective_spreads_bps"),
        }

    def record_sample(self, metrics: Dict[str, Any]) -> None:
        if not self.sample_path:
            return
        self.sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample = self.sample_from_metrics(metrics)
        with self.sample_path.open("a") as fh:
            fh.write(json.dumps(sample, sort_keys=True) + "\n")

    def collect_cached(self, force: bool = False, record: bool = False) -> Dict[str, Any]:
        cached_at, cached = self.metrics_cache
        if cached and not force and time.time() - cached_at < 5:
            return cached
        if not self.lock.acquire(blocking=False):
            if cached:
                stale = copy.deepcopy(cached)
                stale["cache"] = {
                    "stale": True,
                    "reason": "live metrics collection already in progress",
                    "cached_at": dt.datetime.fromtimestamp(
                        cached_at, tz=dt.timezone.utc
                    ).isoformat().replace("+00:00", "Z"),
                }
                return stale
            return {
                "time": iso(),
                "error": "live metrics collection already in progress",
                "run": {"run_dir": str(self.run_dir) if self.run_dir else None},
            }
        try:
            metrics = self.collect_metrics()
            self.metrics_cache = (time.time(), metrics)
            if record:
                self.record_sample(metrics)
            return metrics
        finally:
            self.lock.release()

    def read_history(self, limit: int = 1000) -> List[Dict[str, Any]]:
        if not self.sample_path or not self.sample_path.exists():
            return []
        lines = self.sample_path.read_text(errors="replace").splitlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def start_sampler(self) -> None:
        def loop() -> None:
            while not self.stop_event.is_set():
                try:
                    self.collect_cached(force=True, record=True)
                except Exception as exc:  # noqa: BLE001
                    if self.sample_path:
                        self.sample_path.parent.mkdir(parents=True, exist_ok=True)
                        with self.sample_path.open("a") as fh:
                            fh.write(json.dumps({"time": iso(), "error": str(exc)}) + "\n")
                self.stop_event.wait(self.sample_interval)

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()


class DashboardHandler(SimpleHTTPRequestHandler):
    state: DashboardState

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def write_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API.
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/metrics":
            try:
                self.write_json(self.state.collect_cached(force=False, record=False))
            except Exception as exc:  # noqa: BLE001
                self.write_json({"error": str(exc), "time": iso()}, status=500)
            return
        if parsed.path == "/api/history":
            query = urllib.parse.parse_qs(parsed.query)
            limit = int(query.get("limit", ["1000"])[0])
            self.write_json({"samples": self.state.read_history(limit=limit)})
            return
        if parsed.path == "/api/health":
            self.write_json({"ok": True, "time": iso()})
            return
        super().do_GET()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Archer performance dashboard")
    parser.add_argument("--host", default=os.environ.get("ARCHER_DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ARCHER_DASHBOARD_PORT", "8787")))
    parser.add_argument("--config", default=os.environ.get("ARCHER_DASHBOARD_CONFIG", str(DEFAULT_CONFIG)))
    parser.add_argument("--run-dir", default=os.environ.get("ARCHER_DASHBOARD_RUN_DIR"))
    parser.add_argument(
        "--sample-interval",
        type=int,
        default=int(os.environ.get("ARCHER_DASHBOARD_SAMPLE_INTERVAL", "30")),
    )
    parser.add_argument(
        "--tx-lookback",
        type=int,
        default=int(os.environ.get("ARCHER_DASHBOARD_TX_LOOKBACK", "2000")),
    )
    parser.add_argument(
        "--fee-fetch-batch",
        type=int,
        default=int(os.environ.get("ARCHER_DASHBOARD_FEE_FETCH_BATCH", "30")),
    )
    args = parser.parse_args()

    config_path = pathlib.Path(args.config).expanduser()
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    run_dir = discover_run_dir(args.run_dir)
    state = DashboardState(
        config_path,
        run_dir,
        args.sample_interval,
        args.tx_lookback,
        args.fee_fetch_batch,
    )
    state.start_sampler()

    DashboardHandler.state = state
    handler = functools.partial(DashboardHandler, directory=str(STATIC_DIR))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Archer dashboard running at http://{args.host}:{args.port}")
    print(f"Config: {config_path}")
    print(f"Run dir: {run_dir}")
    try:
        server.serve_forever()
    finally:
        state.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
