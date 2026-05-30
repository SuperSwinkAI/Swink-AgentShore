import { useEffect, useRef, useState } from "react";

import { Camera } from "../engine/camera";
import { OfficeRenderer, type WallSticky } from "../engine/renderer";
import { AgentShoreStateManager } from "../state";
import { updateCharacter } from "../characters/stateMachine";
import { characterScreenBounds } from "../characters/sprites";
import type { ResolvedTheme } from "../theme";
import type { StateUpdate } from "../types";
import { deriveColumns } from "../views/kanban/phase";
import { PHASES } from "../views/kanban/phase";
import { notifySidePanelSelectAgent } from "./SidePanel";

/**
 * In-office mural stickies — module-level notify so the React-port
 * Dashboard can pipe state_update payloads to the canvas's renderer
 * without exposing the OfficeRenderer through React state. Mirrors the
 * imperative bootstrapDashboard.ts:342 path that the React port replaced
 * but never re-wired.
 */
const stickyListeners = new Set<(stickies: WallSticky[]) => void>();
let cachedStickies: WallSticky[] = [];

function hashBeadId(beadId: string): number {
  let hash = 0;
  for (let i = 0; i < beadId.length; i++) {
    hash = (hash * 31 + beadId.charCodeAt(i)) >>> 0;
  }
  return (hash % 1_000_000) + 1;
}

export function notifyDashboardCanvasStickies(state: StateUpdate): void {
  const cols = deriveColumns(
    state.open_issues ?? [],
    state.agents ?? [],
    state.pull_requests ?? [],
    state.graph ?? null,
  );
  const stickies: WallSticky[] = [];
  for (const [sectionIndex, phase] of PHASES.entries()) {
    for (const card of cols[phase]) {
      const issueNumber = card.issue
        ? card.issue.issue_number
        : card.pr
          ? -card.pr.pr_number
          : card.task
            ? -hashBeadId(card.task.bead_id)
            : 0;
      stickies.push({ issueNumber, sectionIndex });
    }
  }
  cachedStickies = stickies;
  stickyListeners.forEach((fn) => fn(stickies));
}

/**
 * Minimum-viable React port of the dashboard canvas (DESIGN §3.2,
 * desktop-ku9.2). The canvas itself is rendered as a JSX ``<canvas>``;
 * the imperative game loop (Camera, OfficeRenderer, AgentShoreStateManager,
 * requestAnimationFrame) lives inside a ``useEffect`` and is cleaned up
 * on unmount — React never touches canvas pixels.
 *
 * This first cut renders the office floor, walls, furniture, and any
 * agent / NPC characters the state manager surfaces. The full HUD port
 * (top bar, side panel, plays panel, etc.) is tracked separately and
 * will hang off this component.
 */
export interface DashboardCanvasProps {
  /**
   * Optional simulation clock seed (ms). Tests can pin this so
   * character motion is deterministic.
   */
  initialClock?: number;
  /**
   * Initial theme. The desktop shell drives the canonical theme
   * selector; we accept the resolved value so the canvas renders in
   * the right palette before the WebSocket bridge connects.
   */
  theme?: ResolvedTheme;
  /**
   * Optional state manager. Tests pass a pre-seeded manager to assert
   * rendering against a known character list. Production callers leave
   * this undefined and get a fresh manager that fills as messages
   * arrive over the (future) WebSocket bridge.
   */
  stateManager?: AgentShoreStateManager;
}

interface FrameContext {
  camera: Camera;
  renderer: OfficeRenderer;
  state: AgentShoreStateManager;
}

function startGameLoop(ctx: FrameContext, initialClock: number): () => void {
  let rafHandle: number | null = null;
  let prevMs = performance.now();
  let cancelled = false;
  void initialClock; // reserved for the (future) clock-pinning seed.

  const tick = () => {
    if (cancelled) return;
    const now = performance.now();
    const dtMs = now - prevMs;
    prevMs = now;

    for (const char of ctx.state.getCharacters()) {
      updateCharacter(char, dtMs / 1000);
    }

    ctx.renderer.render(ctx.state.getCharacters());
    rafHandle = requestAnimationFrame(tick);
  };

  rafHandle = requestAnimationFrame(tick);

  return () => {
    cancelled = true;
    if (rafHandle !== null) {
      cancelAnimationFrame(rafHandle);
    }
  };
}

