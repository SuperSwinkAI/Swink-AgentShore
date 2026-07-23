import type { Rect } from "../../../office/layout";
import type { RenderContext } from "../context";
import { drawFurnitureBase } from "./index";
import { CYAN_ACCENT, WORKBENCH_CASE_PALETTES } from "./palettes";
import {
  drawRaisedBox,
  drawVerticalFaceX,
  drawVerticalFaceY,
  drawWorldRect,
  shade,
  strokeVerticalFaceX,
  strokeVerticalFaceY,
  strokeWorldRect,
} from "../primitives";

export function drawGridPrinterPodV2(rctx: RenderContext, rect: Rect): void {
  const topZ = rctx.surfaceZ.value;
  const dark = rctx.theme === "dark";
  const palette = dark
    ? {
        top: "#16495C",
        front: "#0F3140",
        left: "#1B6176",
        right: "#04151C",
        deck: "rgba(125, 230, 255, 0.32)",
        printerTop: "#16495C",
        printerFront: "#0F3140",
        printerLeft: "#1B6176",
        printerRight: "#04151C",
        amsTop: "#1D6375",
        amsFront: "#0A2330",
        amsLeft: "#2B7B8E",
        amsRight: "#061925",
        glass: "rgba(125, 230, 255, 0.32)",
        glassEdge: "rgba(57, 217, 255, 0.88)",
        glassHighlight: "rgba(233, 255, 255, 0.48)",
        cyan: CYAN_ACCENT.dark,
        orange: "#FF9146",
        green: "#2BE0B1",
        yellow: "#F4D44D",
        violet: "#B880FF",
        panelLine: "#081D27",
        rail: "#39D9FF",
        shadow: "rgba(0, 0, 0, 0.22)",
      }
    : {
        top: "#DDFBFF",
        front: "#72D0E4",
        left: "#BBF6FF",
        right: "#4AA7BF",
        deck: "rgba(244, 255, 255, 0.72)",
        printerTop: "#7DE6FF",
        printerFront: "#168AA5",
        printerLeft: "#B6F5FF",
        printerRight: "#0F6F88",
        amsTop: "#C8F7FF",
        amsFront: "#58C1D4",
        amsLeft: "#D9FDFF",
        amsRight: "#2D93AA",
        glass: "rgba(125, 230, 255, 0.32)",
        glassEdge: "rgba(0, 139, 188, 0.68)",
        glassHighlight: "rgba(255, 255, 255, 0.62)",
        cyan: CYAN_ACCENT.light,
        orange: "#FF9146",
        green: "#2BE0B1",
        yellow: "#F4D44D",
        violet: "#7B42D9",
        panelLine: "#0F5368",
        rail: "#39D9FF",
        shadow: "rgba(0, 86, 112, 0.12)",
      };

  drawWorldRect(
    rctx,
    rect.x + 0.24,
    rect.y + rect.h + 0.18,
    rect.w - 0.48,
    0.24,
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
    palette.cyan,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.36,
    rect.y + 0.34,
    rect.w - 0.72,
    rect.h - 0.68,
    palette.deck,
    topZ + 0.05,
  );
  strokeWorldRect(
    rctx,
    rect.x + 0.36,
    rect.y + 0.34,
    rect.w - 0.72,
    rect.h - 0.68,
    palette.glassEdge,
    topZ + 0.07,
  );

  drawWorldRect(
    rctx,
    rect.x + 0.36,
    rect.y + 0.45,
    0.18,
    rect.h - 0.9,
    palette.rail,
    topZ + 0.1,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.68,
    rect.y + 0.75,
    0.14,
    rect.h - 1.5,
    palette.panelLine,
    topZ + 0.11,
  );
  drawWorldRect(
    rctx,
    rect.x + rect.w - 0.72,
    rect.y + 0.58,
    0.12,
    rect.h - 1.16,
    palette.orange,
    topZ + 0.12,
  );

  const printerBaseZ = topZ;
  const printerHeight = 2.45;
  const printers = [
    { y: rect.y + 0.65, filament: palette.orange },
    { y: rect.y + 3.35, filament: palette.cyan },
    { y: rect.y + 6.05, filament: palette.green },
  ];

  for (const printer of printers) {
    const printerRect = { x: rect.x + 1.75, y: printer.y, w: 2.5, h: 2.1 };
    drawRaisedBox(
      rctx,
      printerRect,
      printerBaseZ,
      printerHeight,
      palette.printerTop,
      palette.printerFront,
      palette.printerLeft,
      palette.printerRight,
      palette.glassEdge,
    );
    drawVerticalFaceY(
      rctx,
      printerRect.x - 0.01,
      printerRect.y + 0.22,
      printerRect.h - 0.44,
      printerBaseZ + 0.35,
      printerHeight - 0.7,
      palette.glass,
    );
    strokeVerticalFaceY(
      rctx,
      printerRect.x - 0.02,
      printerRect.y + 0.36,
      printerRect.h - 0.72,
      printerBaseZ + 0.55,
      printerHeight - 1.1,
      palette.glassEdge,
    );
    drawVerticalFaceY(
      rctx,
      printerRect.x - 0.03,
      printerRect.y + printerRect.h - 0.5,
      0.16,
      printerBaseZ + 0.82,
      0.48,
      palette.yellow,
    );
    drawVerticalFaceY(
      rctx,
      printerRect.x - 0.04,
      printerRect.y + 0.55,
      printerRect.h - 1.1,
      printerBaseZ + 0.55,
      0.16,
      palette.panelLine,
    );
    drawVerticalFaceY(
      rctx,
      printerRect.x - 0.05,
      printerRect.y + 0.78,
      0.42,
      printerBaseZ + 1.45,
      0.18,
      printer.filament,
    );
    drawVerticalFaceY(
      rctx,
      printerRect.x - 0.06,
      printerRect.y + 0.46,
      0.28,
      printerBaseZ + 1.82,
      0.24,
      palette.glassHighlight,
    );

    const ams = {
      x: printerRect.x + 0.22,
      y: printerRect.y + 0.12,
      w: printerRect.w - 0.44,
      h: printerRect.h - 0.24,
    };
    drawRaisedBox(
      rctx,
      ams,
      printerBaseZ + printerHeight,
      0.55,
      palette.amsTop,
      palette.amsFront,
      palette.amsLeft,
      palette.amsRight,
      palette.glassEdge,
    );
    const spoolZ = printerBaseZ + printerHeight + 0.56;
    drawMiniSpool(rctx, ams.x + 0.2, ams.y + 0.25, printer.filament, spoolZ);
    drawMiniSpool(rctx, ams.x + 0.2, ams.y + 0.82, palette.yellow, spoolZ);
    drawMiniSpool(rctx, ams.x + 0.2, ams.y + 1.39, palette.violet, spoolZ);
  }

  drawFlatSpool(rctx, rect.x + 0.35, rect.y + 0.95, palette.green, topZ + 0.04);
  drawFlatSpool(
    rctx,
    rect.x + 0.42,
    rect.y + 2.85,
    palette.yellow,
    topZ + 0.05,
  );
  drawFlatSpool(
    rctx,
    rect.x + 0.32,
    rect.y + 4.95,
    palette.orange,
    topZ + 0.06,
  );
  drawFlatSpool(rctx, rect.x + 0.48, rect.y + 7.0, palette.cyan, topZ + 0.07);
  drawWorldRect(
    rctx,
    rect.x + 1.12,
    rect.y + rect.h - 0.64,
    rect.w - 2.24,
    0.18,
    palette.yellow,
    topZ + 0.18,
  );
}

