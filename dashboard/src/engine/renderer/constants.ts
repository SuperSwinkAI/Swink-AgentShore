import { BACK_WALL_HEIGHT_UNITS } from "../../office/layout";

// North-wall Kanban geometry (tile units)
export const KANBAN_FACE_Y = 6.5; // front-face plane of the shared north wall
export const KANBAN_BOARD_X_START = 5;
export const KANBAN_BOARD_WIDTH = 64;
export const KANBAN_BOARD_Z_START = 1.0;
export const KANBAN_BOARD_Z_END = BACK_WALL_HEIGHT_UNITS - 1.6;
export const KANBAN_HEADER_Z = BACK_WALL_HEIGHT_UNITS - 3.0;
export const KANBAN_HEADER_HEIGHT = 0.75;
export const KANBAN_TITLE_Z = BACK_WALL_HEIGHT_UNITS - 1.95;
export const TAPE_WIDTH_U = 0.15;
export const STICKY_SIZE_U = 0.5;
export const STICKY_INSET_U = 0.4;
export const KANBAN_STICKY_Z_START = 2.0;
export const KANBAN_STICKY_Z_END = KANBAN_HEADER_Z - 0.7;
export const KANBAN_DEPTH_BG = 6.509;
export const KANBAN_DEPTH_HEADER = 6.51;
export const KANBAN_DEPTH_STICKY = 6.511;
export const KANBAN_LANES = [
  { label: "TODO", x: 5, w: 15 },
  { label: "IN PROGRESS", x: 20, w: 17 },
  { label: "REVIEW", x: 37, w: 17 },
  { label: "DONE", x: 54, w: 15 },
] as const;

export const FRONT_DESK_ART_FACE_Y = 21.5;
export const FRONT_DESK_ART_DEPTH = 21.506;
export const EDITOR_ROOM_ART_FACE_Y = 21.5;
export const EDITOR_ROOM_ART_DEPTH = 21.507;
export const SCIENCE_LAB_ART_FACE_Y = 37.5;
export const SCIENCE_LAB_ART_DEPTH = 37.506;
