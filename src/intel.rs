use serde::Deserialize;
use serde_json::Value;

use crate::policy::ArcherQuotePolicy;
use crate::strategy::IntelAdjustments;

const SUPPORTED_SIGNAL_VERSION: &str = "propamm.market-intel.v1";
const SUPPORTED_POLICY_VERSION: &str = "propamm.quote-policy.v1";
const MIN_PUBLIC_SOURCES: u64 = 2;
const MIN_ROUTE_QUALITY_SCORE: f64 = 0.50;

#[derive(Debug, Clone, PartialEq)]
pub struct MarketIntelSignal {
    pub pair: String,
    pub generated_at_us: u64,
    pub fair_value: f64,
    pub adjustments: IntelAdjustments,
    pub policy: ArcherQuotePolicy,
    pub route_quality_score: f64,
    pub reasons: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarketIntelReject {
    pub reason: MarketIntelRejectReason,
    pub detail: String,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum MarketIntelRejectReason {
    Malformed,
    UnsupportedVersion,
    UnsupportedPolicy,
    WrongPair,
    Stale,
    FutureTimestamp,
    Paused,
    QuoteDisabled,
    MissingFairValue,
    InsufficientPublicSources,
    PrivateTelemetryAuthoritative,
    RouteQualityDegraded,
}

#[derive(Debug, Deserialize)]
struct RawSignal {
    version: Option<String>,
    pair: Option<String>,
    generated_at_us: Option<u64>,
    mode: Option<String>,
    recommendation: Option<RawRecommendation>,
    quote_policy: Option<ArcherQuotePolicy>,
    sources: Option<RawSources>,
    route_quality: Option<RawRouteQuality>,
}

#[derive(Debug, Deserialize)]
struct RawRecommendation {
    quote_enabled: Option<bool>,
    fair_value: Option<Value>,
    spread_add_bps: Option<Value>,
    size_multiplier: Option<Value>,
    bid_size_multiplier: Option<Value>,
    ask_size_multiplier: Option<Value>,
    reasons: Option<Vec<String>>,
}

#[derive(Debug, Deserialize)]
struct RawSources {
    public_count: Option<u64>,
    private_bot_telemetry_count: Option<u64>,
}

#[derive(Debug, Deserialize)]
struct RawRouteQuality {
    status: Option<String>,
    score: Option<Value>,
}

impl MarketIntelSignal {
    pub fn from_value(
        value: &Value,
        expected_pair: &str,
        now_us: u64,
        max_age_us: u64,
    ) -> Result<Self, MarketIntelReject> {
        let raw: RawSignal = serde_json::from_value(value.clone()).map_err(|e| {
            reject(
                MarketIntelRejectReason::Malformed,
                format!("signal could not be decoded: {e}"),
            )
        })?;

        let version = raw.version.as_deref().unwrap_or("");
        if version != SUPPORTED_SIGNAL_VERSION {
            return Err(reject(
                MarketIntelRejectReason::UnsupportedVersion,
                format!("unsupported signal version {version}"),
            ));
        }

        let pair = raw.pair.unwrap_or_default();
        if pair != expected_pair {
            return Err(reject(
                MarketIntelRejectReason::WrongPair,
                format!("signal pair {pair} != expected {expected_pair}"),
            ));
        }

        let generated_at_us = raw.generated_at_us.ok_or_else(|| {
            reject(
                MarketIntelRejectReason::Malformed,
                "generated_at_us missing".to_string(),
            )
        })?;
        if generated_at_us > now_us {
            return Err(reject(
                MarketIntelRejectReason::FutureTimestamp,
                "generated_at_us is in the future".to_string(),
            ));
        }
        if now_us.saturating_sub(generated_at_us) > max_age_us {
            return Err(reject(
                MarketIntelRejectReason::Stale,
                "signal timestamp exceeded freshness budget".to_string(),
            ));
        }

        if raw.mode.as_deref() == Some("pause") {
            return Err(reject(
                MarketIntelRejectReason::Paused,
                "signal mode is pause".to_string(),
            ));
        }

        let recommendation = raw.recommendation.ok_or_else(|| {
            reject(
                MarketIntelRejectReason::Malformed,
                "recommendation missing".to_string(),
            )
        })?;
        if recommendation.quote_enabled == Some(false) {
            return Err(reject(
                MarketIntelRejectReason::QuoteDisabled,
                "recommendation disabled quoting".to_string(),
            ));
        }

        let fair_value =
            value_as_positive_f64(recommendation.fair_value.as_ref()).ok_or_else(|| {
                reject(
                    MarketIntelRejectReason::MissingFairValue,
                    "fair_value missing or non-positive".to_string(),
                )
            })?;

        let policy = raw.quote_policy.ok_or_else(|| {
            reject(
                MarketIntelRejectReason::UnsupportedPolicy,
                "quote_policy missing".to_string(),
            )
        })?;
        if policy.version != SUPPORTED_POLICY_VERSION || policy.venue != "archer" {
            return Err(reject(
                MarketIntelRejectReason::UnsupportedPolicy,
                format!(
                    "unsupported policy {} for venue {}",
                    policy.version, policy.venue
                ),
            ));
        }

        let sources = raw.sources.unwrap_or(RawSources {
            public_count: Some(0),
            private_bot_telemetry_count: Some(0),
        });
        if sources.public_count.unwrap_or(0) < MIN_PUBLIC_SOURCES {
            return Err(reject(
                MarketIntelRejectReason::InsufficientPublicSources,
                "not enough public sources for Archer consumer".to_string(),
            ));
        }
        if sources.private_bot_telemetry_count.unwrap_or(0) > 0 {
            return Err(reject(
                MarketIntelRejectReason::PrivateTelemetryAuthoritative,
                "private bot telemetry cannot be authoritative for Archer".to_string(),
            ));
        }

        let route_quality = raw.route_quality.unwrap_or(RawRouteQuality {
            status: Some("unknown".to_string()),
            score: Some(Value::from(0.0)),
        });
        let route_quality_score = value_as_finite_f64(route_quality.score.as_ref()).unwrap_or(0.0);
        if route_quality.status.as_deref() == Some("degraded")
            || route_quality_score < MIN_ROUTE_QUALITY_SCORE
        {
            return Err(reject(
                MarketIntelRejectReason::RouteQualityDegraded,
                "route quality is degraded".to_string(),
            ));
        }

        Ok(Self {
            pair,
            generated_at_us,
            fair_value,
            adjustments: IntelAdjustments {
                spread_add_bps: value_as_finite_f64(recommendation.spread_add_bps.as_ref())
                    .unwrap_or(0.0),
                size_multiplier: bounded_multiplier(recommendation.size_multiplier.as_ref(), 1.0),
                bid_size_multiplier: bounded_multiplier(
                    recommendation.bid_size_multiplier.as_ref(),
                    1.0,
                ),
                ask_size_multiplier: bounded_multiplier(
                    recommendation.ask_size_multiplier.as_ref(),
                    1.0,
                ),
            },
            policy,
            route_quality_score,
            reasons: recommendation.reasons.unwrap_or_default(),
        })
    }
}

fn reject(reason: MarketIntelRejectReason, detail: String) -> MarketIntelReject {
    MarketIntelReject { reason, detail }
}

fn value_as_finite_f64(value: Option<&Value>) -> Option<f64> {
    let parsed = match value? {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.parse::<f64>().ok(),
        _ => None,
    }?;
    parsed.is_finite().then_some(parsed)
}

fn value_as_positive_f64(value: Option<&Value>) -> Option<f64> {
    value_as_finite_f64(value).filter(|value| *value > 0.0)
}

fn bounded_multiplier(value: Option<&Value>, fallback: f64) -> f64 {
    value_as_finite_f64(value)
        .unwrap_or(fallback)
        .clamp(0.0, 1.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base_signal() -> serde_json::Value {
        serde_json::json!({
            "version": "propamm.market-intel.v1",
            "pair": "SOL/USDC",
            "generated_at_us": 1_000_000_u64,
            "mode": "quote",
            "recommendation": {
                "quote_enabled": true,
                "fair_value": 80.25,
                "spread_add_bps": 8.0,
                "size_multiplier": 0.75,
                "bid_size_multiplier": 0.50,
                "ask_size_multiplier": 0.90,
                "reasons": ["public_route_ok"]
            },
            "quote_policy": {
                "version": "propamm.quote-policy.v1",
                "venue": "archer",
                "max_levels": 4,
                "bid_enabled": true,
                "ask_enabled": true,
                "min_spread_bps": 62.0,
                "max_spread_bps": 180.0,
                "max_quote_notional": 120.0,
                "allow_mid_only_update": true,
                "clear_book": false
            },
            "sources": {
                "public_count": 3,
                "private_bot_telemetry_count": 0,
                "classes": ["public_cex", "public_dex", "route_quality"]
            },
            "route_quality": {
                "status": "ok",
                "score": 0.86
            }
        })
    }

    #[test]
    fn accepts_fresh_archer_signal_with_typed_policy() {
        let accepted =
            MarketIntelSignal::from_value(&base_signal(), "SOL/USDC", 1_100_000, 200_000)
                .expect("signal should validate");

        assert_eq!(accepted.pair, "SOL/USDC");
        assert_eq!(accepted.policy.version, "propamm.quote-policy.v1");
        assert_eq!(accepted.policy.venue, "archer");
        assert_eq!(accepted.adjustments.size_multiplier, 0.75);
        assert!(accepted.policy.allow_mid_only_update);
    }

    #[test]
    fn rejects_pause_stale_wrong_market_and_private_authoritative_sources() {
        let mut paused = base_signal();
        paused["mode"] = serde_json::json!("pause");
        assert_eq!(
            MarketIntelSignal::from_value(&paused, "SOL/USDC", 1_100_000, 200_000)
                .unwrap_err()
                .reason,
            MarketIntelRejectReason::Paused
        );

        let mut stale = base_signal();
        stale["generated_at_us"] = serde_json::json!(800_000_u64);
        assert_eq!(
            MarketIntelSignal::from_value(&stale, "SOL/USDC", 1_100_001, 200_000)
                .unwrap_err()
                .reason,
            MarketIntelRejectReason::Stale
        );

        let mut wrong_pair = base_signal();
        wrong_pair["pair"] = serde_json::json!("ETH/USDC");
        assert_eq!(
            MarketIntelSignal::from_value(&wrong_pair, "SOL/USDC", 1_100_000, 200_000)
                .unwrap_err()
                .reason,
            MarketIntelRejectReason::WrongPair
        );

        let mut private_feedback = base_signal();
        private_feedback["sources"]["private_bot_telemetry_count"] = serde_json::json!(1);
        assert_eq!(
            MarketIntelSignal::from_value(&private_feedback, "SOL/USDC", 1_100_000, 200_000)
                .unwrap_err()
                .reason,
            MarketIntelRejectReason::PrivateTelemetryAuthoritative
        );
    }

    #[test]
    fn rejects_unsupported_policy_low_source_count_and_degraded_route_quality() {
        let mut unsupported = base_signal();
        unsupported["quote_policy"]["version"] = serde_json::json!("manifest-only.v1");
        assert_eq!(
            MarketIntelSignal::from_value(&unsupported, "SOL/USDC", 1_100_000, 200_000)
                .unwrap_err()
                .reason,
            MarketIntelRejectReason::UnsupportedPolicy
        );

        let mut low_sources = base_signal();
        low_sources["sources"]["public_count"] = serde_json::json!(1);
        assert_eq!(
            MarketIntelSignal::from_value(&low_sources, "SOL/USDC", 1_100_000, 200_000)
                .unwrap_err()
                .reason,
            MarketIntelRejectReason::InsufficientPublicSources
        );

        let mut degraded = base_signal();
        degraded["route_quality"]["status"] = serde_json::json!("degraded");
        assert_eq!(
            MarketIntelSignal::from_value(&degraded, "SOL/USDC", 1_100_000, 200_000)
                .unwrap_err()
                .reason,
            MarketIntelRejectReason::RouteQualityDegraded
        );
    }
}
