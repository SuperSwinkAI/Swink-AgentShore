//! Suppress macOS App Nap with an NSProcessInfo activity assertion.
//!
//! Hold userInitiated | latencyCritical while a session is active; release
//! when the session ends or the app quits. Public functions are no-ops on
//! other platforms. System sleep prevention is owned by the Python sidecar.

use std::sync::Mutex;

/// Process-global activity-assertion holder. Stored as an opaque trait
/// object so the platform-specific module can park its native token here
/// without leaking macOS types up the include tree.
#[derive(Default)]
pub struct ActivityHolder {
    inner: Mutex<Option<ActivityToken>>,
}

impl ActivityHolder {
    pub fn new() -> Self {
        Self::default()
    }

    /// Acquire the activity if not already held. Idempotent: a second
    /// call while an assertion is live is a no-op (and reports `false`
    /// to signal "no new assertion taken").
    pub fn acquire(&self, reason: &str) -> bool {
        let mut guard = match self.inner.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        if guard.is_some() {
            return false;
        }
        if let Some(token) = platform::begin_activity(reason) {
            *guard = Some(token);
            true
        } else {
            false
        }
    }

    /// Whether an activity assertion is currently held.
    ///
    /// The assertion is acquired on `session.start` and released on
    /// `session.stop` / `session.completed`, so this doubles as the shell's
    /// "is a AgentShore session running?" signal — used to gate the
    /// quit-while-running confirmation prompt.
    pub fn is_active(&self) -> bool {
        let guard = match self.inner.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        guard.is_some()
    }

    /// Release the activity if held. Idempotent: a release on an empty
    /// holder is a safe no-op.
    pub fn release(&self) {
        let mut guard = match self.inner.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        if let Some(token) = guard.take() {
            platform::end_activity(token);
        }
    }
}

#[cfg(target_os = "macos")]
pub use platform_macos::ActivityToken;

#[cfg(not(target_os = "macos"))]
pub use platform_other::ActivityToken;

#[cfg(target_os = "macos")]
mod platform {
    pub use super::platform_macos::{begin_activity, end_activity};
}

#[cfg(not(target_os = "macos"))]
mod platform {
    pub use super::platform_other::{begin_activity, end_activity};
}

#[cfg(target_os = "macos")]
mod platform_macos {
    use objc2::rc::Retained;
    use objc2::runtime::{NSObjectProtocol, ProtocolObject};
    use objc2_foundation::{NSActivityOptions, NSProcessInfo, NSString};

    /// Opaque handle returned by `[[NSProcessInfo processInfo]
    /// beginActivityWithOptions:reason:]`. Must be passed back to
    /// `endActivity:` to release the OS-side reference.
    pub struct ActivityToken {
        token: Retained<ProtocolObject<dyn NSObjectProtocol>>,
    }

    unsafe impl Send for ActivityToken {}

    unsafe impl Sync for ActivityToken {}

    pub fn begin_activity(reason: &str) -> Option<ActivityToken> {
        let options = NSActivityOptions::UserInitiated | NSActivityOptions::LatencyCritical;
        let reason_ns = NSString::from_str(reason);
        let info = NSProcessInfo::processInfo();
        let token = info.beginActivityWithOptions_reason(options, &reason_ns);
        Some(ActivityToken { token })
    }

    pub fn end_activity(token: ActivityToken) {
        let info = NSProcessInfo::processInfo();
        unsafe {
            info.endActivity(&token.token);
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod platform_other {
    /// No-op placeholder on non-macOS platforms. App Nap has no
    /// equivalent on Windows / Linux at this granularity — those
    /// platforms have their own background-throttling models that don't
    /// affect Tauri main-process timing in the same way.
    pub struct ActivityToken;

    pub fn begin_activity(_reason: &str) -> Option<ActivityToken> {
        Some(ActivityToken)
    }

    pub fn end_activity(_token: ActivityToken) {}
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn acquire_then_release_clears_holder() {
        let h = ActivityHolder::new();
        let acquired = h.acquire("test-reason");
        assert!(acquired);
        h.release();
    }

    #[test]
    fn double_acquire_is_idempotent() {
        let h = ActivityHolder::new();
        assert!(h.acquire("first"));
        assert!(!h.acquire("second"));
        h.release();
    }

    #[test]
    fn is_active_tracks_acquire_and_release() {
        let h = ActivityHolder::new();
        assert!(!h.is_active());
        h.acquire("session");
        assert!(h.is_active());
        h.release();
        assert!(!h.is_active());
    }

    #[test]
    fn release_without_acquire_is_safe() {
        let h = ActivityHolder::new();
        h.release();
    }
}
