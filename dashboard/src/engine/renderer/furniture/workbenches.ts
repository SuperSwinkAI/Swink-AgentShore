import type { Rect } from "../../../office/layout";
import type { RenderContext, ScreenPoint } from "../context";
import { drawFurnitureBase } from "./index";
import { CYAN_ACCENT, WORKBENCH_CASE_PALETTES } from "./palettes";
import {
  drawPolygon,
  drawRaisedBox,
  drawVerticalFaceX,
  drawWorldRect,
  projectedPoint,
  shade,
  strokePolygon,
  strokeWorldRect,
  strokeWorldSegment,
} from "../primitives";

export function drawWhiteboard(rctx: RenderContext, rect: Rect): void {
  drawFurnitureBase(rctx, rect, "#D7E8E9", "#F1FBFA", "#6E8E92");
  drawWorldRect(rctx, rect.x + 0.5, rect.y + 0.5, 2.4, 0.14, "#5AA6B8");
  drawWorldRect(rctx, rect.x + 0.5, rect.y + 0.9, 1.8, 0.14, "#D25D5D");
  drawWorldRect(rctx, rect.x + 3.3, rect.y + 0.55, 1.6, 0.14, "#7AA868");
  drawWorldRect(rctx, rect.x + 3.3, rect.y + 1, 0.9, 0.14, "#7AA868");
}

export function drawPinBoard(rctx: RenderContext, rect: Rect): void {
  drawFurnitureBase(rctx, rect, "#8A5F35", "#A97740", "#C6975F");
  drawWorldRect(rctx, rect.x + 0.35, rect.y + 0.45, 0.55, 0.55, "#F0D36A");
  drawWorldRect(rctx, rect.x + 1.05, rect.y + 0.65, 0.5, 0.7, "#E78B68");
  drawWorldRect(rctx, rect.x + 0.5, rect.y + 1.55, 0.7, 0.55, "#8FC3E8");
}

export function drawWorkbench(rctx: RenderContext, rect: Rect): void {
  drawFurnitureBase(rctx, rect, "#5D4330", "#7A583A", "#9D8066");
  for (let x = rect.x + 1; x < rect.x + rect.w - 0.5; x += 2) {
    drawWorldRect(rctx, x, rect.y + 0.7, 0.9, 0.16, "#B8A06E");
  }
  drawWorldRect(rctx, rect.x + 0.75, rect.y + 1.4, 1.25, 0.25, "#C66A43");
  drawWorldRect(
    rctx,
    rect.x + rect.w - 2.25,
    rect.y + 1.35,
    1.5,
    0.25,
    "#8EB9C9",
  );
  drawWorldRect(
    rctx,
    rect.x + 0.5,
    rect.y + rect.h - 0.65,
    rect.w - 1,
    0.18,
    "#2E251F",
  );
}

