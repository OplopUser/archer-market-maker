use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use archer_market_maker::ops_security::{
    AlertDrillRequest, AuditActionInput, LiveApprovalArtifact, LiveApprovalRequest,
    RuntimeKillSwitchArtifact, append_immutable_audit_record, evaluate_runtime_kill_switch,
    fire_drill_template, render_alert_drill, scan_secret_text, validate_live_approval,
    verify_immutable_audit_log,
};
use serde_json::json;

fn temp_path(name: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    std::env::temp_dir().join(format!(
        "archer-mm-{name}-{}-{nanos}.jsonl",
        std::process::id()
    ))
}

fn approval() -> LiveApprovalArtifact {
    LiveApprovalArtifact {
        venue: "archer".to_string(),
        market: "SOL/USDC".to_string(),
        operator_id: "ops-lead".to_string(),
        ticket_ids: vec!["T-162".to_string(), "T-193".to_string()],
        approved_at_ms: 1_000,
        expires_at_ms: 2_000,
        confirmation: "approve live archer SOL/USDC clear_book".to_string(),
        scope: "single guarded clear-book action".to_string(),
    }
}

fn rpc_key_url(secret: &str) -> String {
    format!(
        "https://rpc.invalid/?{}{}",
        ["api", "-key="].concat(),
        secret
    )
}

#[test]
fn live_approval_requires_matching_venue_ticket_phrase_and_unexpired_artifact() {
    let request = LiveApprovalRequest {
        venue: "archer".to_string(),
        market: "SOL/USDC".to_string(),
        operation: "clear_book".to_string(),
        now_ms: 1_500,
        required_ticket_ids: vec!["T-162".to_string()],
    };

    let approved = validate_live_approval(&approval(), &request);
    assert!(approved.approved);
    assert!(approved.reason_codes.is_empty());
    assert_eq!(approved.artifact_hash_sha256.len(), 64);

    let mut stale = approval();
    stale.expires_at_ms = 1_400;
    let rejected = validate_live_approval(&stale, &request);
    assert!(!rejected.approved);
    assert!(
        rejected
            .reason_codes
            .contains(&"approval_expired".to_string())
    );
}

#[test]
fn runtime_kill_switch_file_blocks_independently_and_retains_watchdog_proof() {
    let path = temp_path("runtime-kill-switch");
    fs::write(
        &path,
        serde_json::to_string(&RuntimeKillSwitchArtifact {
            reason: "operator_fire_drill".to_string(),
            operator_id: "ops-lead".to_string(),
            ticket_id: "T-164".to_string(),
            created_at_ms: 1_700,
            watchdog_evidence: Some(
                "watchdog observed kill file and would halt Archer".to_string(),
            ),
        })
        .expect("artifact serializes"),
    )
    .expect("kill switch file writes");

    let proof = evaluate_runtime_kill_switch(&path).expect("kill switch evaluates");
    assert!(proof.active);
    assert!(proof.blocks_live_mode);
    assert_eq!(proof.reason_codes, vec!["operator_fire_drill"]);
    assert!(proof.watchdog_evidence.unwrap().contains("halt Archer"));

    fs::remove_file(path).ok();
}

#[test]
fn immutable_audit_log_chains_hashes_and_redacts_secret_like_details() {
    let path = temp_path("immutable-audit");
    let first = append_immutable_audit_record(
        &path,
        AuditActionInput {
            timestamp_ms: 2_000,
            actor: "ops-lead".to_string(),
            action: "deploy_approval_checked".to_string(),
            target: "archer:SOL/USDC".to_string(),
            ticket_id: "T-194".to_string(),
            details: json!({"rpc": rpc_key_url("SHOULD_NOT_LEAK")}),
        },
    )
    .expect("first record appends");
    let second = append_immutable_audit_record(
        &path,
        AuditActionInput {
            timestamp_ms: 2_100,
            actor: "ops-lead".to_string(),
            action: "kill_switch_checked".to_string(),
            target: "archer:SOL/USDC".to_string(),
            ticket_id: "T-164".to_string(),
            details: json!({"path": "/tmp/archer.kill"}),
        },
    )
    .expect("second record appends");

    assert_eq!(second.previous_hash, first.record_hash);
    let raw = fs::read_to_string(&path).expect("audit log reads");
    assert!(!raw.contains("SHOULD_NOT_LEAK"));
    assert!(raw.contains("[REDACTED_SECRET]"));
    assert!(verify_immutable_audit_log(&path).expect("chain verifies"));

    fs::remove_file(path).ok();
}

#[test]
fn alert_and_fire_drills_are_local_dry_run_artifacts_only() {
    let alert = render_alert_drill(AlertDrillRequest {
        venue: "archer".to_string(),
        drill_id: "T-192-alert-drill".to_string(),
        channel: "pagerduty".to_string(),
        escalation_target: "ops-primary".to_string(),
        message: "simulated clear-book halt".to_string(),
    });
    assert!(alert.dry_run);
    assert_eq!(alert.send_command, None);
    assert!(
        alert
            .retained_artifacts
            .contains(&"alert-drill.json".to_string())
    );

    let drill = fire_drill_template("archer", "live approval denied");
    assert!(!drill.run_live_drill);
    assert!(
        drill
            .retained_artifacts
            .contains(&"immutable-audit-log.jsonl".to_string())
    );
}

#[test]
fn secret_text_scan_flags_rpc_keys_private_keys_and_env_material() {
    let findings = scan_secret_text(
        "config/default.toml",
        &format!(
            "rpc = '{}abc'\n{}\n{}local\n",
            rpc_key_url(""),
            ["PRIVATE", " KEY"].concat(),
            [".", "env"].concat()
        ),
    );
    let codes: Vec<String> = findings
        .iter()
        .map(|finding| finding.reason_code.clone())
        .collect();
    assert!(codes.contains(&"rpc_api_key_literal".to_string()));
    assert!(codes.contains(&"private_key_material".to_string()));
    assert!(codes.contains(&"env_material_reference".to_string()));
}
