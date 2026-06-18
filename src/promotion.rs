use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct PromotionGateInput {
    pub shadow_passed: bool,
    pub live_canary_passed: bool,
    pub after_cost_edge_bps: f64,
    pub min_after_cost_edge_bps: f64,
    pub cross_venue_safe: bool,
    pub requested_capital_usdc: f64,
    pub approved_capital_usdc: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PromotionGate {
    pub shadow_passed: bool,
    pub live_canary_passed: bool,
    pub after_cost_edge_pass: bool,
    pub cross_venue_safe: bool,
    pub capital_increase_allowed: bool,
    pub multi_venue_allowed: bool,
    pub reason_codes: Vec<String>,
}

pub fn evaluate_promotion_gate(input: PromotionGateInput) -> PromotionGate {
    let after_cost_edge_pass = input.after_cost_edge_bps >= input.min_after_cost_edge_bps;
    let capital_increase_allowed = input.shadow_passed
        && input.live_canary_passed
        && after_cost_edge_pass
        && input.cross_venue_safe
        && input.requested_capital_usdc <= input.approved_capital_usdc;
    let multi_venue_allowed = capital_increase_allowed && input.cross_venue_safe;
    let mut reason_codes = Vec::new();

    if !input.shadow_passed {
        reason_codes.push("shadow_not_passed".to_string());
    }
    if !input.live_canary_passed {
        reason_codes.push("live_canary_not_passed".to_string());
    }
    if !after_cost_edge_pass {
        reason_codes.push("after_cost_edge_below_floor".to_string());
    }
    if !input.cross_venue_safe {
        reason_codes.push("cross_venue_not_safe".to_string());
    }
    if input.requested_capital_usdc > input.approved_capital_usdc {
        reason_codes.push("capital_request_above_limit".to_string());
    }

    PromotionGate {
        shadow_passed: input.shadow_passed,
        live_canary_passed: input.live_canary_passed,
        after_cost_edge_pass,
        cross_venue_safe: input.cross_venue_safe,
        capital_increase_allowed,
        multi_venue_allowed,
        reason_codes,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn promotion_gate_blocks_capital_and_multi_venue_until_all_prereqs_pass() {
        let gate = evaluate_promotion_gate(PromotionGateInput {
            shadow_passed: true,
            live_canary_passed: false,
            after_cost_edge_bps: 0.8,
            min_after_cost_edge_bps: 1.0,
            cross_venue_safe: true,
            requested_capital_usdc: 1_500.0,
            approved_capital_usdc: 1_000.0,
        });

        assert!(gate.shadow_passed);
        assert!(!gate.live_canary_passed);
        assert!(!gate.after_cost_edge_pass);
        assert!(gate.cross_venue_safe);
        assert!(!gate.capital_increase_allowed);
        assert!(!gate.multi_venue_allowed);
        assert!(
            gate.reason_codes
                .contains(&"live_canary_not_passed".to_string())
        );
        assert!(
            gate.reason_codes
                .contains(&"after_cost_edge_below_floor".to_string())
        );
        assert!(
            gate.reason_codes
                .contains(&"capital_request_above_limit".to_string())
        );
    }
}
