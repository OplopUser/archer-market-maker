use std::io::{Result as IoResult, Write};

use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Side {
    Bid,
    Ask,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum QuoteDecisionKind {
    ClearBook,
    UpdateMidOnly,
    UpdateFull,
    HoldStale,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MakerBookAction {
    Update,
    Clear,
    MidUpdate,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct NoFillExposure {
    pub bid_notional: f64,
    pub ask_notional: f64,
    pub active_bid_levels: u64,
    pub active_ask_levels: u64,
    pub in_flight_update_notional: f64,
    pub stale_hold: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct QuoteDecisionEvidence {
    pub decision: QuoteDecisionKind,
    pub reason_codes: Vec<String>,
    pub route_quality_score: Option<f64>,
    pub no_fill_exposure: NoFillExposure,
    pub expected_fill_probability: Option<f64>,
    pub expected_edge_bps: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct MakerBookEvidence {
    pub action: MakerBookAction,
    pub bid_levels: u64,
    pub ask_levels: u64,
    pub base_total: f64,
    pub quote_total: f64,
    pub mid_price_ticks: u64,
    pub pending_update_notional: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct FillEvidence {
    pub side: Side,
    pub size_base: f64,
    pub price: f64,
    pub mark_price: f64,
    pub tx_fee_usdc: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct MarketIntelEvidence {
    pub policy_version: String,
    pub fair_value: f64,
    pub spread_add_bps: f64,
    pub size_multiplier: f64,
    pub bid_size_multiplier: f64,
    pub ask_size_multiplier: f64,
    pub route_quality_score: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "event_type", rename_all = "snake_case")]
pub enum RetainedPayload {
    QuoteDecision {
        policy_version: String,
        #[serde(flatten)]
        evidence: QuoteDecisionEvidence,
    },
    MakerbookUpdate {
        #[serde(flatten)]
        evidence: MakerBookEvidence,
    },
    TxFailure {
        purpose: String,
        fee_lamports: u64,
        reason: String,
    },
    Fill {
        #[serde(flatten)]
        evidence: FillEvidence,
    },
    StaleHold {
        feed_age_ms: u64,
        reason: String,
    },
    MarketIntelSnapshot {
        #[serde(flatten)]
        evidence: MarketIntelEvidence,
    },
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RetainedEvent {
    pub schema_version: u16,
    pub venue: String,
    pub run_id: String,
    pub market: String,
    pub timestamp_ms: u64,
    pub sequence: u64,
    #[serde(flatten)]
    pub payload: RetainedPayload,
}

impl RetainedEvent {
    fn new(
        run_id: &str,
        market: &str,
        timestamp_ms: u64,
        sequence: u64,
        payload: RetainedPayload,
    ) -> Self {
        Self {
            schema_version: 1,
            venue: "archer".to_string(),
            run_id: run_id.to_string(),
            market: market.to_string(),
            timestamp_ms,
            sequence,
            payload,
        }
    }

    pub fn quote_decision(
        run_id: &str,
        market: &str,
        timestamp_ms: u64,
        sequence: u64,
        policy_version: &str,
        evidence: QuoteDecisionEvidence,
    ) -> Self {
        Self::new(
            run_id,
            market,
            timestamp_ms,
            sequence,
            RetainedPayload::QuoteDecision {
                policy_version: policy_version.to_string(),
                evidence,
            },
        )
    }

    pub fn makerbook_update(
        run_id: &str,
        market: &str,
        timestamp_ms: u64,
        sequence: u64,
        evidence: MakerBookEvidence,
    ) -> Self {
        Self::new(
            run_id,
            market,
            timestamp_ms,
            sequence,
            RetainedPayload::MakerbookUpdate { evidence },
        )
    }

    pub fn tx_failure(
        run_id: &str,
        market: &str,
        timestamp_ms: u64,
        sequence: u64,
        purpose: &str,
        fee_lamports: u64,
        reason: &str,
    ) -> Self {
        Self::new(
            run_id,
            market,
            timestamp_ms,
            sequence,
            RetainedPayload::TxFailure {
                purpose: purpose.to_string(),
                fee_lamports,
                reason: reason.to_string(),
            },
        )
    }

    pub fn fill(
        run_id: &str,
        market: &str,
        timestamp_ms: u64,
        sequence: u64,
        evidence: FillEvidence,
    ) -> Self {
        Self::new(
            run_id,
            market,
            timestamp_ms,
            sequence,
            RetainedPayload::Fill { evidence },
        )
    }

    pub fn stale_hold(
        run_id: &str,
        market: &str,
        timestamp_ms: u64,
        sequence: u64,
        feed_age_ms: u64,
        reason: &str,
    ) -> Self {
        Self::new(
            run_id,
            market,
            timestamp_ms,
            sequence,
            RetainedPayload::StaleHold {
                feed_age_ms,
                reason: reason.to_string(),
            },
        )
    }

    pub fn market_intel_snapshot(
        run_id: &str,
        market: &str,
        timestamp_ms: u64,
        sequence: u64,
        evidence: MarketIntelEvidence,
    ) -> Self {
        Self::new(
            run_id,
            market,
            timestamp_ms,
            sequence,
            RetainedPayload::MarketIntelSnapshot { evidence },
        )
    }

    pub fn event_type(&self) -> &'static str {
        match self.payload {
            RetainedPayload::QuoteDecision { .. } => "quote_decision",
            RetainedPayload::MakerbookUpdate { .. } => "makerbook_update",
            RetainedPayload::TxFailure { .. } => "tx_failure",
            RetainedPayload::Fill { .. } => "fill",
            RetainedPayload::StaleHold { .. } => "stale_hold",
            RetainedPayload::MarketIntelSnapshot { .. } => "market_intel_snapshot",
        }
    }
}

pub struct RetainedJsonlWriter<W> {
    writer: W,
}

impl<W: Write> RetainedJsonlWriter<W> {
    pub fn new(writer: W) -> Self {
        Self { writer }
    }

    pub fn write_event(&mut self, event: &RetainedEvent) -> IoResult<()> {
        serde_json::to_writer(&mut self.writer, event)?;
        self.writer.write_all(b"\n")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_quote_decision_with_no_fill_exposure_as_one_jsonl_row() {
        let event = RetainedEvent::quote_decision(
            "shadow-run-001",
            "SOL/USDC",
            1_789_000_000_000,
            42,
            "policy-fixture",
            QuoteDecisionEvidence {
                decision: QuoteDecisionKind::UpdateFull,
                reason_codes: vec!["normal_policy".to_string()],
                route_quality_score: Some(0.97),
                no_fill_exposure: NoFillExposure {
                    bid_notional: 50.0,
                    ask_notional: 45.0,
                    active_bid_levels: 2,
                    active_ask_levels: 2,
                    in_flight_update_notional: 12.5,
                    stale_hold: false,
                },
                expected_fill_probability: Some(0.18),
                expected_edge_bps: Some(4.2),
            },
        );

        let mut buffer = Vec::new();
        RetainedJsonlWriter::new(&mut buffer)
            .write_event(&event)
            .expect("writer should serialize event");

        let row: serde_json::Value =
            serde_json::from_slice(&buffer).expect("jsonl row should parse as json");
        assert_eq!(row["schema_version"], 1);
        assert_eq!(row["venue"], "archer");
        assert_eq!(row["event_type"], "quote_decision");
        assert_eq!(row["policy_version"], "policy-fixture");
        assert_eq!(row["no_fill_exposure"]["bid_notional"], 50.0);
        assert_eq!(row["expected_edge_bps"], 4.2);
    }

    #[test]
    fn canonical_events_cover_makerbook_tx_fill_stale_and_market_intel() {
        let events = vec![
            RetainedEvent::makerbook_update(
                "run",
                "SOL/USDC",
                1,
                10,
                MakerBookEvidence {
                    action: MakerBookAction::Clear,
                    bid_levels: 0,
                    ask_levels: 0,
                    base_total: 1.2,
                    quote_total: 100.0,
                    mid_price_ticks: 123,
                    pending_update_notional: 0.0,
                },
            ),
            RetainedEvent::tx_failure("run", "SOL/USDC", 2, 11, "update_book", 5_000, "blockhash"),
            RetainedEvent::fill(
                "run",
                "SOL/USDC",
                3,
                12,
                FillEvidence {
                    side: Side::Bid,
                    size_base: 0.1,
                    price: 134.0,
                    mark_price: 134.3,
                    tx_fee_usdc: 0.001,
                },
            ),
            RetainedEvent::stale_hold("run", "SOL/USDC", 4, 13, 30_000, "price_feed_stale"),
            RetainedEvent::market_intel_snapshot(
                "run",
                "SOL/USDC",
                5,
                14,
                MarketIntelEvidence {
                    policy_version: "policy-fixture".to_string(),
                    fair_value: 134.25,
                    spread_add_bps: 12.0,
                    size_multiplier: 0.8,
                    bid_size_multiplier: 0.7,
                    ask_size_multiplier: 0.9,
                    route_quality_score: Some(0.92),
                },
            ),
        ];

        let event_types: Vec<String> = events
            .iter()
            .map(|event| event.event_type().to_string())
            .collect();

        assert_eq!(
            event_types,
            vec![
                "makerbook_update",
                "tx_failure",
                "fill",
                "stale_hold",
                "market_intel_snapshot"
            ]
        );
    }
}
