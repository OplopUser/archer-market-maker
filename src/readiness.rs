#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArcherReadinessSnapshot {
    pub venue: String,
    pub market: String,
    pub mode: ArcherRunMode,
    pub source_branch: String,
    pub source_commit: String,
    pub config_checksum: String,
    pub policy_version: String,
    pub market_intel_status: String,
    pub target_bid_levels: u64,
    pub target_ask_levels: u64,
    pub active_bid_levels: u64,
    pub active_ask_levels: u64,
    pub certification_passed: bool,
    pub routing_proof_passed: bool,
    pub live_approval: bool,
}

impl ArcherReadinessSnapshot {
    pub fn shadow_fixture() -> Self {
        Self {
            venue: "archer".to_string(),
            market: "SOL/USDC".to_string(),
            mode: ArcherRunMode::Shadow,
            source_branch: "codex/archer-certification".to_string(),
            source_commit: "abc1234".to_string(),
            config_checksum: "sha256:test-config".to_string(),
            policy_version: "propamm.quote-policy.v1".to_string(),
            market_intel_status: "accepted".to_string(),
            target_bid_levels: 2,
            target_ask_levels: 2,
            active_bid_levels: 0,
            active_ask_levels: 0,
            certification_passed: true,
            routing_proof_passed: true,
            live_approval: false,
        }
    }
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum ArcherRunMode {
    Stopped,
    Shadow,
    Live,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReadinessEvaluation {
    pub status: ReadinessStatus,
    pub reasons: Vec<String>,
    pub provenance: ArcherProvenance,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArcherProvenance {
    pub source_branch: String,
    pub source_commit: String,
    pub config_checksum: String,
    pub policy_version: String,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum ReadinessStatus {
    ReadyForShadow,
    ReadyForLive,
    Blocked,
}

pub fn evaluate_archer_readiness(snapshot: &ArcherReadinessSnapshot) -> ReadinessEvaluation {
    let mut reasons = Vec::new();
    if snapshot.venue != "archer" {
        reasons.push("venue is not archer".to_string());
    }
    if snapshot.source_commit.trim().is_empty() {
        reasons.push("source commit missing".to_string());
    }
    if !snapshot.config_checksum.starts_with("sha256:") {
        reasons.push("config checksum missing".to_string());
    }
    if snapshot.policy_version != "propamm.quote-policy.v1" {
        reasons.push("unsupported policy version".to_string());
    }
    if snapshot.market_intel_status != "accepted" {
        reasons.push("market-intel signal not accepted".to_string());
    }
    if !snapshot.certification_passed {
        reasons.push("venue certification missing".to_string());
    }
    if !snapshot.routing_proof_passed {
        reasons.push("cross-venue routing proof missing".to_string());
    }
    if snapshot.mode == ArcherRunMode::Live && !snapshot.live_approval {
        reasons.push("live approval missing".to_string());
    }

    let status = if !reasons.is_empty() {
        ReadinessStatus::Blocked
    } else if snapshot.mode == ArcherRunMode::Live {
        ReadinessStatus::ReadyForLive
    } else {
        ReadinessStatus::ReadyForShadow
    };

    ReadinessEvaluation {
        status,
        reasons,
        provenance: ArcherProvenance {
            source_branch: snapshot.source_branch.clone(),
            source_commit: snapshot.source_commit.clone(),
            config_checksum: snapshot.config_checksum.clone(),
            policy_version: snapshot.policy_version.clone(),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readiness_requires_shadow_safe_provenance_and_certification() {
        let snapshot = ArcherReadinessSnapshot {
            venue: "archer".to_string(),
            market: "SOL/USDC".to_string(),
            mode: ArcherRunMode::Shadow,
            source_branch: "codex/archer-certification".to_string(),
            source_commit: "abc1234".to_string(),
            config_checksum: "sha256:test-config".to_string(),
            policy_version: "propamm.quote-policy.v1".to_string(),
            market_intel_status: "accepted".to_string(),
            target_bid_levels: 2,
            target_ask_levels: 2,
            active_bid_levels: 0,
            active_ask_levels: 0,
            certification_passed: true,
            routing_proof_passed: true,
            live_approval: false,
        };

        let readiness = evaluate_archer_readiness(&snapshot);

        assert_eq!(readiness.status, ReadinessStatus::ReadyForShadow);
        assert!(readiness.reasons.is_empty());
        assert_eq!(readiness.provenance.source_commit, "abc1234");
    }

    #[test]
    fn live_mode_is_blocked_without_approval_and_routing_proof() {
        let mut snapshot = ArcherReadinessSnapshot::shadow_fixture();
        snapshot.mode = ArcherRunMode::Live;
        snapshot.routing_proof_passed = false;

        let readiness = evaluate_archer_readiness(&snapshot);

        assert_eq!(readiness.status, ReadinessStatus::Blocked);
        assert!(
            readiness
                .reasons
                .contains(&"live approval missing".to_string())
        );
        assert!(
            readiness
                .reasons
                .contains(&"cross-venue routing proof missing".to_string())
        );
    }
}
