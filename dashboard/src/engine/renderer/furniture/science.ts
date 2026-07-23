import { TILE_SIZE, type Rect } from "../../../office/layout";
import type { ResolvedTheme } from "../../../theme";
import type { RenderContext } from "../context";
import { drawFurnitureBase } from "./index";
import { CYAN_ACCENT, GREEN_ACCENT } from "./palettes";
import {
  drawGridWarTacticalMapTable,
  drawGridWarTodoWallPlane,
} from "./grid-war";
import {
  drawAssemblyBench,
  drawGridElectronicsBench,
  drawMonitorDesk,
  drawPinBoard,
  drawPrototypeBench,
  drawWhiteboard,
  drawWorkbench,
} from "./workbenches";
import {
  drawGridPrinterPodV2,
  drawMergeButtonCube,
  drawPartsBins,
  drawToolRack,
} from "./printer-and-storage";
import { drawRecoveryScrapPile } from "./recovery";
import {
  drawBookshelf,
  drawDraftingTable,
  drawEditorBookcases,
  drawEditorDesk,
  drawGridEditorPairPod,
  drawGridEditorRepoCube,
  drawPaperStack,
} from "./editor";
import {
  drawCheckInDesk,
  drawConsole,
  drawFrontCounter,
  drawGridBadgeTurnstileV2,
  drawLaunchButton,
  drawLaunchScreen,
} from "./lobby";
import {
  drawGridZenRechargeMatV2,
  drawGridZenRechargePylonClusterV2,
  drawGridZenRechargeRailV2,
  drawGridZenVendingMachineV2,
  drawSeatedBuddha,
} from "./zen";
import {
  drawRaisedBox,
  drawVerticalFaceX,
  drawWorldRect,
  projectedPoint,
  shade,
  strokeVerticalFaceX,
  strokeWorldRect,
  strokeWorldSegment,
} from "../primitives";

// Shared across the three science-lab draw functions below; "orange" here is
// a muted variant distinct from the ORANGE_ACCENT_WARM used elsewhere, so it
// stays local rather than joining the shared palette module.
const SCIENCE_LAB_ACCENT_PALETTES: Record<
  ResolvedTheme,
  { cyan: string; orange: string; green: string }
> = {
  dark: { cyan: CYAN_ACCENT.dark, orange: "#FF9146", green: GREEN_ACCENT.dark },
  light: {
    cyan: CYAN_ACCENT.light,
    orange: "#DD6518",
    green: GREEN_ACCENT.light,
  },
};

export function drawFurniturePiece(
  rctx: RenderContext,
  furniture: Rect & { name: string },
): void {
  switch (furniture.name) {
    case "War Table":
      drawGridWarTacticalMapTable(rctx, furniture);
      break;
    case "War Console":
      drawGridWarTodoWallPlane(rctx, furniture);
      break;
    case "Whiteboard":
      drawWhiteboard(rctx, furniture);
      break;
    case "Pin Board":
      drawPinBoard(rctx, furniture);
      break;
    case "Bench NW":
      drawGridElectronicsBench(rctx, furniture);
      break;
    case "Monitor Desk W":
      drawMonitorDesk(rctx, furniture);
      break;
    case "Printer Pod NE":
      drawGridPrinterPodV2(rctx, furniture);
      break;
    case "Bench SW":
      drawAssemblyBench(rctx, furniture);
      break;
    case "Bench SE":
      drawPrototypeBench(rctx, furniture);
      break;
    case "Bins E":
      drawPartsBins(rctx, furniture);
      break;
    case "Merge Button Cube":
      drawMergeButtonCube(rctx, furniture);
      break;
    case "Editor Bookcases":
      drawEditorBookcases(rctx, furniture);
      break;
    case "Drafting Table":
      drawDraftingTable(rctx, furniture);
      break;
    case "Editor Repo Cube":
      drawGridEditorRepoCube(rctx, furniture);
      break;
    case "Editor Desk":
      drawEditorDesk(rctx, furniture);
      break;
    case "Editor Shelf":
      drawBookshelf(rctx, furniture);
      break;
    case "Papers":
      drawPaperStack(rctx, furniture);
      break;
    case "Counter":
      drawFrontCounter(rctx, furniture);
      break;
    case "Check In":
      drawCheckInDesk(rctx, furniture);
      break;
    case "Recovery Scrap NW":
    case "Recovery Scrap SE":
      drawRecoveryScrapPile(rctx, furniture);
      break;
    case "Big Screen":
      drawLaunchScreen(rctx, furniture);
      break;
    case "Console":
      drawConsole(rctx, furniture);
      break;
    case "Launch Button":
      drawLaunchButton(rctx, furniture);
      break;
    case "Sand":
      drawGridZenRechargeMatV2(rctx, furniture);
      break;
    case "Garden Bench":
      drawGridZenRechargeRailV2(rctx, furniture);
      break;
    case "Stones":
      drawGridZenRechargePylonClusterV2(rctx, furniture);
      break;
    case "Seated Buddha":
      drawSeatedBuddha(rctx, furniture);
      break;
    case "Vending Machine":
      drawGridZenVendingMachineV2(rctx, furniture);
      break;
    case "Lab Bench":
      drawGridScienceLabServiceDeck(rctx, furniture);
      break;
    case "Test Rig":
      drawGridScienceLabReactorCore(rctx, furniture);
      break;
    case "Lab Shelf":
      drawGridScienceLabDiagnosticTower(rctx, furniture);
      break;
    default:
      if (furniture.name.startsWith("Badge Turnstile")) {
        drawGridBadgeTurnstileV2(rctx, furniture);
      } else if (furniture.name.startsWith("Editor Pair Pod")) {
        drawGridEditorPairPod(rctx, furniture);
      } else if (furniture.name.startsWith("Bench")) {
        drawWorkbench(rctx, furniture);
      } else if (furniture.name.startsWith("Bins")) {
        drawPartsBins(rctx, furniture);
      } else if (furniture.name === "Tools") {
        drawToolRack(rctx, furniture);
      } else {
        drawFurnitureBase(
          rctx,
          furniture,
          "#202833",
          "#2F3A47",
          "rgba(232,247,251,0.42)",
        );
      }
  }
}

