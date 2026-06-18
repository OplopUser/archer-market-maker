use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CrossVenueGuardInput {
    pub archer_bid: Option<f64>,
    pub archer_ask: Option<f64>,
    pub peer_bid: Option<f64>,
    pub peer_ask: Option<f64>,
    pub peer_age_ms: u64,
    pub max_peer_age_ms: u64,
    pub duplicate_same_side_notional: f64,
    pub expected_internalization_edge_bps: f64,
    pub risk_reducing_internalization: bool,
}

impl CrossVenueGuardInput {
    pub fn default_safe() -> Self {
        Self {
            archer_bid: Some(134.0),
            archer_ask: Some(134.4),
            peer_bid: Some(133.9),
            peer_ask: Some(134.5),
            peer_age_ms: 100,
            max_peer_age_ms: 5_000,
            duplicate_same_side_notional: 0.0,
            expected_internalization_edge_bps: 0.0,
            risk_reducing_internalization: true,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CrossVenueGuardDecision {
    pub allowed: bool,
    pub internalization_allowed: bool,
    pub reason_codes: Vec<String>,
}

pub fn evaluate_cross_venue_guard(input: CrossVenueGuardInput) -> CrossVenueGuardDecision {
    let mut reason_codes = Vec::new();

    if input.peer_age_ms > input.max_peer_age_ms {
        reason_codes.push("stale_peer_venue".to_string());
    }
    if input.duplicate_same_side_notional > 0.0 {
        reason_codes.push("duplicate_same_side_risk".to_string());
    }
    if crosses_peer(
        input.archer_bid,
        input.archer_ask,
        input.peer_bid,
        input.peer_ask,
    ) {
        reason_codes.push("crossed_peer_quote".to_string());
    }

    let internalization_allowed = input.risk_reducing_internalization
        && input.expected_internalization_edge_bps > 0.0
        && reason_codes.is_empty();
    if input.risk_reducing_internalization && input.expected_internalization_edge_bps <= 0.0 {
        reason_codes.push("internalization_edge_non_positive".to_string());
    }

    CrossVenueGuardDecision {
        allowed: reason_codes.is_empty(),
        internalization_allowed,
        reason_codes,
    }
}

fn crosses_peer(
    archer_bid: Option<f64>,
    archer_ask: Option<f64>,
    peer_bid: Option<f64>,
    peer_ask: Option<f64>,
) -> bool {
    let archer_bid_crosses_peer_ask =
        matches!((archer_bid, peer_ask), (Some(bid), Some(ask)) if bid >= ask);
    let peer_bid_crosses_archer_ask =
        matches!((peer_bid, archer_ask), (Some(bid), Some(ask)) if bid >= ask);
    archer_bid_crosses_peer_ask || peer_bid_crosses_archer_ask
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blocks_crossed_peer_quote_and_stale_peer_venue() {
        let decision = evaluate_cross_venue_guard(CrossVenueGuardInput {
            archer_bid: Some(134.20),
            archer_ask: Some(134.40),
            peer_bid: Some(134.10),
            peer_ask: Some(134.15),
            peer_age_ms: 15_000,
            max_peer_age_ms: 5_000,
            duplicate_same_side_notional: 0.0,
            expected_internalization_edge_bps: 0.0,
            risk_reducing_internalization: false,
        });

        assert!(!decision.allowed);
        assert!(
            decision
                .reason_codes
                .contains(&"crossed_peer_quote".to_string())
        );
        assert!(
            decision
                .reason_codes
                .contains(&"stale_peer_venue".to_string())
        );
    }

    #[test]
    fn allows_risk_reducing_internalization_only_with_positive_edge() {
        let allowed = evaluate_cross_venue_guard(CrossVenueGuardInput {
            archer_bid: Some(134.00),
            archer_ask: Some(134.30),
            peer_bid: Some(133.90),
            peer_ask: Some(134.40),
            peer_age_ms: 100,
            max_peer_age_ms: 5_000,
            duplicate_same_side_notional: 0.0,
            expected_internalization_edge_bps: 1.5,
            risk_reducing_internalization: true,
        });
        let blocked = evaluate_cross_venue_guard(CrossVenueGuardInput {
            expected_internalization_edge_bps: -0.1,
            ..CrossVenueGuardInput::default_safe()
        });

        assert!(allowed.allowed);
        assert!(allowed.internalization_allowed);
        assert!(!blocked.internalization_allowed);
        assert!(
            blocked
                .reason_codes
                .contains(&"internalization_edge_non_positive".to_string())
        );
    }
}
