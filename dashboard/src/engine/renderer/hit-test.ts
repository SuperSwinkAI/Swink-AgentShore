import {
  MAP_COLS,
  MAP_ROWS,
  TILE_SIZE,
  type Tile,
} from "../../office/layout";
import type { Camera } from "../camera";

export function screenToTile(
  camera: Camera,
  screenX: number,
  screenY: number,
): Tile | null {
  const [wx, wy] = camera.screenToWorld(screenX, screenY);
  const tx = Math.floor(wx / TILE_SIZE);
  const ty = Math.floor(wy / TILE_SIZE);
  if (tx < 0 || tx >= MAP_COLS || ty < 0 || ty >= MAP_ROWS) return null;
  return { x: tx, y: ty };
}
