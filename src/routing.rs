#[derive(Debug, Clone, PartialEq)]
pub struct VenueQuote {
    pub venue: String,
    pub side: QuoteSide,
    pub price: f64,
    pub size: f64,
    pub expected_edge_bps: f64,
    pub risk_reducing: bool,
    pub readback_fresh: bool,
}

impl VenueQuote {
    pub fn bid(
        venue: &str,
        price: f64,
        size: f64,
        expected_edge_bps: f64,
        risk_reducing: bool,
    ) -> Self {
        Self::new(
            venue,
            QuoteSide::Bid,
            price,
            size,
            expected_edge_bps,
            risk_reducing,
        )
    }

    pub fn ask(
        venue: &str,
        price: f64,
        size: f64,
        expected_edge_bps: f64,
        risk_reducing: bool,
    ) -> Self {
        Self::new(
            venue,
            QuoteSide::Ask,
            price,
            size,
            expected_edge_bps,
            risk_reducing,
        )
    }

    fn new(
        venue: &str,
        side: QuoteSide,
        price: f64,
        size: f64,
        expected_edge_bps: f64,
        risk_reducing: bool,
    ) -> Self {
        Self {
            venue: venue.to_string(),
            side,
            price,
            size,
            expected_edge_bps,
            risk_reducing,
            readback_fresh: true,
        }
    }

    pub fn with_stale_readback(mut self) -> Self {
        self.readback_fresh = false;
        self
    }
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum QuoteSide {
    Bid,
    Ask,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum PortfolioIntent {
    Neutral,
    ReduceLongBase,
    ReduceShortBase,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum RoutingAction {
    Allow,
    AllowInternalization,
    Block,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum RoutingReason {
    NoConflict,
    SelfCross,
    DuplicateSameSideExposure,
    InventoryReducingPositiveEdge,
    StaleReadback,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub struct RoutingDecision {
    pub action: RoutingAction,
    pub reason: RoutingReason,
}

pub fn evaluate_cross_venue_quotes(
    quotes: &[VenueQuote],
    intent: PortfolioIntent,
) -> RoutingDecision {
    if quotes.iter().any(|quote| !quote.readback_fresh) {
        return block(RoutingReason::StaleReadback);
    }

    for (idx, left) in quotes.iter().enumerate() {
        for right in &quotes[idx + 1..] {
            if left.venue == right.venue {
                continue;
            }
            if left.side == right.side {
                return block(RoutingReason::DuplicateSameSideExposure);
            }
            if crosses(left, right) {
                return block(RoutingReason::SelfCross);
            }
            if internalization_allowed(left, right, intent) {
                return RoutingDecision {
                    action: RoutingAction::AllowInternalization,
                    reason: RoutingReason::InventoryReducingPositiveEdge,
                };
            }
        }
    }

    RoutingDecision {
        action: RoutingAction::Allow,
        reason: RoutingReason::NoConflict,
    }
}

fn crosses(left: &VenueQuote, right: &VenueQuote) -> bool {
    match (left.side, right.side) {
        (QuoteSide::Bid, QuoteSide::Ask) => left.price >= right.price,
        (QuoteSide::Ask, QuoteSide::Bid) => right.price >= left.price,
        _ => false,
    }
}

fn internalization_allowed(left: &VenueQuote, right: &VenueQuote, intent: PortfolioIntent) -> bool {
    let positive_edge = left.expected_edge_bps > 0.0 && right.expected_edge_bps > 0.0;
    let risk_reducing = left.risk_reducing || right.risk_reducing;
    let side_reduces_inventory = match intent {
        PortfolioIntent::ReduceLongBase => {
            left.side == QuoteSide::Ask || right.side == QuoteSide::Ask
        }
        PortfolioIntent::ReduceShortBase => {
            left.side == QuoteSide::Bid || right.side == QuoteSide::Bid
        }
        PortfolioIntent::Neutral => false,
    };
    positive_edge && risk_reducing && side_reduces_inventory
}

fn block(reason: RoutingReason) -> RoutingDecision {
    RoutingDecision {
        action: RoutingAction::Block,
        reason,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blocks_self_crossing_manifest_bid_against_archer_ask() {
        let manifest = VenueQuote::bid("manifest", 80.00, 10.0, 4.0, false);
        let archer = VenueQuote::ask("archer", 79.99, 8.0, 5.0, false);

        let decision = evaluate_cross_venue_quotes(&[manifest, archer], PortfolioIntent::Neutral);

        assert_eq!(decision.action, RoutingAction::Block);
        assert_eq!(decision.reason, RoutingReason::SelfCross);
    }

    #[test]
    fn blocks_duplicate_same_side_exposure_without_internalization_edge() {
        let manifest = VenueQuote::bid("manifest", 80.00, 10.0, 4.0, false);
        let archer = VenueQuote::bid("archer", 79.98, 10.0, 2.0, false);

        let decision = evaluate_cross_venue_quotes(&[manifest, archer], PortfolioIntent::Neutral);

        assert_eq!(decision.action, RoutingAction::Block);
        assert_eq!(decision.reason, RoutingReason::DuplicateSameSideExposure);
    }

    #[test]
    fn permits_positive_edge_inventory_reducing_internalization() {
        let manifest = VenueQuote::bid("manifest", 80.00, 10.0, 4.0, false);
        let archer = VenueQuote::ask("archer", 80.10, 6.0, 7.0, true);

        let decision =
            evaluate_cross_venue_quotes(&[manifest, archer], PortfolioIntent::ReduceLongBase);

        assert_eq!(decision.action, RoutingAction::AllowInternalization);
        assert_eq!(
            decision.reason,
            RoutingReason::InventoryReducingPositiveEdge
        );
    }

    #[test]
    fn stale_venue_state_blocks_combined_quoting() {
        let manifest = VenueQuote::bid("manifest", 80.00, 10.0, 4.0, false);
        let archer = VenueQuote::ask("archer", 80.10, 6.0, 7.0, false).with_stale_readback();

        let decision =
            evaluate_cross_venue_quotes(&[manifest, archer], PortfolioIntent::ReduceLongBase);

        assert_eq!(decision.action, RoutingAction::Block);
        assert_eq!(decision.reason, RoutingReason::StaleReadback);
    }
}
