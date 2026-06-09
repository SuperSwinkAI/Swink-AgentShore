//! Windows desktop GitHub identity helper support.
//!
//! This module backs the `agentshore-github-helper` sidecar utility used by the
//! Python desktop sidecar on Windows. The protocol is JSON over stdin/stdout so
//! tokens never need to travel through process arguments.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use thiserror::Error;

mod credential;
mod github;

pub use github::validate_token_login;

#[derive(Clone, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum HelperRequest {
    CredentialStatus {
        service: String,
    },
    CredentialGet {
        service: String,
    },
    CredentialSet {
        service: String,
        token: String,
    },
    CredentialDelete {
        service: String,
    },
    ValidateToken {
        token: String,
    },
    CheckRepoAccess {
        token: String,
        local_repo_path: Option<PathBuf>,
        remote: Option<String>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CredentialStatus {
    pub service: String,
    pub has_token: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CredentialValue {
    pub service: String,
    pub token: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct TokenValidation {
    pub login: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RepoSlug {
    pub owner: String,
    pub name: String,
}

impl RepoSlug {
    pub fn full_name(&self) -> String {
        format!("{}/{}", self.owner, self.name)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RepoAccessStatus {
    Admin,
    Write,
    ReadOnly,
    NoAccess,
    Error,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RepoAccessResult {
    pub status: RepoAccessStatus,
    pub repo: Option<String>,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HelperErrorBody {
    pub kind: String,
    pub message: String,
}

#[derive(Debug, Error)]
pub enum GitHubMultiError {
    #[error("unsupported platform: {0}")]
    UnsupportedPlatform(&'static str),
    #[error("invalid request: {0}")]
    InvalidRequest(String),
    #[error("credential store error: {0}")]
    Credential(String),
    #[error("repository remote error: {0}")]
    Remote(String),
    #[error("GitHub API error: {0}")]
    GitHub(String),
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),
}

impl GitHubMultiError {
    pub fn body(&self) -> HelperErrorBody {
        HelperErrorBody {
            kind: match self {
                GitHubMultiError::UnsupportedPlatform(_) => "unsupported_platform",
                GitHubMultiError::InvalidRequest(_) => "invalid_request",
                GitHubMultiError::Credential(_) => "credential",
                GitHubMultiError::Remote(_) => "remote",
                GitHubMultiError::GitHub(_) => "github",
                GitHubMultiError::Io(_) => "io",
                GitHubMultiError::Json(_) => "json",
            }
            .to_string(),
            message: self.to_string(),
        }
    }
}

pub async fn handle_request(request: HelperRequest) -> Result<serde_json::Value, GitHubMultiError> {
    match request {
        HelperRequest::CredentialStatus { service } => {
            validate_service(&service)?;
            let token = credential::get_token(&service)?;
            Ok(serde_json::to_value(CredentialStatus {
                service,
                has_token: token
                    .as_deref()
                    .is_some_and(|value| !value.trim().is_empty()),
            })?)
        }
        HelperRequest::CredentialGet { service } => {
            validate_service(&service)?;
            Ok(serde_json::to_value(CredentialValue {
                token: credential::get_token(&service)?,
                service,
            })?)
        }
        HelperRequest::CredentialSet { service, token } => {
            validate_service(&service)?;
            validate_token(&token)?;
            credential::set_token(&service, &token)?;
            Ok(serde_json::json!({ "service": service, "stored": true }))
        }
        HelperRequest::CredentialDelete { service } => {
            validate_service(&service)?;
            let deleted = credential::delete_token(&service)?;
            Ok(serde_json::json!({ "service": service, "deleted": deleted }))
        }
        HelperRequest::ValidateToken { token } => {
            validate_token(&token)?;
            let login = github::validate_token_login(&token).await?;
            Ok(serde_json::to_value(TokenValidation { login })?)
        }
        HelperRequest::CheckRepoAccess {
            token,
            local_repo_path,
            remote,
        } => {
            validate_token(&token)?;
            let remote = match remote {
                Some(remote) if !remote.trim().is_empty() => remote,
                _ => {
                    let path = local_repo_path.ok_or_else(|| {
                        GitHubMultiError::InvalidRequest(
                            "check_repo_access requires local_repo_path or remote".to_string(),
                        )
                    })?;
                    detect_github_remote(&path)?
                }
            };
            let repo = parse_github_remote(&remote)?;
            Ok(serde_json::to_value(
                github::check_repo_access(&token, &repo).await?,
            )?)
        }
    }
}

fn validate_service(service: &str) -> Result<(), GitHubMultiError> {
    let trimmed = service.trim();
    if trimmed.is_empty() {
        return Err(GitHubMultiError::InvalidRequest(
            "credential service must not be empty".to_string(),
        ));
    }
    if trimmed
        .chars()
        .any(|ch| ch == '\0' || ch == '\r' || ch == '\n')
    {
        return Err(GitHubMultiError::InvalidRequest(
            "credential service contains invalid control characters".to_string(),
        ));
    }
    Ok(())
}

fn validate_token(token: &str) -> Result<(), GitHubMultiError> {
    if token.trim().is_empty() {
        return Err(GitHubMultiError::InvalidRequest(
            "token must not be empty".to_string(),
        ));
    }
    if token
        .chars()
        .any(|ch| ch == '\0' || ch == '\r' || ch == '\n')
    {
        return Err(GitHubMultiError::InvalidRequest(
            "token contains invalid control characters".to_string(),
        ));
    }
    Ok(())
}

pub fn detect_github_remote(local_repo_path: &Path) -> Result<String, GitHubMultiError> {
    let git_config = git_config_path(local_repo_path)?;
    let contents = std::fs::read_to_string(&git_config)?;
    parse_git_config_remote(&contents).ok_or_else(|| {
        GitHubMultiError::Remote(format!(
            "no GitHub remote URL found in {}",
            git_config.display()
        ))
    })
}

fn git_config_path(local_repo_path: &Path) -> Result<PathBuf, GitHubMultiError> {
    let dot_git = local_repo_path.join(".git");
    if dot_git.is_dir() {
        let config = dot_git.join("config");
        if config.is_file() {
            return Ok(config);
        }
    }
    if dot_git.is_file() {
        let pointer = std::fs::read_to_string(&dot_git)?;
        let Some(raw_gitdir) = pointer
            .lines()
            .find_map(|line| line.trim().strip_prefix("gitdir:").map(str::trim))
        else {
            return Err(GitHubMultiError::Remote(format!(
                "{} is not a valid gitdir pointer",
                dot_git.display()
            )));
        };
        let gitdir = if Path::new(raw_gitdir).is_absolute() {
            PathBuf::from(raw_gitdir)
        } else {
            local_repo_path.join(raw_gitdir)
        };
        let config = gitdir.join("config");
        if config.is_file() {
            return Ok(config);
        }
    }
    Err(GitHubMultiError::Remote(format!(
        "{} does not look like a Git repository",
        local_repo_path.display()
    )))
}

fn parse_git_config_remote(contents: &str) -> Option<String> {
    let mut current_remote: Option<String> = None;
    let mut first_github: Option<String> = None;
    for raw_line in contents.lines() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            current_remote = parse_remote_section(line);
            continue;
        }
        let Some(remote_name) = current_remote.as_deref() else {
            continue;
        };
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        if key.trim() != "url" {
            continue;
        }
        let remote = value.trim().trim_matches('"').to_string();
        if !is_github_remote(&remote) {
            continue;
        }
        if remote_name == "origin" {
            return Some(remote);
        }
        first_github.get_or_insert(remote);
    }
    first_github
}

fn parse_remote_section(line: &str) -> Option<String> {
    let inner = line.trim_start_matches('[').trim_end_matches(']').trim();
    let rest = inner.strip_prefix("remote")?.trim();
    let quoted = rest.strip_prefix('"')?.strip_suffix('"')?;
    Some(quoted.to_string())
}

fn is_github_remote(remote: &str) -> bool {
    remote.contains("github.com")
}

pub fn parse_github_remote(remote: &str) -> Result<RepoSlug, GitHubMultiError> {
    let trimmed = remote.trim().trim_end_matches('/');
    let without_scheme = trimmed
        .strip_prefix("https://github.com/")
        .or_else(|| trimmed.strip_prefix("http://github.com/"))
        .or_else(|| trimmed.strip_prefix("ssh://git@github.com/"))
        .or_else(|| trimmed.strip_prefix("git@github.com:"))
        .ok_or_else(|| {
            GitHubMultiError::Remote(format!("remote is not a github.com URL: {trimmed}"))
        })?;
    let mut parts = without_scheme.split('/');
    let owner = parts.next().unwrap_or_default().trim();
    let repo = parts
        .next()
        .unwrap_or_default()
        .trim()
        .trim_end_matches(".git");
    if owner.is_empty() || repo.is_empty() || parts.next().is_some() {
        return Err(GitHubMultiError::Remote(format!(
            "remote does not identify a GitHub repository: {trimmed}"
        )));
    }
    Ok(RepoSlug {
        owner: owner.to_string(),
        name: repo.to_string(),
    })
}

pub fn redact_secret(value: &str) -> String {
    if value.is_empty() {
        "<empty>".to_string()
    } else {
        "<redacted>".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn helper_request_parses_credential_status() {
        let request = serde_json::from_str::<HelperRequest>(
            r#"{"op":"credential_status","service":"agentshore/octocat"}"#,
        )
        .expect("parse request");
        assert!(matches!(request, HelperRequest::CredentialStatus { .. }));
    }

    #[test]
    fn helper_request_parses_check_repo_access_without_leaking_token_debug() {
        let raw = r#"{"op":"check_repo_access","token":"ghp_secret","remote":"git@github.com:owner/repo.git"}"#;
        let request = serde_json::from_str::<HelperRequest>(raw).expect("parse request");
        assert!(matches!(request, HelperRequest::CheckRepoAccess { .. }));
        assert_eq!(redact_secret("ghp_secret"), "<redacted>");
    }

    #[test]
    fn parses_common_github_remote_forms() {
        for (remote, owner, name) in [
            ("https://github.com/Owner/Repo.git", "Owner", "Repo"),
            ("http://github.com/Owner/Repo", "Owner", "Repo"),
            ("git@github.com:Owner/Repo.git", "Owner", "Repo"),
            ("ssh://git@github.com/Owner/Repo.git", "Owner", "Repo"),
        ] {
            let parsed = parse_github_remote(remote).expect(remote);
            assert_eq!(parsed.owner, owner);
            assert_eq!(parsed.name, name);
        }
    }

    #[test]
    fn rejects_non_github_remote() {
        let err = parse_github_remote("git@example.com:Owner/Repo.git").unwrap_err();
        assert!(matches!(err, GitHubMultiError::Remote(_)));
    }

    #[test]
    fn git_config_prefers_origin_github_remote() {
        let config = r#"
            [remote "upstream"]
                url = https://github.com/Other/Repo.git
            [remote "origin"]
                url = git@github.com:Owner/Repo.git
        "#;
        assert_eq!(
            parse_git_config_remote(config),
            Some("git@github.com:Owner/Repo.git".to_string())
        );
    }

    #[test]
    fn error_body_serializes_without_secret() {
        let err = GitHubMultiError::InvalidRequest(format!(
            "token {} is invalid",
            redact_secret("ghp_secret")
        ));
        let json = serde_json::to_string(&err.body()).expect("serialize error");
        assert!(json.contains("<redacted>"));
        assert!(!json.contains("ghp_secret"));
    }
}
