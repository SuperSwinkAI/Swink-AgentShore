import {
  MAP_COLS,
  MAP_ROWS,
  TileType,
  isDoorEdgeBuffer,
  isFurnitureBlocked,
  isFurnitureSideBuffer,
  isWallBarrier,
  tileMap,
  type Tile,
} from "./layout";

let walkableGrid: boolean[][] | null = null;

interface WalkOptions {
  ignoreFurniture?: boolean;
}

export function buildWalkableGrid(): boolean[][] {
  const grid: boolean[][] = [];
  for (let y = 0; y < MAP_ROWS; y++) {
    grid[y] = [];
    for (let x = 0; x < MAP_COLS; x++) {
      grid[y][x] =
        tileMap[y][x] === TileType.FLOOR &&
        !isWallBarrier(x, y) &&
        !isDoorEdgeBuffer(x, y) &&
        !isFurnitureBlocked(x, y) &&
        !isFurnitureSideBuffer(x, y);
    }
  }
  walkableGrid = grid;
  return grid;
}

export function isWalkable(x: number, y: number): boolean {
  if (!walkableGrid) walkableGrid = buildWalkableGrid();
  if (x < 0 || x >= MAP_COLS || y < 0 || y >= MAP_ROWS) return false;
  return walkableGrid[y][x];
}

export function isWalkableIgnoringFurniture(x: number, y: number): boolean {
  return isBaseWalkable(x, y, { ignoreFurniture: true });
}

function isBaseWalkable(
  x: number,
  y: number,
  options: WalkOptions = {},
): boolean {
  if (x < 0 || x >= MAP_COLS || y < 0 || y >= MAP_ROWS) return false;
  return (
    tileMap[y][x] === TileType.FLOOR &&
    !isWallBarrier(x, y) &&
    !isDoorEdgeBuffer(x, y) &&
    (options.ignoreFurniture === true ||
      (!isFurnitureBlocked(x, y) && !isFurnitureSideBuffer(x, y)))
  );
}

const DIRS: [number, number][] = [
  [0, -1], // up
  [0, 1], // down
  [-1, 0], // left
  [1, 0], // right
];

function tileKey(x: number, y: number): number {
  return y * MAP_COLS + x;
}

export function bfsPath(
  from: Tile,
  to: Tile,
  options: WalkOptions = {},
): Tile[] {
  const canWalk =
    options.ignoreFurniture === true ? isWalkableIgnoringFurniture : isWalkable;

  if (!canWalk(to.x, to.y)) {
    const nearest = findNearestWalkable(to, canWalk);
    if (!nearest) return [];
    to = nearest;
  }

  if (from.x === to.x && from.y === to.y) return [];

  const visited = new Set<number>();
  const parent = new Map<number, number>();

  const queue: Tile[] = [from];
  visited.add(tileKey(from.x, from.y));

  while (queue.length > 0) {
    const current = queue.shift()!;
    if (current.x === to.x && current.y === to.y) {
      return reconstructPath(parent, from, to);
    }

    for (const [dx, dy] of DIRS) {
      const nx = current.x + dx;
      const ny = current.y + dy;
      const nk = tileKey(nx, ny);
      if (!canWalk(nx, ny) || visited.has(nk)) continue;
      visited.add(nk);
      parent.set(nk, tileKey(current.x, current.y));
      queue.push({ x: nx, y: ny });
    }
  }

  return [];
}

function reconstructPath(
  parent: Map<number, number>,
  from: Tile,
  to: Tile,
): Tile[] {
  const path: Tile[] = [];
  let k = tileKey(to.x, to.y);
  const startK = tileKey(from.x, from.y);

  while (k !== startK) {
    path.push({ x: k % MAP_COLS, y: Math.floor(k / MAP_COLS) });
    const p = parent.get(k);
    if (p === undefined) break;
    k = p;
  }

  path.reverse();
  return path;
}

function findNearestWalkable(
  tile: Tile,
  canWalk: (x: number, y: number) => boolean,
): Tile | null {
  const visited = new Set<number>();
  const queue: Tile[] = [tile];
  visited.add(tileKey(tile.x, tile.y));

  while (queue.length > 0) {
    const current = queue.shift()!;
    if (canWalk(current.x, current.y)) return current;

    for (const [dx, dy] of DIRS) {
      const nx = current.x + dx;
      const ny = current.y + dy;
      if (nx < 0 || nx >= MAP_COLS || ny < 0 || ny >= MAP_ROWS) continue;
      const nk = tileKey(nx, ny);
      if (visited.has(nk)) continue;
      visited.add(nk);
      queue.push({ x: nx, y: ny });
    }
  }

  return null;
}
