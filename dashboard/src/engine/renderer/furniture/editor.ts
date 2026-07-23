import type { Rect } from "../../../office/layout";
import type { ResolvedTheme } from "../../../theme";
import type { RenderContext, ScreenPoint } from "../context";
import { drawFurnitureBase } from "./index";
import { CYAN_ACCENT, GREEN_ACCENT, ORANGE_ACCENT_WARM } from "./palettes";
import {
  drawPolygon,
  drawRaisedBox,
  drawVerticalFaceX,
  drawWorldRect,
  projectedPoint,
  shade,
  strokePolygon,
  strokeVerticalFaceX,
  strokeWorldRect,
  strokeWorldSegment,
} from "../primitives";

// Only reused within this file (editor pair pod NE accent + repo cube),
// so kept local rather than promoted to the shared palettes module.
const PURPLE_ACCENT: Record<ResolvedTheme, string> = {
  dark: "#B266FF",
  light: "#7E4EBA",
};

export function drawEditorBookcases(rctx: RenderContext, rect: Rect): void {
  const topZ = rctx.surfaceZ.value;
  const faceY = rect.y + rect.h + 0.02;
  const wood = "#5C3D2C";

  drawRaisedBox(
    rctx,
    rect,
    0,
    topZ,
    "#6D4C35",
    wood,
    "#7A563C",
    "#412B20",
    "#A57A56",
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.18,
    faceY,
    rect.w - 0.36,
    0.28,
    topZ - 0.56,
    "#4D3528",
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.18,
    faceY,
    rect.w - 0.36,
    topZ - 0.24,
    0.18,
    "#9A6C49",
  );

  for (let shelfZ = 1.05; shelfZ < topZ - 0.6; shelfZ += 1.12) {
    drawVerticalFaceX(
      rctx,
      rect.x + 0.34,
      faceY + 0.01,
      rect.w - 0.68,
      shelfZ,
      0.1,
      "#B1845B",
    );
  }

  for (
    let dividerX = rect.x + 3;
    dividerX < rect.x + rect.w - 0.5;
    dividerX += 3
  ) {
    drawVerticalFaceX(
      rctx,
      dividerX,
      faceY + 0.02,
      0.12,
      0.46,
      topZ - 0.88,
      "#9A6C49",
    );
  }

  const bookColors = [
    "#A84D4D",
    "#4F7EA8",
    "#D6B85A",
    "#7E9A5B",
    "#C76A42",
    "#6A5A91",
  ];
  let colorIndex = 0;
  for (
    let bayX = rect.x + 0.55;
    bayX < rect.x + rect.w - 0.75;
    bayX += 1.08
  ) {
    for (let shelfZ = 1.18; shelfZ < topZ - 0.78; shelfZ += 1.12) {
      const fill = bookColors[colorIndex % bookColors.length];
      const height = 0.46 + (colorIndex % 3) * 0.1;
      drawVerticalFaceX(
        rctx,
        bayX,
        faceY + 0.03,
        0.22,
        shelfZ,
        height,
        fill,
      );
      drawVerticalFaceX(
        rctx,
        bayX + 0.32,
        faceY + 0.03,
        0.18,
        shelfZ,
        height + 0.12,
        bookColors[(colorIndex + 2) % bookColors.length],
      );
      drawVerticalFaceX(
        rctx,
        bayX + 0.58,
        faceY + 0.03,
        0.24,
        shelfZ,
        height - 0.06,
        bookColors[(colorIndex + 4) % bookColors.length],
      );
      colorIndex += 1;
    }
  }

  drawVerticalFaceX(
    rctx,
    rect.x + 0.22,
    faceY + 0.04,
    rect.w - 0.44,
    0.42,
    0.16,
    "#B88A5E",
  );
  drawWorldRect(
    rctx,
    rect.x + 0.22,
    rect.y + 0.18,
    rect.w - 0.44,
    0.22,
    "#8F6748",
    topZ + 0.04,
  );
}

