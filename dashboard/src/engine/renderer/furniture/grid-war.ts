import type { Rect } from "../../../office/layout";
import type { RenderContext } from "../context";
import { CYAN_ACCENT, GREEN_ACCENT, ORANGE_ACCENT_WARM } from "./palettes";
import {
  drawRaisedBox,
  drawVerticalFaceX,
  drawWorldRect,
  shade,
  strokeVerticalFaceX,
  strokeWorldRect,
  strokeWorldSegment,
} from "../primitives";

export function drawGridWarTacticalMapTable(
  rctx: RenderContext,
  rect: Rect,
): void {
  const topZ = rctx.surfaceZ.value;
  const dark = rctx.theme === "dark";
  const palette = dark
    ? {
        tableTop: "rgba(14, 65, 77, 0.96)",
        tableFront: "rgba(6, 30, 39, 0.98)",
        tableLeft: "rgba(13, 53, 65, 0.95)",
        tableRight: "rgba(4, 22, 32, 0.98)",
        tableStroke: "rgba(57, 217, 255, 0.70)",
        surface: "rgba(8, 44, 55, 0.96)",
        map: "rgba(57, 217, 255, 0.18)",
        grid: "rgba(147, 246, 255, 0.32)",
        route: "rgba(244, 212, 77, 0.78)",
        markerA: CYAN_ACCENT.dark,
        markerB: "#F4D44D",
        markerC: GREEN_ACCENT.dark,
        markerD: ORANGE_ACCENT_WARM.dark,
        rail: "rgba(57, 217, 255, 0.52)",
        shadow: "rgba(0, 0, 0, 0.24)",
      }
    : {
        tableTop: "rgba(195, 252, 255, 0.96)",
        tableFront: "rgba(104, 182, 204, 0.96)",
        tableLeft: "rgba(160, 238, 249, 0.96)",
        tableRight: "rgba(72, 149, 176, 0.96)",
        tableStroke: "rgba(0, 126, 174, 0.58)",
        surface: "rgba(222, 255, 255, 0.96)",
        map: "rgba(0, 174, 214, 0.18)",
        grid: "rgba(0, 126, 174, 0.30)",
        route: "rgba(221, 101, 24, 0.78)",
        markerA: CYAN_ACCENT.light,
        markerB: "#DD6518",
        markerC: GREEN_ACCENT.light,
        markerD: ORANGE_ACCENT_WARM.light,
        rail: "rgba(0, 126, 174, 0.42)",
        shadow: "rgba(0, 88, 120, 0.10)",
      };
  const map = {
    x: rect.x + 0.9,
    y: rect.y + 0.64,
    w: rect.w - 1.8,
    h: rect.h - 1.28,
  };

  drawWorldRect(
    rctx,
    rect.x + 0.18,
    rect.y + rect.h + 0.12,
    rect.w - 0.36,
    0.22,
    palette.shadow,
    0.04,
  );
  drawRaisedBox(
    rctx,
    rect,
    0,
    topZ,
    palette.tableTop,
    palette.tableFront,
    palette.tableLeft,
    palette.tableRight,
    palette.tableStroke,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.38,
    rect.y + 0.34,
    rect.w - 0.76,
    rect.h - 0.68,
    palette.surface,
    topZ + 0.04,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.38,
    rect.y + 0.34,
    rect.w - 0.76,
    rect.h - 0.68,
    palette.rail,
    topZ + 0.06,
  );
  drawWorldRect(rctx, map.x, map.y, map.w, map.h, palette.map, topZ + 0.1);
  strokeWorldRect(
    rctx,
    map.x,
    map.y,
    map.w,
    map.h,
    palette.tableStroke,
    topZ + 0.12,
  );

  for (let x = map.x + 0.62; x < map.x + map.w - 0.2; x += 0.62) {
    drawWorldRect(
      rctx,
      x,
      map.y + 0.12,
      0.035,
      map.h - 0.24,
      palette.grid,
      topZ + 0.14,
    );
  }
  for (let y = map.y + 0.48; y < map.y + map.h - 0.2; y += 0.48) {
    drawWorldRect(
      rctx,
      map.x + 0.12,
      y,
      map.w - 0.24,
      0.035,
      palette.grid,
      topZ + 0.14,
    );
  }

  rctx.ctx.save();
  rctx.ctx.setLineDash([7, 5]);
  strokeWorldSegment(
    rctx,
    map.x + 0.5,
    map.y + 1.92,
    topZ + 0.18,
    map.x + 1.65,
    map.y + 0.82,
    topZ + 0.18,
    palette.route,
    2,
  );
  strokeWorldSegment(
    rctx,
    map.x + 1.65,
    map.y + 0.82,
    topZ + 0.18,
    map.x + 3.6,
    map.y + 1.18,
    topZ + 0.18,
    palette.route,
    2,
  );
  strokeWorldSegment(
    rctx,
    map.x + 3.6,
    map.y + 1.18,
    topZ + 0.18,
    map.x + map.w - 0.54,
    map.y + 0.62,
    topZ + 0.18,
    palette.route,
    2,
  );
  rctx.ctx.restore();

  const markers = [
    { x: map.x + 0.52, y: map.y + 0.48, fill: palette.markerA },
    { x: map.x + 1.52, y: map.y + 1.28, fill: palette.markerB },
    { x: map.x + 2.82, y: map.y + 0.58, fill: palette.markerC },
    { x: map.x + map.w - 0.86, y: map.y + 1.42, fill: palette.markerD },
  ];
  for (const marker of markers) {
    drawRaisedBox(
      rctx,
      { x: marker.x, y: marker.y, w: 0.32, h: 0.32 },
      topZ + 0.08,
      0.14,
      marker.fill,
      shade(marker.fill, -42),
      shade(marker.fill, -12),
      shade(marker.fill, -52),
      "rgba(3, 20, 28, 0.76)",
    );
  }

  for (let i = 0; i < 6; i += 1) {
    const fill =
      i % 3 === 0
        ? palette.markerA
        : i % 3 === 1
          ? palette.markerB
          : palette.markerC;
    drawWorldRect(
      rctx,
      rect.x + 1.0 + i * 0.54,
      rect.y + 0.42,
      0.3,
      0.18,
      fill,
      topZ + 0.18,
    );
  }
  drawWorldRect(
    rctx,
    rect.x + 0.72,
    rect.y + rect.h - 0.62,
    rect.w - 1.44,
    0.16,
    palette.rail,
    topZ + 0.16,
  );
}

