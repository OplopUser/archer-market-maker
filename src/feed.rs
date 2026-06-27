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

fn json_bool_at(value: &Value, path: &str) -> Option<bool> {
    value.pointer(path).and_then(Value::as_bool)
}

fn json_str_at<'a>(value: &'a Value, path: &str) -> Option<&'a str> {
    value.pointer(path).and_then(Value::as_str)
}

fn bounded_multiplier(value: Option<f64>, fallback: f64) -> f64 {
    value
        .filter(|v| v.is_finite())
        .unwrap_or(fallback)
        .clamp(0.0, 1.0)
}

#[derive(Debug, PartialEq)]
struct MarketIntelMultipliers {
    size: f64,
    bid_size: f64,
    ask_size: f64,
}

fn capped_market_intel_multiplier(
    signal: &Value,
    recommendation_path: &str,
    risk_cap_path: &str,
    fallback: f64,
) -> f64 {
    let recommendation =
        bounded_multiplier(json_finite_f64_at(signal, recommendation_path), fallback);
    match json_finite_f64_at(signal, risk_cap_path) {
        Some(cap) => recommendation.min(bounded_multiplier(Some(cap), 1.0)),
        None => recommendation,
    }
}

fn market_intel_multipliers(signal: &Value) -> MarketIntelMultipliers {
    let size = capped_market_intel_multiplier(
        signal,
        "/recommendation/size_multiplier",
        "/risk_budget/max_size_multiplier",
        1.0,
    );
    let bid_size = if json_bool_at(signal, "/risk_budget/bid_enabled").unwrap_or(true) {
        capped_market_intel_multiplier(
            signal,
            "/recommendation/bid_size_multiplier",
            "/risk_budget/max_bid_size_multiplier",
            1.0,
        )
    } else {
        0.0
    };
    let ask_size = if json_bool_at(signal, "/risk_budget/ask_enabled").unwrap_or(true) {
        capped_market_intel_multiplier(
            signal,
            "/recommendation/ask_size_multiplier",
            "/risk_budget/max_ask_size_multiplier",
            1.0,
        )
    } else {
        0.0
    };

    MarketIntelMultipliers {
        size,
        bid_size,
        ask_size,
    }
}

fn market_intel_price(signal: &Value) -> Option<f64> {
    let signal = market_intel_signal_payload(signal);
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

fn market_intel_signal_payload(response: &Value) -> &Value {
    response
        .get("signal")
        .filter(|signal| signal.is_object())
        .unwrap_or(response)
}

async fn run_market_intel_feed(
    state: Arc<SharedState>,
    url: String,
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

    tracing::info!(%url, poll_ms = poll.as_millis(), "Using market-intel price feed");
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
            Ok(response) => {
                let signal = market_intel_signal_payload(&response);
                let quote_enabled =
                    json_bool_at(signal, "/recommendation/quote_enabled").unwrap_or(true);
                let mode = json_str_at(signal, "/mode").unwrap_or("unknown");
                if !quote_enabled || mode == "pause" {
                    failures = failures.saturating_add(1);
                    state.feed_alive.store(false, Relaxed);
                    state.intel_size_multiplier.store(0.0, Relaxed);
                    state.intel_bid_size_multiplier.store(0.0, Relaxed);
                    state.intel_ask_size_multiplier.store(0.0, Relaxed);
                    state.price_notify.notify_one();
                    if failures == 1 || failures % 30 == 0 {
                        tracing::warn!(mode, quote_enabled, "market-intel signal is not quoteable");
                    }
                } else if let Some(price) = market_intel_price(signal) {
                    let multipliers = market_intel_multipliers(signal);
                    handle_tick(&state, &mut vol_tracker, price, price);
                    state.intel_spread_add_bps.store(
                        json_finite_f64_at(signal, "/recommendation/spread_add_bps")
                            .filter(|v| v.is_finite())
                            .unwrap_or(0.0),
                        Relaxed,
                    );
                    state.intel_size_multiplier.store(multipliers.size, Relaxed);
                    state
                        .intel_bid_size_multiplier
                        .store(multipliers.bid_size, Relaxed);
                    state
                        .intel_ask_size_multiplier
                        .store(multipliers.ask_size, Relaxed);
                    state.price_notify.notify_one();
                    failures = 0;
                    if !state.feed_alive.load(Relaxed) {
                        state.feed_alive.store(true, Relaxed);
                    }
                } else {
                    failures = failures.saturating_add(1);
                    state.feed_alive.store(false, Relaxed);
                    if failures == 1 || failures % 30 == 0 {
                        tracing::warn!("market-intel signal did not include a usable price");
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
    fn market_intel_price_accepts_archer_scoped_signal_wrapper() {
        let scoped = serde_json::json!({
            "consumer": "archer",
            "allowed_source_set": ["binance", "manifest"],
            "signal": {
                "mode": "normal",
                "recommendation": {
                    "quote_enabled": true,
                    "fair_value": "81.42"
                },
                "reference": {
                    "price": "81.40"
                }
            }
        });

        assert_eq!(market_intel_price(&scoped), Some(81.42));
    }

    #[test]
    fn market_intel_multipliers_apply_risk_budget_caps() {
        let signal = serde_json::json!({
            "recommendation": {
                "size_multiplier": "0.90",
                "bid_size_multiplier": "0.70",
                "ask_size_multiplier": "0.80"
            },
            "risk_budget": {
                "bid_enabled": true,
                "ask_enabled": true,
                "max_size_multiplier": "0.15",
                "max_bid_size_multiplier": "0.35",
                "max_ask_size_multiplier": "1.0"
            }
        });

        assert_eq!(
            market_intel_multipliers(&signal),
            MarketIntelMultipliers {
                size: 0.15,
                bid_size: 0.35,
                ask_size: 0.80
            }
        );
    }

    #[test]
    fn market_intel_multipliers_zero_risk_budget_disabled_side() {
        let signal = serde_json::json!({
            "recommendation": {
                "size_multiplier": "1",
                "bid_size_multiplier": "1",
                "ask_size_multiplier": "1"
            },
            "risk_budget": {
                "bid_enabled": false,
                "ask_enabled": true,
                "max_size_multiplier": "1",
                "max_bid_size_multiplier": "1",
                "max_ask_size_multiplier": "1"
            }
        });

        assert_eq!(
            market_intel_multipliers(&signal),
            MarketIntelMultipliers {
                size: 1.0,
                bid_size: 0.0,
                ask_size: 1.0
            }
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
