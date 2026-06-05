import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

test.describe.configure({ mode: "parallel" });

async function expectCanvasNonBlank(page: Page): Promise<void> {
  await expect
    .poll(async () => {
      return page.locator("#office").evaluate((canvas) => {
        const c = canvas as HTMLCanvasElement;
        const ctx = c.getContext("2d");
        if (!ctx) return 0;
        const data = ctx.getImageData(0, 0, c.width, c.height).data;
        let count = 0;
        for (let i = 0; i < data.length; i += 4) {
          if (data[i] !== 0 || data[i + 1] !== 0 || data[i + 2] !== 0) count++;
        }
        return count;
      });
    })
    .toBeGreaterThan(1000);
}

type Rgb = [number, number, number];

function colorDistance(a: Rgb, b: Rgb): number {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
}

function expectRgbClose(actual: Rgb, expected: Rgb, tolerance = 8): void {
  expect(colorDistance(actual, expected)).toBeLessThanOrEqual(tolerance);
}

interface OfficeMapSample {
  backdrop: string;
  cornerAlpha: number;
  floors: {
    war: Rgb;
    workshop: Rgb;
    scienceLab: Rgb;
  };
}

async function sampleOfficeMap(page: Page): Promise<OfficeMapSample> {
  await expectCanvasNonBlank(page);
  return page.locator("#office").evaluate(async (canvasElement) => {
    const layout = await import("/src/office/layout.ts");
    const canvas = canvasElement as HTMLCanvasElement;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("2D canvas context unavailable");
    const testWindow = window as unknown as {
      __agentshoreDashboardTest?: {
        camera: {
          worldToScreen: (x: number, y: number, z?: number) => [number, number];
        };
      };
    };
    const camera = testWindow.__agentshoreDashboardTest?.camera;
    if (!camera) throw new Error("camera test hook unavailable");

    function sampleTile(tileX: number, tileY: number): Rgb {
      const [px, py] = camera.worldToScreen(
        (tileX + 0.5) * layout.TILE_SIZE,
        (tileY + 0.5) * layout.TILE_SIZE,
      );
      const data = ctx.getImageData(Math.floor(px), Math.floor(py), 1, 1).data;
      return [data[0], data[1], data[2]];
    }

    return {
      backdrop: getComputedStyle(document.body).backgroundImage,
      cornerAlpha: ctx.getImageData(2, 2, 1, 1).data[3],
      floors: {
        war: sampleTile(7, 14),
        workshop: sampleTile(35, 30),
        scienceLab: sampleTile(55, 40),
      },
    };
  });
}

test("demo active scenario renders office, HUD, and active play", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await expect(page.locator("#play-type-label")).toContainText("Issue Pickup");
  await expect(page.locator("#agent-list")).toContainText("Claude");
  await expectCanvasNonBlank(page);
});

test("local dev default route uses demo mock data", async ({ page }) => {
  await page.goto("/?freeze=1");
  await expect(page.locator("#play-type-label")).toContainText("Issue Pickup");
  await expect(page.locator("#agent-list")).toContainText("Claude");
});

test("top bar omits inactive pause and override controls", async ({ page }) => {
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await expect(page.locator("#session-label")).toContainText("RUNNING");
  await expect(page.locator("#theme-toggle")).toBeVisible();
  await expect(page.locator("#pause-resume-btn")).toHaveCount(0);
  await expect(page.locator("#override-play-select")).toHaveCount(0);
  await expect(page.locator("#top-bar #budget-label")).toHaveCount(0);
  await expect(page.locator("#plays-panel .budget-bar")).toBeVisible();
  await expect(page.locator(".pp-title-sub")).toHaveCount(0);

  const titlebar = await page.locator(".pp-titlebar").boundingBox();
  const budget = await page.locator("#plays-panel .budget-bar").boundingBox();
  expect(titlebar).not.toBeNull();
  expect(budget).not.toBeNull();
  if (titlebar && budget) {
    const titlebarCenter = titlebar.x + titlebar.width / 2;
    const budgetCenter = budget.x + budget.width / 2;
    expect(Math.abs(titlebarCenter - budgetCenter)).toBeLessThan(2);
  }
});

test("top bar shows open issue count when available", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  await page.evaluate(async () => {
    const topBar = await import("/src/hud/topBar.ts");
    const availability = {
      tracked_issue_count: 9,
      github_open_issue_count: 1,
      workable_issue_count: 0,
      blocked_issue_count: 1,
      disallowed_issue_count: 1,
      covered_by_open_pr_count: 0,
      resolved_by_merged_pr_count: 0,
      in_flight_issue_count: 0,
      planning_eligible_count: 0,
      implementation_eligible_count: 0,
      refinement_eligible_count: 0,
      debugging_eligible_count: 0,
      reviewable_pr_count: 0,
      mergeable_pr_count: 0,
      unblockable_pr_count: 0,
      actionable_pr_work_count: 0,
      terminal_no_work: true,
    };
    topBar.updateTopBar({
      type: "state_update",
      session_id: "issues-summary",
      session_state: "running",
      policy_mode: "learning",
      total_plays: 42,
      total_cost: 0,
      agents: [],
      open_issues: [],
      pull_requests: [],
      work_availability: availability,
      budget: null,
      trajectory: null,
      active_play: null,
      stats: null,
      same_type_failure_streak: 0,
      last_play_type: null,
      forced_mask_zeros: [],
      action_mask: Array(22).fill(true),
      mask_reasons: {},
    });
  });

  await expect(page.locator("#plays-count")).toContainText(
    "Plays: 42 · Open Issues : 1",
  );
});

test("agent avatars preserve v2 sprite-designed scale against standard eight-foot walls", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await expectCanvasNonBlank(page);

  const metrics = await page.locator("#office").evaluate(async () => {
    const layout = await import("/src/office/layout.ts");
    const sprites = await import("/src/characters/sprites.ts");
    const testWindow = window as unknown as {
      __agentshoreDashboardTest?: { camera: { zoom: number } };
    };
    const camera = testWindow.__agentshoreDashboardTest?.camera;
    if (!camera) throw new Error("camera test hook unavailable");

    const significantFrameHeight = async (
      agentType: string,
      modelTier: "small" | "medium" | "large",
    ) => {
      const spec = sprites.agentSpriteSpecFor(
        agentType,
        modelTier,
        `${agentType}-${modelTier}`,
      );
      if (!spec) throw new Error(`sprite spec unavailable for ${modelTier}`);
      const image = new Image();
      image.src = spec.url;
      await image.decode();
      const spriteCanvas = document.createElement("canvas");
      spriteCanvas.width = spec.frameWidth;
      spriteCanvas.height = spec.frameHeight;
      const spriteCtx = spriteCanvas.getContext("2d");
      if (!spriteCtx) throw new Error("sprite canvas context unavailable");
      spriteCtx.drawImage(
        image,
        spec.frameWidth * 4,
        0,
        spec.frameWidth,
        spec.frameHeight,
        0,
        0,
        spec.frameWidth,
        spec.frameHeight,
      );
      const data = spriteCtx.getImageData(
        0,
        0,
        spec.frameWidth,
        spec.frameHeight,
      ).data;
      let top = spec.frameHeight;
      let bottom = -1;
      for (let y = 0; y < spec.frameHeight; y += 1) {
        for (let x = 0; x < spec.frameWidth; x += 1) {
          if (data[(y * spec.frameWidth + x) * 4 + 3] > 16) {
            top = Math.min(top, y);
            bottom = Math.max(bottom, y);
          }
        }
      }
      return {
        frameHeight: spec.frameHeight,
        visibleHeight: bottom - top + 1,
      };
    };

    const measuredFrames: Record<
      string,
      { frameHeight: number; visibleHeight: number }
    > = {};
    for (const agentType of [
      "api_gpt",
      "api_other",
      "claude_code",
      "codex",
      "gemini",
    ] as const) {
      for (const modelTier of ["small", "medium", "large"] as const) {
        measuredFrames[`${agentType}:${modelTier}`] =
          await significantFrameHeight(agentType, modelTier);
      }
    }
    const smallFrame = measuredFrames["codex:small"];
    const mediumFrame = measuredFrames["codex:medium"];
    const largeFrame = measuredFrames["codex:large"];
    const smallAgentHeight =
      sprites.agentVisualSize(camera.zoom, 1, "small", "codex").height *
      (smallFrame.visibleHeight / smallFrame.frameHeight);
    const mediumAgentHeight =
      sprites.agentVisualSize(camera.zoom, 1, "medium", "codex").height *
      (mediumFrame.visibleHeight / mediumFrame.frameHeight);
    const largeAgentHeight =
      sprites.agentVisualSize(camera.zoom, 1, "large", "codex").height *
      (largeFrame.visibleHeight / largeFrame.frameHeight);
    const projectedFoot =
      layout.TILE_SIZE * layout.AXON_VERTICAL_SCALE * camera.zoom;
    const wallHeight =
      layout.WALL_HEIGHT_UNITS *
      layout.TILE_SIZE *
      layout.AXON_VERTICAL_SCALE *
      camera.zoom;
    const bounds = layout.projectedMapBounds("grid");
    const backWallTop = layout.projectUnits(
      0,
      0,
      layout.BACK_WALL_HEIGHT_UNITS,
      "grid",
    ).y;
    return {
      measuredFrameHeights: Object.fromEntries(
        Object.entries(measuredFrames).map(([key, value]) => [
          key,
          value.visibleHeight,
        ]),
      ),
      smallFrameHeight: smallFrame.visibleHeight,
      mediumFrameHeight: mediumFrame.visibleHeight,
      largeFrameHeight: largeFrame.visibleHeight,
      smallHeightUnits: smallAgentHeight / projectedFoot,
      mediumHeightUnits: mediumAgentHeight / projectedFoot,
      largeHeightUnits: largeAgentHeight / projectedFoot,
      smallRatio: smallAgentHeight / wallHeight,
      mediumRatio: mediumAgentHeight / wallHeight,
      largeRatio: largeAgentHeight / wallHeight,
      standardWallHeight: layout.WALL_HEIGHT_UNITS,
      backWallHeight: layout.BACK_WALL_HEIGHT_UNITS,
      northWallHeight: layout.backWallHeightForY(layout.NORTH_BACK_WALL_Y),
      interiorBackWallHeight: layout.backWallHeightForY(21),
      boundsTop: bounds.top,
      backWallTop,
    };
  });

  expect(metrics.measuredFrameHeights).toEqual({
    "api_gpt:small": 275,
    "api_gpt:medium": 570,
    "api_gpt:large": 741,
    "api_other:small": 275,
    "api_other:medium": 570,
    "api_other:large": 741,
    "claude_code:small": 275,
    "claude_code:medium": 570,
    "claude_code:large": 741,
    "codex:small": 275,
    "codex:medium": 570,
    "codex:large": 741,
    "gemini:small": 275,
    "gemini:medium": 570,
    "gemini:large": 741,
  });
  expect(metrics.smallFrameHeight).toBe(275);
  expect(metrics.mediumFrameHeight).toBe(570);
  expect(metrics.largeFrameHeight).toBe(741);
  expect(metrics.smallHeightUnits).toBeCloseTo((6 * 275) / 570, 3);
  expect(metrics.mediumHeightUnits).toBeCloseTo(6, 3);
  expect(metrics.largeHeightUnits).toBeCloseTo((6 * 741) / 570, 3);
  expect(metrics.smallRatio).toBeCloseTo((6 * 275) / 570 / 8, 3);
  expect(metrics.mediumRatio).toBeCloseTo(0.75, 3);
  expect(metrics.largeRatio).toBeCloseTo((6 * 741) / 570 / 8, 3);
  expect(metrics.standardWallHeight).toBe(8);
  expect(metrics.backWallHeight).toBe(12);
  expect(metrics.northWallHeight).toBe(12);
  expect(metrics.interiorBackWallHeight).toBe(8);
  expect(metrics.boundsTop).toBeCloseTo(metrics.backWallTop, 6);
});

