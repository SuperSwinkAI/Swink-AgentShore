import {
  TILE_SIZE,
  FRONT_DESK_EXIT,
  FRONT_DESK_SPAWN_SPOTS,
  getZone,
  zoneMap,
  type Tile,
  type WorkSeat,
} from "../office/layout";
import { ZoneId } from "../office/layout";
import {
  bfsPath,
  isWalkable,
  isWalkableIgnoringFurniture,
} from "../office/pathfinding";
import {
  Character,
  CharacterState,
  DEFAULT_AGENT_MODEL_TIER,
  Direction,
  NpcKind,
  type AgentModelTier,
  type CharacterBubble,
  type NpcDefinition,
} from "./types";

const WALK_SPEED = 72; // pixels per second
const WALK_FRAME_DURATION = 0.15;
const WORK_FRAME_DURATION = 0.3;
const WANDER_MIN = 5;
const WANDER_MAX = 15;
const WANDER_DIRECTIONS: Tile[] = [
  { x: 1, y: 0 },
  { x: -1, y: 0 },
  { x: 0, y: 1 },
  { x: 0, y: -1 },
];
const WANDER_STEPS = [1, 2];
const WANDER_REVERSE_STEPS = [2, 1];

function randomRange(min: number, max: number): number {
  return min + Math.random() * (max - min);
}

function tileCenter(tile: Tile): { x: number; y: number } {
  return {
    x: tile.x * TILE_SIZE + TILE_SIZE / 2,
    y: tile.y * TILE_SIZE + TILE_SIZE / 2,
  };
}

type CharacterBaseOverrides = Partial<
  Omit<Character, "agentId" | "agentType" | "x" | "y">
>;

function makeCharacterBase(
  agentId: string,
  agentType: string,
  pos: { x: number; y: number },
  overrides: CharacterBaseOverrides = {},
): Character {
  const {
    wanderTimer = randomRange(2, 5),
    reservedSeatKey = null,
    opacity = 0,
    ...rest
  } = overrides;

  return {
    agentId,
    agentType,
    state: CharacterState.IDLE,
    direction: Direction.DOWN,
    x: pos.x,
    y: pos.y,
    path: [],
    pathIndex: 0,
    targetState: CharacterState.IDLE,
    targetDirection: null,
    animFrame: 0,
    animTimer: 0,
    wanderTimer,
    reservedSeatKey,
    activePlayId: null,
    activePlayType: null,
    status: "idle",
    bubble: null,
    bubbleUntil: null,
    opacity,
    despawning: false,
    despawnOnArrival: false,
    ...rest,
  };
}

function posToTile(x: number, y: number): Tile {
  return { x: Math.floor(x / TILE_SIZE), y: Math.floor(y / TILE_SIZE) };
}

function directionFromDelta(dx: number, dy: number): Direction {
  if (Math.abs(dx) > Math.abs(dy)) {
    return dx > 0 ? Direction.RIGHT : Direction.LEFT;
  }
  return dy > 0 ? Direction.DOWN : Direction.UP;
}

function directionFromSeatFacing(facing: WorkSeat["facing"]): Direction | null {
  switch (facing) {
    case "north":
      return Direction.UP;
    case "south":
      return Direction.DOWN;
    case "east":
      return Direction.RIGHT;
    case "west":
      return Direction.LEFT;
    default:
      return null;
  }
}

function shuffled<T>(items: readonly T[]): T[] {
  const result = [...items];
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
}

function tileZone(tile: Tile): ZoneId | null {
  return zoneMap[tile.y]?.[tile.x] ?? null;
}

function sameZoneAs(origin: Tile, target: Tile): boolean {
  const originZone = tileZone(origin);
  return originZone !== null && originZone === tileZone(target);
}

