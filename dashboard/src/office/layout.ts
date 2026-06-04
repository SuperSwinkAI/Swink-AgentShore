export const TILE_SIZE = 16;
export const MAP_COLS = 75;
export const MAP_ROWS = 56;

export const GRID_UNIT_FEET = 1;
export const AXON_DEPTH_ANGLE_DEGREES = 90;
export const AXON_DEPTH_ANGLE_RADIANS =
  (AXON_DEPTH_ANGLE_DEGREES * Math.PI) / 180;
export const AXON_DEPTH_RUN = Math.cos(AXON_DEPTH_ANGLE_RADIANS) * TILE_SIZE;
export const AXON_DEPTH_RISE = Math.sin(AXON_DEPTH_ANGLE_RADIANS) * TILE_SIZE;
export const AXON_VERTICAL_SCALE = 0.6;
export const WALL_HEIGHT_UNITS = 8;
export const BACK_WALL_HEIGHT_UNITS = 12;
export const NORTH_BACK_WALL_Y = 6;
export const WALL_THICKNESS_UNITS = 0.5;
export const ACTOR_HEIGHT_UNITS = 6;
export const ACTOR_WIDTH_UNITS = 1;
export const FURNITURE_HEIGHT_UNITS = 2.75;
export const LOW_PROP_HEIGHT_UNITS = 1.25;
export const TALL_PROP_HEIGHT_UNITS = 5.5;

export enum TileType {
  VOID = 0,
  FLOOR = 1,
  WALL = 2,
}

export enum ZoneId {
  WAR_ROOM,
  WORKSHOP,
  SCIENCE_LAB,
  LAUNCH_CONTROL,
  EDITORS_DESK,
  RECOVERY_BAY,
  ZEN_GARDEN,
  FRONT_DESK,
}

export interface Tile {
  x: number;
  y: number;
}