test("state manager normalizes configured model tiers for agent sprites", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const tiers = await page.evaluate(async () => {
    const stateModule = await import("/src/state.ts");
    const stateMachine = await import("/src/characters/stateMachine.ts");

    stateMachine.__testHooks.clearOccupiedSeats();
    const manager = new stateModule.AgentShoreStateManager();

    function agent(
      agent_id: string,
      agent_type: string,
      model_tier: string | null,
    ) {
      return {
        agent_id,
        agent_type,
        display_name: agent_id,
        model_tier,
        status: "idle",
        context_size: 0,
        total_cost: 0,
        total_tokens: 0,
        tasks_completed: 0,
        tasks_failed: 0,
        current_play: null,
      };
    }

    function state(agents: ReturnType<typeof agent>[]) {
      return {
        type: "state_update",
        session_id: "tier-sprites",
        session_state: "running",
        policy_mode: "learning",
        total_plays: 0,
        total_cost: 0,
        agents,
        open_issues: [],
        pull_requests: [],
        budget: null,
        trajectory: null,
        active_play: null,
        stats: null,
        same_type_failure_streak: 0,
        last_play_type: null,
        forced_mask_zeros: [],
        action_mask: Array(22).fill(true),
        mask_reasons: {},
      } as never;
    }

    manager.handleMessage(
      state([
        agent("agent-claude", "claude_code", "large"),
        agent("agent-codex", "codex", "medium"),
        agent("agent-gemini", "gemini", "small"),
        agent("agent-default", "codex", "experimental"),
      ]),
    );
    const initial = Object.fromEntries(
      manager.getAgents().map((char) => [char.agentId, char.modelTier]),
    );

    manager.handleMessage(
      state([
        agent("agent-claude", "claude_code", "large"),
        agent("agent-codex", "codex", "large"),
        agent("agent-gemini", "gemini", "small"),
        agent("agent-default", "codex", null),
      ]),
    );
    const updated = Object.fromEntries(
      manager.getAgents().map((char) => [char.agentId, char.modelTier]),
    );

    stateMachine.__testHooks.clearOccupiedSeats();
    return { initial, updated };
  });

  expect(tiers.initial).toEqual({
    "agent-claude": "large",
    "agent-codex": "medium",
    "agent-gemini": "small",
    "agent-default": "medium",
  });
  expect(tiers.updated["agent-codex"]).toBe("large");
  expect(tiers.updated["agent-default"]).toBe("medium");
});

test("agent sprite specs map every provider and model tier to v2 sheets", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const specs = await page.evaluate(async () => {
    const sprites = await import("/src/characters/sprites.ts");
    return {
      claudeLarge: sprites.agentSpriteSpecFor(
        "claude_code",
        "large",
        "agent-claude",
      ),
      codexMedium: sprites.agentSpriteSpecFor("codex", "medium", "agent-codex"),
      geminiSmall: sprites.agentSpriteSpecFor(
        "gemini",
        "small",
        "agent-gemini",
      ),
      geminiDefault: sprites.agentSpriteSpecFor(
        "gemini",
        "experimental",
        "agent-gemini",
      ),
      apiGpt: sprites.agentSpriteSpecFor("api_gpt", "large", "agent-api"),
      apiOther: sprites.agentSpriteSpecFor("api_other", "small", "agent-other"),
      unknown: sprites.agentSpriteSpecFor(
        "unknown_agent",
        "medium",
        "agent-unknown",
      ),
    };
  });

  expect(specs.claudeLarge).toMatchObject({
    key: "claude-large-humanoid",
    frameWidth: 416,
    frameHeight: 832,
    sheetWidth: 2912,
    sheetHeight: 2496,
  });
  expect(specs.codexMedium).toMatchObject({ key: "codex-medium-humanoid" });
  expect(specs.geminiSmall).toMatchObject({ key: "gemini-small-ball" });
  expect(specs.geminiDefault).toMatchObject({ key: "gemini-medium-humanoid" });
  expect(specs.apiGpt).toMatchObject({
    key: "api-gpt-large-humanoid",
    frameWidth: 416,
    frameHeight: 832,
    sheetWidth: 2912,
    sheetHeight: 2496,
  });
  expect(specs.apiOther).toMatchObject({ key: "api-other-small-ball" });
  expect(specs.unknown).toBeNull();
});

test("mock AgentShore WebSocket populates HUD and canvas", async ({
  page,
  isMobile,
}) => {
  await page.goto("/?demo=0&freeze=1");
  await expect(page.locator("#session-label")).toContainText("RUNNING");
  await expect(page.locator("#play-type-label")).toContainText("Code Review");
  await expect(page.locator("#agent-list")).toContainText("Test Runner");
  await expect(page.locator("#agent-list")).toContainText("Large Codex");
  await expect(page.locator("#agent-list")).toContainText("Code Review 112");
  if (!isMobile) {
    const agentCard = page.locator(
      '#agent-list .agent-item[data-agent-id="agent-codex"]',
    );
    const agentCardBox = await agentCard.boundingBox();
    const nameBox = await agentCard.locator(".agent-name").boundingBox();
    const typeBox = await agentCard.locator(".agent-type").boundingBox();
    expect(agentCardBox).not.toBeNull();
    expect(nameBox).not.toBeNull();
    expect(typeBox).not.toBeNull();
    if (agentCardBox && nameBox && typeBox) {
      const nameCenter = nameBox.y + nameBox.height / 2;
      const typeCenter = typeBox.y + typeBox.height / 2;
      const typeInset =
        agentCardBox.x + agentCardBox.width - (typeBox.x + typeBox.width);
      expect(Math.abs(nameCenter - typeCenter)).toBeLessThan(3);
      expect(typeInset).toBeLessThan(8);
    }
  }
  await expectCanvasNonBlank(page);
});

test("event drawer cards show short agent, tier/type, and play target", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop event drawer assertion");
  await page.goto("/?demo=0&freeze=1");

  const eventCard = page.locator("#event-list .event-card").first();
  await expect(eventCard).toContainText("Test Runner");
  await expect(eventCard).not.toContainText("Codex: Test Runner");
  await expect(eventCard).toContainText("Large Codex");
  await expect(eventCard).toContainText("Code Review 112");
});

test("event drawer running filter mirrors current play snapshots", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop event drawer assertion");
  await page.goto("/?demo=1&scenario=stress&freeze=1");

  await page.locator('[data-event-filter="started"]').click();
  const runningCards = page.locator("#event-list .event-card");
  await expect(runningCards).toHaveCount(8);
  await expect(page.locator("#event-list")).toContainText("Agent 1");
  await expect(page.locator("#event-list")).toContainText("Issue Pickup 101");
  await expect(page.locator("#event-list")).toContainText("Agent 8");
  await expect(page.locator("#event-list")).toContainText("Issue Pickup 108");
});

test("plays panel active counts follow current play snapshots when status lags", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop plays panel assertion");
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  await page.evaluate(async () => {
    const panel = await import("/src/components/PlaysPanel.tsx");
    const state: Parameters<typeof panel.notifyPlaysPanelUpdate>[0] = {
      type: "state_update",
      session_id: "plays-panel-status-lag",
      session_state: "running",
      policy_mode: "learning",
      total_plays: 0,
      total_cost: 0,
      agents: [
        {
          agent_id: "lag-issue",
          agent_type: "codex",
          display_name: "Codex: Lag Issue",
          model_tier: "medium",
          status: "idle",
          context_size: 0,
          total_cost: 0,
          total_tokens: 0,
          tasks_completed: 0,
          tasks_failed: 0,
          current_play: {
            play_type: "issue_pickup",
            play_id: 501,
            started_at: "2026-01-01T00:00:00.000Z",
            issue_number: 501,
            pr_number: null,
            branch: null,
          },
        },
        {
          agent_id: "lag-qa",
          agent_type: "gemini",
          display_name: "Gemini: Lag QA",
          model_tier: "medium",
          status: "idle",
          context_size: 0,
          total_cost: 0,
          total_tokens: 0,
          tasks_completed: 0,
          tasks_failed: 0,
          current_play: {
            play_type: "run_qa",
            play_id: 502,
            started_at: "2026-01-01T00:00:00.000Z",
            issue_number: null,
            pr_number: 502,
            branch: null,
          },
        },
      ],
      open_issues: [],
      pull_requests: [],
      budget: null,
      trajectory: null,
      active_play: null,
      stats: null,
      same_type_failure_streak: 0,
      last_play_type: null,
      forced_mask_zeros: [],
      action_mask: Array(22).fill(true),
      mask_reasons: {},
    };

    panel.notifyPlaysPanelUpdate(state);
  });

  await expect(page.locator("#pp-totals-active")).toContainText("2 ACTIVE");
  await expect(page.locator('[data-play-key="issue_pickup"]')).toHaveClass(
    /pp-card-active/,
  );
  await expect(page.locator('[data-play-key="run_qa"]')).toHaveClass(
    /pp-card-active/,
  );
  await expect(page.locator('[data-play-key="issue_pickup"]')).toHaveText(
    "Issue Pickup",
  );
  await expect(page.locator('[data-play-key="run_qa"]')).toHaveText("Run QA");
  await expect(page.locator("#plays-panel-grid")).not.toContainText(
    "Lag Issue",
  );
  await expect(page.locator("#plays-panel-grid")).not.toContainText("501");
});

test("plays panel shows all lifecycle plays and greys masked action slots", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop plays panel assertion");

  await page.goto("/?demo=1&scenario=active&freeze=1");

  await page.evaluate(async () => {
    const panel =
      (await import("/src/components/PlaysPanel.tsx")) as typeof import("../../src/components/PlaysPanel");
    const actionMask = Array(22).fill(true);
    actionMask[6] = false;
    actionMask[7] = false;
    panel.notifyPlaysPanelUpdate({
      type: "state_update",
      session_id: "tray-order",
      session_state: "running",
      policy_mode: "learning",
      total_plays: 0,
      total_cost: 0,
      agents: [],
      open_issues: [],
      pull_requests: [],
      budget: null,
      trajectory: null,
      active_play: null,
      stats: null,
      same_type_failure_streak: 0,
      last_play_type: null,
      forced_mask_zeros: [],
      action_mask: actionMask,
      mask_reasons: {
        merge_pr: "No open PR ready to merge",
        run_qa: "No branch ready for QA",
      },
    });
  });

  const rows = await page
    .locator("#plays-panel-grid .pp-card")
    .evaluateAll((cards) => {
      const items = cards.map((card) => {
        const element = card as HTMLElement;
        const rect = element.getBoundingClientRect();
        return {
          key: element.dataset.playKey ?? "",
          left: rect.left,
          top: rect.top,
        };
      });
      const minTop = Math.min(...items.map((item) => item.top));
      const maxTop = Math.max(...items.map((item) => item.top));
      const midpoint = (minTop + maxTop) / 2;
      const byLeft = (a: (typeof items)[number], b: (typeof items)[number]) =>
        a.left - b.left;
      return {
        top: items
          .filter((item) => item.top < midpoint)
          .sort(byLeft)
          .map((item) => item.key),
        bottom: items
          .filter((item) => item.top >= midpoint)
          .sort(byLeft)
          .map((item) => item.key),
      };
    });

  expect(rows.top).toEqual([
    "instantiate_agent",
    "design_audit",
    "refine_task_breakdown",
    "write_implementation_plan",
    "issue_pickup",
    "code_review",
    "merge_pr",
    "future_4",
    "run_qa",
    "end_agent",
    "future_7",
  ]);
  expect(rows.bottom).toEqual([
    "seed_project",
    "groom_backlog",
    "calibrate_alignment",
    "cleanup",
    "systematic_debugging",
    "unblock_pr",
    "reconcile_state",
    "future_6",
    "take_break",
    "end_session",
    "future_8",
  ]);
  await expect(page.locator('[data-play-key="merge_pr"]')).toHaveClass(
    /pp-card-masked/,
  );
  await expect(page.locator('[data-play-key="run_qa"]')).toHaveClass(
    /pp-card-masked/,
  );
  await expect(page.locator('[data-play-key="future_6"]')).toHaveClass(
    /pp-card-masked/,
  );
  await expect(page.locator('[data-play-key="future_7"]')).toHaveClass(
    /pp-card-masked/,
  );
  await expect(page.locator('[data-play-key="future_8"]')).toHaveClass(
    /pp-card-masked/,
  );
  await expect(page.locator("#pp-totals-active")).toContainText("0 ACTIVE");
  await expect(page.locator("#pp-totals-ready")).toContainText("17 READY");
  await expect(page.locator("#pp-totals-masked")).toContainText("5 MASKED");
  await expect(page.locator("#pp-totals-total")).toContainText("22 TOTAL");
});