export function drawDraftingTable(rctx: RenderContext, rect: Rect): void {
  const topZ = rctx.surfaceZ.value;
  const slab = {
    x: rect.x + 0.28,
    y: rect.y + 0.35,
    w: rect.w - 0.56,
    h: rect.h - 0.7,
  };

  for (const leg of [
    { x: rect.x + 0.62, y: rect.y + 0.72 },
    { x: rect.x + rect.w - 0.82, y: rect.y + 0.72 },
    { x: rect.x + 0.62, y: rect.y + rect.h - 0.96 },
    { x: rect.x + rect.w - 0.82, y: rect.y + rect.h - 0.96 },
  ]) {
    drawRaisedBox(
      rctx,
      { x: leg.x, y: leg.y, w: 0.2, h: 0.2 },
      0,
      topZ - 0.35,
      "#2B2521",
      "#1C1714",
      "#382F29",
      "#120E0C",
      "#5C514A",
    );
  }

  strokeWorldSegment(
    rctx,
    rect.x + 0.72,
    rect.y + 1.0,
    0.62,
    rect.x + 1.72,
    rect.y + 3.1,
    topZ - 0.28,
    "#1F1916",
    2,
  );
  strokeWorldSegment(
    rctx,
    rect.x + rect.w - 0.72,
    rect.y + 1.0,
    0.62,
    rect.x + rect.w - 1.72,
    rect.y + 3.1,
    topZ - 0.28,
    "#1F1916",
    2,
  );
  drawRaisedBox(
    rctx,
    slab,
    topZ - 0.32,
    0.32,
    "#9A6B43",
    "#5E3D28",
    "#7F5738",
    "#4B2F20",
    "#C19A72",
  );
  drawWorldRect(
    rctx,
    slab.x + 0.22,
    slab.y + 0.18,
    slab.w - 0.44,
    0.16,
    "#B98252",
    topZ + 0.04,
  );
  drawWorldRect(
    rctx,
    slab.x + 0.22,
    slab.y + slab.h - 0.34,
    slab.w - 0.44,
    0.16,
    "#583824",
    topZ + 0.05,
  );

  const leftBlueprint: ScreenPoint[] = [
    projectedPoint(rctx, rect.x + 0.9, rect.y + 0.72, topZ + 0.08),
    projectedPoint(rctx, rect.x + 2.75, rect.y + 0.52, topZ + 0.08),
    projectedPoint(rctx, rect.x + 2.92, rect.y + 2.08, topZ + 0.08),
    projectedPoint(rctx, rect.x + 0.78, rect.y + 2.24, topZ + 0.08),
  ];
  rctx.ctx.fillStyle = "#2B77B7";
  drawPolygon(rctx, leftBlueprint);
  rctx.ctx.strokeStyle = "#B7D7EA";
  rctx.ctx.lineWidth = 1;
  strokePolygon(rctx, leftBlueprint);

  const rightBlueprint: ScreenPoint[] = [
    projectedPoint(rctx, rect.x + 2.95, rect.y + 0.62, topZ + 0.09),
    projectedPoint(rctx, rect.x + 5.15, rect.y + 0.78, topZ + 0.09),
    projectedPoint(rctx, rect.x + 4.85, rect.y + 2.62, topZ + 0.09),
    projectedPoint(rctx, rect.x + 2.75, rect.y + 2.22, topZ + 0.09),
  ];
  rctx.ctx.fillStyle = "#245EAB";
  drawPolygon(rctx, rightBlueprint);
  rctx.ctx.strokeStyle = "#B7D7EA";
  rctx.ctx.lineWidth = 1;
  strokePolygon(rctx, rightBlueprint);

  for (let i = 0; i < 4; i++) {
    drawWorldRect(
      rctx,
      rect.x + 1.12,
      rect.y + 1.02 + i * 0.26,
      1.25,
      0.04,
      "#D7E8F4",
      topZ + 0.12,
    );
    drawWorldRect(
      rctx,
      rect.x + 3.22,
      rect.y + 1.08 + i * 0.28,
      1.18,
      0.04,
      "#D7E8F4",
      topZ + 0.12,
    );
  }
  drawWorldRect(
    rctx,
    rect.x + 3.58,
    rect.y + 1.98,
    0.76,
    0.04,
    "#D7E8F4",
    topZ + 0.12,
  );
  drawWorldRect(
    rctx,
    rect.x + 3.94,
    rect.y + 1.72,
    0.04,
    0.64,
    "#D7E8F4",
    topZ + 0.12,
  );

  drawWorldRect(
    rctx,
    rect.x + 0.72,
    rect.y + 2.58,
    0.9,
    0.7,
    "#E5DCC8",
    topZ + 0.1,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.86,
    rect.y + 2.82,
    0.5,
    0.05,
    "#9D8B74",
    topZ + 0.13,
  );
  drawWorldRect(
    rctx,
    rect.x + rect.w - 1.58,
    rect.y + 2.76,
    0.82,
    0.58,
    "#E5DCC8",
    topZ + 0.1,
  );
  drawWorldRect(
    rctx,
    rect.x + rect.w - 1.44,
    rect.y + 2.96,
    0.44,
    0.05,
    "#9D8B74",
    topZ + 0.13,
  );

  drawWorldRect(
    rctx,
    rect.x + 0.68,
    rect.y + 0.58,
    0.46,
    0.32,
    "#1B2026",
    topZ + 0.12,
  );
  strokeWorldSegment(
    rctx,
    rect.x + 0.92,
    rect.y + 0.72,
    topZ + 0.28,
    rect.x + 1.08,
    rect.y + 0.32,
    topZ + 1.28,
    "#1B2026",
    2,
  );
  strokeWorldSegment(
    rctx,
    rect.x + 1.08,
    rect.y + 0.32,
    topZ + 1.28,
    rect.x + 1.68,
    rect.y + 0.52,
    topZ + 1.52,
    "#1B2026",
    2,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 1.48,
    rect.y + 0.58,
    0.55,
    topZ + 1.26,
    0.38,
    "#1B2026",
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 1.58,
    rect.y + 0.59,
    0.28,
    topZ + 1.22,
    0.08,
    "#F0D36A",
  );
}

