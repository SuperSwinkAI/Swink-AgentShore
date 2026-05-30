import {
  EDITOR_ROOM_ART_DEPTH,
  EDITOR_ROOM_ART_FACE_Y,
  FRONT_DESK_ART_DEPTH,
  FRONT_DESK_ART_FACE_Y,
  SCIENCE_LAB_ART_DEPTH,
  SCIENCE_LAB_ART_FACE_Y,
} from "./constants";
import type { RenderContext, SceneRenderable } from "./context";
import { drawWallPanel, drawWallText, renderKanbanWall } from "./kanban";
import {
  drawPolygon,
  projectedPoint,
  strokePolygon,
  strokeWorldSegment,
} from "./primitives";

export function renderWallDecorations(
  rctx: RenderContext,
): SceneRenderable[] {
  return [
    ...renderKanbanWall(rctx, rctx.kanbanPalette),
    {
      depth: FRONT_DESK_ART_DEPTH,
      draw: () => drawAgentOfMonthWallArt(rctx),
    },
    {
      depth: EDITOR_ROOM_ART_DEPTH,
      draw: () => drawGridEditorCodeWall(rctx),
    },
    {
      depth: SCIENCE_LAB_ART_DEPTH,
      draw: () => drawGridScienceLabDiagnosticsWall(rctx),
    },
  ];
}

function drawGridEditorCodeWall(rctx: RenderContext): void {
  const faceY = EDITOR_ROOM_ART_FACE_Y;
  const dark = rctx.theme === "dark";
  const palette = dark
    ? {
        panel: "rgba(57, 217, 255, 0.055)",
        stroke: "rgba(57, 217, 255, 0.32)",
        text: "rgba(218, 250, 255, 0.82)",
        codeA: "rgba(43, 224, 177, 0.58)",
        codeB: "rgba(178, 102, 255, 0.48)",
        codeC: "rgba(244, 212, 77, 0.48)",
      }
    : {
        panel: "rgba(0, 174, 214, 0.08)",
        stroke: "rgba(0, 126, 174, 0.36)",
        text: "#13537A",
        codeA: "rgba(18, 153, 112, 0.46)",
        codeB: "rgba(126, 78, 186, 0.38)",
        codeC: "rgba(221, 101, 24, 0.38)",
      };

  drawWallPanel(rctx, 6.2, faceY, 12.6, 2.0, 4.25, palette.panel, palette.stroke);
  drawWallText(rctx, "PAIR REVIEW", 12.5, faceY, 5.72, palette.text, 3.2, true);
  for (let row = 0; row < 5; row += 1) {
    const z = 4.92 - row * 0.48;
    const fill =
      row % 3 === 0
        ? palette.codeA
        : row % 3 === 1
          ? palette.codeB
          : palette.codeC;
    drawWallPanel(
      rctx,
      7.1,
      faceY,
      3.2 + (row % 2) * 0.65,
      z,
      0.12,
      fill,
      fill,
    );
    drawWallPanel(
      rctx,
      11.5,
      faceY,
      4.6 - (row % 2) * 0.55,
      z,
      0.12,
      fill,
      fill,
    );
  }
  drawWallPanel(
    rctx,
    10.84,
    faceY,
    0.18,
    2.44,
    2.64,
    palette.stroke,
    palette.stroke,
  );
  drawWallPanel(
    rctx,
    6.64,
    faceY,
    11.72,
    2.46,
    0.08,
    palette.stroke,
    palette.stroke,
  );
}

