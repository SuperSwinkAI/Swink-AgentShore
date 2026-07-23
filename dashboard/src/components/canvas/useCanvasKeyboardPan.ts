import { useEffect, type RefObject } from "react";

import type { Camera } from "../../engine/camera";
import { notifySidePanelSelectAgent } from "../SidePanel";

const PAN_STEP = 32;

function isInputFocused(): boolean {
  const tag = document.activeElement?.tagName;
  return tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
}

/**
 * Keyboard-driven camera control: arrow keys pan, +/- zoom, "0" clears the
 * selection and re-fits the whole office (reuses the click-select hook's
 * `focusAgent` so the reset behaves identically to a canvas double-click).
 * Ignored while an input/select/textarea has focus so typing elsewhere
 * doesn't steal keystrokes.
 */
export function useCanvasKeyboardPan(
  canvasRef: RefObject<HTMLCanvasElement | null>,
  cameraRef: RefObject<Camera | null>,
  focusAgentRef: RefObject<(agentId: string | null) => void>,
): void {
  useEffect(() => {
    const canvas = canvasRef.current;
    const camera = cameraRef.current;
    if (!canvas || !camera) return;

    const onKey = (event: KeyboardEvent) => {
      if (isInputFocused()) return;
      switch (event.key) {
        case "0":
          notifySidePanelSelectAgent(null);
          focusAgentRef.current(null);
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

    return () => {
      window.removeEventListener("keydown", onKey);
    };
    // canvasRef/cameraRef/focusAgentRef are stable ref objects populated
    // before this effect runs; intentionally runs once at mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
