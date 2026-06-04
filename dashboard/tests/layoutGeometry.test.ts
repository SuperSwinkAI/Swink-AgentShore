import { describe, expect, it } from "vitest";

import {
  DOORS,
  FURNITURE,
  VISUAL_WALL_BARRIERS,
  WALL_BARRIERS,
  WALL_THICKNESS_UNITS,
  ZONES,
  ZoneId,
  doorCenterTiles,
  isDoorEdgeBuffer,
  isFurnitureSideBuffer,
} from "../src/office/layout";
import { buildWalkableGrid, isWalkable } from "../src/office/pathfinding";

describe("office wall geometry", () => {
  it("renders structural wall dividers no thicker than a half tile", () => {
    expect(VISUAL_WALL_BARRIERS).toHaveLength(WALL_BARRIERS.length);

    for (const [index, visual] of VISUAL_WALL_BARRIERS.entries()) {
      const logical = WALL_BARRIERS[index];
      const isVertical = logical.h >= logical.w;

      expect(isVertical ? visual.w : visual.h).toBeLessThanOrEqual(
        WALL_THICKNESS_UNITS,
      );
      if (isVertical) {
        expect(visual.h).toBe(logical.h);
      } else {
        expect(visual.w).toBe(logical.w);
        expect(visual.y + visual.h).toBeCloseTo(logical.y + logical.h);
      }
    }
  });

  it("keeps door walk lanes centered away from wall edges", () => {
    buildWalkableGrid();

    for (const door of DOORS) {
      const centerKeys = new Set(
        doorCenterTiles(door).map((tile) => `${tile.x},${tile.y}`),
      );

      for (let y = door.y; y < door.y + door.h; y += 1) {
        for (let x = door.x; x < door.x + door.w; x += 1) {
          const key = `${x},${y}`;
          if (centerKeys.has(key)) {
            expect(isDoorEdgeBuffer(x, y)).toBe(false);
            expect(isWalkable(x, y)).toBe(true);
          } else {
            expect(isDoorEdgeBuffer(x, y)).toBe(true);
            expect(isWalkable(x, y)).toBe(false);
          }
        }
      }
    }
  });
});

describe("office furniture layout", () => {
  it("does not include the Workshop monitor desk in the shared theme layout", () => {
    expect(FURNITURE.some((item) => item.name === "Monitor Desk W")).toBe(
      false,
    );
  });

  it("does not include the Buddha in the shared theme layout", () => {
    expect(FURNITURE.some((item) => item.name === "Seated Buddha")).toBe(false);
  });

  it("keeps Workshop destinations near furniture with sprite clearance", () => {
    const workshop = ZONES.find((zone) => zone.id === ZoneId.WORKSHOP);
    expect(workshop).toBeDefined();
    const workshopFurniture = FURNITURE.filter(
      (item) => item.zoneId === ZoneId.WORKSHOP,
    );

    for (const seat of workshop?.seats ?? []) {
      expect(seat.y).toBeLessThanOrEqual(34);
      const nearestFurnitureDistance = Math.min(
        ...workshopFurniture.map((item) => {
          const dx = Math.max(
            item.x - seat.x,
            0,
            seat.x - (item.x + item.w - 1),
          );
          const dy = Math.max(
            item.y - seat.y,
            0,
            seat.y - (item.y + item.h - 1),
          );
          return dx + dy;
        }),
      );
      expect(nearestFurnitureDistance).toBe(2);
    }
  });

  it("keeps agent destinations separated for sprite clearance", () => {
    const separationFailures: string[] = [];

    for (const zone of ZONES) {
      for (let i = 0; i < zone.seats.length; i += 1) {
        for (let j = i + 1; j < zone.seats.length; j += 1) {
          const a = zone.seats[i];
          const b = zone.seats[j];
          const tileDistance = Math.max(
            Math.abs(a.x - b.x),
            Math.abs(a.y - b.y),
          );
          if (tileDistance <= 2) {
            separationFailures.push(
              `${zone.name || ZoneId[zone.id]}:${a.x},${a.y}<->${b.x},${b.y}`,
            );
          }
        }
      }
    }

    expect(separationFailures).toEqual([]);
  });

  it("keeps the marked Workshop and Recovery Bay destinations on the floorplan", () => {
    const workshop = ZONES.find((zone) => zone.id === ZoneId.WORKSHOP);
    const recoveryBay = ZONES.find((zone) => zone.id === ZoneId.RECOVERY_BAY);

    expect(workshop?.seats).toEqual(
      expect.arrayContaining([
        { x: 27, y: 22, facing: "north" },
        { x: 32, y: 35, facing: "west" },
        { x: 44, y: 34, facing: "east" },
      ]),
    );
    expect(workshop?.seats).not.toContainEqual({
      x: 52,
      y: 30,
      facing: "east",
    });
    expect(recoveryBay?.seats).toContainEqual({
      x: 9,
      y: 48,
      facing: "east",
    });
  });

  it("keeps destinations and walk lanes off furniture side buffers", () => {
    buildWalkableGrid();

    const destinationFailures: string[] = [];
    for (const zone of ZONES) {
      for (const seat of zone.seats) {
        if (isFurnitureSideBuffer(seat.x, seat.y)) {
          destinationFailures.push(`${zone.name}:${seat.x},${seat.y}`);
        }
      }
    }

    const laneFailures: string[] = [];
    for (const item of FURNITURE) {
      for (let y = item.y; y < item.y + item.h; y++) {
        for (const x of [item.x - 1, item.x + item.w]) {
          if (isFurnitureSideBuffer(x, y) && isWalkable(x, y)) {
            laneFailures.push(`${item.name}:${x},${y}`);
          }
        }
      }
    }

    expect(destinationFailures).toEqual([]);
    expect(laneFailures).toEqual([]);
  });
});