function drawFlatSpool(
  rctx: RenderContext,
  x: number,
  y: number,
  filamentColor: string,
  zUnits: number,
): void {
  drawWorldRect(rctx, x, y, 1.28, 0.92, "#CDAF7B", zUnits);
  drawWorldRect(
    rctx,
    x + 0.15,
    y + 0.16,
    0.98,
    0.6,
    filamentColor,
    zUnits + 0.02,
  );
  drawWorldRect(
    rctx,
    x + 0.47,
    y + 0.3,
    0.34,
    0.28,
    "#20252A",
    zUnits + 0.03,
  );
  drawWorldRect(
    rctx,
    x + 0.16,
    y + 0.08,
    0.96,
    0.12,
    "#F1DEB4",
    zUnits + 0.04,
  );
}

export function drawMiniSpool(
  rctx: RenderContext,
  x: number,
  y: number,
  filamentColor: string,
  zUnits: number,
): void {
  drawWorldRect(rctx, x, y, 0.4, 0.36, "#CDAF7B", zUnits);
  drawWorldRect(
    rctx,
    x + 0.06,
    y + 0.07,
    0.28,
    0.22,
    filamentColor,
    zUnits + 0.01,
  );
  drawWorldRect(
    rctx,
    x + 0.16,
    y + 0.13,
    0.08,
    0.08,
    "#20252A",
    zUnits + 0.02,
  );
}

