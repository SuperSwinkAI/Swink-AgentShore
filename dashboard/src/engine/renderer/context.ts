import type {
  KanbanWallPalette,
  MapVisualPalette,
} from "../../office/palette";
import type { ResolvedTheme } from "../../theme";
import type { Camera } from "../camera";

export interface SceneRenderable {
  depth: number;
  draw: () => void;
}

export interface ScreenPoint {
  x: number;
  y: number;
}

export interface WallSticky {
  issueNumber: number;
  sectionIndex: number; // 0=todo, 1=in_progress, 2=reviewing, 3=done
}

/**
 * Mutable holder so that `withSurfaceZ` can stash + restore the
 * "current surface Z" without going through class state.
 */
export interface SurfaceZHolder {
  value: number;
}

/**
 * Bundle of state passed to every render free-function. Lets us split
 * the giant OfficeRenderer into small modules while preserving the
 * behaviour of the original (where every draw method shared `this`).
 */
export interface RenderContext {
  canvas: HTMLCanvasElement;
  ctx: CanvasRenderingContext2D;
  camera: Camera;
  palette: MapVisualPalette;
  kanbanPalette: KanbanWallPalette;
  theme: ResolvedTheme;
  wallStickies: WallSticky[];
  surfaceZ: SurfaceZHolder;
}

/** Temporarily set the active surface Z while running `draw`. */
export function withSurfaceZ(
  rctx: RenderContext,
  zUnits: number,
  draw: () => void,
): void {
  const previous = rctx.surfaceZ.value;
  rctx.surfaceZ.value = zUnits;
  try {
    draw();
  } finally {
    rctx.surfaceZ.value = previous;
  }
}
