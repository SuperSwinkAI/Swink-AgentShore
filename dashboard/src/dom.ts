type HTMLElementConstructor<T extends HTMLElement> = {
  new (): T;
  readonly name: string;
};

export function requireElement(id: string): HTMLElement;
export function requireElement<T extends HTMLElement>(
  id: string,
  ctor: HTMLElementConstructor<T>,
): T;
export function requireElement<T extends HTMLElement>(
  id: string,
  ctor?: HTMLElementConstructor<T>,
): HTMLElement | T {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Required DOM element #${id} not found`);
  }
  if (ctor && !(element instanceof ctor)) {
    throw new Error(`Element #${id} is not a ${ctor.name}`);
  }
  return element;
}

export function requireCanvas2DContext(
  canvas: HTMLCanvasElement,
): CanvasRenderingContext2D {
  const context = canvas.getContext("2d");
  if (!context) {
    const id = canvas.id ? `#${canvas.id}` : "<canvas>";
    throw new Error(`Could not acquire 2D rendering context for ${id}`);
  }
  return context;
}
