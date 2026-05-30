import type { Rect } from "../../../office/layout";
import type { RenderContext } from "../context";
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

export function drawGridZenRechargeMatV2(
  rctx: RenderContext,
  rect: Rect,
): void {
  const topZ = rctx.surfaceZ.value;
  const dark = rctx.theme === "dark";
  const accent = dark ? "#2BE0B1" : "#129970";
  const palette = dark
    ? {
        top: "rgba(16, 70, 75, 0.96)",
        front: "rgba(5, 31, 35, 0.96)",
        left: "rgba(13, 58, 62, 0.94)",
        right: "rgba(4, 22, 27, 0.96)",
        glass: "rgba(222, 255, 246, 0.16)",
      }
    : {
        top: "rgba(211, 255, 246, 0.96)",
        front: "rgba(126, 210, 197, 0.96)",
        left: "rgba(178, 247, 237, 0.96)",
        right: "rgba(90, 176, 169, 0.96)",
        glass: "rgba(0, 174, 150, 0.12)",
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
    accent,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.34,
    rect.y + 0.32,
    rect.w - 0.68,
    rect.h - 0.64,
    palette.glass,
    topZ + 0.04,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.34,
    rect.y + 0.32,
    rect.w - 0.68,
    rect.h - 0.64,
    accent,
    topZ + 0.06,
  );
  for (let x = rect.x + 1.1; x < rect.x + rect.w - 0.6; x += 1.05) {
    drawWorldRect(
      rctx,
      x,
      rect.y + 0.62,
      0.28,
      rect.h - 1.24,
      "rgba(222, 255, 246, 0.14)",
      topZ + 0.08,
    );
  }
  for (let y = rect.y + 0.78; y < rect.y + rect.h - 0.42; y += 0.58) {
    drawWorldRect(
      rctx,
      rect.x + 0.84,
      y,
      rect.w - 1.68,
      0.06,
      accent,
      topZ + 0.1,
    );
  }
  drawWorldRect(
    rctx,
    rect.x + rect.w - 1.42,
    rect.y + 0.62,
    0.58,
    0.44,
    accent,
    topZ + 0.12,
  );
}

export function drawGridZenRechargeRailV2(
  rctx: RenderContext,
  rect: Rect,
): void {
  const topZ = rctx.surfaceZ.value;
  const dark = rctx.theme === "dark";
  const accent = dark ? "#60F0C6" : "#22AE84";
  const base = dark ? "rgba(10, 53, 55, 0.96)" : "rgba(205, 255, 245, 0.96)";
  const front = dark ? "rgba(4, 27, 31, 0.96)" : "rgba(111, 207, 190, 0.96)";

  drawRaisedBox(
    rctx,
    rect,
    0,
    topZ,
    base,
    front,
    "rgba(18, 79, 76, 0.92)",
    "rgba(6, 32, 38, 0.95)",
    accent,
  );
  for (let x = rect.x + 0.64; x < rect.x + rect.w - 0.74; x += 1.1) {
    drawRaisedBox(
      rctx,
      { x, y: rect.y + 0.46, w: 0.58, h: 0.56 },
      topZ + 0.04,
      0.22,
      accent,
      shade(accent, -42),
      shade(accent, -12),
      shade(accent, -52),
      "rgba(3, 20, 28, 0.76)",
    );
  }
  drawWorldRect(
    rctx,
    rect.x + 0.46,
    rect.y + 1.24,
    rect.w - 0.92,
    0.24,
    "rgba(222, 255, 246, 0.16)",
    topZ + 0.12,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.46,
    rect.y + 1.24,
    rect.w - 0.92,
    0.24,
    accent,
    topZ + 0.13,
  );
}

