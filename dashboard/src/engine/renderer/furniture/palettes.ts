import type { ResolvedTheme } from "../../../theme";

export interface WorkbenchCasePalette {
  baseFill: string;
  baseTop: string;
  baseStroke: string;
  surface: string;
  surfaceStroke: string;
  frontRail: string;
}




export const WORKBENCH_CASE_PALETTES: Record<
  ResolvedTheme,
  WorkbenchCasePalette
> = {
  dark: {
    baseFill: "#0F3140",
    baseTop: "#16495C",
    baseStroke: "rgba(57, 217, 255, 0.88)",
    surface: "rgba(125, 230, 255, 0.24)",
    surfaceStroke: "rgba(57, 217, 255, 0.82)",
    frontRail: "#04151C",
  },
  light: {
    baseFill: "#72D0E4",
    baseTop: "#DDFBFF",
    baseStroke: "rgba(0, 139, 188, 0.68)",
    surface: "rgba(244, 255, 255, 0.72)",
    surfaceStroke: "rgba(0, 139, 188, 0.62)",
    frontRail: "#168AA5",
  },
};




export const CYAN_ACCENT: Record<ResolvedTheme, string> = {
  dark: "#39D9FF",
  light: "#008BBC",
};

export const GREEN_ACCENT: Record<ResolvedTheme, string> = {
  dark: "#2BE0B1",
  light: "#129970",
};

export const ORANGE_ACCENT_WARM: Record<ResolvedTheme, string> = {
  dark: "#FF9146",
  light: "#E45C1C",
};
