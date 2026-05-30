export type UpdateFn = (dt: number) => void;
export type RenderFn = () => void;

const MAX_DELTA = 0.1;

export class GameLoop {
  private lastTime = 0;
  private running = false;
  private rafId = 0;

  constructor(
    private update: UpdateFn,
    private render: RenderFn,
  ) {}

  start(): void {
    if (this.running) return;
    this.running = true;
    this.lastTime = performance.now();
    this.rafId = requestAnimationFrame(this.tick);
  }

  stop(): void {
    this.running = false;
    cancelAnimationFrame(this.rafId);
  }

  private tick = (time: number): void => {
    const dt = Math.min((time - this.lastTime) / 1000, MAX_DELTA);
    this.lastTime = time;
    this.update(dt);
    this.render();
    if (this.running) {
      this.rafId = requestAnimationFrame(this.tick);
    }
  };
}
