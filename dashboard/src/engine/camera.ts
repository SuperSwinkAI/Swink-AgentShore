import {
  TILE_SIZE,
  type ProjectionMode,
  projectWorld,
  projectedMapBounds,
  unprojectWorld,
} from "../office/layout";

const MAX_ZOOM = 6;
const DEFAULT_ZOOM = 1;
const MIN_ZOOM = 0.35;
const FOLLOW_LERP = 0.08;
const WHEEL_ZOOM_SENSITIVITY = 0.0008;

export interface CameraViewport {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export class Camera {
  x = 0;
  y = 0;
  zoom = DEFAULT_ZOOM;

  private targetX = 0;
  private targetY = 0;
  private minimumZoom = MIN_ZOOM;
  private followTarget: { x: number; y: number } | null = null;
  private projectionMode: ProjectionMode = "grid";

  private dragging = false;
  private dragStartX = 0;
  private dragStartY = 0;
  private camStartX = 0;
  private camStartY = 0;
  private movedDuringDrag = false;

  centerOn(canvasWidth: number, canvasHeight: number): void {
    const bounds = projectedMapBounds(this.projectionMode);
    const mapW = (bounds.right - bounds.left) * this.zoom;
    const mapH = (bounds.bottom - bounds.top) * this.zoom;
    this.x = (canvasWidth - mapW) / 2 - bounds.left * this.zoom;
    this.y = (canvasHeight - mapH) / 2 - bounds.top * this.zoom;
    this.targetX = this.x;
    this.targetY = this.y;
  }

