//! NSProcessInfo activity assertion to suppress macOS App Nap.
//!
//! While a AgentShore session is active, the user may background the AgentShore
//! Desktop window (or another app may obscure it for several minutes). macOS
//! App Nap then throttles the Tauri main process's event loop, timer
//! callbacks, and CPU scheduling — the sidecar runs fine in a separate
//! process, but the UI itself becomes sluggish to refresh kanban/agents/cost
//! state from the IPC stream.
//!
//! Fix: hold an `NSProcessInfo activity` with `userInitiated | latencyCritical`
//! for the lifetime of the session. macOS guarantees the process is treated
//! as foreground-priority work for the duration of the assertion. Released
//! when the session ends or the app quits — pmset will confirm the
//! assertion is gone after release.
//!
//! Non-macOS platforms: every public function is a no-op. App Nap is an
//! Apple-specific feature; Windows / Linux don't have a direct analog at
//! this layer. (AgentShore's defense against system-level idle sleep / I/O
//! throttling is handled separately by the Python sidecar's IOKit
//! IOPMAssertion + SetThreadExecutionState — see desktop-gkku.)
//!
//! desktop-bzr2.

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

    // SAFETY: The retained NSObject activity token is managed by
    // NSProcessInfo and may be ended from the thread that owns the desktop
    // shutdown/session callback. Keeping it Send lets ActivityHolder release
    // the token from Tauri command/event paths without moving the underlying
    // Cocoa object into Rust-managed interior mutation.
    unsafe impl Send for ActivityToken {}

    // SAFETY: ActivityToken is an opaque retained token whose only operation
    // is handoff back to NSProcessInfo::endActivity. ActivityHolder guards it
    // with a Mutex, and the token itself has no Rust-visible mutable state.
    unsafe impl Sync for ActivityToken {}

    pub fn begin_activity(reason: &str) -> Option<ActivityToken> {
        // userInitiated already implies idleSystemSleepDisabled +
        // automaticTerminationDisabled + suddenTerminationDisabled +
        // background; OR'ing latencyCritical adds the "treat as
        // foreground" hint for App Nap suppression specifically. The
        // bead spec calls for this combo.
        let options = NSActivityOptions::UserInitiated | NSActivityOptions::LatencyCritical;
        let reason_ns = NSString::from_str(reason);
        let info = NSProcessInfo::processInfo();
        let token = info.beginActivityWithOptions_reason(options, &reason_ns);
        Some(ActivityToken { token })
    }

    pub fn end_activity(token: ActivityToken) {
        let info = NSProcessInfo::processInfo();
        // SAFETY: `token` came from beginActivityWithOptions_reason on
        // the same NSProcessInfo singleton, so endActivity:'s "correct
        // type" precondition is satisfied by construction.
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
        // On non-macOS the platform stub still returns Some() so acquire
        // succeeds vacuously — the holder records the no-op token.
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
        h.release(); // must not panic / deadlock
    }
}