export function drawGridWarTodoWallPlane(
  rctx: RenderContext,
  rect: Rect,
): void {
  const dark = rctx.theme === "dark";
  const palette = dark
    ? {
        baseTop: "rgba(12, 51, 64, 0.96)",
        baseFront: "rgba(5, 24, 34, 0.96)",
        baseLeft: "rgba(15, 59, 72, 0.95)",
        baseRight: "rgba(4, 18, 28, 0.96)",
        stroke: "rgba(57, 217, 255, 0.58)",
        panel: "rgba(57, 217, 255, 0.14)",
        panelEdge: "rgba(57, 217, 255, 0.62)",
        tile: "rgba(57, 217, 255, 0.46)",
        warning: "rgba(244, 212, 77, 0.58)",
        ready: "rgba(43, 224, 177, 0.46)",
      }
    : {
        baseTop: "rgba(208, 253, 255, 0.96)",
        baseFront: "rgba(116, 196, 214, 0.96)",
        baseLeft: "rgba(177, 244, 250, 0.96)",
        baseRight: "rgba(76, 158, 184, 0.96)",
        stroke: "rgba(0, 126, 174, 0.52)",
        panel: "rgba(0, 174, 214, 0.13)",
        panelEdge: "rgba(0, 126, 174, 0.56)",
        tile: "rgba(0, 139, 188, 0.36)",
        warning: "rgba(221, 101, 24, 0.44)",
        ready: "rgba(18, 153, 112, 0.34)",
      };
  const base = {
    x: rect.x + 0.25,
    y: rect.y + 2.02,
    w: rect.w - 0.5,
    h: 0.52,
  };
  const panelX = rect.x + 0.42;
  const panelY = rect.y + 2.1;
  const panelW = rect.w - 0.84;

  drawRaisedBox(
    rctx,
    base,
    0,
    0.72,
    palette.baseTop,
    palette.baseFront,
    palette.baseLeft,
    palette.baseRight,
    palette.stroke,
  );
  drawVerticalFaceX(rctx, panelX, panelY, panelW, 0.72, 2.28, palette.panel);
  strokeVerticalFaceX(
    rctx,
    panelX,
    panelY,
    panelW,
    0.72,
    2.28,
    palette.panelEdge,
  );

  for (let row = 0; row < 3; row += 1) {
    for (let column = 0; column < 4; column += 1) {
      const fill =
        column === 3
          ? palette.warning
          : row === 2
            ? palette.ready
            : palette.tile;
      drawVerticalFaceX(
        rctx,
        panelX + 0.24 + column * 0.42,
        panelY + 0.01,
        0.24,
        1.08 + row * 0.48,
        0.28,
        fill,
      );
    }
  }

  drawVerticalFaceX(
    rctx,
    panelX + 0.18,
    panelY + 0.015,
    panelW - 0.36,
    2.66,
    0.06,
    palette.panelEdge,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.58,
    rect.y + 2.2,
    rect.w - 1.16,
    0.12,
    palette.panelEdge,
    0.78,
  );
}
