import { useEffect, useRef, type RefObject } from "react";

import type { Camera } from "../../engine/camera";

const DEFAULT_VIEW_PADDING = 14;

interface ViewportBounds {
  top: number;
  left: number;
  right: number;
  bottom: number;
}

/**
 * Keeps the canvas backing store sized to its displayed CSS size (HiDPI-
 * crisp) and the camera fit to the space between the surrounding HUD
 * panels. Reacts to window resizes, panel layout changes (ResizeObserver
 * on the canvas), and DPR changes that don't fire a `resize` event (e.g.
 * dragging the window across monitors).
 *
 * Returns a ref holding the current `fitToView` callback so sibling hooks
 * (click-select, keyboard-pan) can re-fit the camera on deselect without
 * re-deriving the viewport-measurement logic.
 */
export function useCanvasResize(
  canvasRef: RefObject<HTMLCanvasElement | null>,
  cameraRef: RefObject<Camera | null>,
): RefObject<() => void> {
  const fitToViewRef = useRef<() => void>(() => {});

  useEffect(() => {
    const canvas = canvasRef.current;
    const camera = cameraRef.current;
    if (!canvas || !camera) return;

    // Size backing-store to displayed size for crisp pixel art on HiDPI (else
    // canvas defaults to 300x150). dpr is re-read in resize() (not frozen at
    // mount) since it changes on cross-monitor moves; a stale value mis-sizes
    // the backing store and blurs the upscale.
    let dpr = window.devicePixelRatio || 1;

    const measureViewport = (): ViewportBounds => {
      // Physical-pixel bounds the office should fit within, measured from the
      // surrounding HUD panels; falls back to full window for panels not yet
      // mounted (e.g. first paint before the HUD subtree settles).
      const topBar = document.getElementById("top-bar");
      const bottomBar = document.getElementById("bottom-bar");
      const leftPanel = document.getElementById("left-panel");
      const sidePanel = document.getElementById("side-panel");
      const winW = window.innerWidth;
      const winH = window.innerHeight;
      const leftRect = leftPanel?.getBoundingClientRect();
      const sideRect = sidePanel?.getBoundingClientRect();
      const topRect = topBar?.getBoundingClientRect();
      const bottomRect = bottomBar?.getBoundingClientRect();
      const leftCss =
        leftRect && !leftPanel?.classList.contains("collapsed") && leftRect.width > 0
          ? leftRect.right
          : 0;
      const rightCss = sideRect && sideRect.width > 0 ? sideRect.left : winW;
      const topCss = topRect ? topRect.bottom : 0;
      const bottomCss = bottomRect ? bottomRect.top : winH;
      return {
        left: leftCss * dpr,
        top: topCss * dpr,
        right: rightCss * dpr,
        bottom: bottomCss * dpr,
      };
    };

    const fitToView = () => {
      camera.fitToViewport(measureViewport(), DEFAULT_VIEW_PADDING * dpr);
    };
    fitToViewRef.current = fitToView;

    const resize = () => {
      // Backing-store and all camera math operate in PHYSICAL pixels; no
      // setTransform. Scaling the context by DPR would double every camera
      // offset on HiDPI and paint the office off-center bottom-right.
      dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
      fitToView();
    };
    resize();
    // Defer a second fit a frame so HUD panels mount before we measure them;
    // else the first resize misses #top-bar/#side-panel, falls back to
    // full-window bounds, and paints too wide on initial load.
    const initialFitRaf = requestAnimationFrame(fitToView);

    const ro = new ResizeObserver(resize);
    ro.observe(canvas);
    const onWindowResize = () => fitToView();
    window.addEventListener("resize", onWindowResize);

    // Cross-monitor moves don't reliably fire `resize`, so watch dpr directly.
    // The query targets the current dppx, so rebuild and re-arm it after each
    // change; resize() then re-reads dpr and re-sizes the backing store.
    let dprQuery: MediaQueryList | null = null;
    const onDprChange = () => {
      resize();
      armDprListener();
    };
    const armDprListener = () => {
      dprQuery?.removeEventListener("change", onDprChange);
      dprQuery = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
      dprQuery.addEventListener("change", onDprChange);
    };
    armDprListener();

    return () => {
      ro.disconnect();
      cancelAnimationFrame(initialFitRaf);
      window.removeEventListener("resize", onWindowResize);
      dprQuery?.removeEventListener("change", onDprChange);
    };
    // canvasRef/cameraRef are stable ref objects populated by the parent's
    // mount effect before this one runs; this effect intentionally runs once
    // at mount, matching the original monolithic effect's `[]` deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return fitToViewRef;
}
