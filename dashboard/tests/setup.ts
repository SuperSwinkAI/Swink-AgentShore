


(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;



HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;



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
