use std::collections::BTreeMap;

use serde::Serialize;

use crate::evidence::Side;

#[derive(Debug, Clone, PartialEq)]
pub struct AfterCostEvent {
    pub side: Side,
    pub policy_version: String,
    pub hour_bucket: String,
    pub regime: String,
    pub notional_usdc: f64,
    pub realized_spread_bps: f64,
    pub markout_bps: f64,
    pub tx_fee_usdc: f64,
    pub failed_tx_cost_usdc: f64,
    pub clear_mid_update_cost_usdc: f64,
    pub inventory_beta_usdc: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct AfterCostReport {
    pub buckets: Vec<AfterCostBucket>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct AfterCostBucket {
    pub side: Side,
    pub policy_version: String,
    pub hour_bucket: String,
    pub regime: String,
    pub fill_count: u64,
    pub notional_usdc: f64,
    pub realized_spread_bps: f64,
    pub markout_bps: f64,
    pub tx_fee_usdc: f64,
    pub failed_tx_cost_usdc: f64,
    pub clear_mid_update_cost_usdc: f64,
    pub inventory_beta_usdc: f64,
    pub after_cost_edge_usdc: f64,
    pub after_cost_edge_bps: f64,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct BucketKey {
    side: Side,
    policy_version: String,
    hour_bucket: String,
    regime: String,
}

#[derive(Default)]
struct Accumulator {
    fill_count: u64,
    notional_usdc: f64,
    realized_spread_usdc: f64,
    markout_usdc: f64,
    tx_fee_usdc: f64,
    failed_tx_cost_usdc: f64,
    clear_mid_update_cost_usdc: f64,
    inventory_beta_usdc: f64,
}

pub fn build_after_cost_report(events: Vec<AfterCostEvent>) -> AfterCostReport {
    let mut buckets: BTreeMap<BucketKey, Accumulator> = BTreeMap::new();
    for event in events {
        let key = BucketKey {
            side: event.side,
            policy_version: event.policy_version,
            hour_bucket: event.hour_bucket,
            regime: event.regime,
        };
        let acc = buckets.entry(key).or_default();
        acc.fill_count += 1;
        acc.notional_usdc += event.notional_usdc;
        acc.realized_spread_usdc += bps_to_usdc(event.notional_usdc, event.realized_spread_bps);
        acc.markout_usdc += bps_to_usdc(event.notional_usdc, event.markout_bps);
        acc.tx_fee_usdc += event.tx_fee_usdc;
        acc.failed_tx_cost_usdc += event.failed_tx_cost_usdc;
        acc.clear_mid_update_cost_usdc += event.clear_mid_update_cost_usdc;
        acc.inventory_beta_usdc += event.inventory_beta_usdc;
    }

    let buckets = buckets
        .into_iter()
        .map(|(key, acc)| {
            let after_cost_edge_usdc = acc.realized_spread_usdc
                - acc.markout_usdc
                - acc.tx_fee_usdc
                - acc.failed_tx_cost_usdc
                - acc.clear_mid_update_cost_usdc
                - acc.inventory_beta_usdc;
            AfterCostBucket {
                side: key.side,
                policy_version: key.policy_version,
                hour_bucket: key.hour_bucket,
                regime: key.regime,
                fill_count: acc.fill_count,
                notional_usdc: acc.notional_usdc,
                realized_spread_bps: usdc_to_bps(acc.notional_usdc, acc.realized_spread_usdc),
                markout_bps: usdc_to_bps(acc.notional_usdc, acc.markout_usdc),
                tx_fee_usdc: acc.tx_fee_usdc,
                failed_tx_cost_usdc: acc.failed_tx_cost_usdc,
                clear_mid_update_cost_usdc: acc.clear_mid_update_cost_usdc,
                inventory_beta_usdc: acc.inventory_beta_usdc,
                after_cost_edge_usdc,
                after_cost_edge_bps: usdc_to_bps(acc.notional_usdc, after_cost_edge_usdc),
            }
        })
        .collect();
    AfterCostReport { buckets }
}

fn bps_to_usdc(notional_usdc: f64, bps: f64) -> f64 {
    notional_usdc * bps / 10_000.0
}

fn usdc_to_bps(notional_usdc: f64, value_usdc: f64) -> f64 {
    if notional_usdc > 0.0 {
        value_usdc / notional_usdc * 10_000.0
    } else {
        0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::evidence::Side;

    #[test]
    fn aggregates_after_cost_edge_by_side_policy_hour_and_regime() {
        let report = build_after_cost_report(vec![
            AfterCostEvent {
                side: Side::Bid,
                policy_version: "policy-a".to_string(),
                hour_bucket: "2026-06-18T01".to_string(),
                regime: "normal".to_string(),
                notional_usdc: 100.0,
                realized_spread_bps: 8.0,
                markout_bps: -1.0,
                tx_fee_usdc: 0.01,
                failed_tx_cost_usdc: 0.02,
                clear_mid_update_cost_usdc: 0.03,
                inventory_beta_usdc: -0.04,
            },
            AfterCostEvent {
                side: Side::Bid,
                policy_version: "policy-a".to_string(),
                hour_bucket: "2026-06-18T01".to_string(),
                regime: "normal".to_string(),
                notional_usdc: 50.0,
                realized_spread_bps: 6.0,
                markout_bps: 2.0,
                tx_fee_usdc: 0.01,
                failed_tx_cost_usdc: 0.0,
                clear_mid_update_cost_usdc: 0.0,
                inventory_beta_usdc: 0.0,
            },
        ]);

        assert_eq!(report.buckets.len(), 1);
        let bucket = &report.buckets[0];
        assert_eq!(bucket.side, Side::Bid);
        assert_eq!(bucket.policy_version, "policy-a");
        assert_eq!(bucket.fill_count, 2);
        assert!((bucket.notional_usdc - 150.0).abs() < f64::EPSILON);
        assert!(bucket.after_cost_edge_usdc > 0.0);
        assert!(bucket.after_cost_edge_bps > 0.0);
    }
}