export function drawGridEditorPairPod(
  rctx: RenderContext,
  rect: Rect & { name: string },
): void {
  const topZ = rctx.surfaceZ.value;
  const dark = rctx.theme === "dark";
  const accent = rect.name.endsWith("NE")
    ? PURPLE_ACCENT[rctx.theme]
    : rect.name.endsWith("SW")
      ? ORANGE_ACCENT_WARM[rctx.theme]
      : rect.name.endsWith("SE")
        ? GREEN_ACCENT[rctx.theme]
        : CYAN_ACCENT[rctx.theme];
  const base = dark
    ? {
        top: "rgba(18, 72, 90, 0.94)",
        front: "rgba(6, 29, 38, 0.96)",
        left: "rgba(12, 51, 63, 0.92)",
        right: "rgba(4, 22, 31, 0.96)",
        panel: "rgba(6, 22, 32, 0.92)",
        glass: "rgba(57, 217, 255, 0.12)",
      }
    : {
        top: "rgba(206, 253, 255, 0.96)",
        front: "rgba(107, 190, 212, 0.96)",
        left: "rgba(166, 241, 250, 0.96)",
        right: "rgba(73, 154, 180, 0.96)",
        panel: "rgba(226, 255, 255, 0.92)",
        glass: "rgba(0, 174, 214, 0.12)",
      };

  drawRaisedBox(
    rctx,
    rect,
    0,
    topZ,
    base.top,
    base.front,
    base.left,
    base.right,
    accent,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.28,
    rect.y + 0.24,
    rect.w - 0.56,
    rect.h - 0.48,
    base.panel,
    topZ + 0.04,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.28,
    rect.y + 0.24,
    rect.w - 0.56,
    rect.h - 0.48,
    accent,
    topZ + 0.06,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.46,
    rect.y + 0.42,
    rect.w - 0.92,
    0.56,
    base.glass,
    topZ + 0.1,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.46,
    rect.y + 0.42,
    rect.w - 0.92,
    0.56,
    accent,
    topZ + 0.12,
  );
  for (let row = 0; row < 3; row += 1) {
    const width = row === 1 ? rect.w - 1.42 : rect.w - 1.72;
    drawWorldRect(
      rctx,
      rect.x + 0.68,
      rect.y + 0.56 + row * 0.16,
      width,
      0.035,
      accent,
      topZ + 0.15,
    );
  }
  drawRaisedBox(
    rctx,
    {
      x: rect.x + rect.w - 0.86,
      y: rect.y + rect.h - 0.72,
      w: 0.38,
      h: 0.34,
    },
    topZ + 0.06,
    0.18,
    accent,
    shade(accent, -42),
    shade(accent, -12),
    shade(accent, -52),
    "rgba(3, 20, 28, 0.76)",
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.34,
    rect.y + rect.h + 0.015,
    rect.w - 0.68,
    topZ + 0.05,
    0.32,
    base.glass,
  );
  strokeVerticalFaceX(
    rctx,
    rect.x + 0.34,
    rect.y + rect.h + 0.015,
    rect.w - 0.68,
    topZ + 0.05,
    0.32,
    accent,
  );
}

