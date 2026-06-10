import { TILE_SIZE } from "../../office/layout";
import type { Character } from "../../characters/types";
import { drawCharacter, drawCharacterBubble } from "../../characters/sprites";
import type { RenderContext, SceneRenderable } from "./context";

function characterDepth(char: Character): number {
  return char.y / TILE_SIZE + char.x * 0.00001;
}

export function renderCharacters(
  rctx: RenderContext,
  characters: Character[],
): SceneRenderable[] {
  return characters.map((char) => ({
    depth: characterDepth(char),
    draw: () => drawCharacter(rctx.ctx, char, rctx.camera.zoom, rctx.camera),
  }));
}

export function renderCharacterBubbles(
  rctx: RenderContext,
  characters: Character[],
): SceneRenderable[] {
  return characters
    .filter((char) => char.bubble !== null)
    .map((char) => ({
      depth: characterDepth(char),
      draw: () =>
        drawCharacterBubble(rctx.ctx, char, rctx.camera.zoom, rctx.camera),
    }));
}
