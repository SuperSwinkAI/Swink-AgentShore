import {
  KANBAN_WALL_PALETTES,
  MAP_PALETTES,
  type KanbanWallPalette,
  type MapVisualPalette,
} from "../../office/palette";
import type { Tile } from "../../office/layout";
import type { ResolvedTheme } from "../../theme";
import type { Character } from "../../characters/types";
import type { Camera } from "../camera";
import { renderCharacterBubbles, renderCharacters } from "./characters";
import type {
  RenderContext,
  SceneRenderable,
  SurfaceZHolder,
  WallSticky,
} from "./context";
import { renderFloor, renderFloorDecorations } from "./floors";
import { renderFurniture } from "./furniture";
import { screenToTile } from "./hit-test";
import { renderWallDecorations } from "./wall-art";
import { renderFloorMarkers, renderWalls } from "./walls";

export class OfficeRenderer {
  private surfaceZ: SurfaceZHolder = { value: 0 };
  private wallStickies: WallSticky[] = [];
  private palette: MapVisualPalette = MAP_PALETTES["light"];
  private kanbanPalette: KanbanWallPalette = KANBAN_WALL_PALETTES["light"];
  private theme: ResolvedTheme = "light";

  constructor(
    private canvas: HTMLCanvasElement,
    private ctx: CanvasRenderingContext2D,
    private camera: Camera,
  ) {}

  setWallStickies(stickies: WallSticky[]): void {
    this.wallStickies = stickies;
  }

  setTheme(theme: ResolvedTheme): void {
    this.theme = theme;
    this.palette = MAP_PALETTES[theme];
    this.kanbanPalette = KANBAN_WALL_PALETTES[theme];
  }

  render(characters: Character[]): void {
    const ctx = this.ctx;
    ctx.imageSmoothingEnabled = false;

    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);

    const rctx = this.buildContext();

    renderFloor(rctx);
    renderFloorDecorations(rctx);
    renderFloorMarkers(rctx);

    const renderables: SceneRenderable[] = [
      ...renderWalls(rctx),
      ...renderFurniture(rctx),
      ...renderWallDecorations(rctx),
      ...renderCharacters(rctx, characters),
    ];

    renderables.sort((a, b) => a.depth - b.depth);
    for (const renderable of renderables) {
      renderable.draw();
    }

    const bubbleRenderables = renderCharacterBubbles(rctx, characters);
    bubbleRenderables.sort((a, b) => a.depth - b.depth);
    for (const renderable of bubbleRenderables) {
      renderable.draw();
    }
  }

  screenToTile(screenX: number, screenY: number): Tile | null {
    return screenToTile(this.camera, screenX, screenY);
  }

  private buildContext(): RenderContext {
    return {
      canvas: this.canvas,
      ctx: this.ctx,
      camera: this.camera,
      palette: this.palette,
      kanbanPalette: this.kanbanPalette,
      theme: this.theme,
      wallStickies: this.wallStickies,
      surfaceZ: this.surfaceZ,
    };
  }
}