export function drawAssemblyBench(rctx: RenderContext, rect: Rect): void {
  const topZ = rctx.surfaceZ.value;
  const gridDark = rctx.theme === "dark";
  const gridLight = rctx.theme === "light";
  const palette = gridDark
    ? {
        ...WORKBENCH_CASE_PALETTES.dark,
        mat: "#0B2F40",
        matStroke: CYAN_ACCENT.dark,
        matGrid: "rgba(57, 217, 255, 0.24)",
        boardStroke: "#F4D44D",
        drawer: "#061925",
        drawerHandle: "#39D9FF",
      }
    : gridLight
      ? {
          ...WORKBENCH_CASE_PALETTES.light,
          mat: "#E7FFFF",
          matStroke: CYAN_ACCENT.light,
          matGrid: "rgba(0, 139, 188, 0.22)",
          boardStroke: "#D99B21",
          drawer: "#0F6F88",
          drawerHandle: "#F4D44D",
        }
      : {
          baseFill: "#65442E",
          baseTop: "#8A623D",
          baseStroke: "#B58C64",
          surface: "#765034",
          surfaceStroke: "#A97C55",
          frontRail: "#3B281D",
          mat: "#344C53",
          matStroke: "#79A7A8",
          matGrid: "rgba(148, 197, 198, 0.28)",
          boardStroke: "#D7D08B",
          drawer: "#513826",
          drawerHandle: "#B8905E",
        };

  drawFurnitureBase(
    rctx,
    rect,
    palette.baseFill,
    palette.baseTop,
    palette.baseStroke,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.42,
    rect.y + 0.38,
    rect.w - 0.84,
    rect.h - 0.76,
    palette.surface,
    topZ + 0.05,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.42,
    rect.y + 0.38,
    rect.w - 0.84,
    rect.h - 0.76,
    palette.surfaceStroke,
    topZ + 0.07,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.78,
    rect.y + rect.h - 0.82,
    rect.w - 1.56,
    0.18,
    palette.frontRail,
    topZ + 0.1,
  );

  const mat = {
    x: rect.x + 0.82,
    y: rect.y + 0.76,
    w: rect.w - 1.64,
    h: 1.55,
  };
  drawWorldRect(rctx, mat.x, mat.y, mat.w, mat.h, palette.mat, topZ + 0.12);
  strokeWorldRect(
    rctx,
    mat.x,
    mat.y,
    mat.w,
    mat.h,
    palette.matStroke,
    topZ + 0.13,
  );
  for (let x = mat.x + 0.5; x < mat.x + mat.w - 0.3; x += 0.6) {
    drawWorldRect(
      rctx,
      x,
      mat.y + 0.1,
      0.035,
      mat.h - 0.2,
      palette.matGrid,
      topZ + 0.15,
    );
  }
  for (let y = mat.y + 0.42; y < mat.y + mat.h - 0.2; y += 0.42) {
    drawWorldRect(
      rctx,
      mat.x + 0.1,
      y,
      mat.w - 0.2,
      0.035,
      palette.matGrid,
      topZ + 0.15,
    );
  }

  const boards = [
    { x: rect.x + 1.05, y: rect.y + 2.68, w: 1.55, h: 0.82, fill: "#386C57" },
    { x: rect.x + 3.0, y: rect.y + 2.58, w: 1.36, h: 0.72, fill: "#2F587A" },
  ];
  for (const board of boards) {
    drawWorldRect(
      rctx,
      board.x,
      board.y,
      board.w,
      board.h,
      board.fill,
      topZ + 0.13,
    );
    strokeWorldRect(
      rctx,
      board.x,
      board.y,
      board.w,
      board.h,
      palette.boardStroke,
      topZ + 0.14,
    );
    drawWorldRect(
      rctx,
      board.x + 0.2,
      board.y + 0.2,
      0.22,
      0.16,
      "#E7C84B",
      topZ + 0.16,
    );
    drawWorldRect(
      rctx,
      board.x + 0.58,
      board.y + 0.3,
      0.18,
      0.14,
      "#BFD6D8",
      topZ + 0.16,
    );
  }

  drawRaisedBox(
    rctx,
    { x: rect.x + rect.w - 2.2, y: rect.y + 2.42, w: 0.85, h: 0.7 },
    topZ + 0.08,
    0.2,
    "#D9B65D",
    "#8E642C",
    "#C79B44",
    "#6F491F",
    "#F6D889",
  );
  drawRaisedBox(
    rctx,
    { x: rect.x + rect.w - 1.15, y: rect.y + 2.58, w: 0.45, h: 0.45 },
    topZ + 0.08,
    0.16,
    "#B8443E",
    "#6D211D",
    "#97342F",
    "#571A17",
    "#E68C86",
  );
  strokeWorldSegment(
    rctx,
    rect.x + 5.0,
    rect.y + 3.08,
    topZ + 0.16,
    rect.x + 6.2,
    rect.y + 3.48,
    topZ + 0.16,
    "#2C2C2C",
    2,
  );

  for (const drawerX of [rect.x + 0.72, rect.x + 2.2, rect.x + 5.85]) {
    drawVerticalFaceX(
      rctx,
      drawerX,
      rect.y + rect.h + 0.02,
      1.05,
      0.38,
      0.58,
      palette.drawer,
    );
    drawVerticalFaceX(
      rctx,
      drawerX + 0.22,
      rect.y + rect.h + 0.03,
      0.52,
      0.62,
      0.08,
      palette.drawerHandle,
    );
  }
}

