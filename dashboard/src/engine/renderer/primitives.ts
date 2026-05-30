import { TILE_SIZE, type Rect } from "../../office/layout";
import type { RenderContext, ScreenPoint } from "./context";

export function projectedPoint(
  rctx: RenderContext,
  x: number,
  y: number,
  zUnits: number = rctx.surfaceZ.value,
): ScreenPoint {
  const [sx, sy] = rctx.camera.worldToScreen(
    x * TILE_SIZE,
    y * TILE_SIZE,
    zUnits,
  );
  return { x: sx, y: sy };
}

export function projectedRect(
  rctx: RenderContext,
  x: number,
  y: number,
  w: number,
  h: number,
  zUnits: number = rctx.surfaceZ.value,
): ScreenPoint[] {
  return [
    projectedPoint(rctx, x, y, zUnits),
    projectedPoint(rctx, x + w, y, zUnits),
    projectedPoint(rctx, x + w, y + h, zUnits),
    projectedPoint(rctx, x, y + h, zUnits),
  ];
}

export function tracePolygon(
  rctx: RenderContext,
  points: ScreenPoint[],
): void {
  rctx.ctx.moveTo(points[0].x, points[0].y);
  for (const point of points.slice(1)) {
    rctx.ctx.lineTo(point.x, point.y);
  }
  rctx.ctx.closePath();
}

export function drawPolygon(
  rctx: RenderContext,
  points: ScreenPoint[],
): void {
  rctx.ctx.beginPath();
  tracePolygon(rctx, points);
  rctx.ctx.fill();
}

export function strokePolygon(
  rctx: RenderContext,
  points: ScreenPoint[],
): void {
  rctx.ctx.beginPath();
  tracePolygon(rctx, points);
  rctx.ctx.stroke();
}

export function drawWorldRect(
  rctx: RenderContext,
  x: number,
  y: number,
  w: number,
  h: number,
  fill: string,
  zUnits: number = rctx.surfaceZ.value,
): void {
  rctx.ctx.fillStyle = fill;
  drawPolygon(rctx, projectedRect(rctx, x, y, w, h, zUnits));
}

export function strokeWorldRect(
  rctx: RenderContext,
  x: number,
  y: number,
  w: number,
  h: number,
  stroke: string,
  zUnits: number = rctx.surfaceZ.value,
): void {
  rctx.ctx.strokeStyle = stroke;
  rctx.ctx.lineWidth = 1;
  strokePolygon(rctx, projectedRect(rctx, x, y, w, h, zUnits));
}

export function strokeWorldSegment(
  rctx: RenderContext,
  x1: number,
  y1: number,
  z1: number,
  x2: number,
  y2: number,
  z2: number,
  stroke: string,
  width = 1,
): void {
  const start = projectedPoint(rctx, x1, y1, z1);
  const end = projectedPoint(rctx, x2, y2, z2);
  rctx.ctx.strokeStyle = stroke;
  rctx.ctx.lineWidth = width;
  rctx.ctx.beginPath();
  rctx.ctx.moveTo(start.x, start.y);
  rctx.ctx.lineTo(end.x, end.y);
  rctx.ctx.stroke();
  rctx.ctx.lineWidth = 1;
}

export function drawVerticalFaceX(
  rctx: RenderContext,
  x: number,
  y: number,
  w: number,
  zStart: number,
  zHeight: number,
  fill: string,
): void {
  rctx.ctx.fillStyle = fill;
  drawPolygon(rctx, [
    projectedPoint(rctx, x, y, zStart),
    projectedPoint(rctx, x + w, y, zStart),
    projectedPoint(rctx, x + w, y, zStart + zHeight),
    projectedPoint(rctx, x, y, zStart + zHeight),
  ]);
}

export function strokeVerticalFaceX(
  rctx: RenderContext,
  x: number,
  y: number,
  w: number,
  zStart: number,
  zHeight: number,
  stroke: string,
): void {
  rctx.ctx.strokeStyle = stroke;
  rctx.ctx.lineWidth = 1;
  strokePolygon(rctx, [
    projectedPoint(rctx, x, y, zStart),
    projectedPoint(rctx, x + w, y, zStart),
    projectedPoint(rctx, x + w, y, zStart + zHeight),
    projectedPoint(rctx, x, y, zStart + zHeight),
  ]);
}

export function drawVerticalFaceY(
  rctx: RenderContext,
  x: number,
  y: number,
  h: number,
  zStart: number,
  zHeight: number,
  fill: string,
): void {
  rctx.ctx.fillStyle = fill;
  drawPolygon(rctx, [
    projectedPoint(rctx, x, y, zStart),
    projectedPoint(rctx, x, y + h, zStart),
    projectedPoint(rctx, x, y + h, zStart + zHeight),
    projectedPoint(rctx, x, y, zStart + zHeight),
  ]);
}