  fitBoundsToViewport(
    bounds: { left: number; top: number; right: number; bottom: number },
    viewport: CameraViewport,
    padding = 0,
  ): void {
    this.followTarget = null;
    const viewportW = Math.max(TILE_SIZE, viewport.right - viewport.left);
    const viewportH = Math.max(TILE_SIZE, viewport.bottom - viewport.top);
    const availableW = Math.max(TILE_SIZE, viewportW - padding * 2);
    const availableH = Math.max(TILE_SIZE, viewportH - padding * 2);
    const worldW = bounds.right - bounds.left;
    const worldH = bounds.bottom - bounds.top;
    const fitX = availableW / worldW;
    const fitY = availableH / worldH;
    this.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, fitX, fitY));
    this.minimumZoom = this.zoom;
    const scaledW = worldW * this.zoom;
    const scaledH = worldH * this.zoom;
    this.x =
      viewport.left +
      padding +
      (availableW - scaledW) / 2 -
      bounds.left * this.zoom;
    this.y =
      viewport.top +
      padding +
      (availableH - scaledH) / 2 -
      bounds.top * this.zoom;
    this.targetX = this.x;
    this.targetY = this.y;
  }

  fitToViewport(viewport: CameraViewport, padding = 0): void {
    this.followTarget = null;
    const viewportW = Math.max(TILE_SIZE, viewport.right - viewport.left);
    const viewportH = Math.max(TILE_SIZE, viewport.bottom - viewport.top);
    const availableW = Math.max(TILE_SIZE, viewportW - padding * 2);
    const availableH = Math.max(TILE_SIZE, viewportH - padding * 2);
    const bounds = projectedMapBounds(this.projectionMode);
    const mapWorldW = bounds.right - bounds.left;
    const mapWorldH = bounds.bottom - bounds.top;
    const fitX = availableW / mapWorldW;
    const fitY = availableH / mapWorldH;
    this.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, fitX, fitY));
    this.minimumZoom = this.zoom;

    const mapW = mapWorldW * this.zoom;
    const mapH = mapWorldH * this.zoom;
    this.x =
      viewport.left +
      padding +
      (availableW - mapW) / 2 -
      bounds.left * this.zoom;
    this.y =
      viewport.top + padding + (availableH - mapH) / 2 - bounds.top * this.zoom;
    this.targetX = this.x;
    this.targetY = this.y;
  }

  reset(
    canvasWidth: number,
    canvasHeight: number,
    viewport?: CameraViewport,
  ): void {
    this.followTarget = null;
    if (viewport) {
      this.fitToViewport(viewport);
      return;
    }
    this.zoom = DEFAULT_ZOOM;
    this.minimumZoom = this.minZoom(canvasWidth, canvasHeight);
    this.centerOn(canvasWidth, canvasHeight);
  }

  minZoom(canvasWidth: number, canvasHeight: number): number {
    const bounds = projectedMapBounds(this.projectionMode);
    const fitX = canvasWidth / (bounds.right - bounds.left);
    const fitY = canvasHeight / (bounds.bottom - bounds.top);
    return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, fitX, fitY));
  }

  setZoom(z: number, canvasWidth: number, canvasHeight: number): void {
    this.setZoomAt(z, canvasWidth / 2, canvasHeight / 2);
  }

  setZoomAt(z: number, cx: number, cy: number): void {
    const oldZoom = this.zoom;
    this.zoom = Math.max(this.minimumZoom, Math.min(MAX_ZOOM, z));
    if (this.zoom === oldZoom) return;
    const worldX = (cx - this.x) / oldZoom;
    const worldY = (cy - this.y) / oldZoom;
    this.x = cx - worldX * this.zoom;
    this.y = cy - worldY * this.zoom;
    this.targetX = this.x;
    this.targetY = this.y;
  }

  follow(target: { x: number; y: number } | null): void {
    this.followTarget = target;
  }

  focusOn(target: { x: number; y: number }, zoomMultiplier = 2.4): void {
    this.followTarget = target;
    this.zoom = Math.max(
      this.minimumZoom,
      Math.min(MAX_ZOOM, this.minimumZoom * zoomMultiplier),
    );
  }

  panBy(dx: number, dy: number): void {
    this.followTarget = null;
    this.x += dx;
    this.y += dy;
    this.targetX = this.x;
    this.targetY = this.y;
  }

  update(dt: number, canvasWidth: number, canvasHeight: number): void {
    if (this.followTarget && !this.dragging) {
      const projected = projectWorld(
        this.followTarget.x,
        this.followTarget.y,
        0,
        this.projectionMode,
      );
      this.targetX = canvasWidth / 2 - projected.x * this.zoom;
      this.targetY = canvasHeight / 2 - projected.y * this.zoom;
    }

    const lerp = 1 - Math.pow(1 - FOLLOW_LERP, dt * 60);
    this.x += (this.targetX - this.x) * lerp;
    this.y += (this.targetY - this.y) * lerp;
  }

  worldToScreen(wx: number, wy: number, zUnits = 0): [number, number] {
    const projected = projectWorld(wx, wy, zUnits, this.projectionMode);
    return [projected.x * this.zoom + this.x, projected.y * this.zoom + this.y];
  }

  screenToWorld(sx: number, sy: number): [number, number] {
    const projectedX = (sx - this.x) / this.zoom;
    const projectedY = (sy - this.y) / this.zoom;
    const world = unprojectWorld(projectedX, projectedY, this.projectionMode);
    return [world.x, world.y];
  }

  setProjectionMode(mode: ProjectionMode): boolean {
    if (this.projectionMode === mode) return false;
    this.projectionMode = mode;
    return true;
  }

  attachInputHandlers(el: HTMLElement): void {
    el.addEventListener("mousedown", (e: MouseEvent) => {
      this.dragging = true;
      this.dragStartX = e.clientX * devicePixelRatio;
      this.dragStartY = e.clientY * devicePixelRatio;
      this.camStartX = this.x;
      this.camStartY = this.y;
      this.followTarget = null;
      this.movedDuringDrag = false;
    });

    window.addEventListener("mousemove", (e) => {
      if (!this.dragging) return;
      const dx = e.clientX * devicePixelRatio - this.dragStartX;
      const dy = e.clientY * devicePixelRatio - this.dragStartY;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
        this.movedDuringDrag = true;
      }
      this.x = this.camStartX + dx;
      this.y = this.camStartY + dy;
      this.targetX = this.x;
      this.targetY = this.y;
    });

    window.addEventListener("mouseup", () => {
      this.dragging = false;
    });

    el.addEventListener(
      "wheel",
      (e: WheelEvent) => {
        e.preventDefault();
        const rect = el.getBoundingClientRect();
        const cx = (e.clientX - rect.left) * devicePixelRatio;
        const cy = (e.clientY - rect.top) * devicePixelRatio;
        const factor = Math.exp(-e.deltaY * WHEEL_ZOOM_SENSITIVITY);
        this.setZoomAt(this.zoom * factor, cx, cy);
      },
      { passive: false },
    );
  }

  wasDragging(): boolean {
    return this.movedDuringDrag;
  }
}