export function drawPartsBins(rctx: RenderContext, rect: Rect): void {
  const topZ = rctx.surfaceZ.value;
  const gridDark = rctx.theme === "dark";
  const gridLight = rctx.theme === "light";
  const palette = gridDark
    ? {
        baseFill: WORKBENCH_CASE_PALETTES.dark.baseFill,
        baseTop: WORKBENCH_CASE_PALETTES.dark.baseTop,
        baseStroke: WORKBENCH_CASE_PALETTES.dark.baseStroke,
        panel: "#0B2F40",
        header: "#1B6176",
        headerLine: CYAN_ACCENT.dark,
        binColors: ["#39D9FF", "#FF9146", "#2BE0B1", "#F4D44D"],
        binStroke: "#04151C",
        label: "#E9FFFF",
        foot: "#061925",
        footHandle: "#39D9FF",
      }
    : gridLight
      ? {
          baseFill: WORKBENCH_CASE_PALETTES.light.baseFill,
          baseTop: WORKBENCH_CASE_PALETTES.light.baseTop,
          baseStroke: WORKBENCH_CASE_PALETTES.light.baseStroke,
          panel: "#E7FFFF",
          header: "#BBF6FF",
          headerLine: CYAN_ACCENT.light,
          binColors: ["#008BBC", "#FF9146", "#2BE0B1", "#F4D44D"],
          binStroke: "#0F5368",
          label: "#F9FFFF",
          foot: "#0F6F88",
          footHandle: "#F4D44D",
        }
      : {
          baseFill: "#303B43",
          baseTop: "#4D5E67",
          baseStroke: "#81939B",
          panel: "#25313A",
          header: "#63737C",
          headerLine: "#D0DBDD",
          binColors: ["#7CA7B7", "#C6904D", "#8EA65C", "#B95C55"],
          binStroke: "#1B252B",
          label: "#F1E4BA",
          foot: "#202A31",
          footHandle: "#9BA9AE",
        };

  drawFurnitureBase(
    rctx,
    rect,
    palette.baseFill,
    palette.baseTop,
    palette.baseStroke,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.25,
    rect.y + 0.28,
    rect.w - 0.5,
    rect.h - 0.56,
    palette.panel,
    topZ + 0.04,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.45,
    rect.y + 0.48,
    rect.w - 0.9,
    0.36,
    palette.header,
    topZ + 0.1,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.62,
    rect.y + 0.58,
    rect.w - 1.24,
    0.08,
    palette.headerLine,
    topZ + 0.13,
  );

  let binIndex = 0;
  for (let row = 0; row < 2; row++) {
    for (let col = 0; col < 4; col++) {
      const x = rect.x + 0.48 + col * 1.06;
      const y = rect.y + 1.02 + row * 0.82;
      const fill = palette.binColors[binIndex % palette.binColors.length];
      drawRaisedBox(
        rctx,
        { x, y, w: 0.78, h: 0.54 },
        topZ + 0.06,
        0.18,
        shade(fill, 12),
        shade(fill, -28),
        fill,
        shade(fill, -40),
        palette.binStroke,
      );
      drawWorldRect(
        rctx,
        x + 0.12,
        y + 0.16,
        0.34,
        0.08,
        palette.label,
        topZ + 0.28,
      );
      drawWorldRect(
        rctx,
        x + 0.52,
        y + 0.18,
        0.12,
        0.1,
        palette.binStroke,
        topZ + 0.29,
      );
      binIndex += 1;
    }
  }

  for (let col = 0; col < 3; col++) {
    const x = rect.x + 0.78 + col * 1.38;
    drawVerticalFaceX(
      rctx,
      x,
      rect.y + rect.h + 0.02,
      0.86,
      0.34,
      0.56,
      palette.foot,
    );
    drawVerticalFaceX(
      rctx,
      x + 0.22,
      rect.y + rect.h + 0.03,
      0.42,
      0.56,
      0.08,
      palette.footHandle,
    );
  }

  drawMiniSpool(rctx, rect.x + 0.46, rect.y + 2.32, "#58C3B4", topZ + 0.16);
  drawMiniSpool(
    rctx,
    rect.x + rect.w - 0.96,
    rect.y + 2.28,
    "#E1B84C",
    topZ + 0.16,
  );
}

