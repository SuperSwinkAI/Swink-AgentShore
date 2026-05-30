import { invoke } from "@tauri-apps/api/core";
import { callJsonRpc } from "./jsonRpcClient";

export interface ReportSection {
  id: string;
  title: string;
}

export interface ArchiveListEntry {
  archive_id: string;
  session_id: string;
  archive_path: string;
  total_cost: number;
  final_alignment: number;
  total_plays: number;
  created_at: string;
}

export interface FetchReportResult {
  html: string;
  sections: ReportSection[];
}

export interface FetchLogsResult {
  lines: string[];
}

export interface LogRange {
  start: number;
  end: number;
}

function extractReportSections(html: string): ReportSection[] {
  const doc = new DOMParser().parseFromString(html, "text/html");
  return Array.from(doc.querySelectorAll("section[id]"))
    .map((section) => {
      const id = section.getAttribute("id");
      const title = section.querySelector("h2")?.textContent?.trim();
      if (!id || !title) return null;
      return { id, title };
    })
    .filter((section): section is ReportSection => section !== null);
}

export async function listArchives(): Promise<ArchiveListEntry[]> {
  return callJsonRpc<ArchiveListEntry[]>("archive.list");
}

export async function fetchReport(archiveId: string): Promise<FetchReportResult> {
  const meta = await callJsonRpc<{ html_path: string; sections: ReportSection[] }>(
    "archive.fetch_report",
    { archive_id: archiveId },
  );
  const html = await invoke<string>("read_text_file", { path: meta.html_path });
  return { html, sections: meta.sections };
}

export async function fetchReportByPath(path: string): Promise<FetchReportResult> {
  const html = await invoke<string>("read_text_file", { path });
  return { html, sections: extractReportSections(html) };
}

function sliceLogLines(content: string, range?: LogRange): string[] {
  const start = range?.start ?? 1;
  const end = range?.end ?? 200;
  return content
    .split(/\r?\n/u)
    .slice(start - 1, end)
    .filter((line, idx, lines) => line.length > 0 || idx < lines.length - 1);
}

export async function fetchLogsByPath(
  path: string,
  range?: LogRange,
): Promise<FetchLogsResult> {
  const content = await invoke<string>("read_text_file", { path });
  return { lines: sliceLogLines(content, range) };
}

export async function fetchLogs(
  archiveId: string,
  range?: LogRange,
): Promise<FetchLogsResult> {
  const params: Record<string, unknown> = { archive_id: archiveId };
  if (range !== undefined) {
    params.range = range;
  }
  return callJsonRpc<FetchLogsResult>("archive.fetch_logs", params);
}

export const archiveClient = {
  listArchives,
  fetchReport,
  fetchReportByPath,
  fetchLogs,
  fetchLogsByPath,
};

export type ArchiveClient = typeof archiveClient;
