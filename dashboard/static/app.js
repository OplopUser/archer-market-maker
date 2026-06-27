const fmtUsd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 3,
});
const fmtNum = new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 });
const fmtTiny = new Intl.NumberFormat("en-US", { maximumFractionDigits: 9 });

const els = {};
for (const id of [
  "runState",
  "lastUpdated",
  "runWindow",
  "progressBar",
  "netTrading",
  "netTradingNote",
  "tradingPnl",
  "tradingPnlNote",
  "feeSpend",
  "feeCoverage",
  "midPrice",
  "midTicks",
  "activeQuotes",
  "lockedNotional",
  "txCount",
  "txFailures",
  "inventoryList",
  "healthList",
  "strategyList",
  "ledgerList",
  "txList",
  "logTail",
]) {
  els[id] = document.getElementById(id);
}

function clsByValue(value) {
  value = cleanMoney(value);
  if (value == null || Number.isNaN(value)) return "";
  if (value > 0) return "positive";
  if (value < 0) return "negative";
  return "";
}

function cleanMoney(value) {
  if (value == null || Number.isNaN(value)) return value;
  return Math.abs(value) < 0.0005 ? 0 : value;
}

function setMoney(el, value) {
  value = cleanMoney(value);
  el.textContent = value == null ? "--" : fmtUsd.format(value);
  el.className = clsByValue(value);
}

function fmtMaybeNum(value, suffix = "") {
  return value == null ? "--" : `${fmtNum.format(value)}${suffix}`;
}