export function drawToolRack(rctx: RenderContext, rect: Rect): void {
  const topZ = rctx.surfaceZ.value;
  const faceY = rect.y + rect.h + 0.02;
  const gridDark = rctx.theme === "dark";
  const gridLight = rctx.theme === "light";
  const palette = gridDark
    ? {
        baseFill: "#04151C",
        baseTop: "#16495C",
        baseStroke: "rgba(57, 217, 255, 0.88)",
        panel: "#0B2F40",
        panelStroke: CYAN_ACCENT.dark,
        rail: "#1B6176",
        peg: "rgba(57, 217, 255, 0.34)",
        yellow: "#F4D44D",
        silver: "#8DEEFF",
        orange: "#FF9146",
        darkTool: "#081D27",
      }
    : gridLight
      ? {
          baseFill: "#0F6F88",
          baseTop: "#DDFBFF",
          baseStroke: "rgba(0, 139, 188, 0.68)",
          panel: "#E7FFFF",
          panelStroke: CYAN_ACCENT.light,
          rail: "#BBF6FF",
          peg: "rgba(0, 139, 188, 0.28)",
          yellow: "#D99B21",
          silver: "#39D9FF",
          orange: "#FF9146",
          darkTool: "#0F5368",
        }
      : {
          baseFill: "#222E36",
          baseTop: "#3F4E58",
          baseStroke: "#81919A",
          panel: "#2E3F48",
          panelStroke: "#77878E",
          rail: "#95A3A8",
          peg: "rgba(174, 190, 195, 0.34)",
          yellow: "#D0A84F",
          silver: "#AAB7BB",
          orange: "#C86B4F",
          darkTool: "#39454C",
        };

  drawFurnitureBase(
    rctx,
    rect,
    palette.baseFill,
    palette.baseTop,
    palette.baseStroke,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.18,
    faceY,
    rect.w - 0.36,
    0.46,
    topZ - 0.92,
    palette.panel,
  );
  strokeVerticalFaceX(
    rctx,
    rect.x + 0.18,
    faceY,
    rect.w - 0.36,
    0.46,
    topZ - 0.92,
    palette.panelStroke,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.3,
    faceY + 0.01,
    rect.w - 0.6,
    topZ - 0.7,
    0.18,
    palette.rail,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.3,
    faceY + 0.01,
    rect.w - 0.6,
    0.62,
    0.16,
    palette.rail,
  );

  for (let z = 1.0; z < topZ - 1.05; z += 0.48) {
    for (let x = rect.x + 0.42; x < rect.x + rect.w - 0.35; x += 0.36) {
      drawVerticalFaceX(rctx, x, faceY + 0.02, 0.055, z, 0.055, palette.peg);
    }
  }

  drawVerticalFaceX(
    rctx,
    rect.x + 0.42,
    faceY + 0.04,
    0.16,
    1.12,
    2.12,
    palette.yellow,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.33,
    faceY + 0.05,
    0.34,
    3.14,
    0.18,
    palette.yellow,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.86,
    faceY + 0.04,
    0.14,
    1.24,
    1.62,
    palette.silver,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 0.78,
    faceY + 0.05,
    0.32,
    2.74,
    0.18,
    palette.silver,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 1.28,
    faceY + 0.04,
    0.12,
    1.02,
    1.95,
    palette.orange,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 1.18,
    faceY + 0.05,
    0.34,
    2.86,
    0.2,
    palette.darkTool,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 1.58,
    faceY + 0.04,
    0.14,
    1.44,
    1.35,
    palette.yellow,
  );
  drawVerticalFaceX(
    rctx,
    rect.x + 1.42,
    faceY + 0.05,
    0.46,
    2.62,
    0.16,
    palette.silver,
  );

  drawWorldRect(
    rctx,
    rect.x + 0.28,
    rect.y + 0.28,
    rect.w - 0.56,
    0.34,
    palette.rail,
    topZ + 0.05,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.46,
    rect.y + 0.4,
    0.44,
    0.12,
    palette.yellow,
    topZ + 0.1,
  );
  drawWorldRect(
    rctx,
    rect.x + 1.08,
    rect.y + 0.4,
    0.5,
    0.12,
    palette.silver,
    topZ + 0.1,
  );
}

