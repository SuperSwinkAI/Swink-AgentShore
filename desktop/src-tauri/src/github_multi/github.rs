use super::{GitHubMultiError, RepoAccessResult, RepoAccessStatus, RepoSlug};
use octocrab::Octocrab;
use serde::Deserialize;

#[derive(Debug, Deserialize)]
struct CurrentUserResponse {
    login: String,
}

#[derive(Debug, Deserialize)]
struct RepositoryResponse {
    permissions: Option<RepositoryPermissions>,
}

#[derive(Debug, Deserialize)]
struct RepositoryPermissions {
    admin: Option<bool>,
    maintain: Option<bool>,
    push: Option<bool>,
    triage: Option<bool>,
    pull: Option<bool>,
}

#[tracing::instrument(skip(token))]
pub async fn validate_token_login(token: &str) -> Result<String, GitHubMultiError> {
    let client = client_for_token(token)?;
    let user: CurrentUserResponse = client
        .get("/user", None::<&()>)
        .await
        .map_err(|err| GitHubMultiError::GitHub(sanitize_github_error(err)))?;
    Ok(user.login)
}

#[tracing::instrument(skip(token), fields(repo = %repo.full_name()))]
pub async fn check_repo_access(
    token: &str,
    repo: &RepoSlug,
) -> Result<RepoAccessResult, GitHubMultiError> {
    let client = client_for_token(token)?;
    let route = format!("/repos/{}/{}", repo.owner, repo.name);
    match client.get::<RepositoryResponse, _, ()>(&route, None).await {
        Ok(response) => {
            let status = map_permissions(response.permissions.as_ref());
            Ok(RepoAccessResult {
                detail: detail_for_status(&status, repo),
                status,
                repo: Some(repo.full_name()),
            })
        }
        Err(err) => {
            let detail = sanitize_github_error(err);
            let status = status_from_error_detail(&detail);
            Ok(RepoAccessResult {
                status,
                repo: Some(repo.full_name()),
                detail,
            })
        }
    }
}

fn client_for_token(token: &str) -> Result<Octocrab, GitHubMultiError> {
    Octocrab::builder()
        .personal_token(token.to_string())
        .build()
        .map_err(|err| GitHubMultiError::GitHub(sanitize_github_error(err)))
}

fn map_permissions(permissions: Option<&RepositoryPermissions>) -> RepoAccessStatus {
    let Some(permissions) = permissions else {
        return RepoAccessStatus::ReadOnly;
    };
    if permissions.admin.unwrap_or(false) {
        return RepoAccessStatus::Admin;
    }
    if permissions.maintain.unwrap_or(false) || permissions.push.unwrap_or(false) {
        return RepoAccessStatus::Write;
    }
    if permissions.triage.unwrap_or(false) || permissions.pull.unwrap_or(false) {
        return RepoAccessStatus::ReadOnly;
    }
    RepoAccessStatus::NoAccess
}

fn detail_for_status(status: &RepoAccessStatus, repo: &RepoSlug) -> String {
    match status {
        RepoAccessStatus::Admin => format!("token has admin access to {}", repo.full_name()),
        RepoAccessStatus::Write => format!("token has write access to {}", repo.full_name()),
        RepoAccessStatus::ReadOnly => {
            format!("token has read-only access to {}", repo.full_name())
        }
        RepoAccessStatus::NoAccess => format!("token does not have access to {}", repo.full_name()),
        RepoAccessStatus::Error => {
            format!("could not determine access to {}", repo.full_name())
        }
    }
}

fn status_from_error_detail(detail: &str) -> RepoAccessStatus {
    let lower = detail.to_ascii_lowercase();
    if lower.contains("rate limit") || lower.contains("timeout") {
        return RepoAccessStatus::Error;
    }
    if lower.contains("404")
        || lower.contains("not found")
        || lower.contains("401")
        || lower.contains("unauthorized")
        || lower.contains("403")
        || lower.contains("forbidden")
    {
        return RepoAccessStatus::NoAccess;
    }
    RepoAccessStatus::Error
}

fn sanitize_github_error(err: octocrab::Error) -> String {
    let message = err.to_string();
    sanitize_error_message(&message)
}

fn sanitize_error_message(message: &str) -> String {
    let mut redacted = Vec::new();
    for word in message.split_whitespace() {
        if word.starts_with("ghp_")
            || word.starts_with("github_pat_")
            || word.starts_with("gho_")
            || word.starts_with("ghu_")
            || word.starts_with("ghs_")
            || word.starts_with("ghr_")
        {
            redacted.push("<redacted>");
        } else {
            redacted.push(word);
        }
    }
    redacted.join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_admin_before_write() {
        let permissions = RepositoryPermissions {
            admin: Some(true),
            maintain: Some(false),
            push: Some(true),
            triage: Some(true),
            pull: Some(true),
        };
        assert_eq!(map_permissions(Some(&permissions)), RepoAccessStatus::Admin);
    }

    #[test]
    fn maps_push_to_write() {
        let permissions = RepositoryPermissions {
            admin: Some(false),
            maintain: Some(false),
            push: Some(true),
            triage: Some(false),
            pull: Some(true),
        };
        assert_eq!(map_permissions(Some(&permissions)), RepoAccessStatus::Write);
    }

    #[test]
    fn maps_public_read_to_read_only_not_write() {
        let permissions = RepositoryPermissions {
            admin: Some(false),
            maintain: Some(false),
            push: Some(false),
            triage: Some(false),
            pull: Some(true),
        };
        assert_eq!(
            map_permissions(Some(&permissions)),
            RepoAccessStatus::ReadOnly
        );
    }

    #[test]
    fn maps_not_found_to_no_access() {
        assert_eq!(
            status_from_error_detail("GitHub said 404 Not Found"),
            RepoAccessStatus::NoAccess
        );
    }

    #[test]
    fn redacts_token_like_error_words() {
        let redacted = sanitize_error_message("bad token ghp_secret and github_pat_secret");
        assert!(!redacted.contains("ghp_secret"));
        assert!(!redacted.contains("github_pat_secret"));
        assert_eq!(redacted.matches("<redacted>").count(), 2);
    }
}
