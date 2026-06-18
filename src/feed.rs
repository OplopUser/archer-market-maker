use std::sync::Arc;
use std::sync::atomic::Ordering::Relaxed;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::Value;
use tokio::time::sleep;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tokio_util::sync::CancellationToken;

use crate::config::FeedSettings;
use crate::intel::{MarketIntelRejectReason, MarketIntelSignal};
use crate::state::{SharedState, now_us};
use crate::volatility::VolatilityTracker;

#[derive(Debug, Deserialize)]
struct BinanceBookTicker {
    s: String,
    b: String,
    a: String,
}

fn parse_binance_book_ticker(txt: &str) -> Option<(String, f64, f64)> {
    let bt: BinanceBookTicker = serde_json::from_str(txt).ok()?;
    let bid: f64 = bt.b.parse().ok()?;
    let ask: f64 = bt.a.parse().ok()?;
    if bid > 0.0 && ask > 0.0 && ask >= bid {
        Some((bt.s, bid, ask))
    } else {
        None
    }
}

fn handle_tick(state: &SharedState, vol_tracker: &mut VolatilityTracker, bid: f64, ask: f64) {
    let mid = (bid + ask) * 0.5;
    state.mid_price.store(mid, Relaxed);
    state.price_timestamp_us.store(now_us(), Relaxed);
    vol_tracker.push(mid);
    state
        .volatility_bps
        .store(vol_tracker.realized_vol_bps(), Relaxed);
}

fn value_as_finite_f64(value: &Value) -> Option<f64> {
    let parsed = match value {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.parse::<f64>().ok(),
        _ => None,
    }?;
    parsed.is_finite().then_some(parsed)
}

fn json_finite_f64_at(value: &Value, path: &str) -> Option<f64> {
    value.pointer(path).and_then(value_as_finite_f64)
}

fn json_positive_f64_at(value: &Value, path: &str) -> Option<f64> {
    json_finite_f64_at(value, path).filter(|value| *value > 0.0)
}

fn bounded_multiplier(value: Option<f64>, fallback: f64) -> f64 {
    value
        .filter(|v| v.is_finite())
        .unwrap_or(fallback)
        .clamp(0.0, 1.0)
}

fn market_intel_price(signal: &Value) -> Option<f64> {
    [
        "/recommendation/fair_value",
        "/reference/price",
        "/sources/manifest/mid",
        "/sources/binance/mid",
        "/sources/hyperliquid/mid",
    ]
    .iter()
    .find_map(|path| json_positive_f64_at(signal, path))
}

fn market_intel_expected_pair(configured_pair: &str, binance_symbol: &str) -> String {
    let configured_pair = configured_pair.trim();
    if !configured_pair.is_empty() {
        return configured_pair.to_string();
    }
    let symbol = binance_symbol.trim().to_uppercase();
    for suffix in ["USDT", "USDC", "USD"] {
        if let Some(base) = symbol.strip_suffix(suffix) {
            return format!("{base}/USDC");
        }
    }
    symbol
}

