import type { Rect } from "../../../office/layout";
import type { RenderContext } from "../context";
import { drawFurnitureBase } from "./index";
import {
  drawRaisedBox,
  drawVerticalFaceX,
  drawWorldRect,
  strokeVerticalFaceX,
} from "../primitives";

export function drawGridBadgeTurnstileV2(
  rctx: RenderContext,
  rect: Rect,
): void {
  const dark = rctx.theme === "dark";
  const topZ = rctx.surfaceZ.value;
  const palette = dark
    ? {
        baseTop: "rgba(10, 42, 61, 0.96)",
        baseFront: "rgba(5, 24, 37, 0.96)",
        baseLeft: "rgba(13, 58, 76, 0.96)",
        baseRight: "rgba(4, 18, 30, 0.96)",
        baseStroke: "rgba(94, 225, 255, 0.58)",
        postTop: "rgba(255, 164, 75, 0.92)",
        postFront: "rgba(136, 68, 24, 0.96)",
        postLeft: "rgba(212, 112, 38, 0.96)",
        postRight: "rgba(82, 39, 20, 0.96)",
        scan: "rgba(255, 142, 54, 0.88)",
        curtain: "rgba(56, 225, 255, 0.20)",
        curtainEdge: "rgba(110, 238, 255, 0.72)",
        glass: "rgba(165, 247, 255, 0.30)",
        shadow: "rgba(0, 0, 0, 0.22)",
      }
    : {
        baseTop: "rgba(224, 255, 255, 0.96)",
        baseFront: "rgba(147, 219, 232, 0.96)",
        baseLeft: "rgba(196, 249, 255, 0.96)",
        baseRight: "rgba(113, 190, 207, 0.96)",
        baseStroke: "rgba(0, 126, 174, 0.48)",
        postTop: "rgba(255, 157, 73, 0.92)",
        postFront: "rgba(194, 89, 31, 0.92)",
        postLeft: "rgba(255, 126, 45, 0.94)",
        postRight: "rgba(156, 68, 25, 0.92)",
        scan: "rgba(255, 127, 45, 0.82)",
        curtain: "rgba(0, 188, 222, 0.18)",
        curtainEdge: "rgba(0, 127, 174, 0.62)",
        glass: "rgba(185, 250, 255, 0.36)",
        shadow: "rgba(0, 85, 112, 0.12)",
      };

  drawWorldRect(
    rctx,
    rect.x + 0.12,
    rect.y + rect.h + 0.12,
    rect.w - 0.24,
    0.2,
    palette.shadow,
    0.04,
  );

  const rail = {
    x: rect.x + 0.18,
    y: rect.y + 0.28,
    w: rect.w - 0.36,
    h: 0.36,
  };
  drawRaisedBox(
    rctx,
    rail,
    0,
    Math.max(0.52, topZ * 0.42),
    palette.baseTop,
    palette.baseFront,
    palette.baseLeft,
    palette.baseRight,
    palette.baseStroke,
  );

  const postBaseZ = 0.54;
  const postHeight = Math.max(2.2, topZ - postBaseZ + 0.25);
  const postRects = [
    { x: rect.x + 0.3, y: rect.y + 0.12, w: 0.28, h: 0.74 },
    { x: rect.x + rect.w - 0.58, y: rect.y + 0.12, w: 0.28, h: 0.74 },
  ];
  for (const post of postRects) {
    drawRaisedBox(
      rctx,
      post,
      postBaseZ,
      postHeight,
      palette.postTop,
      palette.postFront,
      palette.postLeft,
      palette.postRight,
      palette.scan,
    );
    drawWorldRect(
      rctx,
      post.x + 0.07,
      post.y + 0.1,
      post.w - 0.14,
      0.22,
      palette.scan,
      postBaseZ + postHeight + 0.04,
    );
  }

  const curtainY = rect.y + 0.64;
  const curtainX = rect.x + 0.62;
  const curtainW = rect.w - 1.24;
  drawVerticalFaceX(
    rctx,
    curtainX,
    curtainY,
    curtainW,
    0.86,
    2.16,
    palette.curtain,
  );
  strokeVerticalFaceX(
    rctx,
    curtainX,
    curtainY,
    curtainW,
    0.86,
    2.16,
    palette.curtainEdge,
  );

  for (const z of [1.18, 1.76, 2.34]) {
    drawVerticalFaceX(
      rctx,
      curtainX + 0.08,
      curtainY + 0.01,
      curtainW - 0.16,
      z,
      0.06,
      palette.curtainEdge,
    );
  }

  drawWorldRect(
    rctx,
    rect.x + 0.82,
    rect.y + 0.42,
    0.46,
    0.16,
    palette.scan,
    topZ + 0.1,
  );
  drawWorldRect(
    rctx,
    rect.x + rect.w - 1.28,
    rect.y + 0.42,
    0.46,
    0.16,
    palette.scan,
    topZ + 0.1,
  );
  drawWorldRect(
    rctx,
    rect.x + 1.5,
    rect.y + 0.34,
    rect.w - 3.0,
    0.22,
    palette.glass,
    topZ + 0.12,
  );
}