function drawGridScienceLabDiagnosticsWall(rctx: RenderContext): void {
  const faceY = SCIENCE_LAB_ART_FACE_Y;
  const dark = rctx.theme === "dark";
  const palette = dark
    ? {
        panel: "rgba(8, 35, 48, 0.74)",
        stroke: "rgba(57, 217, 255, 0.34)",
        text: "rgba(218, 250, 255, 0.82)",
        cyan: "rgba(57, 217, 255, 0.58)",
        orange: "rgba(255, 145, 70, 0.70)",
        green: "rgba(43, 224, 177, 0.50)",
        rail: "rgba(148, 246, 255, 0.24)",
      }
    : {
        panel: "rgba(222, 255, 255, 0.80)",
        stroke: "rgba(0, 126, 174, 0.36)",
        text: "#13537A",
        cyan: "rgba(0, 139, 188, 0.48)",
        orange: "rgba(221, 101, 24, 0.62)",
        green: "rgba(18, 153, 112, 0.42)",
        rail: "rgba(0, 126, 174, 0.20)",
      };

  drawWallPanel(rctx, 54.9, faceY, 13.1, 1.1, 4.8, palette.panel, palette.stroke);
  drawWallText(rctx, "VERIFY CORE", 61.4, faceY, 5.48, palette.text, 3.2, true);
  drawWallPanel(
    rctx,
    55.35,
    faceY,
    12.2,
    5.02,
    0.08,
    palette.rail,
    palette.rail,
  );
  drawWallPanel(
    rctx,
    55.35,
    faceY,
    12.2,
    1.48,
    0.08,
    palette.rail,
    palette.rail,
  );

  for (let x = 56.3; x <= 66.8; x += 1.75) {
    drawWallPanel(rctx, x, faceY, 0.05, 1.72, 2.92, palette.rail, palette.rail);
  }
  for (let row = 0; row < 4; row += 1) {
    const z = 4.42 - row * 0.58;
    drawWallPanel(
      rctx,
      55.75,
      faceY,
      3.25 + (row % 2) * 0.5,
      z,
      0.1,
      palette.cyan,
      palette.cyan,
    );
    drawWallPanel(
      rctx,
      62.15,
      faceY,
      4.6 - (row % 2) * 0.45,
      z,
      0.1,
      row === 1 ? palette.orange : palette.green,
      row === 1 ? palette.orange : palette.green,
    );
  }

  const coreX = 61.35;
  const coreZ = 3.22;
  drawWallPanel(
    rctx,
    coreX - 1.16,
    faceY,
    2.32,
    coreZ - 0.3,
    0.6,
    "rgba(255, 145, 70, 0.12)",
    palette.orange,
  );
  drawWallHexagon(
    rctx,
    coreX,
    faceY,
    coreZ,
    0.98,
    0.52,
    "rgba(255, 145, 70, 0.22)",
    palette.orange,
  );
  drawWallPanel(
    rctx,
    coreX - 0.16,
    faceY,
    0.32,
    coreZ - 0.14,
    0.28,
    dark ? "rgba(255, 246, 178, 0.88)" : "rgba(246, 198, 90, 0.86)",
    palette.orange,
  );

  const nodes = [
    { x: 57.05, z: 2.38, fill: palette.cyan },
    { x: 58.85, z: 3.74, fill: palette.orange },
    { x: 64.15, z: 3.86, fill: palette.cyan },
    { x: 66.0, z: 2.34, fill: palette.green },
  ];
  for (const node of nodes) {
    drawWallHexagon(
      rctx,
      node.x,
      faceY,
      node.z,
      0.36,
      0.2,
      node.fill,
      node.fill,
    );
  }
  strokeWorldSegment(
    rctx,
    57.05,
    faceY,
    2.38,
    coreX,
    faceY,
    coreZ,
    palette.cyan,
    1.4,
  );
  strokeWorldSegment(
    rctx,
    58.85,
    faceY,
    3.74,
    coreX,
    faceY,
    coreZ,
    palette.orange,
    1.4,
  );
  strokeWorldSegment(
    rctx,
    64.15,
    faceY,
    3.86,
    coreX,
    faceY,
    coreZ,
    palette.cyan,
    1.4,
  );
  strokeWorldSegment(
    rctx,
    66.0,
    faceY,
    2.34,
    coreX,
    faceY,
    coreZ,
    palette.green,
    1.4,
  );
}