function drawGridScienceLabServiceDeck(
  rctx: RenderContext,
  rect: Rect,
): void {
  const topZ = rctx.surfaceZ.value;
  const dark = rctx.theme === "dark";
  const palette = dark
    ? {
        top: "rgba(17, 70, 85, 0.94)",
        front: "rgba(6, 30, 39, 0.97)",
        left: "rgba(14, 56, 68, 0.95)",
        right: "rgba(5, 22, 31, 0.98)",
        panel: "rgba(5, 28, 38, 0.88)",
        glass: "rgba(57, 217, 255, 0.14)",
        cyan: SCIENCE_LAB_ACCENT_PALETTES.dark.cyan,
        orange: SCIENCE_LAB_ACCENT_PALETTES.dark.orange,
        green: SCIENCE_LAB_ACCENT_PALETTES.dark.green,
        shadow: "rgba(0, 0, 0, 0.18)",
      }
    : {
        top: "rgba(207, 253, 255, 0.96)",
        front: "rgba(99, 179, 202, 0.96)",
        left: "rgba(162, 239, 250, 0.96)",
        right: "rgba(70, 150, 176, 0.96)",
        panel: "rgba(225, 255, 255, 0.92)",
        glass: "rgba(0, 174, 214, 0.14)",
        cyan: SCIENCE_LAB_ACCENT_PALETTES.light.cyan,
        orange: SCIENCE_LAB_ACCENT_PALETTES.light.orange,
        green: SCIENCE_LAB_ACCENT_PALETTES.light.green,
        shadow: "rgba(0, 90, 116, 0.10)",
      };

  drawWorldRect(
    rctx,
    rect.x + 0.22,
    rect.y + rect.h + 0.18,
    rect.w - 0.44,
    0.22,
    palette.shadow,
    0.04,
  );
  drawRaisedBox(
    rctx,
    rect,
    0,
    topZ,
    palette.top,
    palette.front,
    palette.left,
    palette.right,
    palette.orange,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.42,
    rect.y + 0.32,
    rect.w - 0.84,
    rect.h - 0.64,
    palette.panel,
    topZ + 0.05,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.42,
    rect.y + 0.32,
    rect.w - 0.84,
    rect.h - 0.64,
    palette.orange,
    topZ + 0.07,
  );

  for (let x = rect.x + 1.1; x < rect.x + rect.w - 1.0; x += 1.18) {
    drawWorldRect(
      rctx,
      x,
      rect.y + 0.52,
      0.06,
      rect.h - 1.02,
      palette.cyan,
      topZ + 0.12,
    );
  }
  drawWorldRect(
    rctx,
    rect.x + 0.86,
    rect.y + 0.66,
    rect.w - 1.72,
    0.08,
    palette.cyan,
    topZ + 0.13,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.86,
    rect.y + rect.h - 0.74,
    rect.w - 1.72,
    0.08,
    palette.green,
    topZ + 0.13,
  );

  const modules = [
    { x: rect.x + 0.82, y: rect.y + 0.56, fill: palette.cyan },
    { x: rect.x + 2.02, y: rect.y + 0.58, fill: palette.orange },
    { x: rect.x + rect.w - 2.22, y: rect.y + 0.56, fill: palette.green },
    { x: rect.x + rect.w - 1.18, y: rect.y + 0.86, fill: palette.cyan },
  ];
  for (const mod of modules) {
    drawRaisedBox(
      rctx,
      { x: mod.x, y: mod.y, w: 0.48, h: 0.42 },
      topZ + 0.08,
      0.16,
      mod.fill,
      shade(mod.fill, -45),
      shade(mod.fill, -14),
      shade(mod.fill, -55),
      dark ? "rgba(218, 250, 255, 0.72)" : "rgba(0, 91, 126, 0.46)",
    );
  }

  drawVerticalFaceX(
    rctx,
    rect.x + 0.58,
    rect.y + rect.h + 0.02,
    rect.w - 1.16,
    topZ + 0.16,
    0.46,
    palette.glass,
  );
  strokeVerticalFaceX(
    rctx,
    rect.x + 0.58,
    rect.y + rect.h + 0.02,
    rect.w - 1.16,
    topZ + 0.16,
    0.46,
    palette.cyan,
  );
}