export function strokeVerticalFaceY(
  rctx: RenderContext,
  x: number,
  y: number,
  h: number,
  zStart: number,
  zHeight: number,
  stroke: string,
): void {
  rctx.ctx.strokeStyle = stroke;
  rctx.ctx.lineWidth = 1;
  strokePolygon(rctx, [
    projectedPoint(rctx, x, y, zStart),
    projectedPoint(rctx, x, y + h, zStart),
    projectedPoint(rctx, x, y + h, zStart + zHeight),
    projectedPoint(rctx, x, y, zStart + zHeight),
  ]);
}

export function drawExtrudedRect(
  rctx: RenderContext,
  rect: Rect,
  heightUnits: number,
  top: string,
  front: string,
  left: string,
  right: string,
  stroke: string,
): void {
  drawVerticalFaceY(rctx, rect.x, rect.y, rect.h, 0, heightUnits, left);
  drawVerticalFaceY(
    rctx,
    rect.x + rect.w,
    rect.y,
    rect.h,
    0,
    heightUnits,
    right,
  );
  drawVerticalFaceX(
    rctx,
    rect.x,
    rect.y + rect.h,
    rect.w,
    0,
    heightUnits,
    front,
  );
  drawWorldRect(rctx, rect.x, rect.y, rect.w, rect.h, top, heightUnits);
  strokeWorldRect(rctx, rect.x, rect.y, rect.w, rect.h, stroke, heightUnits);
  strokeVerticalFaceX(
    rctx,
    rect.x,
    rect.y + rect.h,
    rect.w,
    0,
    heightUnits,
    "rgba(0, 0, 0, 0.25)",
  );
}

export function drawRaisedBox(
  rctx: RenderContext,
  rect: Rect,
  baseZUnits: number,
  heightUnits: number,
  top: string,
  front: string,
  left: string,
  right: string,
  stroke: string,
): void {
  drawVerticalFaceY(
    rctx,
    rect.x,
    rect.y,
    rect.h,
    baseZUnits,
    heightUnits,
    left,
  );
  drawVerticalFaceY(
    rctx,
    rect.x + rect.w,
    rect.y,
    rect.h,
    baseZUnits,
    heightUnits,
    right,
  );
  drawVerticalFaceX(
    rctx,
    rect.x,
    rect.y + rect.h,
    rect.w,
    baseZUnits,
    heightUnits,
    front,
  );
  drawWorldRect(
    rctx,
    rect.x,
    rect.y,
    rect.w,
    rect.h,
    top,
    baseZUnits + heightUnits,
  );
  strokeWorldRect(
    rctx,
    rect.x,
    rect.y,
    rect.w,
    rect.h,
    stroke,
    baseZUnits + heightUnits,
  );
  strokeVerticalFaceX(
    rctx,
    rect.x,
    rect.y + rect.h,
    rect.w,
    baseZUnits,
    heightUnits,
    "rgba(0, 0, 0, 0.3)",
  );
}

export function drawWorldEllipse(
  rctx: RenderContext,
  x: number,
  y: number,
  z: number,
  radiusXUnits: number,
  radiusYUnits: number,
  fill: string | null,
  stroke?: string,
  width = 1,
): void {
  const center = projectedPoint(rctx, x, y, z);
  const ctx = rctx.ctx;
  ctx.save();
  ctx.beginPath();
  ctx.ellipse(
    center.x,
    center.y,
    radiusXUnits * TILE_SIZE * rctx.camera.zoom,
    radiusYUnits * TILE_SIZE * rctx.camera.zoom * 0.68,
    0,
    0,
    Math.PI * 2,
  );
  if (fill) {
    ctx.fillStyle = fill;
    ctx.fill();
  }
  if (stroke) {
    ctx.strokeStyle = stroke;
    ctx.lineWidth = width;
    ctx.stroke();
  }
  ctx.restore();
}

export function shade(hex: string, delta: number): string {
  if (!/^#[\da-f]{6}$/i.test(hex)) return hex;
  const value = Number.parseInt(hex.slice(1), 16);
  const r = clampColor((value >> 16) + delta);
  const g = clampColor(((value >> 8) & 0xff) + delta);
  const b = clampColor((value & 0xff) + delta);
  return `rgb(${r}, ${g}, ${b})`;
}

export function clampColor(value: number): number {
  return Math.max(0, Math.min(255, value));
}
