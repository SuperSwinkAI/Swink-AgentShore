import type { Rect } from "../../../office/layout";
import type { RenderContext } from "../context";
import {
  drawRaisedBox,
  drawWorldRect,
  shade,
  strokeWorldRect,
  strokeWorldSegment,
} from "../primitives";

export function drawRecoveryScrapPile(
  rctx: RenderContext,
  rect: Rect & { name: string },
): void {
  drawGridRecoveryJunkBin(rctx, rect);
}

export function drawGridRecoveryJunkBin(
  rctx: RenderContext,
  rect: Rect & { name: string },
): void {
  const topZ = rctx.surfaceZ.value;
  const dark = rctx.theme === "dark";
  const palette = dark
    ? {
        binTop: "#46606B",
        binFront: "#1C303A",
        binLeft: "#58717C",
        binRight: "#12232C",
        rim: "#8AC7D4",
        cavity: "#0B1820",
        junkA: "#7F98A3",
        junkB: "#D46B58",
        junkC: "#314854",
        junkD: "#D8B75C",
      }
    : {
        binTop: "#D4E6EA",
        binFront: "#7896A0",
        binLeft: "#E1F0F3",
        binRight: "#5E7F8B",
        rim: "#4D92A4",
        cavity: "#B7CDD4",
        junkA: "#758D98",
        junkB: "#C95C62",
        junkC: "#3B5763",
        junkD: "#D99B4A",
      };

  drawRaisedBox(
    rctx,
    rect,
    0,
    topZ,
    palette.binTop,
    palette.binFront,
    palette.binLeft,
    palette.binRight,
    palette.rim,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.32,
    rect.y + 0.34,
    rect.w - 0.64,
    rect.h - 0.82,
    palette.cavity,
    topZ + 0.05,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.32,
    rect.y + 0.34,
    rect.w - 0.64,
    rect.h - 0.82,
    palette.rim,
    topZ + 0.08,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.24,
    rect.y + 0.2,
    rect.w - 0.48,
    0.24,
    palette.rim,
    topZ + 0.12,
  );

  const chunks = rect.name.endsWith("NW")
    ? [
        { x: 0.52, y: 0.62, w: 0.72, h: 0.5, z: 0.13, fill: palette.junkA },
        { x: 1.42, y: 0.74, w: 0.74, h: 0.46, z: 0.22, fill: palette.junkB },
        { x: 0.9, y: 1.42, w: 1.02, h: 0.46, z: 0.16, fill: palette.junkC },
      ]
    : [
        { x: 1.28, y: 0.54, w: 0.86, h: 0.46, z: 0.2, fill: palette.junkA },
        { x: 0.64, y: 1.0, w: 0.76, h: 0.48, z: 0.14, fill: palette.junkB },
        { x: 0.98, y: 1.66, w: 0.96, h: 0.42, z: 0.12, fill: palette.junkC },
      ];

  for (const chunk of chunks) {
    drawRaisedBox(
      rctx,
      { x: rect.x + chunk.x, y: rect.y + chunk.y, w: chunk.w, h: chunk.h },
      topZ + chunk.z,
      0.22,
      shade(chunk.fill, 16),
      shade(chunk.fill, -32),
      chunk.fill,
      shade(chunk.fill, -42),
      palette.rim,
    );
  }

  strokeWorldSegment(
    rctx,
    rect.x + 0.54,
    rect.y + 2.08,
    topZ + 0.28,
    rect.x + 2.12,
    rect.y + 0.88,
    topZ + 0.62,
    palette.junkD,
    1.4,
  );
  strokeWorldSegment(
    rctx,
    rect.x + 0.68,
    rect.y + 0.88,
    topZ + 0.5,
    rect.x + 2.24,
    rect.y + 2.02,
    topZ + 0.32,
    palette.rim,
    1.2,
  );
}
