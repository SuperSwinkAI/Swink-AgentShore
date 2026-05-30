use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReadinessFindingKind {
    IsAgentShoreSourceRepo,
    NotAGitRepository,
    GithubIdentityMissing,
    BeadsNotInitialized,
    ToolingUnavailable,
    Other,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ReadinessFinding {
    pub kind: ReadinessFindingKind,
    pub message: String,
}

impl ReadinessFinding {
    pub fn new(kind: ReadinessFindingKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }
}

pub fn is_hard_blocker(kind: ReadinessFindingKind) -> bool {
    matches!(
        kind,
        ReadinessFindingKind::IsAgentShoreSourceRepo | ReadinessFindingKind::NotAGitRepository
    )
}

pub fn readiness_hard_blocked(findings: &[ReadinessFinding]) -> bool {
    findings.iter().any(|f| is_hard_blocker(f.kind))
}

#[cfg(test)]
mod tests {
    use super::{readiness_hard_blocked, ReadinessFinding, ReadinessFindingKind};

    #[test]
    fn hard_blocks_when_target_is_agentshore_source_repo() {
        let findings = vec![ReadinessFinding::new(
            ReadinessFindingKind::IsAgentShoreSourceRepo,
            "target path points at the AgentShore source repo",
        )];
        assert!(readiness_hard_blocked(&findings));
    }

    #[test]
    fn hard_blocks_when_target_is_not_git_repo() {
        let findings = vec![ReadinessFinding::new(
            ReadinessFindingKind::NotAGitRepository,
            "target path is not a Git repository",
        )];
        assert!(readiness_hard_blocked(&findings));
    }

    #[test]
    fn does_not_hard_block_for_informational_findings() {
        let findings = vec![
            ReadinessFinding::new(
                ReadinessFindingKind::GithubIdentityMissing,
                "no authenticated GitHub identity",
            ),
            ReadinessFinding::new(
                ReadinessFindingKind::BeadsNotInitialized,
                "beads graph not initialized yet",
            ),
            ReadinessFinding::new(
                ReadinessFindingKind::ToolingUnavailable,
                "codex cli unavailable",
            ),
        ];
        assert!(!readiness_hard_blocked(&findings));
    }

    #[test]
    fn does_not_hard_block_for_other_findings() {
        // `Other` is the catch-all variant for surfaces the sidecar might
        // surface in future (e.g. workspace dirty, detached HEAD). It is
        // informational only — the user can still proceed.
        let findings = vec![ReadinessFinding::new(
            ReadinessFindingKind::Other,
            "uncategorized readiness signal",
        )];
        assert!(!readiness_hard_blocked(&findings));
    }

    #[test]
    fn hard_block_still_fires_when_other_finding_is_present_alongside_blocker() {
        // Mixing `Other` with a hard-blocker variant must not mask the block.
        let findings = vec![
            ReadinessFinding::new(ReadinessFindingKind::Other, "noise"),
            ReadinessFinding::new(
                ReadinessFindingKind::IsAgentShoreSourceRepo,
                "target path points at the AgentShore source repo",
            ),
        ];
        assert!(readiness_hard_blocked(&findings));
    }
}
