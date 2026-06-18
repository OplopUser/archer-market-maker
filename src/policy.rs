use serde::Deserialize;

use crate::archer::types::MAX_LEVELS;

#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct ArcherQuotePolicy {
    pub version: String,
    pub venue: String,
    pub max_levels: usize,
    pub bid_enabled: bool,
    pub ask_enabled: bool,
    pub min_spread_bps: f64,
    pub max_spread_bps: f64,
    pub max_quote_notional: f64,
    pub allow_mid_only_update: bool,
    pub clear_book: bool,
}

impl ArcherQuotePolicy {
    pub fn default_archer() -> Self {
        Self {
            version: "propamm.quote-policy.v1".to_string(),
            venue: "archer".to_string(),
            max_levels: 2,
            bid_enabled: true,
            ask_enabled: true,
            min_spread_bps: 62.0,
            max_spread_bps: 180.0,
            max_quote_notional: 100.0,
            allow_mid_only_update: true,
            clear_book: false,
        }
    }
}

#[derive(Debug, Copy, Clone)]
pub struct ArcherPolicyGuardrails {
    pub max_levels: usize,
    pub min_spread_bps: f64,
    pub max_spread_bps: f64,
    pub max_quote_notional: f64,
    pub allow_mid_only_update: bool,
    pub require_post_only_prevention: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AppliedArcherPolicy {
    pub bid_enabled: bool,
    pub ask_enabled: bool,
    pub levels: usize,
    pub min_spread_bps: f64,
    pub max_spread_bps: f64,
    pub max_quote_notional: f64,
    pub mid_only_update_allowed: bool,
    pub clear_book: bool,
    pub diagnostics: Vec<PolicyDiagnostic>,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum PolicyDiagnostic {
    LevelCap,
    MinSpreadFloor,
    MaxSpreadCap,
    NotionalCap,
    MidOnlyDisabledByGuardrail,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum PolicyRejectReason {
    UnsupportedVenue,
    UnsupportedPolicyVersion,
    CrossingPreventionRequired,
    InvalidSpreadBounds,
    InvalidNotionalCap,
}

pub fn apply_quote_policy(
    policy: &ArcherQuotePolicy,
    guardrails: &ArcherPolicyGuardrails,
) -> Result<AppliedArcherPolicy, PolicyRejectReason> {
    if policy.venue != "archer" {
        return Err(PolicyRejectReason::UnsupportedVenue);
    }
    if policy.version != "propamm.quote-policy.v1" {
        return Err(PolicyRejectReason::UnsupportedPolicyVersion);
    }
    if !policy.min_spread_bps.is_finite()
        || !policy.max_spread_bps.is_finite()
        || policy.max_spread_bps < policy.min_spread_bps
    {
        return Err(PolicyRejectReason::InvalidSpreadBounds);
    }
    if guardrails.require_post_only_prevention && policy.min_spread_bps < 0.0 {
        return Err(PolicyRejectReason::CrossingPreventionRequired);
    }
    if !policy.max_quote_notional.is_finite() || policy.max_quote_notional < 0.0 {
        return Err(PolicyRejectReason::InvalidNotionalCap);
    }

    if policy.clear_book {
        return Ok(AppliedArcherPolicy {
            bid_enabled: false,
            ask_enabled: false,
            levels: 0,
            min_spread_bps: guardrails.min_spread_bps,
            max_spread_bps: guardrails.max_spread_bps,
            max_quote_notional: 0.0,
            mid_only_update_allowed: false,
            clear_book: true,
            diagnostics: Vec::new(),
        });
    }

    let mut diagnostics = Vec::new();
    let local_max_levels = guardrails.max_levels.min(MAX_LEVELS);
    let levels = policy.max_levels.min(local_max_levels);
    if levels < policy.max_levels {
        diagnostics.push(PolicyDiagnostic::LevelCap);
    }

    let min_spread_bps = policy.min_spread_bps.max(guardrails.min_spread_bps);
    if min_spread_bps > policy.min_spread_bps {
        diagnostics.push(PolicyDiagnostic::MinSpreadFloor);
    }

    let max_spread_bps = policy.max_spread_bps.min(guardrails.max_spread_bps);
    if max_spread_bps < policy.max_spread_bps {
        diagnostics.push(PolicyDiagnostic::MaxSpreadCap);
    }

    let max_quote_notional = policy.max_quote_notional.min(guardrails.max_quote_notional);
    if max_quote_notional < policy.max_quote_notional {
        diagnostics.push(PolicyDiagnostic::NotionalCap);
    }

    let mid_only_update_allowed = policy.allow_mid_only_update && guardrails.allow_mid_only_update;
    if policy.allow_mid_only_update && !guardrails.allow_mid_only_update {
        diagnostics.push(PolicyDiagnostic::MidOnlyDisabledByGuardrail);
    }

    Ok(AppliedArcherPolicy {
        bid_enabled: policy.bid_enabled,
        ask_enabled: policy.ask_enabled,
        levels,
        min_spread_bps,
        max_spread_bps,
        max_quote_notional,
        mid_only_update_allowed,
        clear_book: false,
        diagnostics,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn guardrails() -> ArcherPolicyGuardrails {
        ArcherPolicyGuardrails {
            max_levels: 4,
            min_spread_bps: 62.0,
            max_spread_bps: 200.0,
            max_quote_notional: 100.0,
            allow_mid_only_update: true,
            require_post_only_prevention: true,
        }
    }

    #[test]
    fn maps_archer_quote_policy_inside_local_guardrails() {
        let policy = ArcherQuotePolicy {
            version: "propamm.quote-policy.v1".to_string(),
            venue: "archer".to_string(),
            max_levels: 8,
            bid_enabled: true,
            ask_enabled: true,
            min_spread_bps: 24.0,
            max_spread_bps: 250.0,
            max_quote_notional: 250.0,
            allow_mid_only_update: true,
            clear_book: false,
        };

        let applied = apply_quote_policy(&policy, &guardrails()).expect("policy should apply");

        assert_eq!(applied.levels, 4);
        assert_eq!(applied.min_spread_bps, 62.0);
        assert_eq!(applied.max_spread_bps, 200.0);
        assert_eq!(applied.max_quote_notional, 100.0);
        assert!(applied.mid_only_update_allowed);
        assert!(applied.diagnostics.contains(&PolicyDiagnostic::LevelCap));
        assert!(applied.diagnostics.contains(&PolicyDiagnostic::NotionalCap));
    }

    #[test]
    fn rejects_manifest_only_or_crossing_unsafe_policy() {
        let mut policy = ArcherQuotePolicy::default_archer();
        policy.venue = "manifest".to_string();
        assert_eq!(
            apply_quote_policy(&policy, &guardrails()).unwrap_err(),
            PolicyRejectReason::UnsupportedVenue
        );

        let mut no_post_only = ArcherQuotePolicy::default_archer();
        no_post_only.min_spread_bps = -1.0;
        assert_eq!(
            apply_quote_policy(&no_post_only, &guardrails()).unwrap_err(),
            PolicyRejectReason::CrossingPreventionRequired
        );
    }

    #[test]
    fn clear_book_policy_survives_guardrails_without_generating_quotes() {
        let mut policy = ArcherQuotePolicy::default_archer();
        policy.clear_book = true;

        let applied =
            apply_quote_policy(&policy, &guardrails()).expect("clear policy should apply");

        assert!(applied.clear_book);
        assert_eq!(applied.levels, 0);
        assert!(!applied.bid_enabled);
        assert!(!applied.ask_enabled);
    }
}