test("play bar summarizes current play snapshots when active play is absent", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=stress&freeze=1");

  await expect(page.locator("#play-type-label")).toContainText(
    "8 active plays",
  );
  await expect(page.locator("#play-agent-label")).toContainText(
    "Issue Pickup x8",
  );
});

test("write implementation plan labels keep issue target visible", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=active&freeze=1");

  const label = await page.evaluate(async () => {
    const formatter =
      (await import("/src/hud/format.ts")) as typeof import("../../src/hud/format");
    return formatter.formatPlayWithTarget("write_implementation_plan", {
      issue_number: 234,
      pr_number: null,
    });
  });

  expect(label).toBe("Write Plan 234");
});

test("agent tray status follows current play when snapshot status lags", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop side panel assertion");
  await page.goto("/?demo=0&freeze=1");

  await page
    .locator('#agent-list .agent-item[data-agent-id="agent-codex"]')
    .click();

  const detailText = await page.locator("#agent-detail").innerText();
  expect(detailText).toContain("Status");
  expect(detailText).toContain("busy");
  expect(detailText).toContain("Current play");
  expect(detailText).toContain("Code Review");
  expect(detailText).not.toContain("idle");
});

test("mouse wheel zoom is incremental and keeps the fitted floor", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop wheel assertion");
  await page.setViewportSize({ width: 1800, height: 1000 });
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await expectCanvasNonBlank(page);

  const before = await page.evaluate(() => {
    const testWindow = window as unknown as {
      __agentshoreDashboardTest?: { camera: { zoom: number } };
    };
    return testWindow.__agentshoreDashboardTest?.camera.zoom ?? 0;
  });

  await page.mouse.move(900, 500);
  await page.mouse.wheel(0, -100);

  const zoomedIn = await page.evaluate(() => {
    const testWindow = window as unknown as {
      __agentshoreDashboardTest?: { camera: { zoom: number } };
    };
    return testWindow.__agentshoreDashboardTest?.camera.zoom ?? 0;
  });

  expect(zoomedIn / before).toBeGreaterThan(1.05);
  expect(zoomedIn / before).toBeLessThan(1.1);

  await page.mouse.wheel(0, 100);
  const zoomedOut = await page.evaluate(() => {
    const testWindow = window as unknown as {
      __agentshoreDashboardTest?: { camera: { zoom: number } };
    };
    return testWindow.__agentshoreDashboardTest?.camera.zoom ?? 0;
  });

  expect(Math.abs(zoomedOut - before)).toBeLessThan(0.02);
});

test("theme query applies grid theme tokens and rejects classic theme params", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");
  await page.evaluate(() => window.localStorage.clear());

  await page.goto("/?demo=1&scenario=active&freeze=1&theme=light");
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme",
    "light",
  );
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme-mode",
    "light",
  );
  await expect
    .poll(async () => {
      return page.evaluate(() => {
        const root = getComputedStyle(document.documentElement);
        return {
          bg: root.getPropertyValue("--color-fm-bg").trim(),
          text: root.getPropertyValue("--color-fm-text").trim(),
          panel: root.getPropertyValue("--color-fm-panel").trim(),
          ok: root.getPropertyValue("--color-fm-ok").trim(),
        };
      });
    })
    .toEqual({
      bg: "#f3f6ff",
      text: "#1f2940",
      panel: "rgba(242,248,255,0.94)",
      ok: "#0f8a6c",
    });

  await page.goto("/?demo=1&scenario=active&freeze=1&theme=dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme-mode",
    "dark",
  );
  await expect
    .poll(async () => {
      return page.evaluate(() => {
        const root = getComputedStyle(document.documentElement);
        return {
          bg: root.getPropertyValue("--color-fm-bg").trim(),
          text: root.getPropertyValue("--color-fm-text").trim(),
          panel: root.getPropertyValue("--color-fm-panel").trim(),
          ok: root.getPropertyValue("--color-fm-ok").trim(),
        };
      });
    })
    .toEqual({
      bg: "#05070d",
      text: "#d8f3ff",
      panel: "rgba(8,12,20,0.92)",
      ok: "#29e3a9",
    });

  for (const legacyTheme of ["light", "dark"] as const) {
    await page.goto("/?demo=1&scenario=active&freeze=1&theme=" + legacyTheme);
    await expect(page.locator("html")).toHaveAttribute(
      "data-theme",
      "light",
    );
    await expect(page.locator("html")).toHaveAttribute(
      "data-theme-mode",
      "light",
    );
  }
});

test("system theme resolves to grid variants", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/?demo=1&scenario=active&freeze=1&theme=system");
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme-mode",
    "system",
  );
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme",
    "light",
  );

  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/?demo=1&scenario=active&freeze=1&theme=system");
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme-mode",
    "system",
  );
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("manual grid theme selection persists and legacy values fall back", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await page.evaluate(() => window.localStorage.clear());
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme",
    "light",
  );
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme-mode",
    "light",
  );

  await page.locator('#theme-toggle [data-theme-mode="dark"]').click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(
    page.locator('#theme-toggle [data-theme-mode="dark"]'),
  ).toHaveAttribute("aria-pressed", "true");
  await expect
    .poll(async () => {
      return page.evaluate(() =>
        window.localStorage.getItem("agentshore.dashboard.theme"),
      );
    })
    .toBe("dark");

  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  await page.evaluate(() =>
    window.localStorage.setItem("agentshore.dashboard.theme", "dark"),
  );
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme",
    "light",
  );
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme-mode",
    "light",
  );

  await page.evaluate(() =>
    window.localStorage.setItem("agentshore.dashboard.theme", "dark"),
  );
  await page.goto("/?demo=1&scenario=active&freeze=1&theme=light");
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme",
    "light",
  );
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme-mode",
    "light",
  );
  await expect
    .poll(async () => {
      return page.evaluate(() =>
        window.localStorage.getItem("agentshore.dashboard.theme"),
      );
    })
    .toBe("dark");
});

test("theme toggle exposes Auto / Light / Dark modes only", async ({ page }) => {
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await page.evaluate(() => window.localStorage.clear());
  await page.reload();

  await expect(
    page.locator('#theme-toggle [data-theme-mode="system"]'),
  ).toHaveText("Auto");
  await expect(
    page.locator('#theme-toggle [data-theme-mode="light"]'),
  ).toHaveText("Light");
  await expect(
    page.locator('#theme-toggle [data-theme-mode="dark"]'),
  ).toHaveText("Dark");

  await page.locator('#theme-toggle [data-theme-mode="dark"]').click();
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme-mode",
    "dark",
  );
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(
    page.locator('#theme-toggle [data-theme-mode="dark"]'),
  ).toHaveAttribute("aria-pressed", "true");
  await expect
    .poll(async () => {
      return page.evaluate(() =>
        window.localStorage.getItem("agentshore.dashboard.theme"),
      );
    })
    .toBe("dark");
});

test("theme storage failures warn and fall back", async ({ page }) => {
  const warnings: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "warning") warnings.push(msg.text());
  });

  await page.addInitScript(() => {
    const originalGetItem = Storage.prototype.getItem;
    const originalSetItem = Storage.prototype.setItem;

    Storage.prototype.getItem = function (key: string): string | null {
      if (key === "agentshore.dashboard.theme")
        throw new Error("theme read unavailable");
      return originalGetItem.call(this, key);
    };
    Storage.prototype.setItem = function (key: string, value: string): void {
      if (key === "agentshore.dashboard.theme")
        throw new Error("theme write unavailable");
      originalSetItem.call(this, key, value);
    };
  });

  await page.goto("/?demo=1&scenario=active&freeze=1");
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme-mode",
    "light",
  );

  await page.locator('#theme-toggle [data-theme-mode="dark"]').click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  await expect
    .poll(() =>
      warnings.some((text) =>
        text.includes("[theme] could not read stored theme mode:"),
      ),
    )
    .toBe(true);
  await expect
    .poll(() =>
      warnings.some((text) =>
        text.includes("[theme] could not persist theme mode:"),
      ),
    )
    .toBe(true);
});

test("HUD components inherit grid theme variables", async ({ page }) => {
  await page.goto("/?demo=1&scenario=active&freeze=1&theme=light");

  await expect
    .poll(async () => {
      return page.evaluate(() => ({
        top: getComputedStyle(document.querySelector("#top-bar")!)
          .backgroundColor,
        side: getComputedStyle(document.querySelector("#side-panel")!)
          .backgroundColor,
        eventCard: getComputedStyle(document.querySelector(".event-card")!)
          .backgroundColor,
        playLabel: getComputedStyle(document.querySelector("#play-type-label")!)
          .color,
      }));
    })
    .toEqual({
      top: "rgba(238, 245, 255, 0.95)",
      side: "rgba(242, 248, 255, 0.94)",
      eventCard: "rgba(255, 255, 255, 0.76)",
      playLabel: "rgb(15, 138, 108)",
    });

  await page.goto("/?demo=1&scenario=feedback&freeze=1&theme=light");
  await expect
    .poll(async () => {
      return page
        .locator("#feedback-modal .modal-box")
        .evaluate((modal) => getComputedStyle(modal).backgroundColor);
    })
    .toBe("rgba(242, 248, 255, 0.94)");
});

test("grid theme changes backdrop and map art", async ({ page, isMobile }) => {
  test.skip(isMobile, "desktop canvas sampling assertion");
  await page.setViewportSize({ width: 1800, height: 1000 });

  await page.goto("/?demo=1&scenario=empty&freeze=1&theme=light");
  const gridLight = await sampleOfficeMap(page);
  await page.goto("/?demo=1&scenario=empty&freeze=1&theme=dark");
  const gridDark = await sampleOfficeMap(page);

  expect(gridLight.backdrop).not.toEqual(gridDark.backdrop);
  expect(gridLight.cornerAlpha).toBe(0);
  expect(gridDark.cornerAlpha).toBe(0);
  expectRgbClose(gridLight.floors.war, [198, 220, 238]);
  expectRgbClose(gridLight.floors.workshop, [180, 244, 250]);
  expectRgbClose(gridLight.floors.scienceLab, [214, 244, 252]);
  expectRgbClose(gridDark.floors.war, [17, 52, 84]);
  expectRgbClose(gridDark.floors.workshop, [16, 69, 95]);
  expectRgbClose(gridDark.floors.scienceLab, [20, 58, 99]);
  expect(
    colorDistance(gridLight.floors.war, gridDark.floors.war),
  ).toBeGreaterThan(220);
});

