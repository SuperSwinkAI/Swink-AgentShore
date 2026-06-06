import http from "node:http";
import { WebSocketServer } from "ws";

const port = Number.parseInt(process.env.AGENTSHORE_MOCK_PORT ?? "9473", 10);
const server = http.createServer((req, res) => {
  if (req.url === "/health") {
    res.writeHead(200, { "content-type": "text/plain" });
    res.end("ok");
    return;
  }
  res.writeHead(404);
  res.end("not found");
});

const wss = new WebSocketServer({ noServer: true });

const state = {
  type: "state_update",
  session_id: "mock-session",
  session_state: "running",
  total_plays: 8,
  total_cost: 1.24,
  agents: [
    {
      agent_id: "agent-codex",
      agent_type: "codex",
      display_name: "Codex: Test Runner",
      model_tier: "large",
      status: "idle",
      context_size: 12000,
      total_cost: 0.36,
      total_tokens: 64000,
      tasks_completed: 4,
      tasks_failed: 0,
      current_play: {
        play_type: "code_review",
        play_id: 8,
        started_at: "2026-01-01T00:00:00.000Z",
        issue_number: null,
        pr_number: 112,
        branch: null,
      },
    },
  ],
  open_issues: [
    {
      issue_number: 60,
      title: "Add dashboard visual checks",
      state: "open",
      priority: 1,
      labels: ["dashboard"],
      source: "github",
      url: "https://github.com/example/agentshore/issues/60",
      created_at: "2026-01-01T00:00:00.000Z",
      closed_at: null,
      bead_id: "task-60",
      bead_epic_id: "epic-dashboard",
      bead_epic_title: "Dashboard",
      bead_status: "open",
      bead_ready: true,
      bead_mirror_status: "mirrored",
    },
  ],
  pull_requests: [],
  budget: {
    enabled: true,
    total_budget: 5,
    spent: 1.24,
    remaining: 3.76,
    estimated_cost_per_play: 0.2,
    time_enabled: true,
    time_total_minutes: 1440,
    time_elapsed_minutes: 120,
    time_remaining_minutes: 1320,
  },
  trajectory: {
    projected_alignment_at_budget_end: 0.81,
    estimated_remaining_plays: 11,
    estimated_remaining_cost: 2.2,
  },
  active_play: {
    play_type: "code_review",
    agent_id: "agent-codex",
    play_id: 8,
    issue_number: null,
    pr_number: 112,
    branch: null,
    started_at: "2026-01-01T00:00:00.000Z",
  },
  same_type_failure_streak: 0,
  last_play_type: "issue_pickup",
  forced_mask_zeros: [],
  action_mask: Array(22).fill(true),
  mask_reasons: {},
  graph: {
    epics: [
      {
        bead_id: "epic-dashboard",
        title: "Dashboard",
        total_tasks: 4,
        closed_tasks: 3,
        closure_ratio: 0.75,
      },
    ],
    tasks: [
      {
        bead_id: "task-60",
        title: "Add dashboard visual checks",
        status: "open",
        parent_id: "epic-dashboard",
        epic_id: "epic-dashboard",
        epic_title: "Dashboard",
        external_ref: "gh-60",
        issue_number: 60,
        ready: true,
      },
    ],
    tasks_ready: 1,
    tasks_total: 4,
    global_closure_ratio: 0.75,
  },
};

server.on("upgrade", (req, socket, head) => {
  if (req.url !== "/ws") {
    socket.destroy();
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => {
    wss.emit("connection", ws, req);
  });
});

wss.on("connection", (ws) => {
  ws.send(JSON.stringify({ type: "auth_token", token: "mock-token" }));
  ws.send(JSON.stringify(state));
  ws.send(
    JSON.stringify({
      type: "play_event",
      status: "started",
      play_type: "code_review",
      agent_id: "agent-codex",
      play_id: 8,
      started_at: "2026-01-01T00:00:00.000Z",
      issue_number: null,
      pr_number: 112,
      branch: null,
    }),
  );

  ws.on("message", (raw) => {
    const text = raw.toString();
    if (text.includes('"command":"pause"')) {
      ws.send(JSON.stringify({ ...state, session_state: "paused" }));
    }
    if (
      text.includes('"command":"resume"') ||
      text.includes('"command":"feedback_response"')
    ) {
      ws.send(JSON.stringify(state));
    }
    if (text.includes('"command":"drain"')) {
      ws.send(
        JSON.stringify({ type: "session_draining", reason: "user_request" }),
      );
      ws.send(JSON.stringify({ ...state, session_state: "draining" }));
      setTimeout(() => {
        ws.send(
          JSON.stringify({ type: "session_ended", reason: "drain_complete" }),
        );
      }, 1000);
    }
    if (text.includes('"command":"hard_stop"')) {
      ws.send(JSON.stringify({ ...state, session_state: "shutting_down" }));
      ws.send(JSON.stringify({ type: "session_ended", reason: "hard_stop" }));
    }
    if (text.includes('"command":"adjust_budget"')) {
      // acknowledge with a state_update (no budget model in mock)
      ws.send(JSON.stringify({ ...state }));
    }
  });
});

server.listen(port, "127.0.0.1");

function shutdown() {
  wss.close();
  server.close(() => process.exit(0));
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
