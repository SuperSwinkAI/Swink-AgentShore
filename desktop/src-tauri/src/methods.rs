//! Canonical JSON-RPC method-name strings shared between the desktop shell's
//! `jsonrpc_call` side-effect hook (`lib.rs`) and the sidecar's timeout
//! classification and notification dispatch (`sidecar.rs`). A single source
//! of truth here means the two independent `match`/`if` sites can't drift
//! apart via a typo in one of the raw string literals.

pub const SESSION_START: &str = "session.start";
pub const SESSION_STOP: &str = "session.stop";
pub const SESSION_COMPLETED: &str = "session.completed";
pub const SESSION_DRAINING: &str = "session.draining";
pub const ESR_READY: &str = "$/esr_ready";
