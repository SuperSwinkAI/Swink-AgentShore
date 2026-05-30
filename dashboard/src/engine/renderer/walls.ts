import {
  DOORS,
  LOW_PROP_HEIGHT_UNITS,
  MAP_COLS,
  MAP_ROWS,
  TileType,
  VISUAL_WALL_BARRIERS,
  WALL_HEIGHT_UNITS,
  WALL_THICKNESS_UNITS,
  ZONES,
  ZoneId,
  backWallHeightForY,
  tileMap,
  type Rect,
  type Zone,
} from "../../office/layout";
import type { RenderContext, SceneRenderable } from "./context";
import {
  drawExtrudedRect,
  drawVerticalFaceX,
  drawVerticalFaceY,
  drawWorldRect,
  projectedRect,
  strokeVerticalFaceX,
  strokeWorldRect,
  tracePolygon,
} from "./primitives";

const TRANSLUCENT_PERIMETER_WALLS: Rect[] = [
  { x: 5, y: 36, w: 15, h: 1 },
  { x: 54, y: 36, w: 15, h: 1 },
];

const TRANSLUCENT_STRUCTURAL_WALLS: Rect[] = [
  { x: 20, y: 40 + WALL_THICKNESS_UNITS, w: 12, h: WALL_THICKNESS_UNITS },
  { x: 38, y: 40 + WALL_THICKNESS_UNITS, w: 16, h: WALL_THICKNESS_UNITS },
];

export function renderWalls(rctx: RenderContext): SceneRenderable[] {
  return [
    ...renderPerimeterWalls(rctx),
    ...renderRoomWallFaces(rctx),
    ...renderBarrierWalls(rctx),
  ];
}

function renderPerimeterWalls(rctx: RenderContext): SceneRenderable[] {
  const renderables: SceneRenderable[] = [];
  for (let y = 0; y < MAP_ROWS; y++) {
    for (let x = 0; x < MAP_COLS; x++) {
      if (tileMap[y][x] !== TileType.WALL) continue;

      const translucent = isTranslucentPerimeterWallTile(x, y);
      renderables.push({
        depth: y + 1 + x * 0.0001,
        draw: () => {
          const drawWall = () =>
            drawExtrudedRect(
              rctx,
              { x, y, w: 1, h: 1 },
              LOW_PROP_HEIGHT_UNITS,
              rctx.palette.perimeterWall.top,
              rctx.palette.perimeterWall.front,
              rctx.palette.perimeterWall.left,
              rctx.palette.perimeterWall.right,
              rctx.palette.perimeterWall.stroke,
            );

          if (translucent) {
            withTranslucentWallAlpha(rctx, drawWall);
            return;
          }
          drawWall();
        },
      });
    }
  }
  return renderables;
}

function isTranslucentPerimeterWallTile(x: number, y: number): boolean {
  return TRANSLUCENT_PERIMETER_WALLS.some((rect) =>
    rectContainsTile(rect, x, y),
  );
}

function rectContainsTile(rect: Rect, x: number, y: number): boolean {
  return (
    x >= rect.x &&
    x < rect.x + rect.w &&
    y >= rect.y &&
    y < rect.y + rect.h
  );
}

function isTranslucentStructuralWall(rect: Rect): boolean {
  return TRANSLUCENT_STRUCTURAL_WALLS.some(
    (target) =>
      rect.x === target.x &&
      rect.y === target.y &&
      rect.w === target.w &&
      rect.h === target.h,
  );
}

function withTranslucentWallAlpha(rctx: RenderContext, draw: () => void): void {
  rctx.ctx.save();
  rctx.ctx.globalAlpha *= rctx.palette.interiorTranslucentWallAlpha;
  try {
    draw();
  } finally {
    rctx.ctx.restore();
  }
}

function renderRoomWallFaces(rctx: RenderContext): SceneRenderable[] {
  const renderables: SceneRenderable[] = [];
  for (const zone of ZONES) {
    const { x, y, w } = zone.bounds;
    for (const segment of wallSegments(x, y, w)) {
      renderables.push({
        depth: segment.y + WALL_THICKNESS_UNITS + segment.x * 0.0001,
        draw: () => drawBackWall(rctx, segment.x, segment.y, segment.w, zone),
      });
    }
  }
  return renderables;
}

function renderBarrierWalls(rctx: RenderContext): SceneRenderable[] {
  return VISUAL_WALL_BARRIERS.map((barrier) => ({
    depth: barrier.y + barrier.h + barrier.x * 0.0001,
    draw: () => drawStructuralWall(rctx, barrier),
  }));
}

function wallSegments(x: number, y: number, w: number): Rect[] {
  let segments: Rect[] = [{ x, y, w, h: WALL_THICKNESS_UNITS }];

  for (const door of DOORS) {
    const cutsThisBackWall =
      door.orientation === "horizontal" && door.y + door.h === y;
    if (!cutsThisBackWall) continue;
    const cutStart = door.x;
    const cutEnd = door.x + door.w;
    segments = segments.flatMap((segment) =>
      subtractHorizontalCut(segment, cutStart, cutEnd),
    );
  }

  return segments;
}

