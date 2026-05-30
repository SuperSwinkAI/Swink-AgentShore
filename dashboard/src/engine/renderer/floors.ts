import {
  MAP_COLS,
  MAP_ROWS,
  TileType,
  ZONES,
  ZoneId,
  tileMap,
  zoneMap,
} from "../../office/layout";
import { ZONE_PAD_PALETTES } from "../../office/palette";
import type { RenderContext } from "./context";
import {
  drawWorldEllipse,
  drawWorldRect,
  strokeWorldRect,
  strokeWorldSegment,
} from "./primitives";

export function renderFloor(rctx: RenderContext): void {
  for (let y = 0; y < MAP_ROWS; y++) {
    for (let x = 0; x < MAP_COLS; x++) {
      if (tileMap[y][x] !== TileType.FLOOR) continue;

      const zoneId = zoneMap[y][x];
      const fill =
        zoneId !== null
          ? rctx.palette.zones[zoneId].floor
          : rctx.palette.fallbackFloor;
      drawWorldRect(rctx, x, y, 1, 1, fill);
      strokeWorldRect(rctx, x, y, 1, 1, rctx.palette.floorGridStroke);
    }
  }
}

export function renderFloorDecorations(rctx: RenderContext): void {
  drawGridWarRoomFloorPads(rctx);
  drawGridEditorRoomFloorPads(rctx);
  drawGridScienceLabFloorPads(rctx);
  drawGridZenRechargeField(rctx);
  drawRecoveryBayFloor(rctx);
}

function drawGridWarRoomFloorPads(rctx: RenderContext): void {
  const warRoom = ZONES.find((zone) => zone.id === ZoneId.WAR_ROOM);
  if (!warRoom) return;

  const padColors = ZONE_PAD_PALETTES[rctx.theme][ZoneId.WAR_ROOM];

  for (const [index, seat] of warRoom.seats.entries()) {
    const pad = padColors[index % padColors.length];
    drawWorldRect(rctx, seat.x + 0.1, seat.y + 0.1, 0.8, 0.8, pad.fill, 0.025);
    strokeWorldRect(
      rctx,
      seat.x + 0.1,
      seat.y + 0.1,
      0.8,
      0.8,
      pad.stroke,
      0.035,
    );
    drawWorldRect(
      rctx,
      seat.x + 0.33,
      seat.y + 0.33,
      0.34,
      0.34,
      pad.core,
      0.045,
    );
  }
}

function drawGridEditorRoomFloorPads(rctx: RenderContext): void {
  const editorRoom = ZONES.find((zone) => zone.id === ZoneId.EDITORS_DESK);
  if (!editorRoom) return;

  const padColors = ZONE_PAD_PALETTES[rctx.theme][ZoneId.EDITORS_DESK];

  for (const [index, seat] of editorRoom.seats.entries()) {
    const pad = padColors[index % padColors.length];
    drawWorldRect(rctx, seat.x + 0.1, seat.y + 0.1, 0.8, 0.8, pad.fill, 0.025);
    strokeWorldRect(
      rctx,
      seat.x + 0.1,
      seat.y + 0.1,
      0.8,
      0.8,
      pad.stroke,
      0.035,
    );
    drawWorldRect(
      rctx,
      seat.x + 0.34,
      seat.y + 0.34,
      0.32,
      0.32,
      pad.core,
      0.045,
    );
  }
}

