import {
  ACTOR_HEIGHT_UNITS,
  AXON_VERTICAL_SCALE,
  TILE_SIZE,
} from "../office/layout";
import type { Camera } from "../engine/camera";
import { dashboardLogger } from "../logger";
import { AGENT_REGISTRY } from "../agentRegistry";
import {
  type Character,
  type CharacterBubble,
  type CharacterBubbleKind,
  CharacterState,
  Direction,
  NpcKind,
  AGENT_COLORS,
  normalizeAgentModelTier,
  type AgentModelTier,
} from "./types";

const V2_SPRITE_FRAME_WIDTH = 416;
const V2_SPRITE_FRAME_HEIGHT = 832;
const V2_SPRITE_SHEET_WIDTH = 2912;
const V2_SPRITE_SHEET_HEIGHT = 2496;
const AGENT_SPRITE_ASPECT = V2_SPRITE_FRAME_WIDTH / V2_SPRITE_FRAME_HEIGHT;
const SPRITE_LOAD_EVENT = "agentshore:agent-sprite-loaded";
// Measured from the accepted v2 idle/down frames at the visible alpha threshold.
const V2_SIGNIFICANT_FRAME_HEIGHT_BY_TIER: Record<AgentModelTier, number> = {
  small: 275,
  medium: 570,
  large: 741,
};
const V2_BASELINE_SIGNIFICANT_FRAME_HEIGHT =
  V2_SIGNIFICANT_FRAME_HEIGHT_BY_TIER.medium;

export interface AgentSpriteSpec {
  key: string;
  url: string;
  frameWidth: number;
  frameHeight: number;
  sheetWidth: number;
  sheetHeight: number;
}

interface LoadedAgentSprite {
  image: HTMLImageElement;
  spec: AgentSpriteSpec;
}

function v2SpriteSpec(key: string, url: string): AgentSpriteSpec {
  return {
    key,
    url,
    frameWidth: V2_SPRITE_FRAME_WIDTH,
    frameHeight: V2_SPRITE_FRAME_HEIGHT,
    sheetWidth: V2_SPRITE_SHEET_WIDTH,
    sheetHeight: V2_SPRITE_SHEET_HEIGHT,
  };
}