function drawGridScienceLabReactorCore(
  rctx: RenderContext,
  rect: Rect,
): void {
  const topZ = rctx.surfaceZ.value;
  const dark = rctx.theme === "dark";
  const palette = dark
    ? {
        top: "rgba(18, 73, 88, 0.96)",
        front: "rgba(6, 30, 39, 0.98)",
        left: "rgba(13, 59, 74, 0.96)",
        right: "rgba(4, 20, 30, 0.98)",
        panel: "rgba(5, 27, 38, 0.92)",
        cyan: SCIENCE_LAB_ACCENT_PALETTES.dark.cyan,
        orange: SCIENCE_LAB_ACCENT_PALETTES.dark.orange,
        core: "#FFF0A4",
        glass: "rgba(57, 217, 255, 0.16)",
      }
    : {
        top: "rgba(206, 253, 255, 0.98)",
        front: "rgba(94, 178, 202, 0.96)",
        left: "rgba(160, 239, 250, 0.96)",
        right: "rgba(68, 149, 176, 0.96)",
        panel: "rgba(225, 255, 255, 0.94)",
        cyan: SCIENCE_LAB_ACCENT_PALETTES.light.cyan,
        orange: SCIENCE_LAB_ACCENT_PALETTES.light.orange,
        core: "#F6C65A",
        glass: "rgba(0, 174, 214, 0.15)",
      };
  const centerX = rect.x + rect.w / 2;
  const centerY = rect.y + rect.h / 2;

  drawRaisedBox(
    rctx,
    rect,
    0,
    topZ,
    palette.top,
    palette.front,
    palette.left,
    palette.right,
    palette.orange,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.38,
    rect.y + 0.34,
    rect.w - 0.76,
    rect.h - 0.68,
    palette.panel,
    topZ + 0.04,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.38,
    rect.y + 0.34,
    rect.w - 0.76,
    rect.h - 0.68,
    palette.cyan,
    topZ + 0.07,
  );

  const arms = [
    { x: rect.x + 0.72, y: rect.y + 0.62, color: palette.cyan },
    { x: rect.x + rect.w - 0.72, y: rect.y + 0.62, color: palette.orange },
    { x: rect.x + 0.95, y: rect.y + rect.h - 0.66, color: palette.orange },
    {
      x: rect.x + rect.w - 0.98,
      y: rect.y + rect.h - 0.66,
      color: palette.cyan,
    },
  ];
  for (const arm of arms) {
    strokeWorldSegment(
      rctx,
      arm.x,
      arm.y,
      topZ + 0.42,
      centerX,
      centerY,
      topZ + 1.24,
      arm.color,
      2,
    );
    drawRaisedBox(
      rctx,
      { x: arm.x - 0.16, y: arm.y - 0.12, w: 0.32, h: 0.28 },
      topZ + 0.14,
      0.18,
      arm.color,
      shade(arm.color, -44),
      shade(arm.color, -12),
      shade(arm.color, -52),
      arm.color,
    );
  }

  drawRaisedBox(
    rctx,
    { x: centerX - 0.52, y: centerY - 0.42, w: 1.04, h: 0.84 },
    topZ + 0.06,
    0.88,
    palette.glass,
    "rgba(4, 18, 26, 0.72)",
    palette.glass,
    "rgba(3, 14, 22, 0.72)",
    palette.cyan,
  );
  drawWorldRect(
    rctx,
    centerX - 0.2,
    centerY - 0.16,
    0.4,
    0.32,
    palette.core,
    topZ + 1.02,
  );
  drawReactorGlow(
    rctx,
    centerX,
    centerY,
    topZ + 1.18,
    dark ? "rgba(255, 145, 70, 0.22)" : "rgba(221, 101, 24, 0.18)",
  );
  drawReactorRing(
    rctx,
    centerX,
    centerY,
    topZ + 1.32,
    1.86,
    0.72,
    palette.orange,
    3,
  );
  drawReactorRing(
    rctx,
    centerX,
    centerY,
    topZ + 1.64,
    1.28,
    0.48,
    palette.cyan,
    2,
  );
}

