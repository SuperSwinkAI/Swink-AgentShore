import type { ResolvedTheme } from "../theme";
import { ZoneId } from "./layout";

export interface ZoneVisualPalette {
  floor: string;
  label: string;
  wall: {
    face: string;
    cap: string;
    trim: string;
    shadow: string;
  };
}

interface ExtrudedVisualPalette {
  top: string;
  front: string;
  left: string;
  right: string;
  stroke: string;
}

interface StructuralWallVisualPalette extends ExtrudedVisualPalette {
  verticalTop: string;
  horizontalTop: string;
  trim: string;
  sideShade: string;
  horizontalTopOverlay: string;
}

export interface MapVisualPalette {
  fallbackFloor: string;
  floorGridStroke: string;
  barrierOverlay: string;
  interiorTranslucentWallAlpha: number;
  wallFaceStroke: string;
  wallCapStroke: string;
  wallLabelShadow: string;
  wallSticky: {
    fill: string;
    stroke: string;
  };
  furnitureShadow: string;
  targetStroke: {
    default: string;
    frontDesk: string;
    recoveryBay: string;
    zenGarden: string;
  };
  perimeterWall: ExtrudedVisualPalette;
  structuralWall: StructuralWallVisualPalette;
  zones: Record<ZoneId, ZoneVisualPalette>;
}