export function drawFrontCounter(rctx: RenderContext, rect: Rect): void {
  drawFurnitureBase(rctx, rect, "#586257", "#737E72", "#A5B1A5");
  drawWorldRect(rctx, rect.x + 0.5, rect.y + 0.55, rect.w - 1, 0.3, "#D7D1B8");
  drawWorldRect(rctx, rect.x + 1, rect.y + 1.35, 1.2, 0.45, "#2D3A33");
  drawWorldRect(rctx, rect.x + rect.w - 2, rect.y + 1.2, 1.2, 0.7, "#9DB7C0");
}

export function drawCheckInDesk(rctx: RenderContext, rect: Rect): void {
  drawFurnitureBase(rctx, rect, "#435248", "#647267", "#9DA99F");
  drawWorldRect(rctx, rect.x + 0.55, rect.y + 0.55, 1.2, 0.7, "#C7B27B");
  drawWorldRect(rctx, rect.x + 2.2, rect.y + 0.55, 2.4, 0.18, "#D3DCCC");
  drawWorldRect(rctx, rect.x + 2.2, rect.y + 1.1, 1.5, 0.18, "#D3DCCC");
}

export function drawLaunchScreen(rctx: RenderContext, rect: Rect): void {
  drawFurnitureBase(rctx, rect, "#182433", "#28384A", "#6E8296");
  drawWorldRect(
    rctx,
    rect.x + 0.55,
    rect.y + 0.55,
    rect.w - 1.1,
    rect.h - 1.1,
    "#0B1620",
  );
  drawWorldRect(rctx, rect.x + 1, rect.y + 1, rect.w - 2, 0.2, "#5CD6B5");
  drawWorldRect(rctx, rect.x + 1, rect.y + 1.6, 1.5, 0.2, "#E0B84E");
  drawWorldRect(rctx, rect.x + 3, rect.y + 1.6, 1.5, 0.2, "#E3655B");
}

export function drawConsole(rctx: RenderContext, rect: Rect): void {
  drawFurnitureBase(rctx, rect, "#263040", "#39475B", "#78879B");
  for (let x = rect.x + 0.6; x < rect.x + rect.w - 0.4; x += 1.25) {
    drawWorldRect(rctx, x, rect.y + 0.55, 0.75, 0.45, "#79C4D6");
    drawWorldRect(rctx, x + 0.2, rect.y + 1.25, 0.25, 0.25, "#E0B84E");
  }
}

export function drawLaunchButton(rctx: RenderContext, rect: Rect): void {
  drawFurnitureBase(rctx, rect, "#484D58", "#656B77", "#9BA4AF");
  drawWorldRect(rctx, rect.x + 0.55, rect.y + 0.55, 0.9, 0.9, "#B3232C");
  drawWorldRect(rctx, rect.x + 0.75, rect.y + 0.35, 0.55, 0.2, "#F06161");
}