function wanderTarget(
  currentTile: Tile,
  canWalk: (x: number, y: number) => boolean,
): Tile | null {
  const validTarget = (tile: Tile) =>
    canWalk(tile.x, tile.y) && sameZoneAs(currentTile, tile);
  const validLineTarget = (direction: Tile, step: number): Tile | null => {
    let target = currentTile;
    for (let i = 1; i <= step; i++) {
      target = {
        x: currentTile.x + direction.x * i,
        y: currentTile.y + direction.y * i,
      };
      if (!validTarget(target)) return null;
    }
    return target;
  };

  for (const direction of shuffled(WANDER_DIRECTIONS)) {
    for (const step of shuffled(WANDER_STEPS)) {
      const forward = validLineTarget(direction, step);
      if (forward) return forward;

      for (const reverseStep of WANDER_REVERSE_STEPS) {
        const reverse = validLineTarget(
          { x: -direction.x, y: -direction.y },
          reverseStep,
        );
        if (reverse) return reverse;
      }
    }
  }

  return null;
}

export function updateCharacter(char: Character, dt: number): void {
  if (!char.despawning && char.opacity < 1) {
    char.opacity = Math.min(1, char.opacity + dt / 0.5);
  }
  if (char.despawning) {
    char.opacity = Math.max(0, char.opacity - dt / 0.5);
    return;
  }
  if (char.bubbleUntil !== null && performance.now() > char.bubbleUntil) {
    char.bubble = null;
    char.bubbleUntil = null;
  }

  switch (char.state) {
    case CharacterState.IDLE:
      updateIdle(char, dt);
      break;
    case CharacterState.WALK:
      updateWalk(char, dt);
      break;
    case CharacterState.WORK:
      updateWork(char, dt);
      break;
  }
}

function updateIdle(char: Character, dt: number): void {
  char.wanderTimer -= dt;
  if (char.wanderTimer <= 0) {
    const currentTile = posToTile(char.x, char.y);
    const ignoreFurniture = char.npcKind === NpcKind.RUSSIAN_BLUE_CAT;
    const canWalk = ignoreFurniture ? isWalkableIgnoringFurniture : isWalkable;
    const target = wanderTarget(currentTile, canWalk);
    if (target) {
      const path = bfsPath(currentTile, target, { ignoreFurniture });
      if (path.length > 0) {
        char.path = path;
        char.pathIndex = 0;
        char.targetState = CharacterState.IDLE;
        char.state = CharacterState.WALK;
        char.animFrame = 0;
        char.animTimer = 0;
        return;
      }
    }
    char.wanderTimer = randomRange(WANDER_MIN, WANDER_MAX);
  }
}

function updateWalk(char: Character, dt: number): void {
  if (char.pathIndex >= char.path.length) {
    if (char.despawnOnArrival) {
      char.despawnOnArrival = false;
      char.despawning = true;
      char.state = CharacterState.IDLE;
      char.animFrame = 0;
      char.animTimer = 0;
      return;
    }
    char.state = char.targetState;
    if (char.targetDirection !== undefined && char.targetDirection !== null) {
      char.direction = char.targetDirection;
      char.targetDirection = null;
    }
    char.animFrame = 0;
    char.animTimer = 0;
    if (char.state === CharacterState.IDLE) {
      char.wanderTimer = randomRange(WANDER_MIN, WANDER_MAX);
    }
    return;
  }

  const target = tileCenter(char.path[char.pathIndex]);
  const dx = target.x - char.x;
  const dy = target.y - char.y;
  const dist = Math.sqrt(dx * dx + dy * dy);

  if (dist < 1) {
    char.x = target.x;
    char.y = target.y;
    char.pathIndex++;
    return;
  }

  const speed = WALK_SPEED * dt;
  const move = Math.min(speed, dist);
  char.x += (dx / dist) * move;
  char.y += (dy / dist) * move;
  char.direction = directionFromDelta(dx, dy);

  // walk animation
  char.animTimer += dt;
  if (char.animTimer >= WALK_FRAME_DURATION) {
    char.animTimer -= WALK_FRAME_DURATION;
    char.animFrame = (char.animFrame + 1) % 4;
  }
}

function updateWork(char: Character, dt: number): void {
  char.animTimer += dt;
  if (char.animTimer >= WORK_FRAME_DURATION) {
    char.animTimer -= WORK_FRAME_DURATION;
    char.animFrame = (char.animFrame + 1) % 2;
  }
}

const occupiedSeats = new Set<string>();