export const MAP_PALETTES: Record<ResolvedTheme, MapVisualPalette> = {
  "dark": {
    fallbackFloor: "rgba(8, 18, 28, 0.92)",
    floorGridStroke: "rgba(72, 184, 255, 0.36)",
    barrierOverlay: "rgba(0, 238, 255, 0.10)",
    interiorTranslucentWallAlpha: 0.64,
    wallFaceStroke: "rgba(102, 214, 255, 0.45)",
    wallCapStroke: "rgba(133, 227, 255, 0.34)",
    wallLabelShadow: "rgba(4, 16, 22, 0.72)",
    wallSticky: {
      fill: "rgba(255, 216, 99, 0.92)",
      stroke: "rgba(255, 149, 0, 0.95)",
    },
    furnitureShadow: "rgba(0, 0, 0, 0.12)",
    targetStroke: {
      default: "rgba(76, 214, 255, 0.64)",
      frontDesk: "rgba(255, 184, 77, 0.72)",
      recoveryBay: "rgba(255, 76, 99, 0.78)",
      zenGarden: "rgba(210, 244, 255, 0.70)",
    },
    perimeterWall: {
      top: "rgba(12, 28, 44, 0.98)",
      front: "rgba(8, 18, 30, 0.96)",
      left: "rgba(11, 24, 39, 0.96)",
      right: "rgba(7, 16, 27, 0.96)",
      stroke: "rgba(120, 225, 255, 0.40)",
    },
    structuralWall: {
      top: "rgba(15, 33, 52, 0.98)",
      verticalTop: "rgba(13, 30, 47, 0.98)",
      horizontalTop: "rgba(15, 33, 52, 0.98)",
      front: "rgba(18, 39, 60, 0.95)",
      left: "rgba(24, 52, 78, 0.92)",
      right: "rgba(10, 22, 36, 0.95)",
      stroke: "rgba(132, 229, 255, 0.42)",
      trim: "rgba(112, 215, 248, 0.82)",
      sideShade: "rgba(0, 10, 16, 0.20)",
      horizontalTopOverlay: "rgba(8, 20, 33, 0.94)",
    },
    zones: {
      [ZoneId.WAR_ROOM]: {
        floor: "rgba(17, 52, 84, 0.92)",
        label: "#8ee4ff",
        wall: {
          face: "rgba(29, 77, 114, 0.84)",
          cap: "rgba(9, 32, 50, 0.94)",
          trim: "rgba(120, 223, 255, 0.88)",
          shadow: "rgba(0, 0, 0, 0.14)",
        },
      },
      [ZoneId.WORKSHOP]: {
        floor: "rgba(16, 69, 95, 0.90)",
        label: "#92e8ff",
        wall: {
          face: "rgba(28, 95, 124, 0.82)",
          cap: "rgba(10, 43, 58, 0.94)",
          trim: "rgba(134, 239, 255, 0.86)",
          shadow: "rgba(0, 0, 0, 0.14)",
        },
      },
      [ZoneId.SCIENCE_LAB]: {
        floor: "rgba(20, 58, 99, 0.90)",
        label: "#a0eeff",
        wall: {
          face: "rgba(34, 88, 137, 0.82)",
          cap: "rgba(11, 36, 64, 0.94)",
          trim: "rgba(145, 232, 255, 0.86)",
          shadow: "rgba(0, 0, 0, 0.14)",
        },
      },
      [ZoneId.LAUNCH_CONTROL]: {
        floor: "rgba(18, 47, 98, 0.90)",
        label: "#9bdfff",
        wall: {
          face: "rgba(30, 75, 136, 0.82)",
          cap: "rgba(9, 30, 64, 0.94)",
          trim: "rgba(129, 216, 255, 0.86)",
          shadow: "rgba(0, 0, 0, 0.14)",
        },
      },
      [ZoneId.EDITORS_DESK]: {
        floor: "rgba(18, 83, 111, 0.90)",
        label: "#97ebff",
        wall: {
          face: "rgba(29, 111, 145, 0.82)",
          cap: "rgba(10, 50, 67, 0.94)",
          trim: "rgba(129, 238, 255, 0.86)",
          shadow: "rgba(0, 0, 0, 0.14)",
        },
      },
      [ZoneId.RECOVERY_BAY]: {
        floor: "rgba(46, 68, 80, 0.90)",
        label: "#b8d6de",
        wall: {
          face: "rgba(63, 87, 99, 0.82)",
          cap: "rgba(27, 43, 54, 0.94)",
          trim: "rgba(135, 197, 210, 0.82)",
          shadow: "rgba(0, 0, 0, 0.15)",
        },
      },
      [ZoneId.ZEN_GARDEN]: {
        floor: "rgba(18, 97, 115, 0.88)",
        label: "#b5f5ff",
        wall: {
          face: "rgba(32, 126, 143, 0.80)",
          cap: "rgba(12, 57, 68, 0.93)",
          trim: "rgba(165, 246, 255, 0.85)",
          shadow: "rgba(0, 0, 0, 0.14)",
        },
      },
      [ZoneId.FRONT_DESK]: {
        floor: "rgba(18, 75, 101, 0.90)",
        label: "#a4f0ff",
        wall: {
          face: "rgba(31, 104, 130, 0.82)",
          cap: "rgba(11, 45, 60, 0.94)",
          trim: "rgba(145, 236, 255, 0.86)",
          shadow: "rgba(0, 0, 0, 0.14)",
        },
      },
    },
  },
  "light": {
    fallbackFloor: "rgba(220, 247, 255, 0.96)",
    floorGridStroke: "rgba(0, 156, 214, 0.42)",
    barrierOverlay: "rgba(0, 134, 186, 0.11)",
    interiorTranslucentWallAlpha: 0.24,
    wallFaceStroke: "rgba(0, 124, 176, 0.40)",
    wallCapStroke: "rgba(0, 124, 176, 0.30)",
    wallLabelShadow: "rgba(255, 255, 255, 0.58)",
    wallSticky: {
      fill: "rgba(255, 145, 64, 0.92)",
      stroke: "rgba(154, 52, 18, 0.92)",
    },
    furnitureShadow: "rgba(0, 80, 120, 0.08)",
    targetStroke: {
      default: "rgba(0, 129, 201, 0.56)",
      frontDesk: "rgba(221, 101, 24, 0.62)",
      recoveryBay: "rgba(218, 48, 78, 0.68)",
      zenGarden: "rgba(55, 132, 106, 0.52)",
    },
    perimeterWall: {
      top: "rgba(212, 244, 255, 0.99)",
      front: "rgba(186, 232, 248, 0.96)",
      left: "rgba(200, 239, 251, 0.96)",
      right: "rgba(174, 225, 243, 0.96)",
      stroke: "rgba(0, 137, 191, 0.30)",
    },
    structuralWall: {
      top: "rgba(194, 236, 250, 0.99)",
      verticalTop: "rgba(188, 232, 248, 0.99)",
      horizontalTop: "rgba(194, 236, 250, 0.99)",
      front: "rgba(206, 243, 253, 0.96)",
      left: "rgba(214, 247, 255, 0.96)",
      right: "rgba(172, 222, 240, 0.96)",
      stroke: "rgba(0, 137, 191, 0.34)",
      trim: "rgba(0, 119, 168, 0.72)",
      sideShade: "rgba(0, 98, 140, 0.12)",
      horizontalTopOverlay: "rgba(167, 220, 239, 0.94)",
    },
    zones: {
      [ZoneId.WAR_ROOM]: {
        floor: "rgba(198, 220, 238, 0.96)",
        label: "#13537a",
        wall: {
          face: "rgba(219, 247, 255, 0.96)",
          cap: "rgba(169, 217, 233, 0.96)",
          trim: "rgba(0, 132, 186, 0.70)",
          shadow: "rgba(0, 85, 118, 0.08)",
        },
      },
      [ZoneId.WORKSHOP]: {
        floor: "rgba(180, 244, 250, 0.96)",
        label: "#0f5d66",
        wall: {
          face: "rgba(208, 252, 255, 0.96)",
          cap: "rgba(157, 224, 230, 0.96)",
          trim: "rgba(0, 153, 171, 0.70)",
          shadow: "rgba(0, 90, 96, 0.08)",
        },
      },
      [ZoneId.SCIENCE_LAB]: {
        floor: "rgba(214, 244, 252, 0.96)",
        label: "#115b81",
        wall: {
          face: "rgba(224, 248, 255, 0.96)",
          cap: "rgba(173, 218, 236, 0.96)",
          trim: "rgba(0, 132, 186, 0.70)",
          shadow: "rgba(0, 85, 118, 0.08)",
        },
      },
      [ZoneId.LAUNCH_CONTROL]: {
        floor: "rgba(196, 216, 226, 0.96)",
        label: "#1c4f86",
        wall: {
          face: "rgba(218, 244, 255, 0.96)",
          cap: "rgba(167, 208, 236, 0.96)",
          trim: "rgba(0, 122, 179, 0.70)",
          shadow: "rgba(0, 78, 112, 0.08)",
        },
      },
      [ZoneId.EDITORS_DESK]: {
        floor: "rgba(186, 236, 229, 0.96)",
        label: "#0f6660",
        wall: {
          face: "rgba(221, 255, 249, 0.96)",
          cap: "rgba(171, 230, 223, 0.96)",
          trim: "rgba(0, 154, 140, 0.70)",
          shadow: "rgba(0, 96, 88, 0.08)",
        },
      },
      [ZoneId.RECOVERY_BAY]: {
        floor: "rgba(187, 202, 210, 0.96)",
        label: "#385c68",
        wall: {
          face: "rgba(218, 235, 240, 0.96)",
          cap: "rgba(160, 187, 197, 0.96)",
          trim: "rgba(73, 135, 154, 0.70)",
          shadow: "rgba(38, 77, 90, 0.09)",
        },
      },
      [ZoneId.ZEN_GARDEN]: {
        floor: "rgba(198, 232, 206, 0.96)",
        label: "#1f6444",
        wall: {
          face: "rgba(223, 255, 240, 0.96)",
          cap: "rgba(170, 226, 204, 0.96)",
          trim: "rgba(41, 140, 110, 0.70)",
          shadow: "rgba(22, 88, 67, 0.08)",
        },
      },
      [ZoneId.FRONT_DESK]: {
        floor: "rgba(189, 232, 232, 0.96)",
        label: "#165f67",
        wall: {
          face: "rgba(218, 255, 255, 0.96)",
          cap: "rgba(168, 224, 224, 0.96)",
          trim: "rgba(0, 150, 164, 0.70)",
          shadow: "rgba(0, 90, 100, 0.08)",
        },
      },
    },
  },
};

