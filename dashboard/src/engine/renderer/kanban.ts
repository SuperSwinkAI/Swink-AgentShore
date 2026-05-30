import { TILE_SIZE } from "../../office/layout";
import type { KanbanWallPalette } from "../../office/palette";
import {
  KANBAN_BOARD_WIDTH,
  KANBAN_BOARD_X_START,
  KANBAN_BOARD_Z_END,
  KANBAN_BOARD_Z_START,
  KANBAN_DEPTH_BG,
  KANBAN_DEPTH_HEADER,
  KANBAN_DEPTH_STICKY,
  KANBAN_FACE_Y,
  KANBAN_HEADER_HEIGHT,
  KANBAN_HEADER_Z,
  KANBAN_LANES,
  KANBAN_STICKY_Z_END,
  KANBAN_STICKY_Z_START,
  KANBAN_TITLE_Z,
  STICKY_INSET_U,
  STICKY_SIZE_U,
  TAPE_WIDTH_U,
} from "./constants";
import type { RenderContext, SceneRenderable } from "./context";
import {
  drawVerticalFaceX,
  strokeVerticalFaceX,
} from "./primitives";

export function renderKanbanWall(
  rctx: RenderContext,
  kanban: KanbanWallPalette,
): SceneRenderable[] {
  return [
    {
      depth: KANBAN_DEPTH_BG,
      draw: () => {
        drawWallPanel(
          rctx,
          KANBAN_BOARD_X_START,
          KANBAN_FACE_Y,
          KANBAN_BOARD_WIDTH,
          KANBAN_BOARD_Z_START,
          KANBAN_BOARD_Z_END - KANBAN_BOARD_Z_START,
          kanban.boardBackground,
          kanban.boardBorder,
        );
        drawWallText(
          rctx,
          "SPRINT BOARD",
          KANBAN_BOARD_X_START + KANBAN_BOARD_WIDTH / 2,
          KANBAN_FACE_Y,
          KANBAN_TITLE_Z,
          kanban.titleColor,
          3.5,
          true,
        );
      },
    },
    {
      depth: KANBAN_DEPTH_HEADER,
      draw: () => {
        for (let i = 0; i < KANBAN_LANES.length; i++) {
          const lane = KANBAN_LANES[i];
          drawWallPanel(
            rctx,
            lane.x,
            KANBAN_FACE_Y,
            lane.w,
            KANBAN_HEADER_Z,
            KANBAN_HEADER_HEIGHT,
            kanban.headerFills[i],
            kanban.headerStroke,
          );
          drawWallText(
            rctx,
            lane.label,
            lane.x + lane.w / 2,
            KANBAN_FACE_Y,
            KANBAN_HEADER_Z + KANBAN_HEADER_HEIGHT / 2,
            kanban.laneTextColors[i],
            lane.label === "IN PROGRESS" ? 2.2 : 2.5,
            true,
          );
        }
        for (const lane of KANBAN_LANES.slice(1)) {
          drawVerticalFaceX(
            rctx,
            lane.x - TAPE_WIDTH_U / 2,
            KANBAN_FACE_Y,
            TAPE_WIDTH_U,
            KANBAN_BOARD_Z_START,
            KANBAN_BOARD_Z_END - KANBAN_BOARD_Z_START,
            kanban.dividerColor,
          );
        }
      },
    },
    {
      depth: KANBAN_DEPTH_STICKY,
      draw: () => {
        const stickyColors = rctx.palette.wallSticky;
        for (const sticky of rctx.wallStickies) {
          const pos = scatterPosition(sticky.issueNumber, sticky.sectionIndex);
          drawVerticalFaceX(
            rctx,
            pos.x,
            KANBAN_FACE_Y,
            STICKY_SIZE_U,
            pos.z,
            STICKY_SIZE_U,
            stickyColors.fill,
          );
          strokeVerticalFaceX(
            rctx,
            pos.x,
            KANBAN_FACE_Y,
            STICKY_SIZE_U,
            pos.z,
            STICKY_SIZE_U,
            stickyColors.stroke,
          );
        }
      },
    },
  ];
}

function scatterPosition(
  issueNumber: number,
  sectionIndex: number,
): { x: number; z: number } {
  const lane = KANBAN_LANES[sectionIndex] ?? KANBAN_LANES[0];
  const h1 = ((issueNumber * 2654435761) >>> 0) / 4294967296;
  const h2 = ((issueNumber * 2246822519) >>> 0) / 4294967296;
  const usableW = lane.w - 2 * STICKY_INSET_U - STICKY_SIZE_U;
  const usableH =
    KANBAN_STICKY_Z_END -
    KANBAN_STICKY_Z_START -
    2 * STICKY_INSET_U -
    STICKY_SIZE_U;
  return {
    x: lane.x + STICKY_INSET_U + h1 * usableW,
    z: KANBAN_STICKY_Z_START + STICKY_INSET_U + h2 * usableH,
  };
}

export function drawWallPanel(
  rctx: RenderContext,
  x: number,
  y: number,
  w: number,
  zStart: number,
  zHeight: number,
  fill: string,
  stroke: string,
): void {
  drawVerticalFaceX(rctx, x, y, w, zStart, zHeight, fill);
  strokeVerticalFaceX(rctx, x, y, w, zStart, zHeight, stroke);
}

export function drawWallText(
  rctx: RenderContext,
  text: string,
  x: number,
  y: number,
  z: number,
  fill: string,
  size: number,
  bold = false,
): void {
  const ctx = rctx.ctx;
  const [sx, sy] = rctx.camera.worldToScreen(x * TILE_SIZE, y * TILE_SIZE, z);
  const fontSize = Math.max(6, size * rctx.camera.zoom);
  ctx.font = `${bold ? "bold " : ""}${fontSize}px monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "rgba(0, 0, 0, 0.35)";
  ctx.fillText(text, sx + 1, sy + 1);
  ctx.fillStyle = fill;
  ctx.fillText(text, sx, sy);
}
