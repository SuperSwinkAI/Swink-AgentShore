// Suppress React's "act environment not configured" warning when using
// react-dom/client directly in vitest + jsdom.
// See: https://github.com/reactwg/react-18/discussions/102
(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

// jsdom does not implement HTMLCanvasElement.getContext(). Stub it out so tests
// that render canvas-backed components (e.g. charts) don't emit noisy warnings.
HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;

// jsdom v27 requires --localstorage-file to be set for localStorage to work.
// Provide a minimal in-memory shim so components that call localStorage API work in tests.
const _localStorageStore: Record<string, string> = {};
const localStorageMock: Storage = {
  getItem: (key: string) => _localStorageStore[key] ?? null,
  setItem: (key: string, value: string) => {
    _localStorageStore[key] = value;
  },
  removeItem: (key: string) => {
    delete _localStorageStore[key];
  },
  clear: () => {
    for (const key of Object.keys(_localStorageStore)) {
      delete _localStorageStore[key];
    }
  },
  get length() {
    return Object.keys(_localStorageStore).length;
  },
  key: (index: number) => Object.keys(_localStorageStore)[index] ?? null,
};
Object.defineProperty(globalThis, "localStorage", {
  value: localStorageMock,
  writable: true,
});

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string): MediaQueryList => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }),
});