export interface KanbanWallPalette {
  boardBackground: string;
  boardBorder: string;
  headerFills: [string, string, string, string];
  headerStroke: string;
  laneTextColors: [string, string, string, string];
  dividerColor: string;
  titleColor: string;
}

export const KANBAN_WALL_PALETTES: Record<ResolvedTheme, KanbanWallPalette> = {
  "dark": {
    boardBackground: "rgba(6, 18, 32, 0.86)",
    boardBorder: "rgba(80, 210, 255, 0.56)",
    headerFills: [
      "rgba(16, 52, 88, 0.92)",
      "rgba(110, 68, 16, 0.92)",
      "rgba(56, 32, 100, 0.92)",
      "rgba(14, 84, 50, 0.92)",
    ],
    headerStroke: "rgba(80, 210, 255, 0.38)",
    laneTextColors: ["#66dfff", "#ffac36", "#b880ff", "#38e890"],
    dividerColor: "rgba(80, 210, 255, 0.50)",
    titleColor: "rgba(180, 240, 255, 0.95)",
  },
  "light": {
    boardBackground: "rgba(242, 252, 255, 0.92)",
    boardBorder: "rgba(0, 130, 186, 0.48)",
    headerFills: [
      "rgba(196, 230, 252, 0.92)",
      "rgba(255, 228, 186, 0.92)",
      "rgba(226, 212, 252, 0.92)",
      "rgba(198, 248, 218, 0.92)",
    ],
    headerStroke: "rgba(0, 130, 186, 0.32)",
    laneTextColors: ["#0e5a96", "#8a4e08", "#5520a0", "#0a7040"],
    dividerColor: "rgba(0, 130, 186, 0.40)",
    titleColor: "rgba(0, 60, 96, 0.92)",
  },
};