export function drawPrototypeBench(rctx: RenderContext, rect: Rect): void {
  const topZ = rctx.surfaceZ.value;
  const gridDark = rctx.theme === "dark";
  const gridLight = rctx.theme === "light";
  const palette = gridDark
    ? {
        ...WORKBENCH_CASE_PALETTES.dark,
        leg: "#061925",
      }
    : gridLight
      ? {
          ...WORKBENCH_CASE_PALETTES.light,
          leg: "#0F6F88",
        }
      : {
          baseFill: "#6A472F",
          baseTop: "#91653D",
          baseStroke: "#B98D5C",
          surface: "#7C5535",
          surfaceStroke: "#B18456",
          frontRail: "#422C1F",
          leg: "#37251B",
        };

  drawFurnitureBase(
    rctx,
    rect,
    palette.baseFill,
    palette.baseTop,
    palette.baseStroke,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.42,
    rect.y + 0.38,
    rect.w - 0.84,
    rect.h - 0.76,
    palette.surface,
    topZ + 0.05,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.42,
    rect.y + 0.38,
    rect.w - 0.84,
    rect.h - 0.76,
    palette.surfaceStroke,
    topZ + 0.07,
  );

  const blueprint: ScreenPoint[] = [
    projectedPoint(rctx, rect.x + 1.0, rect.y + 0.82, topZ + 0.12),
    projectedPoint(rctx, rect.x + 3.7, rect.y + 0.62, topZ + 0.12),
    projectedPoint(rctx, rect.x + 3.95, rect.y + 2.32, topZ + 0.12),
    projectedPoint(rctx, rect.x + 0.88, rect.y + 2.48, topZ + 0.12),
  ];
  rctx.ctx.fillStyle = "#2269A7";
  drawPolygon(rctx, blueprint);
  rctx.ctx.strokeStyle = "#B9D6EA";
  rctx.ctx.lineWidth = 1;
  strokePolygon(rctx, blueprint);
  for (let i = 0; i < 5; i++) {
    drawWorldRect(
      rctx,
      rect.x + 1.2,
      rect.y + 1.02 + i * 0.24,
      1.65,
      0.035,
      "#CBE7F6",
      topZ + 0.16,
    );
  }
  drawWorldRect(
    rctx,
    rect.x + 2.98,
    rect.y + 1.28,
    0.04,
    0.82,
    "#CBE7F6",
    topZ + 0.16,
  );
  drawWorldRect(
    rctx,
    rect.x + 2.74,
    rect.y + 1.66,
    0.62,
    0.04,
    "#CBE7F6",
    topZ + 0.16,
  );

  const crates = [
    { x: rect.x + 4.6, y: rect.y + 0.78, fill: "#C58A3B" },
    { x: rect.x + 5.58, y: rect.y + 0.78, fill: "#8FA65B" },
    { x: rect.x + 6.56, y: rect.y + 0.78, fill: "#B85F50" },
  ];
  for (const crate of crates) {
    drawRaisedBox(
      rctx,
      { x: crate.x, y: crate.y, w: 0.72, h: 0.72 },
      topZ + 0.08,
      0.32,
      shade(crate.fill, 14),
      shade(crate.fill, -30),
      crate.fill,
      shade(crate.fill, -42),
      "#3E3226",
    );
    drawWorldRect(
      rctx,
      crate.x + 0.16,
      crate.y + 0.25,
      0.36,
      0.08,
      "#F1DFAE",
      topZ + 0.44,
    );
  }

  drawRaisedBox(
    rctx,
    { x: rect.x + 5.0, y: rect.y + 2.35, w: 2.0, h: 0.86 },
    topZ + 0.08,
    0.28,
    "#455760",
    "#1B252B",
    "#34464F",
    "#121A1F",
    "#AEBBC0",
  );
  drawWorldRect(
    rctx,
    rect.x + 5.24,
    rect.y + 2.56,
    0.76,
    0.12,
    "#65CDBC",
    topZ + 0.42,
  );
  drawWorldRect(
    rctx,
    rect.x + 6.24,
    rect.y + 2.56,
    0.42,
    0.12,
    "#DDB856",
    topZ + 0.42,
  );
  drawWorldRect(
    rctx,
    rect.x + 5.32,
    rect.y + 2.92,
    1.2,
    0.08,
    "#C95A4F",
    topZ + 0.42,
  );

  drawWorldRect(
    rctx,
    rect.x + 0.72,
    rect.y + rect.h - 0.8,
    rect.w - 1.44,
    0.18,
    palette.frontRail,
    topZ + 0.1,
  );
  for (const legX of [rect.x + 0.75, rect.x + 3.9, rect.x + rect.w - 1.1]) {
    drawVerticalFaceX(
      rctx,
      legX,
      rect.y + rect.h + 0.02,
      0.34,
      0.18,
      0.82,
      palette.leg,
    );
  }
}

