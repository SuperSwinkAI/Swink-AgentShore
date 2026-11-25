//! JSON-RPC method names shared by desktop lifecycle hooks and sidecar dispatch.

pub const SESSION_START: &str = "session.start";
pub const SESSION_STOP: &str = "session.stop";
pub const SESSION_COMPLETED: &str = "session.completed";
pub const SESSION_DRAINING: &str = "session.draining";
pub const ESR_READY: &str = "$/esr_ready";