test("grid themes render north wall Kanban lanes across rooms", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop canvas sampling assertion");
  await page.setViewportSize({ width: 1800, height: 1000 });

  for (const theme of ["light", "dark"] as const) {
    await page.goto(`/?demo=1&scenario=active&freeze=1&theme=${theme}`);
    await expectCanvasNonBlank(page);

    const result = await page
      .locator("#office")
      .evaluate(async (canvasElement) => {
        const layout = await import("/src/office/layout.ts");
        const canvas = canvasElement as HTMLCanvasElement;
        const ctx = canvas.getContext("2d");
        if (!ctx) throw new Error("2D canvas context unavailable");
        const testWindow = window as unknown as {
          __agentshoreDashboardTest?: {
            camera: {
              worldToScreen: (
                x: number,
                y: number,
                z?: number,
              ) => [number, number];
            };
          };
        };
        const camera = testWindow.__agentshoreDashboardTest?.camera;
        if (!camera) throw new Error("camera test hook unavailable");

        const headerCenterZ = layout.BACK_WALL_HEIGHT_UNITS - 3.0 + 0.75 / 2;
        const baseWallZ = 0.55;
        const wallY = 6.5 * layout.TILE_SIZE;
        const lanes = {
          todo: 12.5,
          inProgress: 28.5,
          review: 45.5,
          done: 61.5,
        } as const;

        function sampleWall(laneX: number, z: number): Rgb {
          const [px, py] = camera.worldToScreen(
            laneX * layout.TILE_SIZE,
            wallY,
            z,
          );
          const data = ctx.getImageData(
            Math.floor(px),
            Math.floor(py),
            1,
            1,
          ).data;
          return [data[0], data[1], data[2]];
        }

        return {
          headers: {
            todo: sampleWall(lanes.todo, headerCenterZ),
            inProgress: sampleWall(lanes.inProgress, headerCenterZ),
            review: sampleWall(lanes.review, headerCenterZ),
            done: sampleWall(lanes.done, headerCenterZ),
          },
          wallBase: {
            todo: sampleWall(lanes.todo, baseWallZ),
            inProgress: sampleWall(lanes.inProgress, baseWallZ),
            review: sampleWall(lanes.review, baseWallZ),
            done: sampleWall(lanes.done, baseWallZ),
          },
        };
      });

    expect(
      colorDistance(result.headers.todo, result.wallBase.todo),
    ).toBeGreaterThan(8);
    expect(
      colorDistance(result.headers.inProgress, result.wallBase.inProgress),
    ).toBeGreaterThan(8);
    expect(
      colorDistance(result.headers.review, result.wallBase.review),
    ).toBeGreaterThan(8);
    expect(
      colorDistance(result.headers.done, result.wallBase.done),
    ).toBeGreaterThan(8);
    expect(
      colorDistance(result.headers.inProgress, result.headers.review),
    ).toBeGreaterThan(15);
  }
});

test("grid themes render key office landmarks with theme contrast", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1800, height: 1000 });

  async function sampleLandmarks(theme: "dark" | "light") {
    await page.goto(`/?demo=1&scenario=empty&freeze=1&theme=${theme}`);
    await expectCanvasNonBlank(page);
    return page.locator("#office").evaluate(async (canvasElement) => {
      const layout = await import("/src/office/layout.ts");
      const canvas = canvasElement as HTMLCanvasElement;
      const ctx = canvas.getContext("2d");
      if (!ctx) throw new Error("2D canvas context unavailable");
      const testWindow = window as unknown as {
        __agentshoreDashboardTest?: {
          camera: {
            worldToScreen: (
              x: number,
              y: number,
              z?: number,
            ) => [number, number];
          };
        };
      };
      const camera = testWindow.__agentshoreDashboardTest?.camera;
      if (!camera) throw new Error("camera test hook unavailable");

      function byName(name: string) {
        const item = layout.FURNITURE.find((entry) => entry.name === name);
        if (!item) throw new Error(`${name} missing`);
        return item;
      }

      function sampleWorld(x: number, y: number, z: number): Rgb {
        const [px, py] = camera.worldToScreen(
          x * layout.TILE_SIZE,
          y * layout.TILE_SIZE,
          z,
        );
        const data = ctx.getImageData(
          Math.floor(px),
          Math.floor(py),
          1,
          1,
        ).data;
        return [data[0], data[1], data[2]] as Rgb;
      }

      const turnstile = byName("Badge Turnstile Center");
      const warTable = byName("War Table");
      const printerPod = byName("Printer Pod NE");
      const centerTable = byName("Bench SE");
      const sideBench = byName("Bench SW");
      const bins = byName("Bins E");
      const tools = byName("Tools");
      const electronicsBench = byName("Bench NW");
      const launchCube = byName("Merge Button Cube");
      const editorCube = byName("Editor Repo Cube");
      const zenMat = byName("Sand");
      const vending = byName("Vending Machine");
      const scienceRig = byName("Test Rig");

      return {
        turnstilePost: sampleWorld(turnstile.x + 0.44, turnstile.y + 0.86, 2.6),
        warTable: sampleWorld(warTable.x + 3.5, warTable.y + 2.0, 2.95),
        printerPod: sampleWorld(
          printerPod.x + printerPod.w - 0.66,
          printerPod.y + 4.5,
          2.9,
        ),
        centerTable: {
          table: sampleWorld(
            centerTable.x + 4.5,
            centerTable.y + centerTable.h - 0.72,
            2.86,
          ),
          blueprint: sampleWorld(
            centerTable.x + 1.95,
            centerTable.y + 1.2,
            2.94,
          ),
          crate: sampleWorld(centerTable.x + 4.92, centerTable.y + 1.08, 3.18),
        },
        sideBench: sampleWorld(
          sideBench.x + 4.1,
          sideBench.y + sideBench.h - 0.7,
          2.86,
        ),
        bins: sampleWorld(bins.x + 2.5, bins.y + 0.64, 2.88),
        tools: sampleWorld(tools.x + 1.0, tools.y + tools.h + 0.03, 3.8),
        electronicsBench: sampleWorld(
          electronicsBench.x + 4.0,
          electronicsBench.y + electronicsBench.h - 0.72,
          2.86,
        ),
        recovery: {
          floor: sampleWorld(9.5, 49.5, 0),
          floorArt: sampleWorld(12.5, 43.6, 0.06),
        },
        launchConsole: {
          consoleTop: sampleWorld(
            launchCube.x + 0.62,
            launchCube.y + 0.62,
            2.49,
          ),
          button: sampleWorld(launchCube.x + 1.5, launchCube.y + 1.5, 2.95),
        },
        editorRepoCube: sampleWorld(
          editorCube.x + 1.5,
          editorCube.y + 1.45,
          2.85,
        ),
        zenGarden: {
          mat: sampleWorld(zenMat.x + 3.1, zenMat.y + 1.52, 1.29),
          vending: sampleWorld(vending.x + 1.5, vending.y + 0.33, 5.25),
          hasBuddha: layout.FURNITURE.some(
            (item) => item.name === "Seated Buddha",
          ),
        },
        scienceLab: {
          reactorTop: sampleWorld(scienceRig.x + 3, scienceRig.y + 1.5, 3.78),
          diagnosticWall: sampleWorld(61.35, 37.5, 3.22),
        },
      };
    });
  }

  const gridDark = await sampleLandmarks("dark");
  const gridLight = await sampleLandmarks("light");
  const brightness = (color: Rgb) => color[0] + color[1] + color[2];

  expect(
    colorDistance(gridDark.turnstilePost, gridLight.turnstilePost),
  ).toBeGreaterThan(40);
  expect(colorDistance(gridDark.warTable, gridLight.warTable)).toBeGreaterThan(
    40,
  );
  expect(
    colorDistance(gridDark.printerPod, gridLight.printerPod),
  ).toBeGreaterThan(10);
  expect(
    colorDistance(gridDark.centerTable.table, gridLight.centerTable.table),
  ).toBeGreaterThan(35);
  expect(
    colorDistance(gridDark.centerTable.blueprint, gridDark.centerTable.table),
  ).toBeGreaterThan(25);
  expect(
    colorDistance(gridLight.centerTable.blueprint, gridLight.centerTable.table),
  ).toBeGreaterThan(25);
  expect(
    colorDistance(gridDark.centerTable.crate, gridDark.centerTable.table),
  ).toBeGreaterThan(20);
  expect(
    colorDistance(gridLight.centerTable.crate, gridLight.centerTable.table),
  ).toBeGreaterThan(20);
  expect(
    colorDistance(gridDark.sideBench, gridLight.sideBench),
  ).toBeGreaterThan(35);
  expect(colorDistance(gridDark.bins, gridLight.bins)).toBeGreaterThan(25);
  expect(colorDistance(gridDark.tools, gridLight.tools)).toBeGreaterThan(25);
  expect(
    colorDistance(gridDark.electronicsBench, gridLight.electronicsBench),
  ).toBeGreaterThan(35);
  expect(colorDistance(gridLight.recovery.floor, [187, 202, 210])).toBeLessThan(
    45,
  );
  expect(colorDistance(gridDark.recovery.floor, [46, 68, 80])).toBeLessThan(45);
  expect(
    colorDistance(gridLight.recovery.floorArt, gridLight.recovery.floor),
  ).toBeGreaterThan(30);
  expect(
    colorDistance(gridDark.recovery.floorArt, gridDark.recovery.floor),
  ).toBeGreaterThan(30);
  expect(brightness(gridLight.launchConsole.consoleTop)).toBeGreaterThan(
    brightness(gridDark.launchConsole.consoleTop) + 260,
  );
  expect(
    colorDistance(
      gridLight.launchConsole.consoleTop,
      gridDark.launchConsole.consoleTop,
    ),
  ).toBeGreaterThan(130);
  expect(
    colorDistance(
      gridLight.launchConsole.button,
      gridDark.launchConsole.button,
    ),
  ).toBeLessThan(20);
  expect(
    colorDistance(gridDark.editorRepoCube, gridLight.editorRepoCube),
  ).toBeGreaterThan(35);
  expect(gridDark.zenGarden.hasBuddha).toBe(false);
  expect(gridLight.zenGarden.hasBuddha).toBe(false);
  expect(
    colorDistance(gridDark.zenGarden.mat, gridLight.zenGarden.mat),
  ).toBeGreaterThan(35);
  expect(
    colorDistance(gridDark.zenGarden.vending, gridLight.zenGarden.vending),
  ).toBeGreaterThan(35);
  expect(
    colorDistance(
      gridDark.scienceLab.reactorTop,
      gridLight.scienceLab.reactorTop,
    ),
  ).toBeGreaterThan(35);
  expect(
    colorDistance(
      gridDark.scienceLab.diagnosticWall,
      gridLight.scienceLab.diagnosticWall,
    ),
  ).toBeGreaterThan(35);
});