export function drawGridElectronicsBench(
  rctx: RenderContext,
  rect: Rect,
): void {
  const topZ = rctx.surfaceZ.value;
  const dark = rctx.theme === "dark";
  const palette = dark
    ? {
        ...WORKBENCH_CASE_PALETTES.dark,
        panel: "#061925",
        panelGrid: "rgba(57, 217, 255, 0.28)",
        cyan: CYAN_ACCENT.dark,
        green: "#2BE0B1",
        yellow: "#F4D44D",
        orange: "#FF9146",
        violet: "#B880FF",
        drawer: "#081D27",
      }
    : {
        ...WORKBENCH_CASE_PALETTES.light,
        panel: "#E7FFFF",
        panelGrid: "rgba(0, 139, 188, 0.24)",
        cyan: CYAN_ACCENT.light,
        green: "#2BE0B1",
        yellow: "#F4D44D",
        orange: "#FF9146",
        violet: "#7B42D9",
        drawer: "#0F6F88",
      };

  drawFurnitureBase(
    rctx,
    rect,
    palette.baseFill,
    palette.baseTop,
    palette.baseStroke,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.42,
    rect.y + 0.38,
    rect.w - 0.84,
    rect.h - 0.76,
    palette.surface,
    topZ + 0.05,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.42,
    rect.y + 0.38,
    rect.w - 0.84,
    rect.h - 0.76,
    palette.surfaceStroke,
    topZ + 0.07,
  );

  const panel = {
    x: rect.x + 0.8,
    y: rect.y + 0.72,
    w: rect.w - 1.6,
    h: 1.65,
  };
  drawWorldRect(
    rctx,
    panel.x,
    panel.y,
    panel.w,
    panel.h,
    palette.panel,
    topZ + 0.12,
  );
  strokeWorldRect(
    rctx,
    panel.x,
    panel.y,
    panel.w,
    panel.h,
    palette.cyan,
    topZ + 0.14,
  );
  for (let x = panel.x + 0.42; x < panel.x + panel.w - 0.2; x += 0.54) {
    drawWorldRect(
      rctx,
      x,
      panel.y + 0.12,
      0.04,
      panel.h - 0.24,
      palette.panelGrid,
      topZ + 0.16,
    );
  }
  for (let y = panel.y + 0.38; y < panel.y + panel.h - 0.16; y += 0.38) {
    drawWorldRect(
      rctx,
      panel.x + 0.12,
      y,
      panel.w - 0.24,
      0.04,
      palette.panelGrid,
      topZ + 0.16,
    );
  }

  const modules = [
    {
      x: rect.x + 1.12,
      y: rect.y + 2.62,
      w: 1.18,
      h: 0.64,
      fill: palette.green,
    },
    {
      x: rect.x + 2.66,
      y: rect.y + 2.54,
      w: 1.16,
      h: 0.64,
      fill: palette.cyan,
    },
    {
      x: rect.x + 4.22,
      y: rect.y + 2.48,
      w: 1.08,
      h: 0.7,
      fill: palette.yellow,
    },
    {
      x: rect.x + 5.68,
      y: rect.y + 2.44,
      w: 0.86,
      h: 0.76,
      fill: palette.violet,
    },
  ];
  for (const mod of modules) {
    drawRaisedBox(
      rctx,
      mod,
      topZ + 0.08,
      0.18,
      shade(mod.fill, 18),
      shade(mod.fill, -34),
      mod.fill,
      shade(mod.fill, -44),
      palette.panel,
    );
    drawWorldRect(
      rctx,
      mod.x + 0.18,
      mod.y + 0.22,
      mod.w - 0.36,
      0.08,
      "#F9FFFF",
      topZ + 0.28,
    );
  }

  strokeWorldSegment(
    rctx,
    rect.x + 1.6,
    rect.y + 1.42,
    topZ + 0.2,
    rect.x + 2.86,
    rect.y + 1.08,
    topZ + 0.2,
    palette.orange,
    2,
  );
  strokeWorldSegment(
    rctx,
    rect.x + 2.86,
    rect.y + 1.08,
    topZ + 0.2,
    rect.x + 4.4,
    rect.y + 1.62,
    topZ + 0.2,
    palette.cyan,
    2,
  );
  strokeWorldSegment(
    rctx,
    rect.x + 4.4,
    rect.y + 1.62,
    topZ + 0.2,
    rect.x + 6.2,
    rect.y + 1.04,
    topZ + 0.2,
    palette.green,
    2,
  );

  drawWorldRect(
    rctx,
    rect.x + 0.72,
    rect.y + rect.h - 0.78,
    rect.w - 1.44,
    0.18,
    palette.frontRail,
    topZ + 0.1,
  );
  for (const drawerX of [rect.x + 0.92, rect.x + 3.18, rect.x + 5.52]) {
    drawVerticalFaceX(
      rctx,
      drawerX,
      rect.y + rect.h + 0.02,
      1.08,
      0.36,
      0.54,
      palette.drawer,
    );
    drawVerticalFaceX(
      rctx,
      drawerX + 0.26,
      rect.y + rect.h + 0.03,
      0.44,
      0.58,
      0.08,
      palette.yellow,
    );
  }
}