export function drawMergeButtonCube(rctx: RenderContext, rect: Rect): void {
  const topZ = rctx.surfaceZ.value;
  const gridDark = rctx.theme === "dark";
  const consolePalette = gridDark
    ? {
        top: "rgba(24, 65, 92, 0.72)",
        front: "rgba(8, 28, 46, 0.82)",
        left: "rgba(20, 82, 108, 0.72)",
        right: "rgba(6, 20, 34, 0.82)",
        stroke: "rgba(12, 44, 64, 0.82)",
        gloss: "rgba(255,255,255,0.08)",
        pedestalTop: "#303744",
        pedestalFront: "#171C24",
        pedestalLeft: "#424B58",
        pedestalRight: "#10151C",
        pedestalStroke: "#8EA3B8",
      }
    : {
        top: "rgba(219, 249, 255, 0.9)",
        front: "rgba(129, 205, 226, 0.88)",
        left: "rgba(184, 239, 250, 0.9)",
        right: "rgba(98, 176, 203, 0.88)",
        stroke: "rgba(46, 132, 160, 0.58)",
        gloss: "rgba(255,255,255,0.22)",
        pedestalTop: "#DDEAF0",
        pedestalFront: "#7FA8B8",
        pedestalLeft: "#C0DCE5",
        pedestalRight: "#5E889C",
        pedestalStroke: "#3C7C93",
      };

  drawRaisedBox(
    rctx,
    rect,
    0,
    topZ,
    consolePalette.top,
    consolePalette.front,
    consolePalette.left,
    consolePalette.right,
    consolePalette.stroke,
  );
  drawWorldRect(
    rctx,
    rect.x + 0.42,
    rect.y + 0.42,
    rect.w - 0.84,
    rect.h - 0.84,
    consolePalette.gloss,
    topZ + 0.04,
  );

  const pedestal = { x: rect.x + 1.02, y: rect.y + 1.02, w: 0.96, h: 0.96 };
  drawRaisedBox(
    rctx,
    pedestal,
    topZ + 0.06,
    0.38,
    consolePalette.pedestalTop,
    consolePalette.pedestalFront,
    consolePalette.pedestalLeft,
    consolePalette.pedestalRight,
    consolePalette.pedestalStroke,
  );
  drawWorldRect(
    rctx,
    pedestal.x + 0.18,
    pedestal.y + 0.18,
    0.6,
    0.6,
    "#B82032",
    topZ + 0.5,
  );
  strokeWorldRect(
    rctx,
    pedestal.x + 0.18,
    pedestal.y + 0.18,
    0.6,
    0.6,
    "#FF8A8A",
    topZ + 0.52,
  );
  drawWorldRect(
    rctx,
    pedestal.x + 0.32,
    pedestal.y + 0.12,
    0.32,
    0.16,
    "#FF6B6B",
    topZ + 0.54,
  );
}