export function drawGridZenRechargePylonClusterV2(
  rctx: RenderContext,
  rect: Rect,
): void {
  const topZ = rctx.surfaceZ.value;
  const dark = rctx.theme === "dark";
  const accent = dark ? "#39D9FF" : "#008BBC";
  const base = dark ? "rgba(8, 49, 58, 0.92)" : "rgba(211, 252, 255, 0.92)";

  drawRaisedBox(
    rctx,
    rect,
    0,
    topZ,
    base,
    shade("#1C777E", -24),
    shade("#1C777E", 6),
    shade("#1C777E", -38),
    accent,
  );
  const pylons = [
    { x: rect.x + 0.62, y: rect.y + 0.58 },
    { x: rect.x + 2.34, y: rect.y + 0.76 },
    { x: rect.x + 1.48, y: rect.y + 1.86 },
  ];
  for (const pylon of pylons) {
    drawRaisedBox(
      rctx,
      { x: pylon.x, y: pylon.y, w: 0.5, h: 0.5 },
      topZ + 0.04,
      0.52,
      accent,
      shade(accent, -42),
      shade(accent, -12),
      shade(accent, -52),
      "rgba(3, 20, 28, 0.76)",
    );
  }
  strokeWorldSegment(
    rctx,
    rect.x + 0.88,
    rect.y + 0.84,
    topZ + 0.68,
    rect.x + 2.6,
    rect.y + 1.02,
    topZ + 0.68,
    accent,
    1.5,
  );
  strokeWorldSegment(
    rctx,
    rect.x + 2.6,
    rect.y + 1.02,
    topZ + 0.68,
    rect.x + 1.74,
    rect.y + 2.12,
    topZ + 0.68,
    accent,
    1.5,
  );
}

export function drawSeatedBuddha(rctx: RenderContext, rect: Rect): void {
  const bronze = "#B88B45";
  const bronzeTop = "#D8B362";
  const bronzeShade = "#7F572E";

  drawRaisedBox(
    rctx,
    { x: rect.x + 0.38, y: rect.y + 1.86, w: 2.24, h: 0.76 },
    0,
    0.46,
    "#78684C",
    "#4D4234",
    "#665842",
    "#3B3228",
    "#AFA082",
  );
  drawWorldRect(
    rctx,
    rect.x + 0.62,
    rect.y + 1.98,
    1.76,
    0.24,
    "#C8B986",
    0.52,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.86,
    rect.y + 2.22,
    1.28,
    0.18,
    "#8E7C5B",
    0.54,
  );

  drawRaisedBox(
    rctx,
    { x: rect.x + 0.52, y: rect.y + 1.36, w: 0.86, h: 0.78 },
    0.46,
    0.56,
    bronzeTop,
    bronze,
    shade(bronze, 10),
    bronzeShade,
    "#E0C57A",
  );
  drawRaisedBox(
    rctx,
    { x: rect.x + 1.58, y: rect.y + 1.36, w: 0.86, h: 0.78 },
    0.46,
    0.56,
    bronzeTop,
    bronze,
    shade(bronze, 10),
    bronzeShade,
    "#E0C57A",
  );
  drawWorldRect(
    rctx,
    rect.x + 1.03,
    rect.y + 1.42,
    0.94,
    0.62,
    "#C89948",
    1.08,
  );

  drawRaisedBox(
    rctx,
    { x: rect.x + 1.14, y: rect.y + 0.92, w: 0.72, h: 0.78 },
    1.02,
    0.98,
    bronzeTop,
    bronze,
    shade(bronze, 8),
    bronzeShade,
    "#E4C979",
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.92,
    rect.y + 2.06,
    0.46,
    1.26,
    0.16,
    "#D8B362",
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 1.62,
    rect.y + 2.06,
    0.46,
    1.26,
    0.16,
    "#D8B362",
  );

  drawRaisedBox(
    rctx,
    { x: rect.x + 1.18, y: rect.y + 0.52, w: 0.64, h: 0.54 },
    1.96,
    0.58,
    "#D7AE5A",
    "#B7833C",
    "#C79547",
    "#7A4F2A",
    "#E8CE83",
  );
  drawWorldRect(rctx, rect.x + 1.3, rect.y + 0.46, 0.42, 0.18, "#6D4A2A", 2.6);
  drawWorldRect(
    rctx,
    rect.x + 1.35,
    rect.y + 0.72,
    0.1,
    0.06,
    "#4A3222",
    2.58,
  );
  drawWorldRect(
    rctx,
    rect.x + 1.58,
    rect.y + 0.72,
    0.1,
    0.06,
    "#4A3222",
    2.58,
  );
  drawWorldRect(
    rctx,
    rect.x + 1.39,
    rect.y + 0.88,
    0.24,
    0.06,
    "#7A4F2A",
    2.59,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.72,
    rect.y + 2.5,
    1.56,
    0.08,
    "rgba(255, 236, 175, 0.34)",
    0.58,
  );
}