export function drawGridEditorRepoCube(rctx: RenderContext, rect: Rect): void {
  const topZ = rctx.surfaceZ.value;
  const dark = rctx.theme === "dark";
  const palette = dark
    ? {
        top: "rgba(18, 72, 90, 0.96)",
        front: "rgba(5, 24, 34, 0.98)",
        left: "rgba(13, 58, 76, 0.95)",
        right: "rgba(4, 18, 30, 0.98)",
        stroke: "rgba(244, 212, 77, 0.74)",
        glass: "rgba(57, 217, 255, 0.16)",
        cyan: CYAN_ACCENT.dark,
        purple: PURPLE_ACCENT.dark,
        green: GREEN_ACCENT.dark,
        orange: ORANGE_ACCENT_WARM.dark,
      }
    : {
        top: "rgba(208, 253, 255, 0.98)",
        front: "rgba(104, 182, 204, 0.96)",
        left: "rgba(160, 238, 249, 0.96)",
        right: "rgba(72, 149, 176, 0.96)",
        stroke: "rgba(221, 101, 24, 0.66)",
        glass: "rgba(0, 174, 214, 0.14)",
        cyan: CYAN_ACCENT.light,
        purple: PURPLE_ACCENT.light,
        green: GREEN_ACCENT.light,
        orange: ORANGE_ACCENT_WARM.light,
      };

  drawRaisedBox(
    rctx,
    rect,
    0,
    topZ,
    palette.top,
    palette.front,
    palette.left,
    palette.right,
    palette.stroke,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.42,
    rect.y + 0.38,
    rect.w - 0.84,
    rect.h - 0.76,
    palette.glass,
    topZ + 0.04,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.42,
    rect.y + 0.38,
    rect.w - 0.84,
    rect.h - 0.76,
    palette.cyan,
    topZ + 0.06,
  );
  strokeWorldSegment(
    rctx,
    rect.x + 0.76,
    rect.y + 1.42,
    topZ + 0.16,
    rect.x + 1.45,
    rect.y + 0.82,
    topZ + 0.16,
    palette.cyan,
    2,
  );
  strokeWorldSegment(
    rctx,
    rect.x + 1.45,
    rect.y + 0.82,
    topZ + 0.16,
    rect.x + 2.14,
    rect.y + 1.42,
    topZ + 0.16,
    palette.purple,
    2,
  );
  strokeWorldSegment(
    rctx,
    rect.x + 1.45,
    rect.y + 0.82,
    topZ + 0.16,
    rect.x + 1.45,
    rect.y + 2.12,
    topZ + 0.16,
    palette.green,
    2,
  );

  const nodes = [
    { x: rect.x + 0.64, y: rect.y + 1.3, fill: palette.cyan },
    { x: rect.x + 1.26, y: rect.y + 0.68, fill: palette.stroke },
    { x: rect.x + 1.96, y: rect.y + 1.3, fill: palette.purple },
    { x: rect.x + 1.26, y: rect.y + 2.0, fill: palette.green },
  ];
  for (const node of nodes) {
    drawRaisedBox(
      rctx,
      { x: node.x, y: node.y, w: 0.38, h: 0.34 },
      topZ + 0.08,
      0.16,
      node.fill,
      shade(node.fill, -42),
      shade(node.fill, -12),
      shade(node.fill, -52),
      "rgba(3, 20, 28, 0.76)",
    );
  }

  drawVerticalFaceX(
    rctx,
    rect.x + 0.38,
    rect.y + rect.h + 0.02,
    rect.w - 0.76,
    topZ + 0.04,
    0.38,
    palette.glass,
  );
  strokeVerticalFaceX(
    rctx,
    rect.x + 0.38,
    rect.y + rect.h + 0.02,
    rect.w - 0.76,
    topZ + 0.04,
    0.38,
    palette.orange,
  );
}

export function drawEditorDesk(rctx: RenderContext, rect: Rect): void {
  drawFurnitureBase(rctx, rect, "#5A3C31", "#765344", "#A88470");
  drawWorldRect(rctx, rect.x + 0.7, rect.y + 0.55, 1.4, 0.9, "#DED6C2");
  drawWorldRect(rctx, rect.x + 2.3, rect.y + 0.7, 0.18, 1.1, "#D35C52");
  drawWorldRect(rctx, rect.x + 4.2, rect.y + 0.5, 0.7, 0.7, "#F0CA6A");
  drawWorldRect(rctx, rect.x + 4.45, rect.y + 1.15, 0.18, 0.75, "#56453A");
}

export function drawBookshelf(rctx: RenderContext, rect: Rect): void {
  drawFurnitureBase(rctx, rect, "#49372C", "#664D3D", "#8E715C");
  for (let y = rect.y + 0.65; y < rect.y + rect.h - 0.4; y += 1) {
    drawWorldRect(rctx, rect.x + 0.25, y, rect.w - 0.5, 0.16, "#B08967");
    drawWorldRect(rctx, rect.x + 0.45, y + 0.22, 0.3, 0.55, "#A84D4D");
    drawWorldRect(rctx, rect.x + 0.9, y + 0.22, 0.3, 0.55, "#4F7EA8");
    drawWorldRect(rctx, rect.x + 1.35, y + 0.22, 0.28, 0.55, "#7E9A5B");
  }
}

export function drawPaperStack(rctx: RenderContext, rect: Rect): void {
  drawFurnitureBase(rctx, rect, "#CFC6AD", "#EEE8D8", "#837A66");
  drawWorldRect(rctx, rect.x + 0.35, rect.y + 0.45, 1.7, 0.18, "#9E8E76");
  drawWorldRect(rctx, rect.x + 0.55, rect.y + 1, 1.7, 0.18, "#9E8E76");
  drawWorldRect(rctx, rect.x + 1.85, rect.y + 0.65, 0.5, 0.14, "#D35C52");
}