function subtractHorizontalCut(
  segment: Rect,
  cutStart: number,
  cutEnd: number,
): Rect[] {
  const segmentStart = segment.x;
  const segmentEnd = segment.x + segment.w;
  if (cutEnd <= segmentStart || cutStart >= segmentEnd) return [segment];

  const next: Rect[] = [];
  if (cutStart > segmentStart) {
    next.push({ ...segment, w: cutStart - segmentStart });
  }
  if (cutEnd < segmentEnd) {
    next.push({ ...segment, x: cutEnd, w: segmentEnd - cutEnd });
  }
  return next;
}

function drawBackWall(
  rctx: RenderContext,
  x: number,
  y: number,
  w: number,
  zone: Zone,
): void {
  const faceY = y + WALL_THICKNESS_UNITS;
  const wallHeight = backWallHeightForY(y);
  const wall = rctx.palette.zones[zone.id].wall;

  drawWorldRect(rctx, x, faceY, w, 0.34, wall.shadow);
  drawVerticalFaceX(rctx, x, faceY, w, 0, wallHeight, wall.face);
  drawVerticalFaceX(rctx, x, faceY, w, 0.26, 0.12, wall.trim);
  drawVerticalFaceX(rctx, x, faceY, w, wallHeight - 0.28, 0.28, wall.cap);
  drawWorldRect(rctx, x, y, w, WALL_THICKNESS_UNITS, wall.cap, wallHeight);
  strokeVerticalFaceX(
    rctx,
    x,
    faceY,
    w,
    0,
    wallHeight,
    rctx.palette.wallFaceStroke,
  );
  strokeWorldRect(
    rctx,
    x,
    y,
    w,
    WALL_THICKNESS_UNITS,
    rctx.palette.wallCapStroke,
    wallHeight,
  );
}

function drawStructuralWall(rctx: RenderContext, rect: Rect): void {
  const drawWall = () => {
    const wall = rctx.palette.structuralWall;
    const top = rect.h >= rect.w ? wall.verticalTop : wall.horizontalTop;
    drawExtrudedRect(
      rctx,
      rect,
      WALL_HEIGHT_UNITS,
      top,
      wall.front,
      wall.left,
      wall.right,
      wall.stroke,
    );

    if (rect.h >= rect.w) {
      drawVerticalFaceY(rctx, rect.x, rect.y, rect.h, 0.4, 0.12, wall.trim);
      drawVerticalFaceY(
        rctx,
        rect.x + rect.w,
        rect.y,
        rect.h,
        0,
        WALL_HEIGHT_UNITS,
        wall.sideShade,
      );
    } else {
      drawVerticalFaceX(
        rctx,
        rect.x,
        rect.y + rect.h,
        rect.w,
        0.4,
        0.12,
        wall.trim,
      );
      drawWorldRect(
        rctx,
        rect.x,
        rect.y,
        rect.w,
        rect.h,
        wall.horizontalTopOverlay,
        WALL_HEIGHT_UNITS,
      );
    }
  };

  if (isTranslucentStructuralWall(rect)) {
    withTranslucentWallAlpha(rctx, drawWall);
    return;
  }
  drawWall();
}

export function renderFloorMarkers(rctx: RenderContext): void {
  const ctx = rctx.ctx;
  for (const barrier of VISUAL_WALL_BARRIERS) {
    drawWorldRect(
      rctx,
      barrier.x,
      barrier.y,
      barrier.w,
      barrier.h,
      rctx.palette.barrierOverlay,
    );
  }

  if (!shouldRenderDestinationMarkers()) return;

  for (const zone of ZONES) {
    for (const seat of zone.seats) {
      const corners = projectedRect(
        rctx,
        seat.x + 0.18,
        seat.y + 0.18,
        0.64,
        0.64,
      );
      ctx.beginPath();
      tracePolygon(rctx, corners);
      ctx.strokeStyle = targetStrokeForZone(rctx, zone.id);
      ctx.lineWidth = 1;
      ctx.stroke();
    }
  }
}

function shouldRenderDestinationMarkers(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("markers") === "1";
}

export function targetStrokeForZone(
  rctx: RenderContext,
  zoneId: ZoneId,
): string {
  if (zoneId === ZoneId.RECOVERY_BAY)
    return rctx.palette.targetStroke.recoveryBay;
  if (zoneId === ZoneId.ZEN_GARDEN)
    return rctx.palette.targetStroke.zenGarden;
  if (zoneId === ZoneId.FRONT_DESK)
    return rctx.palette.targetStroke.frontDesk;
  return rctx.palette.targetStroke.default;
}