function drawGridScienceLabDiagnosticTower(
  rctx: RenderContext,
  rect: Rect,
): void {
  const topZ = rctx.surfaceZ.value;
  const dark = rctx.theme === "dark";
  const palette = dark
    ? {
        top: "rgba(13, 48, 64, 0.96)",
        front: "rgba(5, 22, 32, 0.98)",
        left: "rgba(10, 42, 56, 0.96)",
        right: "rgba(4, 17, 26, 0.98)",
        screen: "rgba(57, 217, 255, 0.18)",
        cyan: SCIENCE_LAB_ACCENT_PALETTES.dark.cyan,
        orange: SCIENCE_LAB_ACCENT_PALETTES.dark.orange,
        green: SCIENCE_LAB_ACCENT_PALETTES.dark.green,
      }
    : {
        top: "rgba(210, 249, 255, 0.98)",
        front: "rgba(105, 190, 210, 0.96)",
        left: "rgba(171, 242, 250, 0.96)",
        right: "rgba(77, 158, 181, 0.96)",
        screen: "rgba(0, 174, 214, 0.13)",
        cyan: SCIENCE_LAB_ACCENT_PALETTES.light.cyan,
        orange: SCIENCE_LAB_ACCENT_PALETTES.light.orange,
        green: SCIENCE_LAB_ACCENT_PALETTES.light.green,
      };

  drawRaisedBox(
    rctx,
    rect,
    0,
    topZ,
    palette.top,
    palette.front,
    palette.left,
    palette.right,
    palette.cyan,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.2,
    rect.y + rect.h + 0.02,
    rect.w - 0.4,
    0.74,
    topZ - 0.3,
    palette.screen,
  );
  strokeVerticalFaceX(
    rctx,
    rect.x + 0.2,
    rect.y + rect.h + 0.02,
    rect.w - 0.4,
    0.74,
    topZ - 0.3,
    palette.cyan,
  );

  for (let row = 0; row < 5; row += 1) {
    const z = 1.12 + row * 0.72;
    const fill =
      row % 3 === 0
        ? palette.cyan
        : row % 3 === 1
          ? palette.orange
          : palette.green;
    drawVerticalFaceX(
      rctx,
      rect.x + 0.42,
      rect.y + rect.h + 0.04,
      rect.w - 0.84,
      z,
      0.1,
      fill,
    );
  }
  drawWorldRect(
    rctx,
    rect.x + 0.36,
    rect.y + 0.42,
    rect.w - 0.72,
    0.18,
    palette.orange,
    topZ + 0.08,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.55,
    rect.y + 0.86,
    rect.w - 1.1,
    0.16,
    palette.green,
    topZ + 0.1,
  );
}

function drawReactorRing(
  rctx: RenderContext,
  x: number,
  y: number,
  z: number,
  radiusXUnits: number,
  radiusYUnits: number,
  stroke: string,
  width: number,
): void {
  const center = projectedPoint(rctx, x, y, z);
  const ctx = rctx.ctx;
  ctx.save();
  ctx.strokeStyle = stroke;
  ctx.lineWidth = width;
  ctx.beginPath();
  ctx.ellipse(
    center.x,
    center.y,
    radiusXUnits * TILE_SIZE * rctx.camera.zoom,
    radiusYUnits * TILE_SIZE * rctx.camera.zoom * 0.68,
    0,
    0,
    Math.PI * 2,
  );
  ctx.stroke();
  ctx.restore();
}

function drawReactorGlow(
  rctx: RenderContext,
  x: number,
  y: number,
  z: number,
  fill: string,
): void {
  const center = projectedPoint(rctx, x, y, z);
  const ctx = rctx.ctx;
  const radius = 28 * rctx.camera.zoom;
  const glow = ctx.createRadialGradient(
    center.x,
    center.y,
    0,
    center.x,
    center.y,
    radius,
  );
  glow.addColorStop(0, fill);
  glow.addColorStop(1, "rgba(255, 145, 70, 0)");
  ctx.save();
  ctx.fillStyle = glow;
  ctx.beginPath();
  ctx.arc(center.x, center.y, radius, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}
