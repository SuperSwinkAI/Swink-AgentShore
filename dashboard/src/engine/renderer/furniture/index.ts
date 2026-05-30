import {
  FURNITURE,
  FURNITURE_HEIGHT_UNITS,
  LOW_PROP_HEIGHT_UNITS,
  TALL_PROP_HEIGHT_UNITS,
  type Rect,
} from "../../../office/layout";
import type { RenderContext, SceneRenderable } from "../context";
import { withSurfaceZ } from "../context";
import {
  drawExtrudedRect,
  drawVerticalFaceX,
  drawWorldRect,
  shade,
} from "../primitives";
import { drawFurniturePiece } from "./science";

export function renderFurniture(rctx: RenderContext): SceneRenderable[] {
  return FURNITURE.map((furniture) => ({
    depth: furniture.y + furniture.h + furniture.x * 0.0001,
    draw: () => {
      drawFurnitureShadow(rctx, furniture);
      withSurfaceZ(rctx, furnitureHeightFor(furniture), () =>
        drawFurniturePiece(rctx, furniture),
      );
    },
  }));
}

export function furnitureHeightFor(furniture: Rect & { name: string }): number {
  if (
    ["Sand", "Stones", "Garden Bench", "Papers", "Launch Button"].includes(
      furniture.name,
    )
  ) {
    return LOW_PROP_HEIGHT_UNITS;
  }
  if (furniture.name === "Monitor Desk W") {
    return 3;
  }
  if (furniture.name === "Vending Machine") {
    return 5.2;
  }
  if (furniture.name === "Merge Button Cube") {
    return 2.45;
  }
  if (furniture.name.startsWith("Recovery Scrap")) {
    return 1.6;
  }
  if (furniture.name === "Drafting Table") {
    return 2.15;
  }
  if (furniture.name === "Editor Repo Cube") {
    return 2.65;
  }
  if (furniture.name.startsWith("Editor Pair Pod")) {
    return 1.85;
  }
  if (furniture.name === "Editor Bookcases") {
    return 6.35;
  }
  if (furniture.name.startsWith("Badge Turnstile")) {
    return 3.35;
  }
  if (
    [
      "Whiteboard",
      "Pin Board",
      "Big Screen",
      "Editor Shelf",
      "Lab Shelf",
      "Tools",
    ].includes(furniture.name)
  ) {
    return TALL_PROP_HEIGHT_UNITS;
  }
  return FURNITURE_HEIGHT_UNITS;
}

export function drawFurnitureShadow(rctx: RenderContext, rect: Rect): void {
  drawWorldRect(
    rctx,
    rect.x + 0.18,
    rect.y + rect.h - 0.08,
    rect.w,
    0.22,
    rctx.palette.furnitureShadow,
  );
}

export function drawFurnitureBase(
  rctx: RenderContext,
  rect: Rect,
  fill: string,
  top: string,
  stroke: string,
): void {
  drawExtrudedRect(
    rctx,
    rect,
    rctx.surfaceZ.value,
    top,
    fill,
    shade(fill, -24),
    shade(fill, -42),
    stroke,
  );
  drawWorldRect(rctx, rect.x + 0.16, rect.y + 0.16, rect.w - 0.32, 0.28, top);
  drawVerticalFaceX(
    rctx,
    rect.x,
    rect.y + rect.h,
    rect.w,
    0,
    0.32,
    "rgba(0,0,0,0.22)",
  );
}