test("manual theme toggle rerenders frozen office map", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop canvas sampling assertion");
  await page.setViewportSize({ width: 1800, height: 1000 });
  await page.goto("/?demo=1&scenario=empty&freeze=1&theme=dark");

  expectRgbClose((await sampleOfficeMap(page)).floors.war, [35, 74, 103], 55);
  await page.locator('#theme-toggle [data-theme-mode="light"]').click();
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme",
    "light",
  );
  expectRgbClose((await sampleOfficeMap(page)).floors.war, [190, 220, 232], 55);

  await page.locator('#theme-toggle [data-theme-mode="dark"]').click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expectRgbClose((await sampleOfficeMap(page)).floors.war, [35, 74, 103], 55);
});

test("dark and light query params set theme attributes", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=active&freeze=1&theme=dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme-mode",
    "dark",
  );
  await expect(
    page.locator('#theme-toggle [data-theme-mode="dark"]'),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.locator('#theme-toggle [data-theme-mode="light"]'),
  ).toHaveCount(0);

  await page.goto("/?demo=1&scenario=active&freeze=1&theme=light");
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme",
    "light",
  );
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme-mode",
    "light",
  );
  await expect(
    page.locator('#theme-toggle [data-theme-mode="light"]'),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.locator('#theme-toggle [data-theme-mode="dark"]'),
  ).toHaveCount(0);
});

test("system theme resolves to grid variant attributes", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/?demo=1&scenario=active&freeze=1&theme=system");
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme-mode",
    "system",
  );
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme",
    "light",
  );

  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/?demo=1&scenario=active&freeze=1&theme=system");
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme-mode",
    "system",
  );
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("grid themes produce visually distinct office canvas palettes", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop canvas sampling assertion");
  await page.setViewportSize({ width: 1800, height: 1000 });

  await page.goto("/?demo=1&scenario=empty&freeze=1&theme=dark");
  const gridDark = await sampleOfficeMap(page);
  await page.goto("/?demo=1&scenario=empty&freeze=1&theme=light");
  const gridLight = await sampleOfficeMap(page);

  expect(gridDark.floors.war[0]).toBeLessThan(40);
  expect(gridDark.floors.war[1]).toBeLessThan(70);
  expect(gridDark.floors.war[2]).toBeLessThan(95);

  expect(gridLight.floors.war[2]).toBeGreaterThan(140);
  expect(
    colorDistance(gridLight.floors.war, gridDark.floors.war),
  ).toBeGreaterThan(180);
});

test("grid themes keep floorplan screen coordinates unchanged", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop canvas check");
  await page.setViewportSize({ width: 1800, height: 1000 });

  const readCameraSample = () =>
    page.evaluate(() => {
      const testWindow = window as unknown as {
        __agentshoreDashboardTest?: {
          camera: {
            worldToScreen: (x: number, y: number) => [number, number];
            projectionMode: string;
          };
        };
      };
      const camera = testWindow.__agentshoreDashboardTest?.camera;
      if (!camera) throw new Error("camera hook unavailable");
      return {
        mode: camera.projectionMode,
        coords: camera.worldToScreen(400, 400),
      };
    });

  await page.goto("/?demo=1&scenario=empty&freeze=1&theme=light");
  await expectCanvasNonBlank(page);
  const gridLightCoords = await readCameraSample();

  await page.goto("/?demo=1&scenario=empty&freeze=1&theme=dark");
  await expectCanvasNonBlank(page);
  const gridDarkCoords = await readCameraSample();

  expect(gridLightCoords.mode).toBe("grid");
  expect(gridDarkCoords.mode).toBe("grid");
  const dist = Math.hypot(
    gridLightCoords.coords[0] - gridDarkCoords.coords[0],
    gridLightCoords.coords[1] - gridDarkCoords.coords[1],
  );
  expect(dist).toBeLessThan(1);
});

test("screenToWorld round-trips for grid theme floorplan", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop canvas check");
  await page.setViewportSize({ width: 1800, height: 1000 });
  await page.goto("/?demo=1&scenario=empty&freeze=1&theme=dark");
  await expectCanvasNonBlank(page);

  const roundTripError = await page.evaluate(() => {
    const testWindow = window as unknown as {
      __agentshoreDashboardTest?: {
        camera: {
          worldToScreen: (x: number, y: number) => [number, number];
          screenToWorld: (x: number, y: number) => [number, number];
        };
      };
    };
    const camera = testWindow.__agentshoreDashboardTest?.camera;
    if (!camera) throw new Error("camera hook unavailable");
    const [sx, sy] = camera.worldToScreen(400, 600);
    const [wx, wy] = camera.screenToWorld(sx, sy);
    return Math.hypot(wx - 400, wy - 600);
  });

  expect(roundTripError).toBeLessThan(1);
});

test("Events tab renders merged lifecycle cards", async ({ page }) => {
  await page.goto("/?demo=1&scenario=active&freeze=1");

  const firstCard = page.locator("#event-list .event-card").first();
  await expect(firstCard).not.toContainText("Name");
  await expect(firstCard).not.toContainText("Type");
  await expect(firstCard).not.toContainText("Status/Result");
  await expect(firstCard).toContainText("Claude");
  await expect(firstCard).toContainText("Issue Pickup");
  await expect(firstCard).toContainText("Running");
  await expect(firstCard.locator(".event-time-range")).toHaveText(
    /^\d{2}:\d{2} -$/,
  );
});

test("bottom strip only shows active play and progress moved to stats tab", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop side panel assertion");
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await expect(page.locator("#bottom-bar #play-bar")).toBeVisible();
  await expect(page.locator("#bottom-bar #alignment-bar")).toHaveCount(0);
  await expect(page.locator("#side-panel #alignment-section")).toHaveCount(0);
  await expect(page.locator("#side-panel #epic-section")).toHaveCount(0);
  await page.getByRole("tab", { name: /Stats/ }).click();
  await expect(page.locator("#stats-stage")).toBeVisible();
  await expect(page.locator("#stats-stage")).toContainText("Alignment");
  await expect(page.locator("#stats-stage")).toContainText(
    "Authentication System",
  );
  await expect(
    page.locator("#stats-stage .cluster-name", {
      hasText: "Authentication System",
    }),
  ).toBeVisible();
  await expect(
    page.locator("#stats-stage .epic-name", {
      hasText: "Authentication System",
    }),
  ).toBeVisible();
});

test("stats tab toggles center stage and preserves agent-only right panel", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop stats tab assertion");
  await page.goto("/?demo=1&scenario=active&freeze=1");

  await page.getByRole("tab", { name: /Stats/ }).click();

  await expect(page.locator('.stage-tab[data-mode="stats"]')).toHaveClass(
    /active/,
  );
  await expect(page.locator("#stats-stage")).toBeVisible();
  await expect(page.locator("#office")).not.toBeVisible();
  await expect(page.locator("#kanban-stage")).toBeHidden();
  await expect(page.locator("#side-panel")).toContainText("Agents");
  await expect(page.locator("#side-panel")).not.toContainText("Alignment");
  await expect(page.locator("#side-panel")).not.toContainText("Epics");
  await expect(page.locator("#stats-stage")).toContainText("Epics");
});

test("stats tab aligns alignment and epics headers", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop stats layout assertion");
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await page.getByRole("tab", { name: /Stats/ }).click();

  const alignmentHeader = page
    .locator("#stats-stage .stats-grid > .stats-section")
    .nth(0)
    .locator("h2");
  const epicsHeader = page
    .locator("#stats-stage .stats-grid > .stats-section")
    .nth(1)
    .locator("h2");
  await expect(alignmentHeader).toHaveText("Alignment");
  await expect(epicsHeader).toHaveText("Epics");

  const alignmentBox = await alignmentHeader.boundingBox();
  const epicsBox = await epicsHeader.boundingBox();
  expect(alignmentBox).not.toBeNull();
  expect(epicsBox).not.toBeNull();
  expect(Math.abs(alignmentBox!.y - epicsBox!.y)).toBeLessThanOrEqual(1);
});

test("stats tab renders full-session play success and failure counts", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop stats table assertion");
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await page.getByRole("tab", { name: /Stats/ }).click();

  await expect(page.locator("#stats-stage")).toContainText("9 ok / 3 failed");
  await expect(page.locator("#stats-stage")).toContainText("Issue Pickup");
  await expect(page.locator("#stats-stage")).toContainText("Code Review");
  await expect(page.locator("#stats-stage")).toContainText("Run QA");
  await expect(page.locator("#stats-stage")).toContainText("80%");
});

test("kanban renders bead epic legend and mirror badges", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop kanban assertion");
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await page.getByRole("tab", { name: /Kanban/ }).click();
  await expect(page.locator(".km-legend")).toContainText(
    "Authentication System",
  );
  await expect(
    page
      .locator(".km-card")
      .filter({ hasText: "Implement session budget guard" }),
  ).toContainText("mirrored");
  await expect(
    page
      .locator(".km-card")
      .filter({ hasText: "Implement session budget guard" }),
  ).toContainText("ready");
});

test("kanban cards render two truncated rows", async ({ page, isMobile }) => {
  test.skip(isMobile, "desktop kanban assertion");
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await page.getByRole("tab", { name: /Kanban/ }).click();

  const card = page
    .locator(".km-card")
    .filter({ hasText: "Implement session budget guard" })
    .first();
  await expect(card.locator(".km-card-title")).toHaveCSS(
    "text-overflow",
    "ellipsis",
  );
  await expect(card.locator(".km-card-title")).toHaveCSS(
    "white-space",
    "nowrap",
  );
  await expect(card.locator(".km-card-tags")).toHaveCSS(
    "text-overflow",
    "ellipsis",
  );
  await expect(card.locator(".km-card-tags")).toHaveCSS(
    "white-space",
    "nowrap",
  );

  const metrics = await card.evaluate((el) => {
    const title = el.querySelector(".km-card-title") as HTMLElement | null;
    const tags = el.querySelector(".km-card-tags") as HTMLElement | null;
    if (!title || !tags) throw new Error("card rows missing");
    return {
      rowCount: el.querySelectorAll(".km-card-title, .km-card-tags").length,
      cardHeight: el.getBoundingClientRect().height,
    };
  });
  expect(metrics.rowCount).toBe(2);
  expect(metrics.cardHeight).toBeLessThanOrEqual(44);
});

