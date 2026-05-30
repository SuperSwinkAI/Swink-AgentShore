import { TILE_SIZE } from "../../office/layout";
import type { Character } from "../../characters/types";
import { drawCharacter } from "../../characters/sprites";
import type { RenderContext, SceneRenderable } from "./context";

export function renderCharacters(
  rctx: RenderContext,
  characters: Character[],
): SceneRenderable[] {
  return characters.map((char) => ({
    depth: char.y / TILE_SIZE + char.x * 0.00001,
    draw: () => drawCharacter(rctx.ctx, char, rctx.camera.zoom, rctx.camera),
  }));
}