// ---------------------------------------------------------------------------
// Floor-pad accent palettes (per-zone, per-theme)
//
// The floor-rendering passes use a small repeating cycle of accent colors to
// distinguish seat pads inside a zone. The original code hard-coded the
// arrays inline at four call sites; here we keep a single source of truth so
// theme/colour changes touch one place. Key: zone → theme → ordered list of
// PadColor entries (the renderer indexes into this with `i % padColors.length`).
// ---------------------------------------------------------------------------

export interface PadColor {
  fill: string;
  core: string;
  stroke: string;
}

const PAD_PALETTE_WAR_ROOM_DARK: PadColor[] = [
  { fill: "rgba(57, 217, 255, 0.15)", core: "rgba(57, 217, 255, 0.46)", stroke: "rgba(57, 217, 255, 0.72)" },
  { fill: "rgba(244, 212, 77, 0.14)", core: "rgba(244, 212, 77, 0.46)", stroke: "rgba(244, 212, 77, 0.70)" },
  { fill: "rgba(43, 224, 177, 0.13)", core: "rgba(43, 224, 177, 0.42)", stroke: "rgba(43, 224, 177, 0.66)" },
  { fill: "rgba(255, 145, 70, 0.13)", core: "rgba(255, 145, 70, 0.42)", stroke: "rgba(255, 145, 70, 0.66)" },
];
const PAD_PALETTE_WAR_ROOM_LIGHT: PadColor[] = [
  { fill: "rgba(0, 174, 214, 0.14)", core: "rgba(0, 139, 188, 0.38)", stroke: "rgba(0, 126, 174, 0.62)" },
  { fill: "rgba(221, 101, 24, 0.13)", core: "rgba(221, 101, 24, 0.36)", stroke: "rgba(175, 73, 20, 0.60)" },
  { fill: "rgba(18, 153, 112, 0.12)", core: "rgba(18, 153, 112, 0.32)", stroke: "rgba(18, 126, 95, 0.54)" },
  { fill: "rgba(255, 128, 55, 0.13)", core: "rgba(228, 92, 28, 0.34)", stroke: "rgba(177, 67, 20, 0.58)" },
];

const PAD_PALETTE_EDITORS_DESK_DARK: PadColor[] = [
  { fill: "rgba(57, 217, 255, 0.15)", core: "rgba(57, 217, 255, 0.44)", stroke: "rgba(57, 217, 255, 0.70)" },
  { fill: "rgba(178, 102, 255, 0.14)", core: "rgba(178, 102, 255, 0.40)", stroke: "rgba(178, 102, 255, 0.68)" },
  { fill: "rgba(43, 224, 177, 0.13)", core: "rgba(43, 224, 177, 0.38)", stroke: "rgba(43, 224, 177, 0.64)" },
  { fill: "rgba(255, 145, 70, 0.13)", core: "rgba(255, 145, 70, 0.38)", stroke: "rgba(255, 145, 70, 0.64)" },
];
const PAD_PALETTE_EDITORS_DESK_LIGHT: PadColor[] = [
  { fill: "rgba(0, 174, 214, 0.13)", core: "rgba(0, 139, 188, 0.34)", stroke: "rgba(0, 126, 174, 0.58)" },
  { fill: "rgba(140, 88, 206, 0.12)", core: "rgba(126, 78, 186, 0.30)", stroke: "rgba(106, 61, 166, 0.52)" },
  { fill: "rgba(18, 153, 112, 0.11)", core: "rgba(18, 153, 112, 0.28)", stroke: "rgba(18, 126, 95, 0.50)" },
  { fill: "rgba(255, 128, 55, 0.12)", core: "rgba(228, 92, 28, 0.30)", stroke: "rgba(177, 67, 20, 0.54)" },
];