function normalizeResolvedTheme(value: string | undefined, fallback: ResolvedTheme): ResolvedTheme {
  return value === "dark" || value === "light" ? value : fallback;
}

export function DashboardCanvas({
  initialClock = 0,
  theme = "light",
  stateManager,
}: DashboardCanvasProps = {}): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx2d = canvas.getContext("2d");
    if (!ctx2d) return;

    // Match the canvas backing-store to the displayed size so pixel art
    // stays crisp on HiDPI screens. Without this the canvas defaults to
    // 300x150 and the office floor draws into a tiny corner.
    const dpr = window.devicePixelRatio || 1;

    const camera = new Camera();
    const renderer = new OfficeRenderer(canvas, ctx2d, camera);

    // Resolve the initial theme from documentElement.dataset.theme if
    // ThemeToggle has already painted by the time the canvas mounts —
    // otherwise the canvas would render with the default `theme` prop
    // (light) until something forced a re-theme. The MutationObserver
    // below then keeps the canvas in sync as the user flips themes.
    const initialTheme = normalizeResolvedTheme(document.documentElement.dataset.theme, theme);
    renderer.setTheme(initialTheme);

    // Theme changes happen through ThemeToggle setting data-theme on
    // documentElement (used by CSS variables). The canvas doesn't get a
    // theme prop update on flip; watch the attribute and re-call
    // setTheme so the office palette tracks the chrome.
    const themeObserver = new MutationObserver(() => {
      renderer.setTheme(normalizeResolvedTheme(document.documentElement.dataset.theme, theme));
    });
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });

    const state = stateManager ?? new AgentShoreStateManager();

    const DEFAULT_VIEW_PADDING = 14;

    const measureViewport = (): { top: number; left: number; right: number; bottom: number } => {
      // Mirrors bootstrapDashboard's defaultCameraViewport: returns
      // physical-pixel bounds inside which the office should fit, by
      // measuring the surrounding HUD panels. Falls back to the full
      // window if a given panel isn't mounted yet (e.g. on first paint
      // before the HUD subtree settles).
      const topBar = document.getElementById("top-bar");
      const bottomBar = document.getElementById("bottom-bar");
      const leftPanel = document.getElementById("left-panel");
      const sidePanel = document.getElementById("side-panel");
      const winW = window.innerWidth;
      const winH = window.innerHeight;
      const leftRect = leftPanel?.getBoundingClientRect();
      const sideRect = sidePanel?.getBoundingClientRect();
      const topRect = topBar?.getBoundingClientRect();
      const bottomRect = bottomBar?.getBoundingClientRect();
      const leftCss =
        leftRect && !leftPanel?.classList.contains("collapsed") && leftRect.width > 0
          ? leftRect.right
          : 0;
      const rightCss = sideRect && sideRect.width > 0 ? sideRect.left : winW;
      const topCss = topRect ? topRect.bottom : 0;
      const bottomCss = bottomRect ? bottomRect.top : winH;
      return {
        left: leftCss * dpr,
        top: topCss * dpr,
        right: rightCss * dpr,
        bottom: bottomCss * dpr,
      };
    };

    const fitToView = () => {
      camera.fitToViewport(measureViewport(), DEFAULT_VIEW_PADDING * dpr);
    };

    const resize = () => {
      // Match bootstrapDashboard: canvas backing-store is sized in
      // PHYSICAL pixels and the renderer draws in those same physical
      // coordinates. No setTransform — the camera math (and
      // characterScreenBounds, fitToViewport, etc.) all operate in
      // physical pixels. Scaling the context by DPR here would double
      // every camera offset on HiDPI screens and paint the office
      // off-center to the bottom-right.
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
      fitToView();
    };
    resize();
    // Defer a second fit until the next frame so HUD elements that
    // mount alongside the canvas have a chance to land in the DOM
    // before we measure them. Without this the very first resize sees
    // missing #top-bar / #side-panel and falls back to full-window
    // bounds, which renders too wide on the initial paint.
    const initialFitRaf = requestAnimationFrame(fitToView);

    const ro = new ResizeObserver(resize);
    ro.observe(canvas);
    const onWindowResize = () => fitToView();
    window.addEventListener("resize", onWindowResize);

    // Mouse drag / wheel zoom (DESIGN §3.2). Matches the imperative
    // bootstrapDashboard.ts wiring so the React canvas behaves the same
    // as the legacy bridge SPA.
    camera.attachInputHandlers(canvas);

    const characterAt = (screenX: number, screenY: number) => {
      const chars = [...state.getCharacters()].reverse();
      for (const char of chars) {
        const bounds = characterScreenBounds(char, camera.zoom, camera);
        if (
          screenX >= bounds.left &&
          screenX <= bounds.right &&
          screenY >= bounds.top &&
          screenY <= bounds.bottom
        ) {
          return char;
        }
      }
      return null;
    };

    const onClick = (event: MouseEvent) => {
      if (camera.wasDragging()) return;
      const dpr = window.devicePixelRatio || 1;
      const character = characterAt(event.clientX * dpr, event.clientY * dpr);
      if (!character) return;
      if (!character.npcKind) {
        notifySidePanelSelectAgent(character.agentId);
      }
    };
    const onDblClick = () => {
      notifySidePanelSelectAgent(null);
    };
    canvas.addEventListener("click", onClick);
    canvas.addEventListener("dblclick", onDblClick);

    const isInputFocused = (): boolean => {
      const tag = document.activeElement?.tagName;
      return tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
    };
    const PAN_STEP = 32;
    const onKey = (event: KeyboardEvent) => {
      if (isInputFocused()) return;
      switch (event.key) {
        case "0":
          notifySidePanelSelectAgent(null);
          break;
        case "+":
        case "=":
          camera.setZoom(camera.zoom + 1, canvas.width, canvas.height);
          break;
        case "-":
        case "_":
          camera.setZoom(camera.zoom - 1, canvas.width, canvas.height);
          break;
        case "ArrowLeft":
          camera.panBy(PAN_STEP, 0);
          break;
        case "ArrowRight":
          camera.panBy(-PAN_STEP, 0);
          break;
        case "ArrowUp":
          camera.panBy(0, PAN_STEP);
          break;
        case "ArrowDown":
          camera.panBy(0, -PAN_STEP);
          break;
      }
    };
    window.addEventListener("keydown", onKey);

    setMounted(true);
    const stopLoop = startGameLoop({ camera, renderer, state }, initialClock);

    // Wire the in-office mural to state_update payloads. notifyDashboard-
    // CanvasStickies is module-level so Dashboard.tsx can call it from
    // its message switch; we register the renderer-side push here.
    const onStickies = (stickies: WallSticky[]): void => {
      renderer.setWallStickies(stickies);
    };
    stickyListeners.add(onStickies);
    // Apply any sticker payload that arrived before this canvas mounted.
    if (cachedStickies.length > 0) {
      renderer.setWallStickies(cachedStickies);
    }

    return () => {
      stopLoop();
      ro.disconnect();
      themeObserver.disconnect();
      cancelAnimationFrame(initialFitRaf);
      window.removeEventListener("resize", onWindowResize);
      canvas.removeEventListener("click", onClick);
      canvas.removeEventListener("dblclick", onDblClick);
      window.removeEventListener("keydown", onKey);
      stickyListeners.delete(onStickies);
      setMounted(false);
    };
    // initialClock/theme/stateManager intentionally captured at mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="agentshore-dashboard-canvas"
      data-agentshore-dashboard-canvas
      data-mounted={mounted ? "true" : "false"}
      style={{ width: "100%", height: "100%", display: "block" }}
    />
  );
}
