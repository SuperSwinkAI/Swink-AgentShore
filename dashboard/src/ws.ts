import type { AgentShoreMessage } from "./types";
import { isPlainObject } from "./guards";
import { dashboardLogger, errorContext } from "./logger";

export type ConnectionState = "connecting" | "open" | "closed" | "reconnecting";

const KNOWN_MESSAGE_TYPES = new Set<string>([
  "state_update",
  "play_event",
  "agent_changed",
  "feedback_requested",
  "session_paused",
  "session_draining",
  "session_ended",
  "connection_lost",
  "connection_restored",
  "active_play_replay",
  "event_history_replay",
  "auth_token",
  "read_only",
  "error",
  "bootstrap_phase",
]);

function isAgentShoreMessage(v: unknown): v is AgentShoreMessage {
  if (!isPlainObject(v)) return false;
  const t = v.type;
  return typeof t === "string" && KNOWN_MESSAGE_TYPES.has(t);
}

export function normalizeAgentShoreWireMessage(
  raw: string,
): Record<string, unknown> | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    dashboardLogger.warn("ws", "malformed broadcast frame, dropping", {
      ...errorContext(err),
    });
    return null;
  }

  if (!isPlainObject(parsed) || typeof parsed.type !== "string") return null;

  // Unwrap the documented envelope: {type, id, timestamp, payload: {...}}
  // into the flat AgentShoreMessage shape the rest of the app expects.
  // If there is no payload object (e.g. bridge-generated synthetic messages),
  // treat the whole object as already flat and pass it through unchanged.
  if (!isPlainObject(parsed.payload)) return parsed;

  return {
    ...parsed.payload,
    type: parsed.type,
    id: parsed.id ?? "",
    timestamp: parsed.timestamp ?? "",
    ...(typeof parsed.seq === "number" ? { seq: parsed.seq } : {}),
  };
}

export interface DashboardTransport {
  state: ConnectionState;
  token: string | null;
  readOnly: boolean;
  onMessage: ((msg: AgentShoreMessage) => void) | null;
  onStateChange: ((state: ConnectionState) => void) | null;
  onReadOnlyChange: ((readOnly: boolean) => void) | null;
  connect(): void;
  send(command: Record<string, unknown>): void;
  disconnect(): void;
}

const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 16000, 30000];

export class WebSocketClient implements DashboardTransport {
  private ws: WebSocket | null = null;
  private url: string;
  private reconnectIndex = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  state: ConnectionState = "closed";
  token: string | null = null;
  readOnly = true;
  onMessage: ((msg: AgentShoreMessage) => void) | null = null;
  onStateChange: ((state: ConnectionState) => void) | null = null;
  onReadOnlyChange: ((readOnly: boolean) => void) | null = null;

  constructor(url: string) {
    this.url = url;
  }

  connect(): void {
    this.setState("connecting");
    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      this.reconnectIndex = 0;
      this.setState("open");
    };

    this.ws.onmessage = (event: MessageEvent) => {
      if (typeof event.data !== "string") return;
      const flat = normalizeAgentShoreWireMessage(event.data);
      if (flat === null) return;
      if (!isAgentShoreMessage(flat)) {
        dashboardLogger.warn("ws", "unknown message type, dropping", {
          type: flat.type,
        });
        return;
      }
      // Auth handshake: the server sends auth_token + read_only as part of
      // the connection handshake. Capture them on the transport before
      // forwarding so downstream send()'s already carry the token, and the
      // UI's read-only chrome reflects the right mode.
      if (flat.type === "auth_token") {
        const token = flat.token;
        if (typeof token === "string") {
          this.token = token;
        }
      } else if (flat.type === "read_only") {
        if (!this.readOnly) {
          this.readOnly = true;
          this.onReadOnlyChange?.(true);
        }
      }
      this.onMessage?.(flat);
    };

    this.ws.onclose = (event: CloseEvent) => {
      // Connection close visibility — Tauri-origin policy blocks surface as
      // 1006 with no reason and were silently invisible before. Route through
      // dashboardLogger.error so it bypasses the DEV/debug gate (errors are
      // always surfaced) and stays consistent with the rest of the dashboard.
      dashboardLogger.error("ws", "connection closed", {
        url: this.url,
        code: event.code,
        reason: event.reason,
        wasClean: event.wasClean,
      });
      this.ws = null;
      // Clear stale auth state so a reconnect starts fresh — otherwise
      // a previous-connection token leaks into the new session and the
      // server's read_only/auth_token handshake silently fails.
      if (this.token !== null) {
        this.token = null;
      }
      if (!this.readOnly) {
        this.readOnly = true;
        this.onReadOnlyChange?.(true);
      }
      this.scheduleReconnect();
    };

    this.ws.onerror = () => {
      dashboardLogger.error("ws", "connection error — likely refused or blocked", {
        url: this.url,
        readyState: this.ws?.readyState,
      });
      this.ws?.close();
    };
  }

  send(command: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      const payload = this.token ? { ...command, token: this.token } : command;
      this.ws.send(JSON.stringify(payload));
      return;
    }
    // Surface dropped commands. The previous silent-drop hid e.g.
    // FeedbackModal button submissions during reconnect — the user
    // clicked, nothing happened, and there was no console trace.
    dashboardLogger.warn("ws", "dropping command — socket not open", {
      readyState: this.ws?.readyState ?? "no socket",
      command_type: command.type,
    });
  }

  disconnect(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
    this.setState("closed");
  }

  private scheduleReconnect(): void {
    this.setState("reconnecting");
    const delay =
      RECONNECT_DELAYS[
        Math.min(this.reconnectIndex, RECONNECT_DELAYS.length - 1)
      ];
    this.reconnectIndex++;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private setState(state: ConnectionState): void {
    this.state = state;
    this.onStateChange?.(state);
  }
}