export interface WorkSeat extends Tile {
  facing?: "north" | "south" | "east" | "west";
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Zone {
  id: ZoneId;
  name: string;
  bounds: Rect;
  seats: WorkSeat[];
}

export interface Furniture extends Rect {
  name: string;
  zoneId: ZoneId;
}

export interface Door extends Rect {
  name: string;
  orientation: "vertical" | "horizontal";
  kind: "room" | "garden" | "exit";
}

export interface ProjectedPoint {
  x: number;
  y: number;
}

export interface ProjectedBounds {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export type ProjectionMode = "grid";

interface ProjectionParams {
  depthRun: number;
  depthRise: number;
  verticalScale: number;
}

function projectionParams(_mode: ProjectionMode): ProjectionParams {
  return {
    depthRun: AXON_DEPTH_RUN,
    depthRise: AXON_DEPTH_RISE,
    verticalScale: AXON_VERTICAL_SCALE,
  };
}

export function projectionModeForTheme(_theme: string): ProjectionMode {
  return "grid";
}

export function projectUnits(
  x: number,
  y: number,
  z = 0,
  mode: ProjectionMode = "grid",
): ProjectedPoint {
  const params = projectionParams(mode);
  return {
    x: x * TILE_SIZE + y * params.depthRun,
    y: y * params.depthRise - z * TILE_SIZE * params.verticalScale,
  };
}

export function projectWorld(
  wx: number,
  wy: number,
  z = 0,
  mode: ProjectionMode = "grid",
): ProjectedPoint {
  return projectUnits(wx / TILE_SIZE, wy / TILE_SIZE, z, mode);
}

export function unprojectWorld(
  px: number,
  py: number,
  mode: ProjectionMode = "grid",
): { x: number; y: number } {
  const params = projectionParams(mode);
  const yUnits = py / params.depthRise;
  const xUnits = (px - yUnits * params.depthRun) / TILE_SIZE;
  return { x: xUnits * TILE_SIZE, y: yUnits * TILE_SIZE };
}

export function projectedMapBounds(
  mode: ProjectionMode = "grid",
): ProjectedBounds {
  const topLeft = projectUnits(0, 0, BACK_WALL_HEIGHT_UNITS, mode);
  const topRight = projectUnits(MAP_COLS, 0, BACK_WALL_HEIGHT_UNITS, mode);
  const bottomLeft = projectUnits(0, MAP_ROWS, 0, mode);
  const bottomRight = projectUnits(MAP_COLS, MAP_ROWS, 0, mode);

  return {
    left: Math.min(topLeft.x, topRight.x, bottomLeft.x, bottomRight.x),
    top: Math.min(topLeft.y, topRight.y, bottomLeft.y, bottomRight.y),
    right: Math.max(topLeft.x, topRight.x, bottomLeft.x, bottomRight.x),
    bottom: Math.max(topLeft.y, topRight.y, bottomLeft.y, bottomRight.y),
  };
}

export function backWallHeightForY(y: number): number {
  return y === NORTH_BACK_WALL_Y ? BACK_WALL_HEIGHT_UNITS : WALL_HEIGHT_UNITS;
}

export const FRONT_DESK_SPAWN_SPOTS: Tile[] = [
  { x: 67, y: 23 },
  { x: 67, y: 27 },
  { x: 67, y: 32 },
];

export const FRONT_DESK_EXIT: Tile = { x: 68, y: 28 };

export const ZONES: Zone[] = [
  {
    id: ZoneId.WAR_ROOM,
    name: "WAR ROOM",
    bounds: { x: 5, y: 6, w: 15, h: 15 },
    seats: [
      { x: 13, y: 8, facing: "south" },
      { x: 18, y: 12, facing: "west" },
      { x: 13, y: 16, facing: "north" },
      { x: 8, y: 14, facing: "east" },
    ],
  },
  {
    id: ZoneId.WORKSHOP,
    name: "",
    bounds: { x: 20, y: 6, w: 34, h: 35 },
    seats: [
      { x: 24, y: 14, facing: "south" },
      { x: 28, y: 14, facing: "south" },
      { x: 21, y: 18, facing: "east" },
      { x: 32, y: 18, facing: "west" },
      { x: 46, y: 17, facing: "east" },
      { x: 46, y: 20, facing: "east" },
      { x: 46, y: 23, facing: "east" },
      { x: 27, y: 22, facing: "north" },
      { x: 34, y: 22, facing: "south" },
      { x: 38, y: 22, facing: "south" },
      { x: 31, y: 26, facing: "east" },
      { x: 43, y: 26, facing: "west" },
      { x: 38, y: 30, facing: "north" },
      { x: 27, y: 31, facing: "south" },
      { x: 46, y: 31, facing: "south" },
      { x: 32, y: 35, facing: "west" },
      { x: 44, y: 34, facing: "east" },
    ],
  },
  {
    id: ZoneId.LAUNCH_CONTROL,
    name: "LAUNCH CONTROL",
    bounds: { x: 54, y: 6, w: 15, h: 15 },
    seats: [
      { x: 61, y: 9, facing: "south" },
      { x: 57, y: 12, facing: "east" },
      { x: 65, y: 12, facing: "west" },
      { x: 61, y: 16, facing: "north" },
    ],
  },
  {
    id: ZoneId.EDITORS_DESK,
    name: "",
    bounds: { x: 5, y: 21, w: 15, h: 15 },
    seats: [
      { x: 12, y: 26, facing: "south" },
      { x: 15, y: 28, facing: "west" },
      { x: 12, y: 30, facing: "north" },
      { x: 9, y: 28, facing: "east" },
    ],
  },
  {
    id: ZoneId.FRONT_DESK,
    name: "FRONT DESK",
    bounds: { x: 54, y: 21, w: 15, h: 15 },
    seats: FRONT_DESK_SPAWN_SPOTS,
  },
  {
    id: ZoneId.RECOVERY_BAY,
    name: "RECOVERY BAY",
    bounds: { x: 5, y: 37, w: 15, h: 15 },
    seats: [
      { x: 9, y: 40, facing: "east" },
      { x: 12, y: 44 },
      { x: 9, y: 48, facing: "east" },
      { x: 16, y: 47, facing: "west" },
    ],
  },
  {
    id: ZoneId.ZEN_GARDEN,
    name: "ZEN GARDEN",
    bounds: { x: 20, y: 41, w: 34, h: 11 },
    seats: [
      { x: 22, y: 46 },
      { x: 50, y: 44 },
      { x: 34, y: 51 },
      { x: 49, y: 51 },
    ],
  },
  {
    id: ZoneId.SCIENCE_LAB,
    name: "SCIENCE LAB",
    bounds: { x: 54, y: 37, w: 15, h: 15 },
    seats: [
      { x: 56, y: 45 },
      { x: 61, y: 42 },
      { x: 64, y: 47 },
    ],
  },
];

export const DOORS: Door[] = [
  {
    name: "war to workshop",
    x: 20,
    y: 11,
    w: 1,
    h: 5,
    orientation: "vertical",
    kind: "room",
  },
  {
    name: "launch to workshop",
    x: 53,
    y: 11,
    w: 1,
    h: 5,
    orientation: "vertical",
    kind: "room",
  },
  {
    name: "editor to workshop",
    x: 20,
    y: 26,
    w: 1,
    h: 5,
    orientation: "vertical",
    kind: "room",
  },
  {
    name: "front desk to workshop",
    x: 53,
    y: 26,
    w: 1,
    h: 5,
    orientation: "vertical",
    kind: "room",
  },
  {
    name: "recovery to workshop",
    x: 20,
    y: 37,
    w: 1,
    h: 3,
    orientation: "vertical",
    kind: "room",
  },
  {
    name: "science lab to workshop",
    x: 53,
    y: 37,
    w: 1,
    h: 3,
    orientation: "vertical",
    kind: "room",
  },
  {
    name: "workshop to zen garden",
    x: 32,
    y: 40,
    w: 6,
    h: 1,
    orientation: "horizontal",
    kind: "garden",
  },
  {
    name: "front desk exit",
    x: 68,
    y: 26,
    w: 1,
    h: 6,
    orientation: "vertical",
    kind: "exit",
  },
];

export function doorCenterTiles(door: Door): Tile[] {
  const span = door.orientation === "vertical" ? door.h : door.w;
  const laneWidth = span % 2 === 0 ? 2 : 1;
  const laneStart =
    (door.orientation === "vertical" ? door.y : door.x) +
    Math.floor((span - laneWidth) / 2);
  const tiles: Tile[] = [];

  for (let offset = 0; offset < laneWidth; offset += 1) {
    tiles.push(
      door.orientation === "vertical"
        ? { x: door.x, y: laneStart + offset }
        : { x: laneStart + offset, y: door.y },
    );
  }

  return tiles;
}

export function isDoorEdgeBuffer(x: number, y: number): boolean {
  const door = DOORS.find((candidate) => rectContains(candidate, x, y));
  if (!door) return false;
  return !doorCenterTiles(door).some((tile) => tile.x === x && tile.y === y);
}

export const WALL_BARRIERS: Rect[] = [
  // Left rooms to Workshop/Zen. Door rows are intentionally omitted.
  { x: 20, y: 6, w: 1, h: 5 },
  { x: 20, y: 16, w: 1, h: 10 },
  { x: 20, y: 31, w: 1, h: 5 },
  { x: 20, y: 40, w: 1, h: 12 },

  // Right rooms to Workshop/Zen. Door rows are intentionally omitted.
  { x: 53, y: 6, w: 1, h: 5 },
  { x: 53, y: 16, w: 1, h: 10 },
  { x: 53, y: 31, w: 1, h: 5 },
  { x: 53, y: 40, w: 1, h: 12 },

  // Stacked side rooms do not connect directly to each other.
  { x: 5, y: 20, w: 15, h: 1 },
  { x: 54, y: 20, w: 15, h: 1 },

  // Zen Garden is entered from the Workshop gate only.
  { x: 20, y: 40, w: 12, h: 1 },
  { x: 38, y: 40, w: 16, h: 1 },
];

export function visualWallBarrierFor(rect: Rect): Rect {
  const workshop = getZone(ZoneId.WORKSHOP);
  const thickness = WALL_THICKNESS_UNITS;

  if (rect.h >= rect.w) {
    const w = Math.min(rect.w, thickness);
    const rightWorkshopEdge = workshop.bounds.x + workshop.bounds.w;
    const anchorsToWorkshopRightEdge = rect.x + rect.w === rightWorkshopEdge;
    return {
      ...rect,
      x: anchorsToWorkshopRightEdge ? rect.x + rect.w - w : rect.x,
      w,
    };
  }

  const h = Math.min(rect.h, thickness);
  return {
    ...rect,
    y: rect.y + rect.h - h,
    h,
  };
}

export const VISUAL_WALL_BARRIERS: Rect[] =
  WALL_BARRIERS.map(visualWallBarrierFor);

export const FURNITURE: Furniture[] = [
  { name: "War Table", zoneId: ZoneId.WAR_ROOM, x: 10, y: 10, w: 7, h: 4 },
  { name: "War Console", zoneId: ZoneId.WAR_ROOM, x: 5, y: 10, w: 3, h: 3 },

  { name: "Bench NW", zoneId: ZoneId.WORKSHOP, x: 23, y: 16, w: 8, h: 5 },
  { name: "Printer Pod NE", zoneId: ZoneId.WORKSHOP, x: 48, y: 16, w: 5, h: 9 },
  { name: "Bench SW", zoneId: ZoneId.WORKSHOP, x: 23, y: 33, w: 8, h: 5 },
  { name: "Bench SE", zoneId: ZoneId.WORKSHOP, x: 33, y: 24, w: 9, h: 5 },
  { name: "Bins E", zoneId: ZoneId.WORKSHOP, x: 46, y: 33, w: 5, h: 3 },
  { name: "Tools", zoneId: ZoneId.WORKSHOP, x: 51, y: 32, w: 2, h: 5 },

  {
    name: "Merge Button Cube",
    zoneId: ZoneId.LAUNCH_CONTROL,
    x: 60,
    y: 11,
    w: 3,
    h: 3,
  },

  {
    name: "Editor Pair Pod NW",
    zoneId: ZoneId.EDITORS_DESK,
    x: 7,
    y: 24,
    w: 3,
    h: 2,
  },
  {
    name: "Editor Pair Pod NE",
    zoneId: ZoneId.EDITORS_DESK,
    x: 15,
    y: 24,
    w: 3,
    h: 2,
  },
  {
    name: "Editor Repo Cube",
    zoneId: ZoneId.EDITORS_DESK,
    x: 11,
    y: 27,
    w: 3,
    h: 3,
  },
  {
    name: "Editor Pair Pod SW",
    zoneId: ZoneId.EDITORS_DESK,
    x: 7,
    y: 32,
    w: 3,
    h: 2,
  },
  {
    name: "Editor Pair Pod SE",
    zoneId: ZoneId.EDITORS_DESK,
    x: 15,
    y: 32,
    w: 3,
    h: 2,
  },

  {
    name: "Badge Turnstile North",
    zoneId: ZoneId.FRONT_DESK,
    x: 58,
    y: 24,
    w: 4,
    h: 1,
  },
  {
    name: "Badge Turnstile Center",
    zoneId: ZoneId.FRONT_DESK,
    x: 58,
    y: 27,
    w: 4,
    h: 1,
  },
  {
    name: "Badge Turnstile South",
    zoneId: ZoneId.FRONT_DESK,
    x: 58,
    y: 30,
    w: 4,
    h: 1,
  },

  {
    name: "Recovery Scrap NW",
    zoneId: ZoneId.RECOVERY_BAY,
    x: 5,
    y: 37,
    w: 3,
    h: 3,
  },
  {
    name: "Recovery Scrap SE",
    zoneId: ZoneId.RECOVERY_BAY,
    x: 16,
    y: 48,
    w: 3,
    h: 3,
  },

  { name: "Sand", zoneId: ZoneId.ZEN_GARDEN, x: 25, y: 47, w: 8, h: 3 },
  { name: "Garden Bench", zoneId: ZoneId.ZEN_GARDEN, x: 41, y: 49, w: 8, h: 2 },
  { name: "Stones", zoneId: ZoneId.ZEN_GARDEN, x: 40, y: 44, w: 4, h: 3 },
  {
    name: "Vending Machine",
    zoneId: ZoneId.ZEN_GARDEN,
    x: 49,
    y: 41,
    w: 4,
    h: 2,
  },

  { name: "Lab Bench", zoneId: ZoneId.SCIENCE_LAB, x: 57, y: 48, w: 8, h: 2 },
  { name: "Test Rig", zoneId: ZoneId.SCIENCE_LAB, x: 58, y: 43, w: 6, h: 3 },
  { name: "Lab Shelf", zoneId: ZoneId.SCIENCE_LAB, x: 66, y: 42, w: 2, h: 7 },
];

export function rectContains(rect: Rect, x: number, y: number): boolean {
  return (
    x >= rect.x && x < rect.x + rect.w && y >= rect.y && y < rect.y + rect.h
  );
}

export function isWallBarrier(x: number, y: number): boolean {
  return WALL_BARRIERS.some((rect) => rectContains(rect, x, y));
}

export function isFurnitureBlocked(x: number, y: number): boolean {
  return FURNITURE.some((rect) => rectContains(rect, x, y));
}

export function isFurnitureSideBuffer(x: number, y: number): boolean {
  if (isFurnitureBlocked(x, y)) return false;
  return FURNITURE.some(
    (rect) =>
      y >= rect.y &&
      y < rect.y + rect.h &&
      (x === rect.x - 1 || x === rect.x + rect.w),
  );
}

function buildTileMap(): TileType[][] {
  const map: TileType[][] = [];
  for (let y = 0; y < MAP_ROWS; y++) {
    map[y] = [];
    for (let x = 0; x < MAP_COLS; x++) {
      map[y][x] = TileType.VOID;
    }
  }

  for (const zone of ZONES) {
    const { x: bx, y: by, w, h } = zone.bounds;
    for (let y = by; y < by + h; y++) {
      for (let x = bx; x < bx + w; x++) {
        map[y][x] = TileType.FLOOR;
      }
    }
  }

  for (let y = 0; y < MAP_ROWS; y++) {
    for (let x = 0; x < MAP_COLS; x++) {
      if (map[y][x] !== TileType.FLOOR) continue;
      for (const [dx, dy] of [
        [-1, 0],
        [1, 0],
        [0, -1],
        [0, 1],
        [-1, -1],
        [1, -1],
        [-1, 1],
        [1, 1],
      ]) {
        const nx = x + dx;
        const ny = y + dy;
        if (nx < 0 || nx >= MAP_COLS || ny < 0 || ny >= MAP_ROWS) continue;
        if (map[ny][nx] === TileType.VOID) {
          map[ny][nx] = TileType.WALL;
        }
      }
    }
  }

  return map;
}

export const tileMap: TileType[][] = buildTileMap();

function buildZoneMap(): (ZoneId | null)[][] {
  const map: (ZoneId | null)[][] = [];
  for (let y = 0; y < MAP_ROWS; y++) {
    map[y] = [];
    for (let x = 0; x < MAP_COLS; x++) {
      map[y][x] = null;
    }
  }
  for (const zone of ZONES) {
    const { x: bx, y: by, w, h } = zone.bounds;
    for (let y = by; y < by + h; y++) {
      for (let x = bx; x < bx + w; x++) {
        map[y][x] = zone.id;
      }
    }
  }
  return map;
}

export const zoneMap: (ZoneId | null)[][] = buildZoneMap();

export function getZone(id: ZoneId): Zone {
  return ZONES.find((z) => z.id === id)!;
}