function drawWallHexagon(
  rctx: RenderContext,
  x: number,
  y: number,
  z: number,
  radiusX: number,
  radiusZ: number,
  fill: string,
  stroke: string,
): void {
  const points = [
    projectedPoint(rctx, x - radiusX * 0.55, y, z + radiusZ),
    projectedPoint(rctx, x + radiusX * 0.55, y, z + radiusZ),
    projectedPoint(rctx, x + radiusX, y, z),
    projectedPoint(rctx, x + radiusX * 0.55, y, z - radiusZ),
    projectedPoint(rctx, x - radiusX * 0.55, y, z - radiusZ),
    projectedPoint(rctx, x - radiusX, y, z),
  ];
  rctx.ctx.fillStyle = fill;
  drawPolygon(rctx, points);
  rctx.ctx.strokeStyle = stroke;
  rctx.ctx.lineWidth = 1;
  strokePolygon(rctx, points);
}

function drawAgentOfMonthWallArt(rctx: RenderContext): void {
  const faceY = FRONT_DESK_ART_FACE_Y;
  drawWallPanel(rctx, 56.2, faceY, 10.65, 1.95, 4.8, "#DCE7E4", "#6E8585");
  drawWallPanel(rctx, 56.45, faceY, 10.15, 6.05, 0.52, "#31444A", "#A7B7B3");
  drawWallText(
    rctx,
    "AGENT OF THE MONTH",
    61.52,
    faceY,
    6.32,
    "#F0E8C8",
    4.5,
    true,
  );

  drawHeadshot(
    rctx,
    57.0,
    faceY,
    2.45,
    2.25,
    2.9,
    "#D8A23F",
    "#5E6A91",
    "#8B513A",
    true,
  );
  drawWallPanel(rctx, 58.82, faceY, 0.38, 5.02, 0.38, "#E4B94A", "#8F6B25");
  drawWallPanel(rctx, 58.9, faceY, 0.22, 5.12, 0.12, "#FFF1A8", "#FFF1A8");

  const portraits = [
    { x: 60.0, shirt: "#6B9E7A", hair: "#3F3630" },
    { x: 61.55, shirt: "#4E86A7", hair: "#6A4A34" },
    { x: 63.1, shirt: "#9A6DA4", hair: "#2F3742" },
    { x: 64.65, shirt: "#B7715B", hair: "#4C3329" },
  ];
  for (const portrait of portraits) {
    drawHeadshot(
      rctx,
      portrait.x,
      faceY,
      2.72,
      1.18,
      1.7,
      portrait.shirt,
      portrait.hair,
      "#A86E52",
    );
  }

  drawWallPanel(rctx, 60.05, faceY, 5.95, 4.85, 0.18, "#9BAAA8", "#9BAAA8");
  drawWallPanel(rctx, 60.05, faceY, 5.05, 4.42, 0.14, "#C2CECB", "#C2CECB");
}

function drawHeadshot(
  rctx: RenderContext,
  x: number,
  faceY: number,
  z: number,
  w: number,
  h: number,
  shirt: string,
  hair: string,
  skin: string,
  winner = false,
): void {
  const mat = winner ? "#F4E7B8" : "#F3F6F4";
  const stroke = winner ? "#B88A2B" : "#879899";
  drawWallPanel(rctx, x, faceY, w, z, h, mat, stroke);
  drawWallPanel(
    rctx,
    x + 0.12,
    faceY,
    w - 0.24,
    z + 0.12,
    h - 0.24,
    "#DDE8E7",
    "#DDE8E7",
  );
  drawWallPanel(
    rctx,
    x + w * 0.3,
    faceY,
    w * 0.4,
    z + h * 0.45,
    h * 0.22,
    hair,
    hair,
  );
  drawWallPanel(
    rctx,
    x + w * 0.35,
    faceY,
    w * 0.3,
    z + h * 0.34,
    h * 0.22,
    skin,
    skin,
  );
  drawWallPanel(
    rctx,
    x + w * 0.25,
    faceY,
    w * 0.5,
    z + 0.24,
    h * 0.32,
    shirt,
    shirt,
  );
  drawWallPanel(
    rctx,
    x + w * 0.22,
    faceY,
    w * 0.56,
    z + 0.18,
    0.08,
    "rgba(44, 54, 58, 0.34)",
    "rgba(44, 54, 58, 0.34)",
  );
}