test("kanban card opens issue detail modal with GitHub link", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop kanban assertion");
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await page.getByRole("tab", { name: /Kanban/ }).click();

  await page
    .locator(".km-card")
    .filter({ hasText: "Implement session budget guard" })
    .first()
    .click();
  await expect(page.locator("#issue-detail-modal")).toHaveClass(/visible/);
  await expect(page.locator("#issue-detail-title")).toContainText(
    "Implement session budget guard",
  );
  await expect(page.locator("#issue-detail-body")).toContainText(
    "needs-dashboard-review",
  );
  await expect(page.locator("#issue-detail-body")).toContainText(
    "Authentication System",
  );
  await expect(page.locator("#issue-detail-body")).toContainText(
    "Current Signals",
  );
  await expect(page.locator("#issue-detail-body")).toContainText(
    "Issue #47 is Open",
  );
  await expect(page.locator("#issue-detail-body")).toContainText(
    "Bead task-47 is Open",
  );
  await expect(page.getByRole("button", { name: "Copy issue #" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Copy branch" })).toBeDisabled();
  await expect(page.locator("#issue-detail-open-github")).toHaveAttribute(
    "href",
    "https://github.com/example/agentshore/issues/47",
  );

  await page.keyboard.press("Escape");
  await expect(page.locator("#issue-detail-modal")).not.toHaveClass(/visible/);

  await page
    .locator(".km-card")
    .filter({ hasText: "Implement session budget guard" })
    .first()
    .click();
  await page.locator("#issue-detail-close").click();
  await expect(page.locator("#issue-detail-modal")).not.toHaveClass(/visible/);
});

test("kanban issue detail modal remains readable in light and dark themes", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop modal color assertion");

  for (const theme of ["light", "dark"] as const) {
    await page.goto("/?demo=1&scenario=active&freeze=1");
    await page.locator(`#theme-toggle [data-theme-mode="${theme}"]`).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await page.getByRole("tab", { name: /Kanban/ }).click();

    await page
      .locator(".km-card")
      .filter({ hasText: "Implement session budget guard" })
      .first()
      .click();
    await expect(page.locator("#issue-detail-modal")).toHaveClass(/visible/);

    const colors = await page.locator(".issue-detail-box").evaluate((modal) => {
      const title = modal.querySelector(".issue-detail-title");
      if (!(title instanceof HTMLElement)) {
        throw new Error("issue detail title missing");
      }
      const modalStyle = getComputedStyle(modal);
      const titleStyle = getComputedStyle(title);
      return {
        background: modalStyle.backgroundColor,
        borderColor: modalStyle.borderTopColor,
        titleColor: titleStyle.color,
      };
    });
    expect(colors.background).not.toBe("rgba(0, 0, 0, 0)");
    expect(colors.borderColor).not.toBe("rgba(0, 0, 0, 0)");
    expect(colors.titleColor).not.toBe(colors.background);

    await page.keyboard.press("Escape");
    await expect(page.locator("#issue-detail-modal")).not.toHaveClass(/visible/);
  }
});

test("kanban stage drag does not open issue detail modal", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop kanban assertion");
  await page.goto("/?demo=1&scenario=active&freeze=1");
  await page.getByRole("tab", { name: /Kanban/ }).click();

  const cardBox = await page.locator(".km-card").first().boundingBox();
  if (!cardBox) throw new Error("card missing");
  const x = cardBox.x + cardBox.width / 2;
  const y = cardBox.y + cardBox.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 28, y + 2);
  await page.mouse.up();

  await expect(page.locator("#issue-detail-modal")).not.toHaveClass(/visible/);
});

test("wall depth rendering distinguishes back walls and open doorways", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop canvas sampling assertion");
  await page.setViewportSize({ width: 1800, height: 1000 });
  await page.goto("/?demo=1&scenario=empty&freeze=1&theme=dark");
  await expectCanvasNonBlank(page);

  const samples = await page
    .locator("#office")
    .evaluate(async (canvasElement) => {
      const layout = await import("/src/office/layout.ts");
      const canvas = canvasElement as HTMLCanvasElement;
      const ctx = canvas.getContext("2d");
      if (!ctx) throw new Error("2D canvas context unavailable");

      const testWindow = window as unknown as {
        __agentshoreDashboardTest?: {
          camera: {
            worldToScreen: (
              x: number,
              y: number,
              z?: number,
            ) => [number, number];
          };
        };
      };
      const camera = testWindow.__agentshoreDashboardTest?.camera;
      if (!camera) throw new Error("camera test hook unavailable");

      function sampleTile(
        tileX: number,
        tileY: number,
        offsetX = 0.5,
        offsetY = 0.5,
        z = 0,
      ): Rgb {
        const [sx, sy] = camera.worldToScreen(
          (tileX + offsetX) * layout.TILE_SIZE,
          (tileY + offsetY) * layout.TILE_SIZE,
          z,
        );
        const px = Math.floor(sx);
        const py = Math.floor(sy);
        const data = ctx.getImageData(px, py, 1, 1).data;
        return [data[0], data[1], data[2]];
      }

      return {
        backWall: sampleTile(
          7,
          6,
          0.5,
          layout.WALL_THICKNESS_UNITS,
          layout.BACK_WALL_HEIGHT_UNITS / 2,
        ),
        warFloor: sampleTile(7, 14),
        door: sampleTile(21, 13),
        workshopFloor: sampleTile(22, 13),
        gardenDoor: sampleTile(34, 40),
        gardenThresholdFloor: sampleTile(34, 39),
        gardenWallBreak: sampleTile(34, 41),
        gardenFloor: sampleTile(34, 44),
      };
    });

  expect(colorDistance(samples.backWall, samples.warFloor)).toBeGreaterThan(30);
  expect(colorDistance(samples.door, [240, 195, 91])).toBeGreaterThan(100);
  expect(colorDistance(samples.door, samples.workshopFloor)).toBeLessThan(5);
  expect(
    colorDistance(samples.gardenDoor, samples.gardenThresholdFloor),
  ).toBeLessThan(5);
  expect(
    colorDistance(samples.gardenWallBreak, samples.gardenFloor),
  ).toBeLessThan(5);
});

test("blueprint layout targets are walkable and route through the Workshop", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const layout = await import("/src/office/layout.ts");
    const pathfinding = await import("/src/office/pathfinding.ts");
    pathfinding.buildWalkableGrid();

    const workZoneIds = [
      layout.ZoneId.WAR_ROOM,
      layout.ZoneId.WORKSHOP,
      layout.ZoneId.EDITORS_DESK,
      layout.ZoneId.LAUNCH_CONTROL,
      layout.ZoneId.RECOVERY_BAY,
      layout.ZoneId.SCIENCE_LAB,
    ];
    const targetCounts = {
      warRoom: layout.getZone(layout.ZoneId.WAR_ROOM).seats.length,
      editorRoom: layout.getZone(layout.ZoneId.EDITORS_DESK).seats.length,
      workshop: layout.getZone(layout.ZoneId.WORKSHOP).seats.length,
      zenGarden: layout.getZone(layout.ZoneId.ZEN_GARDEN).seats.length,
      frontDesk: layout.getZone(layout.ZoneId.FRONT_DESK).seats.length,
      launchControl: layout.getZone(layout.ZoneId.LAUNCH_CONTROL).seats.length,
      recoveryBay: layout.getZone(layout.ZoneId.RECOVERY_BAY).seats.length,
    };
    const workshopSeats = layout.getZone(layout.ZoneId.WORKSHOP).seats;
    const markedWorkshopSeats = [
      { x: 27, y: 22, facing: "north" },
      { x: 32, y: 35, facing: "west" },
      { x: 44, y: 34, facing: "east" },
    ].every((expected) =>
      workshopSeats.some(
        (seat) =>
          seat.x === expected.x &&
          seat.y === expected.y &&
          seat.facing === expected.facing,
      ),
    );
    const oldWorkshopSeat = workshopSeats.some(
      (seat) => seat.x === 52 && seat.y === 30 && seat.facing === "east",
    );
    const markedRecoverySeat = layout
      .getZone(layout.ZoneId.RECOVERY_BAY)
      .seats.some(
        (seat) => seat.x === 9 && seat.y === 48 && seat.facing === "east",
      );

    const targetFailures: string[] = [];
    for (const zone of layout.ZONES) {
      for (const seat of zone.seats) {
        if (!pathfinding.isWalkable(seat.x, seat.y)) {
          targetFailures.push(`${zone.name}:${seat.x},${seat.y}`);
        }
      }
    }
    if (
      !pathfinding.isWalkable(
        layout.FRONT_DESK_EXIT.x,
        layout.FRONT_DESK_EXIT.y,
      )
    ) {
      targetFailures.push(
        `FRONT EXIT:${layout.FRONT_DESK_EXIT.x},${layout.FRONT_DESK_EXIT.y}`,
      );
    }

    const furnitureFailures: string[] = [];
    const catFurnitureFailures: string[] = [];
    const sideBufferFailures: string[] = [];
    for (const rect of layout.FURNITURE) {
      for (let y = rect.y; y < rect.y + rect.h; y++) {
        for (let x = rect.x; x < rect.x + rect.w; x++) {
          if (pathfinding.isWalkable(x, y)) {
            furnitureFailures.push(`${rect.name}:${x},${y}`);
          }
          if (!pathfinding.isWalkableIgnoringFurniture(x, y)) {
            catFurnitureFailures.push(`${rect.name}:${x},${y}`);
          }
        }
        for (const x of [rect.x - 1, rect.x + rect.w]) {
          if (
            layout.isFurnitureSideBuffer(x, y) &&
            pathfinding.isWalkable(x, y)
          ) {
            sideBufferFailures.push(`${rect.name}:${x},${y}`);
          }
        }
      }
    }

    const sources = [
      { name: "front", tile: layout.FRONT_DESK_SPAWN_SPOTS[0] },
      { name: "zen", tile: layout.getZone(layout.ZoneId.ZEN_GARDEN).seats[0] },
    ];
    const reachabilityFailures: string[] = [];
    const workshopRouteFailures: string[] = [];
    const routeClearanceFailures: string[] = [];
    const doorEdgeRouteFailures: string[] = [];
    for (const source of sources) {
      for (const zoneId of workZoneIds) {
        const zone = layout.getZone(zoneId);
        const path = pathfinding.bfsPath(source.tile, zone.seats[0]);
        if (path.length === 0) {
          reachabilityFailures.push(`${source.name}->${zone.name}`);
          continue;
        }
        const touchesWorkshop =
          zoneId === layout.ZoneId.WORKSHOP ||
          path.some(
            (tile) =>
              layout.zoneMap[tile.y]?.[tile.x] === layout.ZoneId.WORKSHOP,
          );
        if (!touchesWorkshop) {
          workshopRouteFailures.push(`${source.name}->${zone.name}`);
        }
        const sideBufferStep = path.find((tile) =>
          layout.isFurnitureSideBuffer(tile.x, tile.y),
        );
        if (sideBufferStep) {
          routeClearanceFailures.push(
            `${source.name}->${zone.name}:${sideBufferStep.x},${sideBufferStep.y}`,
          );
        }
        const doorEdgeStep = path.find((tile) =>
          layout.isDoorEdgeBuffer(tile.x, tile.y),
        );
        if (doorEdgeStep) {
          doorEdgeRouteFailures.push(
            `${source.name}->${zone.name}:${doorEdgeStep.x},${doorEdgeStep.y}`,
          );
        }
      }
    }

    return {
      targetCounts,
      markedWorkshopSeats,
      oldWorkshopSeat,
      markedRecoverySeat,
      targetFailures,
      furnitureFailures,
      catFurnitureFailures,
      sideBufferFailures,
      reachabilityFailures,
      workshopRouteFailures,
      routeClearanceFailures,
      doorEdgeRouteFailures,
    };
  });

  expect(result.targetCounts).toEqual({
    warRoom: 4,
    editorRoom: 4,
    workshop: 17,
    zenGarden: 4,
    frontDesk: 3,
    launchControl: 4,
    recoveryBay: 4,
  });
  expect(result.markedWorkshopSeats).toBe(true);
  expect(result.oldWorkshopSeat).toBe(false);
  expect(result.markedRecoverySeat).toBe(true);
  expect(result.targetFailures).toEqual([]);
  expect(result.furnitureFailures).toEqual([]);
  expect(result.catFurnitureFailures).toEqual([]);
  expect(result.sideBufferFailures).toEqual([]);
  expect(result.reachabilityFailures).toEqual([]);
  expect(result.workshopRouteFailures).toEqual([]);
  expect(result.routeClearanceFailures).toEqual([]);
  expect(result.doorEdgeRouteFailures).toEqual([]);
});

