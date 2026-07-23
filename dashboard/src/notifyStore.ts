import { useSyncExternalStore } from "react";

/**
 * Generic module-level pub-sub store, replacing the `listeners = new Set()` +
 * `latestState` + `broadcast()` boilerplate hand-rolled per component.
 * `notify()` is the external write side (called from Dashboard.tsx's message
 * router or the component's own handlers); `use()` is the React read side,
 * built on `useSyncExternalStore` so every subscriber sees the same snapshot
 * with no tearing between mount and the first live update.
 */
export interface NotifyStore<T> {
  /** Publish a new value to every subscriber (including future mounts). */
  notify(value: T): void;
  /** Read the current value outside of React (e.g. from another store). */
  get(): T;
  /** Subscribe to the current value; re-renders on every notify(). */
  use(): T;
  /** Low-level subscribe, for stores that need to layer extra behavior on top. */
  subscribe(onStoreChange: () => void): () => void;
  /**
   * Test-only escape hatch: set state without notifying, and drop every
   * current subscriber. Mirrors the `listeners.clear()` some hand-rolled
   * stores did in their `resetXForTests()` helper as a belt-and-suspenders
   * cleanup between test cases (real unmounts already unsubscribe).
   */
  resetForTests(value: T): void;
}

export function createNotifyStore<T>(initial: T): NotifyStore<T> {
  let state = initial;
  let listeners = new Set<() => void>();

  function notify(value: T): void {
    state = value;
    listeners.forEach((fn) => fn());
  }

  function get(): T {
    return state;
  }

  function subscribe(onStoreChange: () => void): () => void {
    listeners.add(onStoreChange);
    return () => {
      listeners.delete(onStoreChange);
    };
  }

  function use(): T {
    return useSyncExternalStore(subscribe, get);
  }

  function resetForTests(value: T): void {
    state = value;
    listeners = new Set();
  }

  return { notify, get, use, subscribe, resetForTests };
}

/**
 * Reducer variant of {@link createNotifyStore} for the components that were
 * dispatching actions into a shared reducer instead of replacing a whole
 * value (EventDrawer, SidePanel, PlaysPanel). One canonical state lives in
 * the store rather than being recomputed per-mount, so a component that
 * mounts after other dispatches have already fired reads the up-to-date
 * state instead of racing a "hydrate" bootstrap action.
 */
export interface ActionStore<S, A> {
  dispatch(action: A): void;
  get(): S;
  use(): S;
  subscribe(onStoreChange: () => void): () => void;
}

export function createActionStore<S, A>(
  reducer: (state: S, action: A) => S,
  initial: S,
): ActionStore<S, A> {
  const store = createNotifyStore(initial);

  function dispatch(action: A): void {
    store.notify(reducer(store.get(), action));
  }

  return { dispatch, get: store.get, use: store.use, subscribe: store.subscribe };
}