export function drawGridZenVendingMachineV2(
  rctx: RenderContext,
  rect: Rect,
): void {
  const topZ = rctx.surfaceZ.value;
  const dark = rctx.theme === "dark";
  const accent = dark ? "#2BE0B1" : "#129970";
  const secondary = dark ? "#39D9FF" : "#008BBC";
  const colors = dark
    ? {
        top: "rgba(15, 77, 74, 0.98)",
        front: "rgba(4, 31, 35, 0.98)",
        left: "rgba(9, 58, 61, 0.96)",
        right: "rgba(3, 20, 27, 0.98)",
        glassFrame: "rgba(46, 255, 205, 0.30)",
        glass: "rgba(10, 47, 54, 0.88)",
        shelf: "rgba(221, 255, 247, 0.62)",
        panel: "rgba(216, 255, 247, 0.90)",
        panelScreen: "rgba(5, 27, 35, 0.96)",
        bay: "rgba(5, 20, 28, 0.96)",
        itemShadow: "rgba(0, 15, 22, 0.72)",
      }
    : {
        top: "rgba(203, 255, 245, 0.98)",
        front: "rgba(98, 190, 181, 0.98)",
        left: "rgba(169, 246, 234, 0.98)",
        right: "rgba(64, 148, 149, 0.98)",
        glassFrame: "rgba(18, 153, 112, 0.32)",
        glass: "rgba(225, 255, 250, 0.88)",
        shelf: "rgba(0, 96, 112, 0.52)",
        panel: "rgba(246, 255, 253, 0.98)",
        panelScreen: "rgba(7, 70, 82, 0.92)",
        bay: "rgba(16, 93, 96, 0.88)",
        itemShadow: "rgba(0, 95, 102, 0.34)",
      };
  const faceY = rect.y + rect.h + 0.04;
  const windowX = rect.x + 0.34;
  const windowW = rect.w - 1.38;
  const panelX = rect.x + rect.w - 0.9;
  const itemColors = dark
    ? ["#2BE0B1", "#39D9FF", "#60F0C6", "#FFB45F"]
    : ["#129970", "#008BBC", "#22AE84", "#E0702C"];

  drawRaisedBox(
    rctx,
    rect,
    0,
    topZ,
    colors.top,
    colors.front,
    colors.left,
    colors.right,
    accent,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.22,
    rect.y + 0.2,
    rect.w - 0.44,
    0.24,
    secondary,
    topZ + 0.04,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.32,
    rect.y + 0.28,
    rect.w - 0.64,
    0.06,
    "rgba(255, 255, 255, 0.42)",
    topZ + 0.06,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.18,
    rect.y + 0.16,
    rect.w - 0.36,
    rect.h - 0.32,
    secondary,
    topZ + 0.07,
  );

  drawVerticalFaceX(
    rctx,
    windowX,
    faceY + 0.01,
    windowW,
    0.86,
    3.74,
    colors.glassFrame,
  );
  drawVerticalFaceX(
    rctx,
    windowX + 0.12,
    faceY + 0.02,
    windowW - 0.24,
    1.02,
    3.42,
    colors.glass,
  );
  drawVerticalFaceX(
    rctx,
    windowX + 0.18,
    faceY + 0.03,
    0.08,
    1.12,
    3.18,
    "rgba(255, 255, 255, 0.18)",
  );
  for (let shelf = 0; shelf < 4; shelf++) {
    const z = 1.42 + shelf * 0.68;
    drawVerticalFaceX(
      rctx,
      windowX + 0.22,
      faceY + 0.04,
      windowW - 0.42,
      z,
      0.05,
      colors.shelf,
    );
    for (let item = 0; item < 4; item++) {
      const x = windowX + 0.38 + item * 0.42;
      const itemColor = itemColors[(shelf + item) % itemColors.length];
      drawVerticalFaceX(
        rctx,
        x,
        faceY + 0.05,
        0.24,
        z + 0.1,
        0.3,
        colors.itemShadow,
      );
      drawVerticalFaceX(
        rctx,
        x + 0.02,
        faceY + 0.06,
        0.2,
        z + 0.13,
        0.22,
        itemColor,
      );
      drawVerticalFaceX(
        rctx,
        x + 0.05,
        faceY + 0.07,
        0.14,
        z + 0.29,
        0.03,
        "rgba(255, 255, 255, 0.52)",
      );
    }
  }
  for (let x = windowX + 0.46; x < windowX + windowW - 0.34; x += 0.54) {
    strokeWorldSegment(
      rctx,
      x,
      faceY + 0.045,
      1.02,
      x,
      faceY + 0.045,
      4.36,
      "rgba(222, 255, 246, 0.18)",
      0.8,
    );
  }

  drawVerticalFaceX(rctx, panelX, faceY + 0.02, 0.56, 0.82, 3.8, colors.panel);
  drawVerticalFaceX(
    rctx,
    panelX + 0.08,
    faceY + 0.03,
    0.4,
    3.56,
    0.42,
    colors.panelScreen,
  );
  drawVerticalFaceX(
    rctx,
    panelX + 0.12,
    faceY + 0.04,
    0.32,
    3.78,
    0.08,
    accent,
  );
  for (let row = 0; row < 4; row++) {
    for (let col = 0; col < 2; col++) {
      const fill = (row + col) % 2 === 0 ? accent : secondary;
      drawVerticalFaceX(
        rctx,
        panelX + 0.12 + col * 0.16,
        faceY + 0.04,
        0.09,
        2.64 + row * 0.18,
        0.09,
        fill,
      );
    }
  }
  drawVerticalFaceX(
    rctx,
    panelX + 0.12,
    faceY + 0.04,
    0.32,
    1.94,
    0.24,
    colors.panelScreen,
  );
  drawVerticalFaceX(
    rctx,
    panelX + 0.18,
    faceY + 0.05,
    0.2,
    2.02,
    0.06,
    "#FFB45F",
  );
  drawVerticalFaceX(
    rctx,
    panelX + 0.13,
    faceY + 0.04,
    0.3,
    1.2,
    0.28,
    colors.bay,
  );
  drawVerticalFaceX(
    rctx,
    panelX + 0.2,
    faceY + 0.05,
    0.16,
    1.28,
    0.05,
    "rgba(222, 255, 246, 0.62)",
  );

  drawGridVendingGlyph(
    rctx,
    rect.x + rect.w - 0.42,
    faceY + 0.04,
    4.34,
    accent,
    secondary,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.42,
    faceY + 0.03,
    2.0,
    0.54,
    0.54,
    colors.bay,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.66,
    faceY + 0.04,
    1.5,
    0.8,
    0.08,
    "rgba(222, 255, 246, 0.74)",
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.78,
    faceY + 0.05,
    1.26,
    0.94,
    0.08,
    colors.panelScreen,
  );
  strokeWorldSegment(
    rctx,
    rect.x + 0.28,
    faceY + 0.04,
    4.72,
    rect.x + rect.w - 0.28,
    faceY + 0.04,
    4.72,
    accent,
    1.4,
  );
}

function drawGridVendingGlyph(
  rctx: RenderContext,
  x: number,
  y: number,
  z: number,
  accent: string,
  secondary: string,
): void {
  const node = projectedPoint(rctx, x, y, z);
  const points = [
    projectedPoint(rctx, x - 0.3, y, z - 0.18),
    projectedPoint(rctx, x + 0.24, y, z - 0.34),
    projectedPoint(rctx, x + 0.34, y, z + 0.2),
    projectedPoint(rctx, x - 0.08, y, z + 0.42),
  ];
  rctx.ctx.fillStyle = accent;
  drawPolygon(rctx, points);
  rctx.ctx.strokeStyle = secondary;
  rctx.ctx.lineWidth = 1;
  strokePolygon(rctx, points);
  rctx.ctx.fillStyle = "rgba(255, 255, 255, 0.64)";
  rctx.ctx.beginPath();
  rctx.ctx.arc(node.x, node.y, 2.2, 0, Math.PI * 2);
  rctx.ctx.fill();
  rctx.ctx.lineWidth = 1;
}
