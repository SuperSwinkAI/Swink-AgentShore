import { useEffect, useRef, useState, type JSX } from "react";

import { Camera } from "../engine/camera";
import { OfficeRenderer } from "../engine/renderer";
import { AgentShoreStateManager } from "../state";
import { updateCharacter } from "../characters/stateMachine";
import { characterScreenBounds } from "../characters/sprites";
import type { ResolvedTheme } from "../theme";
import { useCanvasClickSelect } from "./canvas/useCanvasClickSelect";
import { useCanvasKeyboardPan } from "./canvas/useCanvasKeyboardPan";
import { useCanvasResize } from "./canvas/useCanvasResize";
import { useCanvasWallStickies } from "./canvas/useCanvasWallStickies";

export { notifyDashboardCanvasStickies } from "./canvas/useCanvasWallStickies";

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
  hidden?: boolean;
}

interface FrameContext {
  canvas: HTMLCanvasElement;
  camera: Camera;
  renderer: OfficeRenderer;
  state: AgentShoreStateManager;
}

function startGameLoop(ctx: FrameContext, initialClock: number): () => void {
  let rafHandle: number | null = null;
  let prevMs = performance.now();
  let cancelled = false;
  void initialClock;

  const tick = () => {
    if (cancelled) return;
    const now = performance.now();
    const dtMs = now - prevMs;
    prevMs = now;

    for (const char of ctx.state.getCharacters()) {
      updateCharacter(char, dtMs / 1000);
    }

    ctx.camera.update(dtMs / 1000, ctx.canvas.width, ctx.canvas.height);
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
  hidden = false,
}: DashboardCanvasProps = {}): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const cameraRef = useRef<Camera | null>(null);
  const rendererRef = useRef<OfficeRenderer | null>(null);
  const stateRef = useRef<AgentShoreStateManager | null>(null);
  const [mounted, setMounted] = useState(false);






  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx2d = canvas.getContext("2d");
    if (!ctx2d) return;

    const camera = new Camera();
    const renderer = new OfficeRenderer(canvas, ctx2d, camera);
    cameraRef.current = camera;
    rendererRef.current = renderer;

    if (import.meta.env.DEV) {
      const testWindow = window as typeof window & {
        __agentshoreDashboardTest?: Record<string, unknown>;
      };
      testWindow.__agentshoreDashboardTest = {
        ...(testWindow.__agentshoreDashboardTest ?? {}),
        camera,
        characters: () =>
          state.getCharacters().map((char) => ({
            agentId: char.agentId,
            agentType: char.agentType,
            modelTier: char.modelTier ?? null,
            npcKind: char.npcKind ?? null,
            x: char.x,
            y: char.y,
            screenBounds: characterScreenBounds(char, camera.zoom, camera),
          })),
      };
    }




    const initialTheme = normalizeResolvedTheme(document.documentElement.dataset.theme, theme);
    renderer.setTheme(initialTheme);



    const themeObserver = new MutationObserver(() => {
      renderer.setTheme(normalizeResolvedTheme(document.documentElement.dataset.theme, theme));
    });
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });

    const state = stateManager ?? new AgentShoreStateManager();
    stateRef.current = state;


    camera.attachInputHandlers(canvas);

    return () => {
      themeObserver.disconnect();
      if (import.meta.env.DEV) {
        const testWindow = window as typeof window & {
          __agentshoreDashboardTest?: Record<string, unknown>;
        };
        if (testWindow.__agentshoreDashboardTest?.camera === camera) {
          delete testWindow.__agentshoreDashboardTest.camera;
          delete testWindow.__agentshoreDashboardTest.characters;
        }
      }
      cameraRef.current = null;
      rendererRef.current = null;
      stateRef.current = null;
    };

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fitToViewRef = useCanvasResize(canvasRef, cameraRef);
  const focusAgentRef = useCanvasClickSelect(canvasRef, cameraRef, stateRef, fitToViewRef);
  useCanvasKeyboardPan(canvasRef, cameraRef, focusAgentRef);
  useCanvasWallStickies(rendererRef);







  useEffect(() => {
    const canvas = canvasRef.current;
    const camera = cameraRef.current;
    const renderer = rendererRef.current;
    const state = stateRef.current;
    if (!canvas || !camera || !renderer || !state) return;

    setMounted(true);
    const stopLoop = startGameLoop({ canvas, camera, renderer, state }, initialClock);

    return () => {
      stopLoop();
      setMounted(false);
    };


    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);



  return (
    <canvas
      ref={canvasRef}
      id="office"
      className="agentshore-dashboard-canvas"
      data-agentshore-dashboard-canvas
      data-mounted={mounted ? "true" : "false"}
      style={{
        width: "100%",
        height: "100%",
        display: hidden ? "none" : "block",
        imageRendering: "pixelated",
      }}
    />
  );
}