const PAD_PALETTE_ZEN_GARDEN_DARK: PadColor[] = [
  { fill: "rgba(43, 224, 177, 0.13)", core: "rgba(43, 224, 177, 0.38)", stroke: "rgba(43, 224, 177, 0.66)" },
  { fill: "rgba(57, 217, 255, 0.12)", core: "rgba(57, 217, 255, 0.34)", stroke: "rgba(57, 217, 255, 0.62)" },
  { fill: "rgba(96, 240, 198, 0.12)", core: "rgba(96, 240, 198, 0.34)", stroke: "rgba(96, 240, 198, 0.60)" },
];
const PAD_PALETTE_ZEN_GARDEN_LIGHT: PadColor[] = [
  { fill: "rgba(18, 153, 112, 0.12)", core: "rgba(18, 153, 112, 0.28)", stroke: "rgba(18, 126, 95, 0.52)" },
  { fill: "rgba(0, 139, 188, 0.11)", core: "rgba(0, 139, 188, 0.26)", stroke: "rgba(0, 126, 174, 0.48)" },
  { fill: "rgba(34, 174, 132, 0.11)", core: "rgba(34, 174, 132, 0.26)", stroke: "rgba(24, 139, 104, 0.48)" },
];

const PAD_PALETTE_SCIENCE_LAB_DARK: PadColor[] = [
  { fill: "rgba(255, 145, 70, 0.13)", core: "rgba(255, 145, 70, 0.40)", stroke: "rgba(255, 145, 70, 0.68)" },
  { fill: "rgba(57, 217, 255, 0.13)", core: "rgba(57, 217, 255, 0.38)", stroke: "rgba(57, 217, 255, 0.64)" },
  { fill: "rgba(43, 224, 177, 0.12)", core: "rgba(43, 224, 177, 0.34)", stroke: "rgba(43, 224, 177, 0.60)" },
];
const PAD_PALETTE_SCIENCE_LAB_LIGHT: PadColor[] = [
  { fill: "rgba(221, 101, 24, 0.13)", core: "rgba(221, 101, 24, 0.34)", stroke: "rgba(175, 73, 20, 0.58)" },
  { fill: "rgba(0, 139, 188, 0.12)", core: "rgba(0, 139, 188, 0.30)", stroke: "rgba(0, 126, 174, 0.52)" },
  { fill: "rgba(18, 153, 112, 0.11)", core: "rgba(18, 153, 112, 0.28)", stroke: "rgba(18, 126, 95, 0.50)" },
];

type PadZoneId =
  | ZoneId.WAR_ROOM
  | ZoneId.EDITORS_DESK
  | ZoneId.ZEN_GARDEN
  | ZoneId.SCIENCE_LAB;

export const ZONE_PAD_PALETTES: Record<ResolvedTheme, Record<PadZoneId, PadColor[]>> = {
  "dark": {
    [ZoneId.WAR_ROOM]: PAD_PALETTE_WAR_ROOM_DARK,
    [ZoneId.EDITORS_DESK]: PAD_PALETTE_EDITORS_DESK_DARK,
    [ZoneId.ZEN_GARDEN]: PAD_PALETTE_ZEN_GARDEN_DARK,
    [ZoneId.SCIENCE_LAB]: PAD_PALETTE_SCIENCE_LAB_DARK,
  },
  "light": {
    [ZoneId.WAR_ROOM]: PAD_PALETTE_WAR_ROOM_LIGHT,
    [ZoneId.EDITORS_DESK]: PAD_PALETTE_EDITORS_DESK_LIGHT,
    [ZoneId.ZEN_GARDEN]: PAD_PALETTE_ZEN_GARDEN_LIGHT,
    [ZoneId.SCIENCE_LAB]: PAD_PALETTE_SCIENCE_LAB_LIGHT,
  },
};