export function drawMonitorDesk(rctx: RenderContext, rect: Rect): void {
  const topZ = rctx.surfaceZ.value;
  const legTopZ = topZ - 0.28;

  drawRaisedBox(
    rctx,
    rect,
    legTopZ,
    0.28,
    "#8A765E",
    "#5E4C3B",
    "#75624E",
    "#4C3E31",
    "#C6B69E",
  );

  const legColor = "#39424A";
  const legTop = "#59636A";
  const legStroke = "#839099";
  for (const leg of [
    { x: rect.x + 0.28, y: rect.y + 0.35 },
    { x: rect.x + rect.w - 0.62, y: rect.y + 0.35 },
    { x: rect.x + 0.28, y: rect.y + rect.h - 0.7 },
    { x: rect.x + rect.w - 0.62, y: rect.y + rect.h - 0.7 },
  ]) {
    drawRaisedBox(
      rctx,
      { x: leg.x, y: leg.y, w: 0.34, h: 0.34 },
      0,
      legTopZ,
      legTop,
      legColor,
      shade(legColor, 18),
      shade(legColor, -18),
      legStroke,
    );
  }

  drawWorldRect(
    rctx,
    rect.x + 0.35,
    rect.y + 0.35,
    rect.w - 0.7,
    0.12,
    "#BBAA90",
    topZ + 0.02,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.35,
    rect.y + rect.h - 0.47,
    rect.w - 0.7,
    0.12,
    "#4E4134",
    topZ + 0.03,
  );

  const panelZ = topZ + 0.1;
  drawDesktopMonitorPanel(
    rctx,
    rect.x + 0.42,
    rect.y + 1.4,
    rect.x + 1.62,
    rect.y + 0.72,
    panelZ,
    "screen",
    "#D6B85A",
  );
  drawWorldRect(
    rctx,
    rect.x + 0.34,
    rect.y + 2.08,
    0.12,
    1.32,
    "#111820",
    panelZ + 0.04,
  );
  drawDesktopMonitorPanel(
    rctx,
    rect.x + 0.42,
    rect.y + 4.28,
    rect.x + 1.55,
    rect.y + 5.02,
    panelZ,
    "back",
    "#8EB9C9",
  );

  for (const stand of [
    { x: rect.x + 0.7, y: rect.y + 1.44 },
    { x: rect.x + 0.7, y: rect.y + 4.5 },
  ]) {
    drawWorldRect(rctx, stand.x, stand.y, 0.36, 0.16, "#2B333A", topZ + 0.08);
  }
}

