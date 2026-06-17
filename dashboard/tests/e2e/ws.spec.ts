import { expect, test } from "@playwright/test";

test("WebSocket envelope payload flattens with envelope metadata", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { normalizeAgentShoreWireMessage } = await import("/src/ws.ts");
    return normalizeAgentShoreWireMessage(
      JSON.stringify({
        type: "auth_token",
        id: "msg-1",
        timestamp: "2026-05-11T12:00:00Z",
        payload: { token: "secret", type: "error", id: "payload-id" },
      }),
    );
  });

  expect(result).toEqual({
    type: "auth_token",
    id: "msg-1",
    timestamp: "2026-05-11T12:00:00Z",
    token: "secret",
  });
});

test("WebSocket flat synthetic message passes through", async ({ page }) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { normalizeAgentShoreWireMessage } = await import("/src/ws.ts");
    return normalizeAgentShoreWireMessage(
      JSON.stringify({ type: "connection_lost", reason: "synthetic" }),
    );
  });

  expect(result).toEqual({ type: "connection_lost", reason: "synthetic" });
});

test("WebSocket malformed or objectless messages return null", async ({
  page,
}) => {
  const warnings: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "warning") warnings.push(msg.text());
  });

  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { normalizeAgentShoreWireMessage } = await import("/src/ws.ts");
    return [
      normalizeAgentShoreWireMessage("{"),
      normalizeAgentShoreWireMessage("null"),
      normalizeAgentShoreWireMessage("[]"),
      normalizeAgentShoreWireMessage(JSON.stringify({ payload: {} })),
      normalizeAgentShoreWireMessage(JSON.stringify({ type: 42, payload: {} })),
    ];
  });

  expect(result).toEqual([null, null, null, null, null]);
  expect(
    warnings.some((text) =>
      text.includes("[agentshore-dashboard]") &&
      text.includes("ws") &&
      text.includes("malformed broadcast frame, dropping"),
    ),
  ).toBe(true);
});

test("WebSocket unknown string type remains a drop candidate", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { normalizeAgentShoreWireMessage } = await import("/src/ws.ts");
    return normalizeAgentShoreWireMessage(
      JSON.stringify({ type: "future_message", payload: { value: 1 } }),
    );
  });

  expect(result).toEqual({
    type: "future_message",
    id: "",
    timestamp: "",
    value: 1,
  });
});