test("idle wander bounces inside the current room instead of crossing edges", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const layout = await import("/src/office/layout.ts");
    const pathfinding = await import("/src/office/pathfinding.ts");
    const stateMachine = await import("/src/characters/stateMachine.ts");
    const types = await import("/src/characters/types.ts");
    const originalRandom = Math.random;

    function walkFrom(tile: { x: number; y: number }, randomValues: number[]) {
      let index = 0;
      Math.random = () => randomValues[index++] ?? 0.99;
      const char = {
        agentId: `test-${tile.x}-${tile.y}`,
        agentType: "codex",
        state: types.CharacterState.IDLE,
        direction: types.Direction.DOWN,
        x: tile.x * layout.TILE_SIZE + layout.TILE_SIZE / 2,
        y: tile.y * layout.TILE_SIZE + layout.TILE_SIZE / 2,
        path: [],
        pathIndex: 0,
        targetState: types.CharacterState.IDLE,
        animFrame: 0,
        animTimer: 0,
        wanderTimer: 0,
        reservedSeatKey: null,
        status: "idle",
        bubble: null,
        bubbleUntil: null,
        opacity: 1,
        despawning: false,
        despawnOnArrival: false,
      };

      stateMachine.updateCharacter(char, 0.1);
      return {
        path: char.path,
        zones: char.path.map((step) => layout.zoneMap[step.y]?.[step.x]),
      };
    }

    try {
      pathfinding.buildWalkableGrid();
      return {
        doorBounce: walkFrom({ x: 19, y: 13 }, [0.99, 0.99, 0.99, 0.99]),
        wallBounce: walkFrom({ x: 5, y: 13 }, [0.99, 0.99, 0, 0.99]),
        warRoom: layout.ZoneId.WAR_ROOM,
        workshop: layout.ZoneId.WORKSHOP,
      };
    } finally {
      Math.random = originalRandom;
    }
  });

  expect(result.doorBounce.path.at(-1)).toEqual({ x: 17, y: 13 });
  expect(result.doorBounce.zones).toEqual([result.warRoom, result.warRoom]);
  expect(result.doorBounce.zones).not.toContain(result.workshop);
  expect(result.wallBounce.path.at(-1)).toEqual({ x: 7, y: 13 });
  expect(result.wallBounce.zones).toEqual([result.warRoom, result.warRoom]);
});

test("planning and strategy plays route to dedicated work rooms", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const layout = await import("/src/office/layout.ts");
    const zones = await import("/src/office/zones.ts");

    return {
      writeImplementationPlan: zones.PLAY_TO_ZONE.write_implementation_plan,
      seedProject: zones.PLAY_TO_ZONE.seed_project,
      groomBacklog: zones.PLAY_TO_ZONE.groom_backlog,
      calibrateAlignment: zones.PLAY_TO_ZONE.calibrate_alignment,
      designAudit: zones.PLAY_TO_ZONE.design_audit,
      warRoom: layout.ZoneId.WAR_ROOM,
      editorsDesk: layout.ZoneId.EDITORS_DESK,
      zenGarden: layout.ZoneId.ZEN_GARDEN,
    };
  });

  expect(result.writeImplementationPlan).toBe(result.editorsDesk);
  expect(result.writeImplementationPlan).not.toBe(result.zenGarden);
  expect(result.seedProject).toBe(result.warRoom);
  expect(result.groomBacklog).toBe(result.warRoom);
  expect(result.calibrateAlignment).toBe(result.warRoom);
  expect(result.designAudit).toBe(result.editorsDesk);
});

test("all active play room mappings are explicit", async ({ page }) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const layout = await import("/src/office/layout.ts");
    const zones = await import("/src/office/zones.ts");
    const expectedMappings = {
      unblock_pr: layout.ZoneId.WORKSHOP,
      instantiate_agent: layout.ZoneId.FRONT_DESK,
      end_agent: layout.ZoneId.FRONT_DESK,
      end_session: layout.ZoneId.FRONT_DESK,
      issue_pickup: layout.ZoneId.WORKSHOP,
      code_review: layout.ZoneId.EDITORS_DESK,
      merge_pr: layout.ZoneId.LAUNCH_CONTROL,
      run_qa: layout.ZoneId.SCIENCE_LAB,
      systematic_debugging: layout.ZoneId.WORKSHOP,
      design_audit: layout.ZoneId.EDITORS_DESK,
      write_implementation_plan: layout.ZoneId.EDITORS_DESK,
      refine_task_breakdown: layout.ZoneId.WAR_ROOM,
      cleanup: layout.ZoneId.WORKSHOP,
      reconcile_state: layout.ZoneId.RECOVERY_BAY,
      take_break: layout.ZoneId.RECOVERY_BAY,
      seed_project: layout.ZoneId.WAR_ROOM,
      groom_backlog: layout.ZoneId.WAR_ROOM,
      calibrate_alignment: layout.ZoneId.WAR_ROOM,
    };
    const frontDeskLifecycle = new Set([
      "instantiate_agent",
      "end_agent",
      "end_session",
    ]);
    const countsByZone = Object.fromEntries(
      [
        layout.ZoneId.WAR_ROOM,
        layout.ZoneId.WORKSHOP,
        layout.ZoneId.SCIENCE_LAB,
        layout.ZoneId.LAUNCH_CONTROL,
        layout.ZoneId.EDITORS_DESK,
        layout.ZoneId.RECOVERY_BAY,
      ].map((zoneId) => [zoneId, 0]),
    );
    for (const [playType, zoneId] of Object.entries(expectedMappings)) {
      if (!frontDeskLifecycle.has(playType)) {
        countsByZone[zoneId] += 1;
      }
    }

    return {
      expectedMappings,
      actualMappings: Object.fromEntries(
        Object.keys(expectedMappings).map((playType) => [
          playType,
          zones.PLAY_TO_ZONE[playType],
        ]),
      ),
      countsByZone,
      reservedStayPut: [...zones.PLAY_RESERVED].every((playType) =>
        zones.CURRENT_LOCATION_PLAY_TYPES.has(playType),
      ),
      currentLocationOnlyReserved: [...zones.CURRENT_LOCATION_PLAY_TYPES].every(
        (playType) => zones.PLAY_RESERVED.has(playType),
      ),
      takeBreakCanRouteToRecovery: zones.RECOVERY_PLAY_TYPES.has("take_break"),
      recoveryBayPlayCount: countsByZone[layout.ZoneId.RECOVERY_BAY] ?? 0,
    };
  });

  expect(result.actualMappings).toEqual(result.expectedMappings);
  for (const count of Object.values(result.countsByZone)) {
    expect(count).toBeGreaterThanOrEqual(1);
    expect(count).toBeLessThanOrEqual(4);
  }
  expect(result.reservedStayPut).toBe(true);
  expect(result.currentLocationOnlyReserved).toBe(true);
  expect(result.takeBreakCanRouteToRecovery).toBe(true);
  expect(result.recoveryBayPlayCount).toBe(2);
});

test("failed and error-triggered agents route to recovery while healthy idle returns to Zen", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { AgentShoreStateManager } = await import("/src/state.ts");
    const layout = await import("/src/office/layout.ts");
    const stateMachine = await import("/src/characters/stateMachine.ts");
    stateMachine.__testHooks.clearOccupiedSeats();

    const baseAgent = {
      agent_type: "codex",
      status: "idle",
      context_size: 0,
      total_cost: 0,
      total_tokens: 0,
      tasks_completed: 0,
      tasks_failed: 0,
      model: null,
      model_tier: null,
      reasoning_effort: null,
      current_play: null,
    };

    function state(agentId: string, patch = {}) {
      return {
        type: "state_update",
        session_id: `recovery-routing-${agentId}`,
        session_state: "running",
        policy_mode: "learning",
        total_plays: 0,
        total_cost: 0,
        agents: [{ ...baseAgent, ...patch, agent_id: agentId }],
        open_issues: [],
        pull_requests: [],
        budget: null,
        trajectory: null,
        active_play: null,
        stats: null,
        same_type_failure_streak: 0,
        last_play_type: null,
        forced_mask_zeros: [],
        action_mask: Array(22).fill(true),
        mask_reasons: {},
      } as never;
    }

    type TestManager = {
      getAgents: () => Array<{
        agentId: string;
        path: Array<{ x: number; y: number }>;
        reservedSeatKey: string | null;
        targetState: string;
      }>;
    };

    function zoneFor(manager: TestManager, agentId: string): number | null {
      const char = manager
        .getAgents()
        .find((agent) => agent.agentId === agentId);
      const tile = char?.reservedSeatKey
        ? (() => {
            const [x, y] = char.reservedSeatKey.split(",").map(Number);
            return { x, y };
          })()
        : char?.path[char.path.length - 1];
      if (!tile) return null;
      const { x, y } = tile;
      return layout.zoneMap[y]?.[x] ?? null;
    }

    function targetStateFor(
      manager: TestManager,
      agentId: string,
    ): string | null {
      return (
        manager.getAgents().find((agent) => agent.agentId === agentId)
          ?.targetState ?? null
      );
    }

    const failed = new AgentShoreStateManager();
    failed.handleMessage(state("failed-agent"));
    failed.handleMessage({
      type: "play_event",
      status: "started",
      play_type: "issue_pickup",
      agent_id: "failed-agent",
      play_id: 1,
    } as never);
    failed.handleMessage({
      type: "play_event",
      status: "failed",
      play_type: "issue_pickup",
      agent_id: "failed-agent",
      success: false,
      duration_seconds: 1,
      dollar_cost: 0,
      token_cost: 0,
      artifacts: [],
      alignment_delta: 0,
      error: "boom",
      play_id: 1,
    } as never);

    const error = new AgentShoreStateManager();
    error.handleMessage(state("error-agent"));
    error.handleMessage({
      type: "agent_changed",
      agent_id: "error-agent",
      status: "error",
    } as never);

    const success = new AgentShoreStateManager();
    success.handleMessage(state("success-agent"));
    success.handleMessage({
      type: "play_event",
      status: "started",
      play_type: "issue_pickup",
      agent_id: "success-agent",
      play_id: 2,
    } as never);
    success.handleMessage({
      type: "play_event",
      status: "completed",
      play_type: "issue_pickup",
      agent_id: "success-agent",
      success: true,
      duration_seconds: 1,
      dollar_cost: 0,
      token_cost: 0,
      artifacts: [],
      alignment_delta: 0,
      error: null,
      play_id: 2,
    } as never);

    const cooldown = new AgentShoreStateManager();
    cooldown.handleMessage(state("cooldown-agent"));
    cooldown.handleMessage({
      type: "play_event",
      status: "started",
      play_type: "take_break",
      agent_id: "cooldown-agent",
      play_id: 3,
      trigger_agent_id: "cooldown-agent",
      trigger_agent_type: "codex",
      trigger_error_class: "rate_limit",
    } as never);

    const manualBreak = new AgentShoreStateManager();
    manualBreak.handleMessage(state("manual-agent"));
    manualBreak.handleMessage({
      type: "play_event",
      status: "started",
      play_type: "take_break",
      agent_id: "manual-agent",
      play_id: 4,
    } as never);

    return {
      failedZone: zoneFor(failed, "failed-agent"),
      errorZone: zoneFor(error, "error-agent"),
      successZone: zoneFor(success, "success-agent"),
      cooldownZone: zoneFor(cooldown, "cooldown-agent"),
      manualBreakZone: zoneFor(manualBreak, "manual-agent"),
      failedTargetState: targetStateFor(failed, "failed-agent"),
      errorTargetState: targetStateFor(error, "error-agent"),
      manualBreakTargetState: targetStateFor(manualBreak, "manual-agent"),
      recoveryBay: layout.ZoneId.RECOVERY_BAY,
      zenGarden: layout.ZoneId.ZEN_GARDEN,
    };
  });

  expect(result.failedZone).toBe(result.recoveryBay);
  expect(result.errorZone).toBe(result.recoveryBay);
  expect(result.cooldownZone).toBe(result.recoveryBay);
  expect(result.successZone).toBe(result.zenGarden);
  expect(result.manualBreakZone).toBe(result.recoveryBay);
  expect(result.failedTargetState).toBe("idle");
  expect(result.errorTargetState).toBe("idle");
  expect(result.manualBreakTargetState).toBe("idle");
});