function drawDesktopMonitorPanel(
  rctx: RenderContext,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  zUnits: number,
  visibleSide: "screen" | "back",
  accent: string,
): void {
  const bezel = visibleSide === "screen" ? "#17212A" : "#29343C";
  const stroke = visibleSide === "screen" ? "#6E7A82" : "#101820";
  const dx = x2 - x1;
  const dy = y2 - y1;
  const length = Math.max(0.001, Math.hypot(dx, dy));
  const halfThickness = 0.16;
  const nx = (-dy / length) * halfThickness;
  const ny = (dx / length) * halfThickness;
  const baseZ = zUnits + 0.12;
  const panelHeight = 0.88;
  const face = [
    projectedPoint(rctx, x1, y1, baseZ),
    projectedPoint(rctx, x2, y2, baseZ),
    projectedPoint(rctx, x2, y2, baseZ + panelHeight),
    projectedPoint(rctx, x1, y1, baseZ + panelHeight),
  ];

  rctx.ctx.fillStyle = "#1B252D";
  drawPolygon(rctx, [
    projectedPoint(rctx, x1 + nx, y1 + ny, zUnits),
    projectedPoint(rctx, x2 + nx, y2 + ny, zUnits),
    projectedPoint(rctx, x2 - nx, y2 - ny, zUnits),
    projectedPoint(rctx, x1 - nx, y1 - ny, zUnits),
  ]);

  rctx.ctx.fillStyle = bezel;
  drawPolygon(rctx, face);
  rctx.ctx.strokeStyle = stroke;
  rctx.ctx.lineWidth = 1;
  strokePolygon(rctx, face);

  rctx.ctx.fillStyle = shade(bezel, 24);
  drawPolygon(rctx, [
    projectedPoint(rctx, x2, y2, baseZ),
    projectedPoint(rctx, x2 + nx * 0.7, y2 + ny * 0.7, baseZ),
    projectedPoint(rctx, x2 + nx * 0.7, y2 + ny * 0.7, baseZ + panelHeight),
    projectedPoint(rctx, x2, y2, baseZ + panelHeight),
  ]);

  if (visibleSide === "screen") {
    rctx.ctx.fillStyle = accent;
    drawPolygon(rctx, [
      projectedPoint(rctx, x1 + dx * 0.12, y1 + dy * 0.12, baseZ + 0.22),
      projectedPoint(rctx, x1 + dx * 0.42, y1 + dy * 0.42, baseZ + 0.22),
      projectedPoint(rctx, x1 + dx * 0.42, y1 + dy * 0.42, baseZ + 0.34),
      projectedPoint(rctx, x1 + dx * 0.12, y1 + dy * 0.12, baseZ + 0.34),
    ]);
    rctx.ctx.fillStyle = "#8EB9C9";
    drawPolygon(rctx, [
      projectedPoint(rctx, x1 + dx * 0.52, y1 + dy * 0.52, baseZ + 0.52),
      projectedPoint(rctx, x1 + dx * 0.86, y1 + dy * 0.86, baseZ + 0.52),
      projectedPoint(rctx, x1 + dx * 0.86, y1 + dy * 0.86, baseZ + 0.64),
      projectedPoint(rctx, x1 + dx * 0.52, y1 + dy * 0.52, baseZ + 0.64),
    ]);
  } else {
    rctx.ctx.fillStyle = "#111820";
    drawPolygon(rctx, [
      projectedPoint(rctx, x1 + dx * 0.32, y1 + dy * 0.32, baseZ + 0.34),
      projectedPoint(rctx, x1 + dx * 0.58, y1 + dy * 0.58, baseZ + 0.34),
      projectedPoint(rctx, x1 + dx * 0.58, y1 + dy * 0.58, baseZ + 0.48),
      projectedPoint(rctx, x1 + dx * 0.32, y1 + dy * 0.32, baseZ + 0.48),
    ]);
  }
}
