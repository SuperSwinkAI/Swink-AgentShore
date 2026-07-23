import { useEffect, useRef, type RefObject } from "react";

import type { Camera } from "../../engine/camera";
import { characterScreenBounds } from "../../characters/sprites";
import type { AgentShoreStateManager } from "../../state";
import { notifySidePanelClickHandler, notifySidePanelSelectAgent } from "../SidePanel";

/**
 * Click-to-select handling on the canvas: clicking a character selects it
 * (side panel + camera focus); double-click clears the selection and
 * re-fits the whole office. Also registers `focusAgent` as the side
 * panel's click handler so selecting an agent from the side panel list
 * focuses it on the canvas too.
 *
 * Returns a ref holding `focusAgent` so the keyboard-pan hook can reuse it
 * for the "0" reset-to-fit key without duplicating the character lookup.
 */
export function useCanvasClickSelect(
  canvasRef: RefObject<HTMLCanvasElement | null>,
  cameraRef: RefObject<Camera | null>,
  stateRef: RefObject<AgentShoreStateManager | null>,
  fitToViewRef: RefObject<() => void>,
): RefObject<(agentId: string | null) => void> {
  const focusAgentRef = useRef<(agentId: string | null) => void>(() => {});

  useEffect(() => {
    const canvas = canvasRef.current;
    const camera = cameraRef.current;
    const state = stateRef.current;
    if (!canvas || !camera || !state) return;

    const focusAgent = (agentId: string | null) => {
      if (agentId === null) {
        fitToViewRef.current();
        return;
      }
      const character = state
        .getCharacters()
        .find((char) => !char.npcKind && char.agentId === agentId);
      if (!character) return;
      camera.focusOn(character);
    };
    focusAgentRef.current = focusAgent;

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
        focusAgent(character.agentId);
      }
    };
    const onDblClick = () => {
      notifySidePanelSelectAgent(null);
      focusAgent(null);
    };
    canvas.addEventListener("click", onClick);
    canvas.addEventListener("dblclick", onDblClick);
    notifySidePanelClickHandler(focusAgent);

    return () => {
      canvas.removeEventListener("click", onClick);
      canvas.removeEventListener("dblclick", onDblClick);
      notifySidePanelClickHandler(null);
    };
    // canvasRef/cameraRef/stateRef/fitToViewRef are stable ref objects
    // populated before this effect runs; intentionally runs once at mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return focusAgentRef;
}
