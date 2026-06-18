#[derive(Debug, Clone)]
pub struct VenueCertificationInput {
    pub venue: String,
    pub supports_dry_run: bool,
    pub shadow_mode_default: bool,
    pub live_requires_explicit_approval: bool,
    pub preflight_checks: Vec<String>,
    pub order_builder_cases: Vec<String>,
    pub cancel_cases: Vec<String>,
    pub readback_cases: Vec<String>,
    pub error_cases: Vec<String>,
    pub metrics_cases: Vec<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct VenueCertificationReport {
    pub venue: String,
    pub passed: bool,
    pub artifact_required: bool,
    pub coverage: Vec<VenueCertificationCheck>,
    pub failures: Vec<String>,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum VenueCertificationCheck {
    DryRun,
    ShadowDefault,
    LiveApprovalGate,
    Preflight,
    OrderBuilder,
    Cancel,
    CleanBookReadback,
    Readback,
    ErrorSemantics,
    MetricsReadiness,
}

pub fn certify_venue_adapter(input: VenueCertificationInput) -> VenueCertificationReport {
    let mut coverage = Vec::new();
    let mut failures = Vec::new();

    check_bool(
        input.supports_dry_run,
        VenueCertificationCheck::DryRun,
        "dry-run support missing",
        &mut coverage,
        &mut failures,
    );
    check_bool(
        input.shadow_mode_default,
        VenueCertificationCheck::ShadowDefault,
        "shadow mode is not default",
        &mut coverage,
        &mut failures,
    );
    check_bool(
        input.live_requires_explicit_approval,
        VenueCertificationCheck::LiveApprovalGate,
        "live mode does not require explicit approval",
        &mut coverage,
        &mut failures,
    );

    require_cases(
        &input.preflight_checks,
        &["market_intel", "clean_book", "wallet", "route_quality"],
        VenueCertificationCheck::Preflight,
        "preflight checks incomplete",
        &mut coverage,
        &mut failures,
    );
    require_cases(
        &input.order_builder_cases,
        &["full_book_update", "mid_only_update", "clear_book"],
        VenueCertificationCheck::OrderBuilder,
        "order builder cases incomplete",
        &mut coverage,
        &mut failures,
    );
    require_cases(
        &input.cancel_cases,
        &["clear_book"],
        VenueCertificationCheck::Cancel,
        "cancel semantics incomplete",
        &mut coverage,
        &mut failures,
    );
    require_cases(
        &input.readback_cases,
        &["maker_book_sequence", "active_levels", "balance_totals"],
        VenueCertificationCheck::Readback,
        "readback cases incomplete",
        &mut coverage,
        &mut failures,
    );
    if contains_case(&input.readback_cases, "active_levels")
        && contains_case(&input.cancel_cases, "clear_book")
    {
        coverage.push(VenueCertificationCheck::CleanBookReadback);
    } else {
        failures.push("clean-book readback is not proven".to_string());
    }
    require_cases(
        &input.error_cases,
        &["stale_signal", "wrong_market", "rpc_error"],
        VenueCertificationCheck::ErrorSemantics,
        "error semantics incomplete",
        &mut coverage,
        &mut failures,
    );
    require_cases(
        &input.metrics_cases,
        &["readiness", "provenance", "policy_status"],
        VenueCertificationCheck::MetricsReadiness,
        "metrics/readiness cases incomplete",
        &mut coverage,
        &mut failures,
    );

    VenueCertificationReport {
        venue: input.venue,
        passed: failures.is_empty(),
        artifact_required: true,
        coverage,
        failures,
    }
}

fn check_bool(
    ok: bool,
    check: VenueCertificationCheck,
    failure: &str,
    coverage: &mut Vec<VenueCertificationCheck>,
    failures: &mut Vec<String>,
) {
    if ok {
        coverage.push(check);
    } else {
        failures.push(failure.to_string());
    }
}

fn require_cases(
    actual: &[String],
    required: &[&str],
    check: VenueCertificationCheck,
    failure: &str,
    coverage: &mut Vec<VenueCertificationCheck>,
    failures: &mut Vec<String>,
) {
    if required.iter().all(|case| contains_case(actual, case)) {
        coverage.push(check);
    } else {
        failures.push(failure.to_string());
    }
}

fn contains_case(actual: &[String], required: &str) -> bool {
    actual.iter().any(|case| case == required)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn archer_adapter_certifies_shadow_safe_order_lifecycle() {
        let report = certify_venue_adapter(VenueCertificationInput {
            venue: "archer".to_string(),
            supports_dry_run: true,
            shadow_mode_default: true,
            live_requires_explicit_approval: true,
            preflight_checks: vec![
                "market_intel".to_string(),
                "clean_book".to_string(),
                "wallet".to_string(),
                "route_quality".to_string(),
            ],
            order_builder_cases: vec![
                "full_book_update".to_string(),
                "mid_only_update".to_string(),
                "clear_book".to_string(),
            ],
            cancel_cases: vec!["clear_book".to_string(), "shutdown_clear".to_string()],
            readback_cases: vec![
                "maker_book_sequence".to_string(),
                "active_levels".to_string(),
                "balance_totals".to_string(),
            ],
            error_cases: vec![
                "stale_signal".to_string(),
                "wrong_market".to_string(),
                "rpc_error".to_string(),
                "priority_fee_sampling_failed".to_string(),
            ],
            metrics_cases: vec![
                "readiness".to_string(),
                "provenance".to_string(),
                "policy_status".to_string(),
            ],
        });

        assert!(report.passed);
        assert_eq!(report.venue, "archer");
        assert!(report.artifact_required);
        assert!(
            report
                .coverage
                .contains(&VenueCertificationCheck::CleanBookReadback)
        );
    }

    #[test]
    fn certification_fails_without_shadow_default_or_error_semantics() {
        let report = certify_venue_adapter(VenueCertificationInput {
            venue: "archer".to_string(),
            supports_dry_run: true,
            shadow_mode_default: false,
            live_requires_explicit_approval: true,
            preflight_checks: vec!["market_intel".to_string(), "clean_book".to_string()],
            order_builder_cases: vec!["full_book_update".to_string()],
            cancel_cases: vec!["clear_book".to_string()],
            readback_cases: vec!["maker_book_sequence".to_string()],
            error_cases: vec![],
            metrics_cases: vec!["readiness".to_string()],
        });

        assert!(!report.passed);
        assert!(
            report
                .failures
                .contains(&"shadow mode is not default".to_string())
        );
        assert!(
            report
                .failures
                .iter()
                .any(|failure| failure.contains("error semantics"))
        );
    }
}