function shortTime(iso) {
  if (!iso) return "--";
  return new Date(iso).toLocaleString([], {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function duration(seconds) {
  if (seconds == null) return "--";
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${String(m).padStart(2, "0")}m`;
}

function row(label, value) {
  const item = document.createElement("div");
  item.className = "row";
  const left = document.createElement("span");
  left.textContent = label;
  const right = document.createElement("strong");
  right.textContent = value;
  item.append(left, right);
  return item;
}

function healthRow(label, value, state) {
  const item = document.createElement("div");
  item.className = `health-row ${state}`;
  const left = document.createElement("span");
  left.textContent = label;
  const right = document.createElement("strong");
  right.textContent = value;
  item.append(left, right);
  return item;
}

function renderInventory(data) {
  const status = data.status || {};
  const wallet = data.wallet?.balances || {};
  const pnl = data.pnl || {};
  const baseLocked = status.base_locked || 0;
  const quoteLocked = status.quote_locked || 0;
  const mid = data.mid_price || 0;
  els.inventoryList.replaceChildren(
    row("MakerBook SOL total", `${fmtNum.format(status.base_total || 0)} SOL`),
    row("MakerBook USDC total", `${fmtNum.format(status.quote_total || 0)} USDC`),
    row("SOL delta from start", fmtMaybeNum(pnl.base_delta, " SOL")),
    row("USDC delta from start", fmtMaybeNum(pnl.quote_delta, " USDC")),
    row("Locked SOL", `${fmtNum.format(baseLocked)} SOL`),
    row("Locked USDC", `${fmtNum.format(quoteLocked)} USDC`),
    row("Locked notional", fmtUsd.format(baseLocked * mid + quoteLocked)),
    row("Wallet native SOL", `${fmtNum.format(wallet.native_sol || 0)} SOL`),
    row("Wallet WSOL", `${fmtNum.format(wallet.wsol || 0)} WSOL`),
    row("Wallet USDC", `${fmtNum.format(wallet.usdc || 0)} USDC`),
  );
}

function renderHealth(data) {
  const process = data.process || {};
  const logs = data.logs?.counts || {};
  const tx = data.transactions || {};
  const live = data.archer_live_status || {};
  const kindCounts = tx.kind_counts || {};
  const txMix = `mid+book ${kindCounts.mid_book || 0} · book ${kindCounts.book_only || 0} · mid ${kindCounts.mid_only || 0} · clear ${kindCounts.clear || 0}`;
  const txMixState = (kindCounts.clear || 0) > 1 ? "warn" : "ok";
  const liveState = live.state ? `${live.state} · ${live.action || "--"}` : "--";
  const liveRowState = live.alert?.severity === "critical" ? "bad" : live.alert?.severity === "warning" ? "warn" : "ok";
  const lastFillSeconds = data.pnl?.seconds_since_last_fill;
  const lastFillText = lastFillSeconds == null ? "--" : `${duration(lastFillSeconds)} ago`;
  const lastFillState = data.pnl?.fills_this_window ? "ok" : lastFillSeconds > 3600 ? "warn" : "ok";
  const rows = [
    healthRow("Archer live status", liveState, liveRowState),
    healthRow("Controller", process.controller_running ? "running" : "down", process.controller_running ? "ok" : "bad"),
    healthRow("Bot process", process.bot_running ? "running" : "down", process.bot_running ? "ok" : "bad"),
    healthRow("Price feed stale", String(logs.price_feed_stale || 0), logs.price_feed_stale ? "warn" : "ok"),
    healthRow("TX send failures", String(logs.tx_send_failed || 0), logs.tx_send_failed ? "warn" : "ok"),
    healthRow("RPC 429", String(logs.rpc_429 || 0), logs.rpc_429 ? "bad" : "ok"),
    healthRow("Priority fee sample failures", String(logs.priority_fee_sampling_failed || 0), logs.priority_fee_sampling_failed ? "warn" : "ok"),
    healthRow("TX circuit breaker", String(logs.tx_circuit_breaker || 0), logs.tx_circuit_breaker ? "bad" : "ok"),
    healthRow("On-chain failed tx", String(tx.failed_count || 0), tx.failed_count ? "warn" : "ok"),
    healthRow("Archer TX mix", txMix, txMixState),
    healthRow("Fill PnL signal", data.pnl?.fill_detected ? "inventory changed" : "no inventory change", data.pnl?.fill_detected ? "ok" : "warn"),
    healthRow("Last fill", lastFillText, lastFillState),
  ];
  els.healthList.replaceChildren(...rows);
}

function renderStrategy(data) {
  const s = data.strategy || {};
  const market = data.market || {};
  const intel = data.market_intel || {};
  const approval = s.profile_approval || {};
  const approvalText = approval.approved == null
    ? "--"
    : `${approval.approved ? "approved" : "blocked"} · ${approval.requested_profile || s.active_profile || "--"} -> ${approval.effective_profile || s.active_profile || "--"} · ${approval.source || "--"}`;
  const intelText = intel.enabled
    ? intel.ok
      ? `${intel.mode || "--"} · add ${fmtMaybeNum(intel.spread_add_bps, " bps")} · size ${fmtMaybeNum(intel.size_multiplier)} / ${fmtMaybeNum(intel.bid_size_multiplier)} / ${fmtMaybeNum(intel.ask_size_multiplier)}`
      : `unavailable · ${intel.error || "request failed"}`
    : "disabled";
  els.strategyList.replaceChildren(
    row("Active profile", s.active_profile || "static"),
    row("Profile approval", approvalText),
    row("Profile reason", s.active_reason || "--"),
    row("Spreads", `${(s.spreads_bps || []).join(" / ")} bps`),
    row("Market intel", intelText),
    row("Inventory pct", `${s.inventory_pct ?? "--"}%`),
    row("Heartbeat", `${s.heartbeat_ms ?? "--"} ms`),
    row("Mid update throttle", `${s.min_mid_update_interval_ms ?? "--"} ms / ${s.min_mid_update_ticks ?? "--"} ticks`),
    row("Forced discovery trial", s.forced_transition_remaining_trial_minutes == null ? "--" : `${fmtNum.format(s.forced_transition_remaining_trial_minutes)}m left`),
    row("Priority fee", `${s.priority_fee_mode || "--"} cap ${s.priority_fee_cap ?? "--"} micro-lamports`),
    row("Maker fee", `${market.maker_fee_ppm ?? "--"} ppm`),
    row("Taker fee", `${market.taker_fee_ppm ?? "--"} ppm`),
  );
}

function renderLedger(data) {
  const entries = (data.strategy_ledger?.entries || []).slice(-8).reverse();
  if (!entries.length) {
    els.ledgerList.replaceChildren(row("No ledger entries", "--"));
    return;
  }
  const nodes = entries.map((entry) => {
    const item = document.createElement("div");
    item.className = "tx-row";
    const left = document.createElement("span");
    const code = document.createElement("code");
    code.textContent = `${entry.event || "event"} · ${entry.profile || "--"}`;
    const meta = document.createElement("small");
    meta.textContent = `${shortTime(entry.time)} · ${entry.reason || ""}`;
    left.append(code, meta);
    const right = document.createElement("strong");
    right.textContent = entry.changed ? "changed" : "held";
    item.append(left, right);
    return item;
  });
  els.ledgerList.replaceChildren(...nodes);
}

function renderTx(data) {
  const txs = data.transactions?.last || [];
  if (!txs.length) {
    els.txList.replaceChildren(row("No transactions", "--"));
    return;
  }
  const nodes = txs.map((tx) => {
    const item = document.createElement("div");
    item.className = "tx-row";
    const left = document.createElement("span");
    const code = document.createElement("code");
    code.textContent = tx.signature;
    const meta = document.createElement("small");
    meta.textContent = `${shortTime(tx.block_time)}${tx.kind ? ` · ${tx.kind}` : ""}${tx.err ? " · failed" : ""}`;
    left.append(code, meta);
    const right = document.createElement("strong");
    right.textContent = tx.fee_sol == null ? "--" : `${fmtTiny.format(tx.fee_sol)} SOL`;
    item.append(left, right);
    return item;
  });
  els.txList.replaceChildren(...nodes);
}

function updateKpis(data) {
  const pnl = data.pnl || {};
  const status = data.status || {};
  const tx = data.transactions || {};
  const run = data.run || {};
  const process = data.process || {};
  const strategy = data.strategy || {};
  const lockedNotional = (status.base_locked || 0) * (data.mid_price || 0) + (status.quote_locked || 0);
  const directRun = !process.controller_running && process.bot_running && /adaptive disabled|direct/i.test(strategy.active_reason || "");
  const feeSol = pnl.fee_sol ?? tx.fee_sol_total ?? 0;
  const feeUsdc = pnl.fee_usdc ?? (feeSol * (data.mid_price || 0));

  setMoney(els.netTrading, pnl.net_trading_vs_hold_usdc);
  setMoney(els.tradingPnl, pnl.trading_vs_hold_usdc);
  if (pnl.available === false) {
    els.netTradingNote.textContent = pnl.reason || "PnL baseline unavailable";
    els.tradingPnlNote.textContent = "Waiting for a baseline sample";
  } else {
    els.netTradingNote.textContent = pnl.fill_detected
      ? "After estimated tx fees"
      : "No fill PnL detected; currently fee drag";
    els.tradingPnlNote.textContent = pnl.fill_detected
      ? "Excludes SOL market drift"
      : "Inventory is unchanged from start";
  }
  els.feeSpend.textContent = `${fmtTiny.format(feeSol)} SOL`;
  els.feeCoverage.textContent = `${fmtUsd.format(feeUsdc)} · ${tx.fees_known_count || 0}/${tx.since_start_count || 0} tx priced`;
  els.midPrice.textContent = data.mid_price == null ? "--" : fmtUsd.format(data.mid_price);
  els.midTicks.textContent = status.mid_ticks == null ? "--" : `${status.mid_ticks} ticks`;
  els.activeQuotes.textContent = `${status.bid_levels || 0} bids / ${status.ask_levels || 0} asks`;
  els.lockedNotional.textContent = fmtUsd.format(lockedNotional);
  els.txCount.textContent = String(tx.since_start_count || 0);
  els.txFailures.textContent = `${tx.failed_count || 0} failed`;
  els.lastUpdated.textContent = `Updated ${shortTime(data.time)}`;

  const running = process.bot_running && (process.controller_running || directRun);
  els.runState.textContent = running ? (directRun ? "Running Direct" : "Running") : data.run?.completed_by_time ? "Complete" : "Attention";
  els.runState.className = `state-pill ${running ? "running" : data.run?.completed_by_time ? "warning" : "down"}`;

  const remaining = run.duration_seconds && run.elapsed_seconds != null ? run.duration_seconds - run.elapsed_seconds : null;
  els.runWindow.textContent = `${shortTime(run.start)} to ${shortTime(run.expected_end)} · ${duration(remaining)} left`;
  els.progressBar.style.width = `${Math.round((run.progress || 0) * 100)}%`;
}

function drawChart(canvas, samples, series) {
  const ctx = canvas.getContext("2d");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);

  const pad = { left: 58, right: 18, top: 20, bottom: 34 };
  const plotW = Math.max(10, width - pad.left - pad.right);
  const plotH = Math.max(10, height - pad.top - pad.bottom);
  const points = samples
    .filter((s) => s.time)
    .map((s) => ({ ...s, t: new Date(s.time).getTime() }))
    .filter((s) => Number.isFinite(s.t));

  ctx.strokeStyle = "#d9e0e8";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top);
  ctx.lineTo(pad.left, pad.top + plotH);
  ctx.lineTo(pad.left + plotW, pad.top + plotH);
  ctx.stroke();

  if (points.length < 2) {
    ctx.fillStyle = "#647184";
    ctx.font = "13px system-ui";
    ctx.fillText("Collecting samples", pad.left + 14, pad.top + 28);
    return;
  }

  const xs = points.map((p) => p.t);
  const ys = [];
  for (const p of points) {
    for (const s of series) {
      const value = p[s.key];
      if (value != null && Number.isFinite(value)) ys.push(value);
    }
  }
  if (!ys.length) return;
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  let minY = Math.min(...ys);
  let maxY = Math.max(...ys);
  if (minY === maxY) {
    minY -= 1;
    maxY += 1;
  }
  const yPad = (maxY - minY) * 0.12;
  minY -= yPad;
  maxY += yPad;

  const x = (t) => pad.left + ((t - minX) / Math.max(1, maxX - minX)) * plotW;
  const y = (v) => pad.top + plotH - ((v - minY) / Math.max(0.000001, maxY - minY)) * plotH;

  ctx.fillStyle = "#647184";
  ctx.font = "12px system-ui";
  ctx.fillText(fmtUsd.format(maxY), 8, pad.top + 10);
  ctx.fillText(fmtUsd.format(minY), 8, pad.top + plotH);

  const zeroY = y(0);
  if (zeroY >= pad.top && zeroY <= pad.top + plotH) {
    ctx.strokeStyle = "#b7c2cf";
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.left, zeroY);
    ctx.lineTo(pad.left + plotW, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  for (const s of series) {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    let started = false;
    for (const p of points) {
      const value = p[s.key];
      if (value == null || !Number.isFinite(value)) continue;
      if (!started) {
        ctx.moveTo(x(p.t), y(value));
        started = true;
      } else {
        ctx.lineTo(x(p.t), y(value));
      }
    }
    ctx.stroke();
  }

  let legendX = pad.left;
  for (const s of series) {
    ctx.fillStyle = s.color;
    ctx.fillRect(legendX, height - 18, 10, 10);
    ctx.fillStyle = "#17202a";
    ctx.fillText(s.label, legendX + 14, height - 9);
    legendX += ctx.measureText(s.label).width + 34;
  }
}

async function loadJson(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} ${res.status}`);
  return res.json();
}

async function refresh() {
  try {
    const [metrics, history] = await Promise.all([
      loadJson("/api/metrics"),
      loadJson("/api/history?limit=1000"),
    ]);
    updateKpis(metrics);
    renderInventory(metrics);
    renderHealth(metrics);
    renderStrategy(metrics);
    renderLedger(metrics);
    renderTx(metrics);
    els.logTail.textContent = (metrics.logs?.summary_tail || []).join("\n") || "No summary lines yet.";
    const samples = history.samples || [];
    drawChart(document.getElementById("pnlChart"), samples, [
      { key: "gross_pnl_usdc", label: "gross", color: "#2f63d7" },
      { key: "net_trading_vs_hold_usdc", label: "net vs hold", color: "#007a78" },
    ]);
    drawChart(document.getElementById("feeChart"), samples, [
      { key: "fee_usdc", label: "fees", color: "#b7791f" },
    ]);
  } catch (err) {
    els.runState.textContent = "API error";
    els.runState.className = "state-pill down";
    els.lastUpdated.textContent = err.message;
  }
}

refresh();
setInterval(refresh, 15000);
window.addEventListener("resize", refresh);