async fn run_market_intel_feed(
    state: Arc<SharedState>,
    url: String,
    expected_pair: String,
    max_signal_age_us: u64,
    poll_ms: u64,
    vol_window: usize,
    cancel: CancellationToken,
) {
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
    {
        Ok(client) => client,
        Err(e) => {
            tracing::error!("market-intel HTTP client initialization failed: {e}");
            return;
        }
    };
    let poll = Duration::from_millis(poll_ms.max(1_000));
    let mut vol_tracker = VolatilityTracker::new(vol_window);
    let mut failures = 0u64;

    tracing::info!(%url, %expected_pair, poll_ms = poll.as_millis(), "Using typed market-intel price feed");
    loop {
        if cancel.is_cancelled() {
            return;
        }

        let result = async {
            client
                .get(&url)
                .send()
                .await?
                .error_for_status()?
                .json::<Value>()
                .await
        }
        .await;

        match result {
            Ok(signal) => {
                match MarketIntelSignal::from_value(
                    &signal,
                    &expected_pair,
                    now_us(),
                    max_signal_age_us,
                ) {
                    Ok(accepted) => {
                        handle_tick(
                            &state,
                            &mut vol_tracker,
                            accepted.fair_value,
                            accepted.fair_value,
                        );
                        state
                            .intel_spread_add_bps
                            .store(accepted.adjustments.spread_add_bps, Relaxed);
                        state
                            .intel_size_multiplier
                            .store(accepted.adjustments.size_multiplier, Relaxed);
                        state
                            .intel_bid_size_multiplier
                            .store(accepted.adjustments.bid_size_multiplier, Relaxed);
                        state
                            .intel_ask_size_multiplier
                            .store(accepted.adjustments.ask_size_multiplier, Relaxed);
                        state.price_notify.notify_one();
                        failures = 0;
                        if !state.feed_alive.load(Relaxed) {
                            state.feed_alive.store(true, Relaxed);
                        }
                    }
                    Err(reject) => {
                        failures = failures.saturating_add(1);
                        state.feed_alive.store(false, Relaxed);
                        state.intel_size_multiplier.store(0.0, Relaxed);
                        state.intel_bid_size_multiplier.store(0.0, Relaxed);
                        state.intel_ask_size_multiplier.store(0.0, Relaxed);
                        state.price_notify.notify_one();
                        if failures == 1 || failures % 30 == 0 {
                            tracing::warn!(
                                ?reject.reason,
                                detail = %reject.detail,
                                "market-intel signal rejected by typed Archer validator"
                            );
                        }
                        if matches!(
                            reject.reason,
                            MarketIntelRejectReason::Paused
                                | MarketIntelRejectReason::QuoteDisabled
                        ) {
                            state.intel_spread_add_bps.store(0.0, Relaxed);
                        }
                    }
                }
            }
            Err(e) => {
                failures = failures.saturating_add(1);
                state.feed_alive.store(false, Relaxed);
                if failures == 1 || failures % 30 == 0 {
                    tracing::warn!("market-intel feed request failed: {e}");
                }
            }
        }

        tokio::select! {
            _ = cancel.cancelled() => return,
            _ = sleep(poll) => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn market_intel_price_requires_positive_value() {
        let signal = serde_json::json!({
            "recommendation": {
                "fair_value": 0,
                "spread_add_bps": 0,
                "ask_size_multiplier": 0
            },
            "reference": {
                "price": "80.25"
            }
        });

        assert_eq!(market_intel_price(&signal), Some(80.25));
        assert_eq!(
            json_finite_f64_at(&signal, "/recommendation/ask_size_multiplier"),
            Some(0.0)
        );
        assert_eq!(
            json_finite_f64_at(&signal, "/recommendation/spread_add_bps"),
            Some(0.0)
        );
    }

    #[test]
    fn market_intel_multipliers_accept_zero_as_intentional_pause() {
        assert_eq!(bounded_multiplier(Some(0.0), 1.0), 0.0);
        assert_eq!(bounded_multiplier(Some(1.5), 1.0), 1.0);
        assert_eq!(bounded_multiplier(Some(0.45), 1.0), 0.45);
        assert_eq!(bounded_multiplier(None, 1.0), 1.0);
    }

    #[test]
    fn derives_archer_signal_pair_from_binance_symbol() {
        assert_eq!(market_intel_expected_pair("", "SOLUSDT"), "SOL/USDC");
        assert_eq!(
            market_intel_expected_pair("SOL/USDC", "SOLUSDT"),
            "SOL/USDC"
        );
    }
}

pub async fn run_feed(
    state: Arc<SharedState>,
    config: FeedSettings,
    vol_window: usize,
    cancel: CancellationToken,
) {
    if let Some(url) = config
        .market_intel_signal_url
        .as_deref()
        .map(str::trim)
        .filter(|url| !url.is_empty())
    {
        run_market_intel_feed(
            state,
            url.to_string(),
            market_intel_expected_pair(&config.market_intel_pair, &config.binance_symbol),
            config.staleness_timeout_ms.saturating_mul(1000),
            config.market_intel_poll_ms,
            vol_window,
            cancel,
        )
        .await;
        return;
    }

    let primary = config.binance_symbol.to_uppercase();
    let cross = config.cross_symbol.to_uppercase();
    let use_cross = !cross.is_empty();

    let primary_stream = format!("{}@bookTicker", primary.to_lowercase());
    let mut streams: Vec<String> = vec![primary_stream];
    if use_cross {
        streams.push(format!("{}@bookTicker", cross.to_lowercase()));
    }

    let subscribe_msg = serde_json::json!({
        "method": "SUBSCRIBE",
        "params": streams,
        "id": 1
    })
    .to_string();

    let mut backoff_ms: u64 = 100;
    let mut vol_tracker = VolatilityTracker::new(vol_window);

    let mut primary_bid: f64 = 0.0;
    let mut primary_ask: f64 = 0.0;
    let mut cross_bid: f64 = 1.0;
    let mut cross_ask: f64 = 1.0;

    loop {
        if cancel.is_cancelled() {
            return;
        }

        let url = &config.binance_ws_url;
        tracing::info!(%url, ?streams, "Connecting to Binance");

        match connect_async(url).await {
            Ok((ws_stream, _)) => {
                backoff_ms = 100;
                state.feed_alive.store(true, Relaxed);

                let (mut write, mut read) = ws_stream.split();

                if let Err(e) = write.send(Message::Text(subscribe_msg.clone())).await {
                    tracing::warn!("Subscribe send failed: {e}");
                    state.feed_alive.store(false, Relaxed);
                    continue;
                }

                loop {
                    tokio::select! {
                        _ = cancel.cancelled() => return,
                        msg = read.next() => {
                            match msg {
                                Some(Ok(Message::Text(txt))) => {
                                    if let Some(bt) = parse_binance_book_ticker(&txt) {
                                        let sym = bt.0.to_uppercase();
                                        if sym == primary {
                                            primary_bid = bt.1;
                                            primary_ask = bt.2;
                                        } else if use_cross && sym == cross {
                                            cross_bid = bt.1;
                                            cross_ask = bt.2;
                                        } else {
                                            continue;
                                        }

                                        if primary_bid <= 0.0 || primary_ask <= 0.0 {
                                            continue;
                                        }
                                        if use_cross && (cross_bid <= 0.0 || cross_ask <= 0.0) {
                                            continue;
                                        }

                                        let (bid, ask) = if use_cross {
                                            let cross_mid = (cross_bid + cross_ask) * 0.5;
                                            (primary_bid / cross_mid, primary_ask / cross_mid)
                                        } else {
                                            (primary_bid, primary_ask)
                                        };

                                        handle_tick(&state, &mut vol_tracker, bid, ask);
                                        state.price_notify.notify_one();

                                        if !state.feed_alive.load(Relaxed) {
                                            state.feed_alive.store(true, Relaxed);
                                        }
                                    }
                                }
                                Some(Ok(Message::Ping(data))) => {
                                    let _ = write.send(Message::Pong(data)).await;
                                }
                                Some(Ok(Message::Close(_))) | None => {
                                    tracing::warn!("Binance WS closed");
                                    break;
                                }
                                Some(Err(e)) => {
                                    tracing::warn!("Binance WS error: {e}");
                                    break;
                                }
                                _ => {}
                            }
                        }
                    }
                }

                state.feed_alive.store(false, Relaxed);
            }
            Err(e) => {
                tracing::warn!("Binance connect failed: {e}");
                state.feed_alive.store(false, Relaxed);
            }
        }

        tracing::info!(backoff_ms, "Reconnecting in {backoff_ms}ms");
        sleep(Duration::from_millis(backoff_ms)).await;
        backoff_ms = (backoff_ms * 2).min(5000);
    }
}