test("grid office themes expose NE Launch and SW Recovery room semantics", async ({
  page,
}) => {
  const themes = ["light", "dark"];
  const results: Record<string, unknown> = {};

  for (const theme of themes) {
    await page.goto(`/?demo=1&scenario=empty&freeze=1&theme=${theme}`);
    results[theme] = await page.locator("#office").evaluate(async () => {
      const layout = await import("/src/office/layout.ts");
      return {
        launchZone: layout.zoneMap[10]?.[60],
        recoveryZone: layout.zoneMap[40]?.[9],
        launchName: layout.getZone(layout.ZoneId.LAUNCH_CONTROL).name,
        recoveryName: layout.getZone(layout.ZoneId.RECOVERY_BAY).name,
        launchSeats: layout.getZone(layout.ZoneId.LAUNCH_CONTROL).seats.length,
        recoverySeats: layout.getZone(layout.ZoneId.RECOVERY_BAY).seats.length,
        launchControl: layout.ZoneId.LAUNCH_CONTROL,
        recoveryBay: layout.ZoneId.RECOVERY_BAY,
      };
    });
  }

  for (const result of Object.values(results) as Array<{
    launchZone: number;
    recoveryZone: number;
    launchName: string;
    recoveryName: string;
    launchSeats: number;
    recoverySeats: number;
    launchControl: number;
    recoveryBay: number;
  }>) {
    expect(result.launchZone).toBe(result.launchControl);
    expect(result.recoveryZone).toBe(result.recoveryBay);
    expect(result.launchName).toBe("LAUNCH CONTROL");
    expect(result.recoveryName).toBe("RECOVERY BAY");
    expect(result.launchSeats).toBe(4);
    expect(result.recoverySeats).toBe(3);
  }
});

test("state snapshots route current-play agents to rooms when status lags", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { AgentShoreStateManager } = await import("/src/state.ts");
    const layout = await import("/src/office/layout.ts");
    const stateMachine = await import("/src/characters/stateMachine.ts");
    stateMachine.__testHooks.clearOccupiedSeats();

    const manager = new AgentShoreStateManager();
    const baseAgent = {
      status: "idle",
      context_size: 0,
      total_cost: 0,
      total_tokens: 0,
      tasks_completed: 0,
      tasks_failed: 0,
      model: null,
      model_tier: null,
      reasoning_effort: null,
    };
    manager.handleMessage({
      type: "state_update",
      session_id: "snapshot-routing",
      session_state: "running",
      policy_mode: "learning",
      total_plays: 2,
      total_cost: 0,
      agents: [
        {
          ...baseAgent,
          agent_id: "review-agent",
          agent_type: "gemini",
          current_play: {
            play_type: "code_review",
            play_id: 248,
            started_at: "2026-01-01T00:00:00.000Z",
            issue_number: null,
            pr_number: 248,
            branch: null,
          },
        },
        {
          ...baseAgent,
          agent_id: "qa-agent",
          agent_type: "codex",
          current_play: {
            play_type: "run_qa",
            play_id: 249,
            started_at: "2026-01-01T00:00:00.000Z",
            issue_number: null,
            pr_number: 249,
            branch: null,
          },
        },
      ],
      open_issues: [],
      pull_requests: [],
      budget: null,
      trajectory: null,
      active_play: null,
      stats: null,
      same_type_failure_streak: 0,
      last_play_type: null,
      forced_mask_zeros: [],
      action_mask: Array(22).fill(true),
      mask_reasons: {},
    } as never);

    function zoneFor(agentId: string): number | null {
      const char = manager
        .getAgents()
        .find((agent) => agent.agentId === agentId);
      if (!char?.reservedSeatKey) return null;
      const [x, y] = char.reservedSeatKey.split(",").map(Number);
      return layout.zoneMap[y]?.[x] ?? null;
    }

    return {
      reviewZone: zoneFor("review-agent"),
      qaZone: zoneFor("qa-agent"),
      reviewTargetState: manager
        .getAgents()
        .find((agent) => agent.agentId === "review-agent")?.targetState,
      qaTargetState: manager
        .getAgents()
        .find((agent) => agent.agentId === "qa-agent")?.targetState,
      reviewStatus: manager
        .getAgents()
        .find((agent) => agent.agentId === "review-agent")?.status,
      qaStatus: manager
        .getAgents()
        .find((agent) => agent.agentId === "qa-agent")?.status,
      editorsDesk: layout.ZoneId.EDITORS_DESK,
      scienceLab: layout.ZoneId.SCIENCE_LAB,
    };
  });

  expect(result.reviewZone).toBe(result.editorsDesk);
  expect(result.qaZone).toBe(result.scienceLab);
  expect(result.reviewTargetState).toBe("work");
  expect(result.qaTargetState).toBe("work");
  expect(result.reviewStatus).toBe("busy");
  expect(result.qaStatus).toBe("busy");
});

test("room seat selection picks from available spots randomly", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const layout = await import("/src/office/layout.ts");
    const stateMachine = await import("/src/characters/stateMachine.ts");
    const seats = layout.getZone(layout.ZoneId.WAR_ROOM).seats;
    const random = Math.random;
    stateMachine.__testHooks.clearOccupiedSeats();

    try {
      Math.random = () => 0.99;
      const lastSeat = stateMachine.__testHooks.reserveSeat(seats).seat;
      Math.random = () => 0;
      const firstRemainingSeat =
        stateMachine.__testHooks.reserveSeat(seats).seat;

      return { firstSeat: seats[0], lastSeat, firstRemainingSeat };
    } finally {
      Math.random = random;
    }
  });

  expect(result.lastSeat).not.toEqual(result.firstSeat);
  expect(result.firstRemainingSeat).toEqual(result.firstSeat);
});

test.afterEach(async ({ page }) => {
  await page
    .evaluate(async () => {
      const stateMachine = await import("/src/characters/stateMachine.ts");
      stateMachine.__testHooks.clearOccupiedSeats();
    })
    .catch(() => undefined);
});

test("feedback scenario shows feedback modal", async ({ page }) => {
  await page.goto("/?demo=1&scenario=feedback&freeze=1");
  const modal = page.locator("#react-root #feedback-modal");
  await expect(modal).toHaveClass(/visible/);
  await expect(modal.locator("#feedback-reason")).toContainText(
    "Budget exhaustion predicted",
  );
});

test("feedback modal frame contains every action button", async ({ page }) => {
  await page.goto("/?demo=1&scenario=feedback&freeze=1");
  const modal = page.locator("#react-root #feedback-modal");
  await expect(modal).toHaveClass(/visible/);

  const result = await modal.locator(".modal-box").evaluate(
    (modal) => {
      const modalRect = modal.getBoundingClientRect();
      const buttons = Array.from(
        modal.querySelectorAll<HTMLElement>("#feedback-main-buttons .modal-btn"),
      ).map((button) => {
        const rect = button.getBoundingClientRect();
        return {
          id: button.id,
          left: rect.left,
          right: rect.right,
          top: rect.top,
          bottom: rect.bottom,
        };
      });
      return { modal: modalRect.toJSON(), buttons };
    },
  );

  for (const button of result.buttons) {
    expect(button.left).toBeGreaterThanOrEqual(result.modal.left);
    expect(button.right).toBeLessThanOrEqual(result.modal.right);
    expect(button.top).toBeGreaterThanOrEqual(result.modal.top);
    expect(button.bottom).toBeLessThanOrEqual(result.modal.bottom);
  }
});

test("feedback modal buttons are enabled and Continue dismisses the modal", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=feedback&freeze=1");
  const modal = page.locator("#react-root #feedback-modal");
  await expect(modal).toHaveClass(/visible/);

  await expect(modal.locator("#feedback-continue")).not.toBeDisabled();
  await expect(modal.locator("#feedback-pause")).not.toBeDisabled();
  await expect(modal.locator("#feedback-stop")).not.toBeDisabled();

  await modal.locator("#feedback-continue").click();
  await expect(modal).not.toHaveClass(/visible/);
});

test("disconnected scenario shows reconnect overlay", async ({ page }) => {
  await page.goto("/?demo=1&scenario=disconnected&freeze=1");
  await expect(page.locator("#connection-status")).toContainText(
    "reconnecting",
  );
  await expectCanvasNonBlank(page);
});

test("selected agent details show current play or idle state", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop side panel assertion");
  await page.goto("/?demo=1&scenario=active&freeze=1");

  await page
    .locator('#agent-list .agent-item[data-agent-id="agent-claude"]')
    .click();
  await expect(page.locator("#agent-detail")).toContainText("Current play");
  await expect(page.locator("#agent-detail")).toContainText("Issue Pickup");
  await expect(page.locator("#agent-detail")).toContainText("Issue #47");

  await page
    .locator('#agent-list .agent-item[data-agent-id="agent-codex"]')
    .click();
  await expect(page.locator("#agent-detail")).toContainText("Current play");
  await expect(page.locator("#agent-detail")).toContainText("Idle");
});

test("character click selects an agent detail", async ({ page, isMobile }) => {
  test.skip(isMobile, "desktop side panel detail assertion");
  await page.setViewportSize({ width: 1800, height: 1000 });
  await page.goto("/?demo=1&scenario=active&freeze=1");
  const canvas = page.locator("#office");
  await expectCanvasNonBlank(page);
  const clickPoints = await canvas.evaluate(async (canvasElement) => {
    const layout = await import("/src/office/layout.ts");
    const sprites = await import("/src/characters/sprites.ts");
    const canvas = canvasElement as HTMLCanvasElement;
    const testWindow = window as unknown as {
      __agentshoreDashboardTest?: {
        camera: {
          zoom: number;
          worldToScreen: (x: number, y: number, z?: number) => [number, number];
        };
      };
    };
    const camera = testWindow.__agentshoreDashboardTest?.camera;
    if (!camera) throw new Error("camera test hook unavailable");
    const scale = canvas.width / canvas.getBoundingClientRect().width;
    const agentSize = sprites.agentVisualSize(camera.zoom);
    return layout.FRONT_DESK_SPAWN_SPOTS.map((spawn) => {
      const [sx, sy] = camera.worldToScreen(
        spawn.x * layout.TILE_SIZE + layout.TILE_SIZE / 2,
        spawn.y * layout.TILE_SIZE + layout.TILE_SIZE / 2,
      );
      return {
        x: sx / scale,
        y: (sy - agentSize.height / 2) / scale,
      };
    });
  });
  for (const point of clickPoints) {
    await canvas.click({ position: point });
    if (
      (await page.locator("#agent-detail").textContent())?.includes("Tokens")
    ) {
      break;
    }
  }
  await expect(page.locator("#agent-detail")).toContainText("Tokens");
});

test("clicking selected side-panel agent toggles selection off", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "desktop side panel assertion");
  await page.goto("/?demo=1&scenario=active&freeze=1");

  const firstAgent = page.locator("#agent-list .agent-item").first();
  await firstAgent.click();
  await expect(firstAgent).toHaveClass(/selected/);
  await expect(page.locator("#agent-detail")).toContainText("Tokens");

  await firstAgent.click();
  await expect(firstAgent).not.toHaveClass(/selected/);
  await expect(page.locator("#agent-detail")).toContainText("Select an agent");
});
