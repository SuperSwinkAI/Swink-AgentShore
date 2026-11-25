import "@testing-library/jest-dom/vitest";













function makeStorageShim(): Storage {
  let store = new Map<string, string>();
  const shim: Storage = {
    get length() {
      return store.size;
    },
    clear: () => {
      store = new Map();
    },
    getItem: (key: string): string | null => {
      return store.has(key) ? (store.get(key) as string) : null;
    },
    key: (index: number): string | null => {
      const keys = Array.from(store.keys());
      return index >= 0 && index < keys.length ? keys[index] : null;
    },
    removeItem: (key: string): void => {
      store.delete(key);
    },
    setItem: (key: string, value: string): void => {
      store.set(key, String(value));
    },
  };
  return shim;
}

if (typeof globalThis.localStorage === "undefined" ||
    typeof globalThis.localStorage.setItem !== "function") {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    writable: true,
    value: makeStorageShim(),
  });
}
if (typeof globalThis.sessionStorage === "undefined" ||
    typeof globalThis.sessionStorage.setItem !== "function") {
  Object.defineProperty(globalThis, "sessionStorage", {
    configurable: true,
    writable: true,
    value: makeStorageShim(),
  });
}


if (typeof window !== "undefined") {
  if (typeof window.localStorage?.setItem !== "function") {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      writable: true,
      value: globalThis.localStorage,
    });
  }
  if (typeof window.sessionStorage?.setItem !== "function") {
    Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      writable: true,
      value: globalThis.sessionStorage,
    });
  }
}