function seatKey(tile: Tile): string {
  return `${tile.x},${tile.y}`;
}

function reserveSeat(seats: WorkSeat[]): {
  seat: WorkSeat | null;
  key: string | null;
} {
  const available = seats
    .map((seat) => ({ seat, key: seatKey(seat) }))
    .filter((candidate) => !occupiedSeats.has(candidate.key));
  if (available.length === 0) {
    return { seat: null, key: null };
  }

  const selected = available[Math.floor(Math.random() * available.length)];
  occupiedSeats.add(selected.key);
  return selected;
}

export function assignToZone(char: Character, zoneId: ZoneId): void {
  moveToZone(char, zoneId, CharacterState.WORK);
}

function moveToZone(
  char: Character,
  zoneId: ZoneId,
  targetState: CharacterState,
): void {
  releaseSeat(char);
  const zone = getZone(zoneId);
  const { seat, key } = reserveSeat(zone.seats);
  let targetSeat = seat;

  if (!targetSeat) {
    targetSeat = {
      x: zone.bounds.x + Math.floor(zone.bounds.w / 2),
      y: zone.bounds.y + Math.floor(zone.bounds.h / 2),
    };
  }

  char.reservedSeatKey = key;
  char.targetDirection = directionFromSeatFacing(targetSeat.facing);
  char.despawnOnArrival = false;
  const currentTile = posToTile(char.x, char.y);
  const path = bfsPath(currentTile, targetSeat);
  char.path = path;
  char.pathIndex = 0;
  char.targetState = targetState;
  char.state = CharacterState.WALK;
  char.animFrame = 0;
  char.animTimer = 0;
}

export function returnToIdle(char: Character): void {
  moveToZone(char, ZoneId.ZEN_GARDEN, CharacterState.IDLE);
}

export function sendToRecovery(char: Character): void {
  moveToZone(char, ZoneId.RECOVERY_BAY, CharacterState.IDLE);
}

export function sendToFrontDeskExit(char: Character): void {
  if (char.despawning || char.despawnOnArrival) return;
  releaseSeat(char);
  const currentTile = posToTile(char.x, char.y);
  const path = bfsPath(currentTile, FRONT_DESK_EXIT);
  char.path = path;
  char.pathIndex = 0;
  char.targetState = CharacterState.IDLE;
  char.targetDirection = null;
  char.state = path.length > 0 ? CharacterState.WALK : CharacterState.IDLE;
  char.animFrame = 0;
  char.animTimer = 0;
  char.despawnOnArrival = true;
  if (path.length === 0) {
    char.despawnOnArrival = false;
    char.despawning = true;
  }
}

export function releaseSeat(char: Character): void {
  if (char.reservedSeatKey) {
    occupiedSeats.delete(char.reservedSeatKey);
    char.reservedSeatKey = null;
  }
}

export function showBubble(
  char: Character,
  bubble: CharacterBubble,
  durationMs?: number,
): void {
  char.bubble = bubble;
  char.bubbleUntil =
    durationMs === undefined ? null : performance.now() + durationMs;
}

export function spawnCharacter(
  agentId: string,
  agentType: string,
  modelTier: AgentModelTier = DEFAULT_AGENT_MODEL_TIER,
): Character {
  const { seat, key } = reserveSeat(FRONT_DESK_SPAWN_SPOTS);
  const pos = tileCenter(seat ?? FRONT_DESK_EXIT);
  return makeCharacterBase(agentId, agentType, pos, {
    modelTier,
    wanderTimer: randomRange(2, 5),
    reservedSeatKey: key,
    opacity: 0,
  });
}

export function spawnNpcCharacter(definition: NpcDefinition): Character {
  const pos = tileCenter(definition.startTile);
  return makeCharacterBase(definition.id, `npc_${definition.kind}`, pos, {
    displayName: definition.name,
    npcKind: definition.kind,
    scale: definition.scale,
    wanderTimer: randomRange(1, 8),
    opacity: 1,
  });
}

export const __testHooks = {
  reserveSeat,
  clearOccupiedSeats: () => occupiedSeats.clear(),
};