// Sprite key = asset filename stem; keeps keys in lockstep with PNG assets, no separate mapping.
function spriteKeyFromUrl(url: string, fallback: string): string {
  const file = url.split(/[?#]/)[0].split("/").pop() ?? "";
  const stem = file.replace(/\.[a-z0-9]+$/i, "");
  return stem || fallback;
}

// Built from AGENT_REGISTRY: a new agent type needs only a registry entry + PNG assets.
const V2_AGENT_SPRITE_SPECS: Partial<
  Record<string, Record<AgentModelTier, AgentSpriteSpec>>
> = Object.fromEntries(
  Object.entries(AGENT_REGISTRY)
    .filter(([, entry]) => entry.spriteUrls !== null)
    .map(([key, entry]) => {
      const urls = entry.spriteUrls!;
      return [
        key,
        {
          small: v2SpriteSpec(spriteKeyFromUrl(urls.small, `${key}-small`), urls.small),
          medium: v2SpriteSpec(spriteKeyFromUrl(urls.medium, `${key}-medium`), urls.medium),
          large: v2SpriteSpec(spriteKeyFromUrl(urls.large, `${key}-large`), urls.large),
        } satisfies Record<AgentModelTier, AgentSpriteSpec>,
      ];
    }),
);

const agentSprites = new Map<string, HTMLImageElement>();

export interface CharacterScreenBounds {
  left: number;
  top: number;
  right: number;
  bottom: number;
  width: number;
  height: number;
  footX: number;
  footY: number;
}

export function agentHeightUnitsForTier(
  modelTier: string | null | undefined,
): number {
  const tier = normalizeAgentModelTier(modelTier);
  return (
    (ACTOR_HEIGHT_UNITS * V2_SIGNIFICANT_FRAME_HEIGHT_BY_TIER[tier]) /
    V2_BASELINE_SIGNIFICANT_FRAME_HEIGHT
  );
}

export function agentVisibleFrameRatio(
  modelTier: string | null | undefined,
  agentType?: string,
): number {
  const tier = normalizeAgentModelTier(modelTier);
  if (agentType && !V2_AGENT_SPRITE_SPECS[agentType]) return 1;
  return V2_SIGNIFICANT_FRAME_HEIGHT_BY_TIER[tier] / V2_SPRITE_FRAME_HEIGHT;
}

export function agentVisibleHeight(
  zoom: number,
  scale = 1,
  modelTier?: string | null,
): number {
  return (
    agentHeightUnitsForTier(modelTier) *
    TILE_SIZE *
    AXON_VERTICAL_SCALE *
    scale *
    zoom
  );
}

export function agentVisualSize(
  zoom: number,
  scale = 1,
  modelTier?: string | null,
  agentType?: string,
): { width: number; height: number } {
  const frameHeightUnits =
    agentType && !V2_AGENT_SPRITE_SPECS[agentType]
      ? ACTOR_HEIGHT_UNITS
      : agentHeightUnitsForTier(modelTier) /
        agentVisibleFrameRatio(modelTier, agentType);
  const height =
    frameHeightUnits * TILE_SIZE * AXON_VERTICAL_SCALE * scale * zoom;
  return {
    width: height * AGENT_SPRITE_ASPECT,
    height,
  };
}

export function characterScreenBounds(
  char: Character,
  zoom: number,
  camera: Camera,
): CharacterScreenBounds {
  const scale = char.scale ?? 1;
  const [footX, footY] = camera.worldToScreen(char.x, char.y);
  const size = char.npcKind
    ? npcDrawSize(char.npcKind)
    : agentVisualSize(zoom, scale, char.modelTier, char.agentType);
  const width = size.width * (char.npcKind ? scale * zoom : 1);
  const height = size.height * (char.npcKind ? scale * zoom : 1);
  const left = footX - width / 2;
  const top = footY - height;
  return {
    left,
    top,
    right: left + width,
    bottom: footY,
    width,
    height,
    footX,
    footY,
  };
}

export function addCharacterSpriteLoadListener(
  handler: () => void,
): () => void {
  window.addEventListener(SPRITE_LOAD_EVENT, handler);
  return () => window.removeEventListener(SPRITE_LOAD_EVENT, handler);
}

export function drawCharacter(
  ctx: CanvasRenderingContext2D,
  char: Character,
  zoom: number,
  camera: Camera,
): void {
  if (char.npcKind) {
    drawNpcCharacter(ctx, char, zoom, camera);
    return;
  }

  const sprite = agentSpriteFor(char);
  if (sprite) {
    drawSpriteCharacter(ctx, char, zoom, camera, sprite);
    return;
  }

  drawPlaceholderAgentCharacter(ctx, char, zoom, camera);
}

function agentSpriteFor(char: Character): LoadedAgentSprite | null {
  const spec = agentSpriteSpecFor(char.agentType, char.modelTier, char.agentId);
  if (!spec) return null;

  let img = agentSprites.get(spec.url);
  if (!img) {
    img = new Image();
    img.decoding = "async";
    const loadedImg = img;
    loadedImg.addEventListener(
      "load",
      () => {
        if (
          loadedImg.naturalWidth !== spec.sheetWidth ||
          loadedImg.naturalHeight !== spec.sheetHeight
        ) {
          dashboardLogger.warn("sprites", "agent sprite dimensions mismatch", {
            key: spec.key,
            actual: `${loadedImg.naturalWidth}x${loadedImg.naturalHeight}`,
            expected: `${spec.sheetWidth}x${spec.sheetHeight}`,
          });
        }
        window.dispatchEvent(new Event(SPRITE_LOAD_EVENT));
      },
      { once: true },
    );
    img.src = spec.url;
    agentSprites.set(spec.url, img);
  }

  return img.complete && img.naturalWidth > 0 ? { image: img, spec } : null;
}

export function agentSpriteSpecFor(
  agentType: string,
  modelTier: string | null | undefined,
  _agentId = "",
): AgentSpriteSpec | null {
  const v2Specs = V2_AGENT_SPRITE_SPECS[agentType];
  if (v2Specs) {
    return v2Specs[normalizeAgentModelTier(modelTier)];
  }

  return null;
}

function drawSpriteCharacter(
  ctx: CanvasRenderingContext2D,
  char: Character,
  zoom: number,
  camera: Camera,
  sprite: LoadedAgentSprite,
): void {
  const scale = char.scale ?? 1;
  const bounds = characterScreenBounds(char, zoom, camera);
  const { image, spec } = sprite;
  const w = bounds.width;
  const h = bounds.height;
  const sx = bounds.left;
  const sy = bounds.top;
  const bob =
    char.state === CharacterState.WALK
      ? Math.sin(char.animFrame * Math.PI) * 2 * zoom
      : 0;
  const sourceCol = spriteColumnFor(char);
  const sourceRow = spriteRowFor(char.direction);
  const flip = char.direction === Direction.LEFT;
  const footprintW = TILE_SIZE * scale * zoom;

  ctx.save();
  ctx.globalAlpha = char.opacity;
  drawShadow(
    ctx,
    bounds.footX - footprintW / 2,
    bounds.footY - 2 * zoom,
    footprintW,
    zoom,
  );

  if (flip) {
    ctx.translate(sx + w, sy - bob);
    ctx.scale(-1, 1);
    ctx.drawImage(
      image,
      sourceCol * spec.frameWidth,
      sourceRow * spec.frameHeight,
      spec.frameWidth,
      spec.frameHeight,
      0,
      0,
      w,
      h,
    );
  } else {
    ctx.drawImage(
      image,
      sourceCol * spec.frameWidth,
      sourceRow * spec.frameHeight,
      spec.frameWidth,
      spec.frameHeight,
      sx,
      sy - bob,
      w,
      h,
    );
  }

  ctx.restore();

  ctx.save();
  ctx.globalAlpha = char.opacity;
  if (char.status === "error") {
    ctx.fillStyle = "rgba(244, 67, 54, 0.35)";
    ctx.fillRect(sx, sy - bob, w, h);
  }

  drawErrorStatusMarker(ctx, char.status, sx + w, sy - bob, zoom);
  drawName(
    ctx,
    char.displayName ?? char.agentType.replace("api_", ""),
    bounds.footX,
    bounds.footY + 2,
    zoom,
  );
  ctx.restore();
}

export function drawCharacterBubble(
  ctx: CanvasRenderingContext2D,
  char: Character,
  zoom: number,
  camera: Camera,
): void {
  if (char.npcKind || !char.bubble) return;

  const bounds = characterScreenBounds(char, zoom, camera);
  const bob =
    char.state === CharacterState.WALK
      ? Math.sin(char.animFrame * Math.PI) * 2 * zoom
      : 0;

  ctx.save();
  ctx.globalAlpha = char.opacity;
  drawBubble(
    ctx,
    char.bubble,
    bounds.left + bounds.width / 2,
    bounds.top - bob - 8 * zoom,
    zoom,
  );
  ctx.restore();
}

function spriteColumnFor(char: Character): number {
  const frame = Math.floor(char.animFrame);
  if (char.state === CharacterState.WALK) return frame % 4;
  if (char.state === CharacterState.WORK) return 5 + (frame % 2);
  return 4;
}

function spriteRowFor(direction: Direction): number {
  if (direction === Direction.UP) return 1;
  if (direction === Direction.RIGHT || direction === Direction.LEFT) return 2;
  return 0;
}

function drawPlaceholderAgentCharacter(
  ctx: CanvasRenderingContext2D,
  char: Character,
  zoom: number,
  camera: Camera,
): void {
  const colors = AGENT_COLORS[char.agentType] ?? {
    fill: "#888888",
    label: "?",
  };
  const bob =
    char.state === CharacterState.WALK
      ? Math.sin(char.animFrame * Math.PI) * 2 * zoom
      : 0;
  const scale = char.scale ?? 1;
  const bounds = characterScreenBounds(char, zoom, camera);
  const w = bounds.width;
  const h = bounds.height;
  const sx = bounds.left;
  const sy = bounds.top;
  const footprintW = TILE_SIZE * scale * zoom;

  let alpha = 1;
  if (char.state === CharacterState.WORK) {
    alpha = 0.78 + 0.22 * Math.sin((char.animFrame + 1) * Math.PI * 0.5);
  }

  ctx.save();
  ctx.globalAlpha = alpha * char.opacity;
  drawShadow(
    ctx,
    bounds.footX - footprintW / 2,
    bounds.footY - 2 * zoom,
    footprintW,
    zoom,
  );

  ctx.fillStyle = colors.fill;
  ctx.fillRect(sx, sy - bob, w, h);
  ctx.strokeStyle = "#000";
  ctx.lineWidth = 1;
  ctx.strokeRect(sx, sy - bob, w, h);

  ctx.fillStyle = "#FFF";
  const fontSize = Math.max(8, 10 * zoom);
  ctx.font = `bold ${fontSize}px monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(colors.label, sx + w / 2, sy - bob + h / 2);

  if (char.status === "error") {
    ctx.fillStyle = "rgba(244, 67, 54, 0.35)";
    ctx.fillRect(sx, sy - bob, w, h);
  }

  drawErrorStatusMarker(ctx, char.status, sx + w, sy - bob, zoom);
  drawName(
    ctx,
    char.displayName ?? char.agentType.replace("api_", ""),
    bounds.footX,
    bounds.footY + 2,
    zoom,
  );
  ctx.restore();
}

function drawErrorStatusMarker(
  ctx: CanvasRenderingContext2D,
  status: string,
  right: number,
  top: number,
  zoom: number,
): void {
  if (status !== "error") return;

  const dotSize = Math.max(3, 4 * zoom);
  ctx.fillStyle = "#F44336";
  ctx.fillRect(right - dotSize - 1, top + 1, dotSize, dotSize);
}

interface NpcPalette {
  body: string;
  accent: string;
  dark: string;
  light: string;
  label: string;
}

const NPC_PALETTES: Record<NpcKind, NpcPalette> = {
  [NpcKind.MASTIFF]: {
    body: "#7A4A2A",
    accent: "#9A6A44",
    dark: "#3A2416",
    light: "#C49A6C",
    label: "#E6C9A8",
  },
  [NpcKind.GERMAN_SHEPHERD]: {
    body: "#B98645",
    accent: "#2E241B",
    dark: "#17120E",
    light: "#D3A465",
    label: "#E8D2A4",
  },
  [NpcKind.RUSSIAN_BLUE_CAT]: {
    body: "#6F8294",
    accent: "#93A6B8",
    dark: "#344452",
    light: "#BFD0DC",
    label: "#C8D6DF",
  },
};

function drawNpcCharacter(
  ctx: CanvasRenderingContext2D,
  char: Character,
  zoom: number,
  camera: Camera,
): void {
  const scale = char.scale ?? 1;
  const bounds = characterScreenBounds(char, zoom, camera);
  const size = npcDrawSize(char.npcKind ?? NpcKind.MASTIFF);
  const w = size.width * scale * zoom;
  const h = size.height * scale * zoom;
  const sx = bounds.left;
  const sy = bounds.top;
  const px = Math.max(1, Math.round(zoom * scale));
  const bob =
    char.state === CharacterState.WALK
      ? Math.sin(char.animFrame * Math.PI) * px
      : 0;
  const palette = NPC_PALETTES[char.npcKind ?? NpcKind.MASTIFF];

  ctx.save();
  ctx.globalAlpha = char.opacity;
  drawShadow(ctx, sx, bounds.footY - px, w, Math.max(1, px * 0.75));

  if (char.npcKind === NpcKind.RUSSIAN_BLUE_CAT) {
    drawCat(ctx, sx, sy - bob, w, h, px, palette, char.direction);
  } else {
    drawDog(ctx, sx, sy - bob, w, h, px, palette, char.npcKind, char.direction);
  }

  drawName(
    ctx,
    char.displayName ?? char.agentId,
    bounds.footX,
    bounds.footY + 2 * px,
    zoom,
    palette.label,
  );
  ctx.restore();
}

function npcDrawSize(kind: NpcKind): { width: number; height: number } {
  switch (kind) {
    case NpcKind.MASTIFF:
      return { width: 22, height: 15 };
    case NpcKind.GERMAN_SHEPHERD:
      return { width: 20, height: 13 };
    case NpcKind.RUSSIAN_BLUE_CAT:
      return { width: 18, height: 10 };
  }
}

function drawDog(
  ctx: CanvasRenderingContext2D,
  sx: number,
  sy: number,
  w: number,
  h: number,
  px: number,
  palette: NpcPalette,
  kind: NpcKind | undefined,
  direction: Direction,
): void {
  const facingLeft = direction === Direction.LEFT;
  const headX = facingLeft ? sx + w * 0.04 : sx + w * 0.62;
  const tailX = facingLeft ? sx + w * 0.82 : sx + w * 0.04;

  ctx.fillStyle = palette.body;
  ctx.fillRect(sx + w * 0.18, sy + h * 0.36, w * 0.58, h * 0.32);
  ctx.fillRect(headX, sy + h * 0.18, w * 0.34, h * 0.38);

  ctx.fillStyle = palette.dark;
  if (kind === NpcKind.GERMAN_SHEPHERD) {
    ctx.fillRect(sx + w * 0.28, sy + h * 0.34, w * 0.34, h * 0.2);
  }
  ctx.fillRect(headX + w * 0.24, sy + h * 0.34, w * 0.14, h * 0.12);

  ctx.fillStyle = palette.accent;
  ctx.fillRect(headX + w * 0.04, sy + h * 0.06, w * 0.1, h * 0.18);
  ctx.fillRect(headX + w * 0.22, sy + h * 0.06, w * 0.1, h * 0.18);
  ctx.fillRect(tailX, sy + h * 0.3, w * 0.14, Math.max(px, h * 0.12));

  ctx.fillStyle = palette.light;
  ctx.fillRect(sx + w * 0.26, sy + h * 0.66, w * 0.1, h * 0.28);
  ctx.fillRect(sx + w * 0.58, sy + h * 0.66, w * 0.1, h * 0.28);

  ctx.fillStyle = "#111";
  ctx.fillRect(
    headX + (facingLeft ? w * 0.1 : w * 0.26),
    sy + h * 0.32,
    px,
    px,
  );
}

function drawCat(
  ctx: CanvasRenderingContext2D,
  sx: number,
  sy: number,
  w: number,
  h: number,
  px: number,
  palette: NpcPalette,
  direction: Direction,
): void {
  const facingLeft = direction === Direction.LEFT;
  const headX = facingLeft ? sx + w * 0.1 : sx + w * 0.58;
  const tailX = facingLeft ? sx + w * 0.78 : sx + w * 0.04;

  ctx.fillStyle = palette.body;
  ctx.fillRect(sx + w * 0.22, sy + h * 0.44, w * 0.48, h * 0.24);
  ctx.fillRect(headX, sy + h * 0.26, w * 0.26, h * 0.32);
  ctx.fillRect(tailX, sy + h * 0.24, w * 0.1, Math.max(px, h * 0.5));

  ctx.fillStyle = palette.dark;
  ctx.beginPath();
  ctx.moveTo(headX + w * 0.04, sy + h * 0.26);
  ctx.lineTo(headX + w * 0.1, sy + h * 0.08);
  ctx.lineTo(headX + w * 0.16, sy + h * 0.26);
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(headX + w * 0.14, sy + h * 0.26);
  ctx.lineTo(headX + w * 0.22, sy + h * 0.08);
  ctx.lineTo(headX + w * 0.28, sy + h * 0.26);
  ctx.fill();

  ctx.fillStyle = palette.accent;
  ctx.fillRect(sx + w * 0.28, sy + h * 0.64, w * 0.08, h * 0.22);
  ctx.fillRect(sx + w * 0.58, sy + h * 0.64, w * 0.08, h * 0.22);

  ctx.fillStyle = "#D8F2C8";
  ctx.fillRect(
    headX + (facingLeft ? w * 0.08 : w * 0.18),
    sy + h * 0.42,
    px,
    px,
  );
}

function drawName(
  ctx: CanvasRenderingContext2D,
  name: string,
  x: number,
  y: number,
  zoom: number,
  color = "rgba(255,255,255,0.72)",
): void {
  const fontSize = Math.max(5, 5 * zoom);
  ctx.font = `${fontSize}px monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.fillStyle = "rgba(0,0,0,0.65)";
  ctx.fillText(name, x + 1, y + 1);
  ctx.fillStyle = color;
  ctx.fillText(name, x, y);
}

function drawShadow(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  zoom: number,
): void {
  ctx.fillStyle = "rgba(0,0,0,0.28)";
  ctx.fillRect(x + w * 0.12, y, w * 0.76, Math.max(2, zoom * 2));
}

function drawBubble(
  ctx: CanvasRenderingContext2D,
  bubble: CharacterBubble | null,
  x: number,
  y: number,
  zoom: number,
): void {
  if (!bubble) return;
  const presentation = bubblePresentation(bubble);
  const fontSize = Math.max(8, 7 * zoom);
  ctx.font = `bold ${fontSize}px monospace`;
  const paddingX = 4 * zoom;
  const maxWidth = 112 * zoom;
  const measured = ctx.measureText(presentation.text).width;
  const w = Math.min(maxWidth, Math.max(16 * zoom, measured + paddingX * 2));
  const h = 13 * zoom;
  const sx = x - w / 2;
  const sy = y - h;
  const text = fitBubbleText(ctx, presentation.text, w - paddingX * 2);

  ctx.fillStyle = "#F8F8F0";
  ctx.fillRect(sx, sy, w, h);
  ctx.strokeStyle = "#222";
  ctx.strokeRect(sx, sy, w, h);
  ctx.fillStyle = "#F8F8F0";
  ctx.fillRect(x - zoom, sy + h - 1, 2 * zoom, 3 * zoom);

  ctx.fillStyle = presentation.color;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, x, sy + h / 2);
}

function bubblePresentation(bubble: CharacterBubble): {
  text: string;
  color: string;
} {
  if (typeof bubble !== "string") {
    return {
      text: bubble.text,
      color: bubbleColor(bubble.tone ?? "work"),
    };
  }
  switch (bubble) {
    case "work":
      return { text: "*", color: bubbleColor(bubble) };
    case "success":
      return { text: "OK", color: bubbleColor(bubble) };
    case "fail":
      return { text: "X", color: bubbleColor(bubble) };
    case "feedback":
      return { text: "?", color: bubbleColor(bubble) };
    case "error":
      return { text: "!", color: bubbleColor(bubble) };
  }
}

function bubbleColor(tone: CharacterBubbleKind): string {
  switch (tone) {
    case "work":
      return "#555";
    case "success":
      return "#2E7D32";
    case "fail":
    case "error":
      return "#C62828";
    case "feedback":
      return "#B26A00";
  }
}

function fitBubbleText(
  ctx: CanvasRenderingContext2D,
  text: string,
  maxWidth: number,
): string {
  if (ctx.measureText(text).width <= maxWidth) return text;
  let next = text;
  while (next.length > 1 && ctx.measureText(`${next}...`).width > maxWidth) {
    next = next.slice(0, -1);
  }
  return `${next}...`;
}
