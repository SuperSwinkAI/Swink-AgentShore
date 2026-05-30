import { describe, expect, it } from "vitest";
import { Camera } from "../src/engine/camera";
import {
  TILE_SIZE,
  projectedMapBounds,
  projectionModeForTheme,
} from "../src/office/layout";

const EPSILON = 1e-6;

function expectRoundTrip(worldX: number, worldY: number): void {
  const camera = new Camera();
  const viewport = { left: 60, top: 40, right: 1260, bottom: 860 };
  camera.fitToViewport(viewport, 16);

  const [sx, sy] = camera.worldToScreen(worldX, worldY);
  const [wx, wy] = camera.screenToWorld(sx, sy);

  expect(wx).toBeCloseTo(worldX, 6);
  expect(wy).toBeCloseTo(worldY, 6);
}

describe("office projection modes", () => {
  it("round-trips world and screen coordinates", () => {
    expectRoundTrip(24.5 * TILE_SIZE, 17.25 * TILE_SIZE);
  });

  it("fits the projection inside viewport bounds", () => {
    const viewport = { left: 80, top: 64, right: 1360, bottom: 880 };

    const camera = new Camera();
    camera.fitToViewport(viewport, 20);

    const bounds = projectedMapBounds();
    const left = bounds.left * camera.zoom + camera.x;
    const top = bounds.top * camera.zoom + camera.y;
    const right = bounds.right * camera.zoom + camera.x;
    const bottom = bounds.bottom * camera.zoom + camera.y;

    expect(left).toBeGreaterThanOrEqual(viewport.left + 20 - EPSILON);
    expect(top).toBeGreaterThanOrEqual(viewport.top + 20 - EPSILON);
    expect(right).toBeLessThanOrEqual(viewport.right - 20 + EPSILON);
    expect(bottom).toBeLessThanOrEqual(viewport.bottom - 20 + EPSILON);
  });

  it("derives grid projection for supported theme modes", () => {
    for (const theme of [
      "system",
      "light",
      "dark",
      "light",
      "dark",
    ]) {
      expect(projectionModeForTheme(theme)).toBe("grid");
    }
  });

  it("keeps map bounds identical across accepted theme inputs", () => {
    const gridBounds = projectedMapBounds();

    for (const theme of [
      "system",
      "light",
      "dark",
      "light",
      "dark",
    ]) {
      const bounds = projectedMapBounds(projectionModeForTheme(theme));
      expect(bounds.left).toBeCloseTo(gridBounds.left, 6);
      expect(bounds.top).toBeCloseTo(gridBounds.top, 6);
      expect(bounds.right).toBeCloseTo(gridBounds.right, 6);
      expect(bounds.bottom).toBeCloseTo(gridBounds.bottom, 6);
    }
  });
});
