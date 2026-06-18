use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;

const RPC_API_KEY_PATTERN: &str = concat!("api", "-key=");
const RPC_APIKEY_PATTERN: &str = concat!("api", "key=");
const ENV_FILE_MARKER: &str = concat!(".", "env");
const HOME_PATH_MARKERS: [&str; 2] = [concat!("/", "users/"), concat!("/", "home/")];

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct LiveApprovalArtifact {
    pub venue: String,
    pub market: String,
    pub operator_id: String,
    pub ticket_ids: Vec<String>,
    pub approved_at_ms: u64,
    pub expires_at_ms: u64,
    pub confirmation: String,
    pub scope: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LiveApprovalRequest {
    pub venue: String,
    pub market: String,
    pub operation: String,
    pub now_ms: u64,
    pub required_ticket_ids: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct LiveApprovalDecision {
    pub approved: bool,
    pub reason_codes: Vec<String>,
    pub artifact_hash_sha256: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeKillSwitchArtifact {
    pub reason: String,
    pub operator_id: String,
    pub ticket_id: String,
    pub created_at_ms: u64,
    pub watchdog_evidence: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeKillSwitchProof {
    pub active: bool,
    pub blocks_live_mode: bool,
    pub reason_codes: Vec<String>,
    pub source_path: String,
    pub watchdog_evidence: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AuditActionInput {
    pub timestamp_ms: u64,
    pub actor: String,
    pub action: String,
    pub target: String,
    pub ticket_id: String,
    pub details: Value,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ImmutableAuditRecord {
    pub timestamp_ms: u64,
    pub actor: String,
    pub action: String,
    pub target: String,
    pub ticket_id: String,
    pub details: Value,
    pub previous_hash: String,
    pub record_hash: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlertDrillRequest {
    pub venue: String,
    pub drill_id: String,
    pub channel: String,
    pub escalation_target: String,
    pub message: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AlertDrillArtifact {
    pub venue: String,
    pub drill_id: String,
    pub channel: String,
    pub escalation_target: String,
    pub dry_run: bool,
    pub send_command: Option<String>,
    pub message: String,
    pub retained_artifacts: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct FireDrillArtifact {
    pub venue: String,
    pub scenario: String,
    pub run_live_drill: bool,
    pub required_steps: Vec<String>,
    pub retained_artifacts: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SecretScanFinding {
    pub path: String,
    pub line: usize,
    pub reason_code: String,
}

pub fn validate_live_approval(
    artifact: &LiveApprovalArtifact,
    request: &LiveApprovalRequest,
) -> LiveApprovalDecision {
    let mut reason_codes = Vec::new();
    if artifact.venue != request.venue {
        reason_codes.push("approval_venue_mismatch".to_string());
    }
    if artifact.market != request.market {
        reason_codes.push("approval_market_mismatch".to_string());
    }
    if artifact.expires_at_ms <= request.now_ms {
        reason_codes.push("approval_expired".to_string());
    }
    for ticket in &request.required_ticket_ids {
        if !artifact.ticket_ids.contains(ticket) {
            reason_codes.push(format!("approval_missing_ticket:{ticket}"));
        }
    }
    let expected = format!(
        "approve live {} {} {}",
        request.venue, request.market, request.operation
    );
    if artifact.confirmation != expected {
        reason_codes.push("approval_confirmation_mismatch".to_string());
    }
    LiveApprovalDecision {
        approved: reason_codes.is_empty(),
        reason_codes,
        artifact_hash_sha256: sha256_json(artifact),
    }
}

pub fn read_and_validate_live_approval(
    path: impl AsRef<Path>,
    request: &LiveApprovalRequest,
) -> Result<LiveApprovalDecision> {
    let artifact: LiveApprovalArtifact = serde_json::from_str(
        &fs::read_to_string(path.as_ref())
            .with_context(|| format!("read live approval artifact {}", path.as_ref().display()))?,
    )
    .context("parse live approval artifact")?;
    Ok(validate_live_approval(&artifact, request))
}

pub fn evaluate_runtime_kill_switch(path: impl AsRef<Path>) -> Result<RuntimeKillSwitchProof> {
    let path = path.as_ref();
    if !path.exists() {
        return Ok(RuntimeKillSwitchProof {
            active: false,
            blocks_live_mode: false,
            reason_codes: Vec::new(),
            source_path: path.display().to_string(),
            watchdog_evidence: None,
        });
    }
    let artifact: RuntimeKillSwitchArtifact = serde_json::from_str(
        &fs::read_to_string(path)
            .with_context(|| format!("read runtime kill switch {}", path.display()))?,
    )
    .context("parse runtime kill switch artifact")?;
    Ok(RuntimeKillSwitchProof {
        active: true,
        blocks_live_mode: true,
        reason_codes: vec![artifact.reason],
        source_path: path.display().to_string(),
        watchdog_evidence: artifact.watchdog_evidence,
    })
}

pub fn append_immutable_audit_record(
    path: impl AsRef<Path>,
    input: AuditActionInput,
) -> Result<ImmutableAuditRecord> {
    let path = path.as_ref();
    let previous_hash = last_record_hash(path)?.unwrap_or_else(|| "GENESIS".to_string());
    let sanitized_details = redact_json_value(input.details);
    let record_hash = audit_record_hash(
        input.timestamp_ms,
        &input.actor,
        &input.action,
        &input.target,
        &input.ticket_id,
        &sanitized_details,
        &previous_hash,
    );
    let record = ImmutableAuditRecord {
        timestamp_ms: input.timestamp_ms,
        actor: input.actor,
        action: input.action,
        target: input.target,
        ticket_id: input.ticket_id,
        details: sanitized_details,
        previous_hash,
        record_hash,
    };
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("create audit parent {}", parent.display()))?;
    }
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .with_context(|| format!("open immutable audit log {}", path.display()))?;
    writeln!(file, "{}", serde_json::to_string(&record)?)
        .with_context(|| format!("append immutable audit log {}", path.display()))?;
    Ok(record)
}

pub fn verify_immutable_audit_log(path: impl AsRef<Path>) -> Result<bool> {
    let mut previous = "GENESIS".to_string();
    let contents = fs::read_to_string(path.as_ref())
        .with_context(|| format!("read immutable audit log {}", path.as_ref().display()))?;
    for line in contents.lines().filter(|line| !line.trim().is_empty()) {
        let record: ImmutableAuditRecord =
            serde_json::from_str(line).context("parse immutable audit record")?;
        if record.previous_hash != previous {
            return Ok(false);
        }
        let expected = audit_record_hash(
            record.timestamp_ms,
            &record.actor,
            &record.action,
            &record.target,
            &record.ticket_id,
            &record.details,
            &record.previous_hash,
        );
        if record.record_hash != expected {
            return Ok(false);
        }
        previous = record.record_hash;
    }
    Ok(true)
}

pub fn render_alert_drill(request: AlertDrillRequest) -> AlertDrillArtifact {
    AlertDrillArtifact {
        venue: request.venue,
        drill_id: request.drill_id,
        channel: request.channel,
        escalation_target: request.escalation_target,
        dry_run: true,
        send_command: None,
        message: request.message,
        retained_artifacts: vec![
            "alert-drill.json".to_string(),
            "escalation-proof.md".to_string(),
            "immutable-audit-log.jsonl".to_string(),
        ],
    }
}

pub fn fire_drill_template(
    venue: impl Into<String>,
    scenario: impl Into<String>,
) -> FireDrillArtifact {
    FireDrillArtifact {
        venue: venue.into(),
        scenario: scenario.into(),
        run_live_drill: false,
        required_steps: vec![
            "prepare live approval artifact but do not execute it".to_string(),
            "write local runtime kill-switch proof file".to_string(),
            "render external alert drill as dry-run artifact".to_string(),
            "append operator/deploy action to immutable audit log".to_string(),
            "retain postmortem and command transcript placeholders".to_string(),
        ],
        retained_artifacts: vec![
            "live-approval.json".to_string(),
            "runtime-kill-switch.json".to_string(),
            "alert-drill.json".to_string(),
            "immutable-audit-log.jsonl".to_string(),
            "postmortem.md".to_string(),
        ],
    }
}

pub fn scan_secret_text(path: impl Into<String>, contents: &str) -> Vec<SecretScanFinding> {
    let path = path.into();
    let mut findings = Vec::new();
    for (index, line) in contents.lines().enumerate() {
        let lower = line.to_ascii_lowercase();
        if lower.contains(RPC_API_KEY_PATTERN) || lower.contains(RPC_APIKEY_PATTERN) {
            findings.push(finding(&path, index, "rpc_api_key_literal"));
        }
        if line.contains(&["PRIVATE", " KEY"].concat())
            || lower.contains(&["private", "_key"].concat())
        {
            findings.push(finding(&path, index, "private_key_material"));
        }
        if lower.contains(ENV_FILE_MARKER) {
            findings.push(finding(&path, index, "env_material_reference"));
        }
        if lower.contains("keypair")
            && HOME_PATH_MARKERS
                .iter()
                .any(|marker| lower.contains(marker))
        {
            findings.push(finding(&path, index, "wallet_path_literal"));
        }
    }
    findings
}

fn finding(path: &str, index: usize, reason_code: &str) -> SecretScanFinding {
    SecretScanFinding {
        path: path.to_string(),
        line: index + 1,
        reason_code: reason_code.to_string(),
    }
}

fn last_record_hash(path: &Path) -> Result<Option<String>> {
    if !path.exists() {
        return Ok(None);
    }
    let contents = fs::read_to_string(path)
        .with_context(|| format!("read immutable audit log {}", path.display()))?;
    let Some(line) = contents.lines().rev().find(|line| !line.trim().is_empty()) else {
        return Ok(None);
    };
    let record: ImmutableAuditRecord =
        serde_json::from_str(line).context("parse previous immutable audit record")?;
    Ok(Some(record.record_hash))
}

fn audit_record_hash(
    timestamp_ms: u64,
    actor: &str,
    action: &str,
    target: &str,
    ticket_id: &str,
    details: &Value,
    previous_hash: &str,
) -> String {
    sha256_json(&json!({
        "timestamp_ms": timestamp_ms,
        "actor": actor,
        "action": action,
        "target": target,
        "ticket_id": ticket_id,
        "details": details,
        "previous_hash": previous_hash,
    }))
}

fn sha256_json(value: &impl Serialize) -> String {
    let bytes = serde_json::to_vec(value).expect("serializable value hashes");
    let digest = Sha256::digest(bytes);
    format!("{digest:x}")
}

fn redact_json_value(value: Value) -> Value {
    match value {
        Value::String(text) => {
            let lower = text.to_ascii_lowercase();
            if lower.contains(RPC_API_KEY_PATTERN)
                || lower.contains(RPC_APIKEY_PATTERN)
                || lower.contains(&["private", " key"].concat())
                || lower.contains(&["private", "_key"].concat())
            {
                Value::String("[REDACTED_SECRET]".to_string())
            } else {
                Value::String(text)
            }
        }
        Value::Array(values) => Value::Array(values.into_iter().map(redact_json_value).collect()),
        Value::Object(map) => Value::Object(
            map.into_iter()
                .map(|(key, value)| (key, redact_json_value(value)))
                .collect(),
        ),
        other => other,
    }
}
