import { describe, expect, it } from "vitest";

import {
  renderCharacterBubbles,
  renderCharacters,
} from "../src/engine/renderer/characters";
import { TILE_SIZE } from "../src/office/layout";
import {
  type Character,
  CharacterState,
  Direction,
} from "../src/characters/types";
import type { RenderContext } from "../src/engine/renderer/context";

function makeCharacter(
  agentId: string,
  xTiles: number,
  yTiles: number,
  bubble: Character["bubble"] = null,
): Character {
  return {
    agentId,
    agentType: "codex",
    modelTier: "medium",
    displayName: agentId,
    state: CharacterState.IDLE,
    direction: Direction.DOWN,
    x: xTiles * TILE_SIZE,
    y: yTiles * TILE_SIZE,
    path: [],
    pathIndex: 0,
    targetState: CharacterState.IDLE,
    targetDirection: null,
    animFrame: 0,
    animTimer: 0,
    wanderTimer: 0,
    reservedSeatKey: null,
    activePlayId: null,
    activePlayType: null,
    status: "busy",
    bubble,
    bubbleUntil: null,
    opacity: 1,
    despawning: false,
    despawnOnArrival: false,
  };
}

describe("dashboard renderer layering", () => {
  it("keeps thought bubbles in a separate overlay pass", () => {
    const rctx = {} as RenderContext;
    const front = makeCharacter("front", 3, 12, {
      text: "Issue Pickup 12",
      tone: "work",
    });
    const back = makeCharacter("back", 2, 5, {
      text: "Code Review 7",
      tone: "work",
    });
    const silent = makeCharacter("silent", 1, 9);

    const bodyRenderables = renderCharacters(rctx, [front, back, silent]);
    const bubbleRenderables = renderCharacterBubbles(rctx, [
      front,
      back,
      silent,
    ]);

    expect(bodyRenderables).toHaveLength(3);
    expect(bubbleRenderables).toHaveLength(2);
    expect(
      bubbleRenderables
        .map((renderable) => renderable.depth)
        .sort((a, b) => a - b),
    ).toEqual([
      back.y / TILE_SIZE + back.x * 0.00001,
      front.y / TILE_SIZE + front.x * 0.00001,
    ]);
  });
});