function drawGridZenRechargeField(rctx: RenderContext): void {
  const padColors = ZONE_PAD_PALETTES[rctx.theme][ZoneId.ZEN_GARDEN];
  const stations = [
    { x: 27, y: 44 },
    { x: 43, y: 45 },
    { x: 35, y: 49 },
  ];

  for (const [index, station] of stations.entries()) {
    const pad = padColors[index % padColors.length];
    drawWorldRect(
      rctx,
      station.x - 0.55,
      station.y - 0.55,
      2.1,
      2.1,
      pad.fill,
      0.022,
    );
    strokeWorldRect(
      rctx,
      station.x - 0.55,
      station.y - 0.55,
      2.1,
      2.1,
      pad.stroke,
      0.034,
    );
    drawWorldRect(
      rctx,
      station.x - 0.25,
      station.y - 0.25,
      1.5,
      1.5,
      "rgba(222, 255, 246, 0.11)",
      0.028,
    );
    strokeWorldRect(
      rctx,
      station.x - 0.25,
      station.y - 0.25,
      1.5,
      1.5,
      pad.core,
      0.04,
    );
    drawWorldRect(
      rctx,
      station.x + 0.28,
      station.y + 0.28,
      0.44,
      0.44,
      pad.core,
      0.048,
    );
  }

  strokeWorldSegment(
    rctx,
    24.6,
    47.2,
    0.04,
    27.3,
    46.8,
    0.04,
    padColors[0].stroke,
    1.5,
  );
  strokeWorldSegment(
    rctx,
    41.2,
    49.8,
    0.04,
    43.8,
    49.35,
    0.04,
    padColors[1].stroke,
    1.5,
  );
}

function drawGridScienceLabFloorPads(rctx: RenderContext): void {
  const scienceLab = ZONES.find((zone) => zone.id === ZoneId.SCIENCE_LAB);
  if (!scienceLab) return;

  const padColors = ZONE_PAD_PALETTES[rctx.theme][ZoneId.SCIENCE_LAB];

  for (const [index, seat] of scienceLab.seats.entries()) {
    const pad = padColors[index % padColors.length];
    drawWorldRect(
      rctx,
      seat.x + 0.08,
      seat.y + 0.08,
      0.84,
      0.84,
      pad.fill,
      0.026,
    );
    strokeWorldRect(
      rctx,
      seat.x + 0.08,
      seat.y + 0.08,
      0.84,
      0.84,
      pad.stroke,
      0.038,
    );
    drawWorldRect(
      rctx,
      seat.x + 0.33,
      seat.y + 0.33,
      0.34,
      0.34,
      pad.core,
      0.05,
    );
  }

  strokeWorldSegment(
    rctx,
    59.4,
    44.65,
    0.06,
    61.3,
    43.0,
    0.06,
    padColors[1].stroke,
    1.4,
  );
  strokeWorldSegment(
    rctx,
    63.5,
    44.0,
    0.06,
    65.25,
    45.1,
    0.06,
    padColors[0].stroke,
    1.4,
  );
}

function drawRecoveryBayFloor(rctx: RenderContext): void {
  const dark = rctx.theme === "dark";
  const wash = dark
    ? "rgba(165, 198, 207, 0.11)"
    : "rgba(88, 132, 146, 0.14)";
  const paint = dark
    ? "rgba(255, 118, 139, 0.72)"
    : "rgba(176, 74, 92, 0.68)";
  const wornPaint = dark
    ? "rgba(255, 170, 80, 0.30)"
    : "rgba(196, 122, 72, 0.30)";

  drawWorldEllipse(rctx, 12.5, 43.6, 0.028, 4.1, 2.55, wash);
  drawWorldEllipse(rctx, 12.5, 43.6, 0.042, 3.85, 2.3, null, paint, 3);
  strokeWorldSegment(rctx, 9.4, 42.0, 0.055, 15.6, 45.2, 0.055, paint, 3);
  strokeWorldSegment(rctx, 15.6, 42.0, 0.055, 9.4, 45.2, 0.055, paint, 3);
  strokeWorldSegment(
    rctx,
    10.3,
    46.55,
    0.045,
    12.0,
    46.55,
    0.045,
    wornPaint,
    2,
  );
  strokeWorldSegment(
    rctx,
    13.2,
    46.95,
    0.045,
    14.6,
    46.95,
    0.045,
    wornPaint,
    2,
  );
}
