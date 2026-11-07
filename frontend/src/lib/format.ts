import type { AppStatus, CallChannel, CallOutcome, DecisionCode, RefStatus } from "../api/types";

export function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return "Not stated";
  return `$${Math.round(value).toLocaleString("en-US")}`;
}

export function requestLine(limit: number | null, terms: string | null): string {
  if (limit === null && !terms) return "No amount requested";
  if (limit === null) return `Requested ${terms}`;
  return `Requested ${money(limit)}${terms ? ` on ${terms}` : ""}`;
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 45) return "just now";
  if (diff < 90) return "a minute ago";
  if (diff < 3600) return `${Math.round(diff / 60)} minutes ago`;
  if (diff < 5400) return "an hour ago";
  if (diff < 86400) return `${Math.round(diff / 3600)} hours ago`;
  const days = Math.round(diff / 86400);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

export function clock(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

export function countdown(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  const m = Math.floor(s / 60);
  const rest = s % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${h}:${String(m % 60).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
  }
  return `${m}:${String(rest).padStart(2, "0")}`;
}

export function duration(startIso: string | null, endIso: string | null): string {
  if (!startIso) return "";
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  return countdown((end - new Date(startIso).getTime()) / 1000);
}

export const STATUS_LABEL: Record<AppStatus, string> = {
  received: "Received",
  reading: "Reading",
  verifying: "Checking records",
  checking_references: "Calling references",
  deciding: "Writing memo",
  ready_for_review: "Ready for review",
  approved: "Approved",
  synced: "Approved and synced",
  declined: "Declined",
  info_requested: "Info requested",
  failed: "Failed",
};

export function statusTone(status: AppStatus): { cls: string; live: boolean } {
  switch (status) {
    case "ready_for_review":
      return { cls: "lime", live: false };
    case "approved":
    case "synced":
      return { cls: "ink", live: false };
    case "declined":
      return { cls: "", live: false };
    case "info_requested":
      return { cls: "outline", live: false };
    case "failed":
      return { cls: "bad", live: false };
    default:
      return { cls: "dark", live: true };
  }
}

export const DECISION_LABEL: Record<DecisionCode, string> = {
  approve: "Approve",
  approve_reduced: "Approve, reduced limit",
  request_info: "Needs more information",
  manual_review: "Credit manager review",
  decline: "Decline",
};

export const REF_STATUS_LABEL: Record<RefStatus, string> = {
  pending: "Waiting",
  verified: "Verified",
  flagged: "Flagged",
  queued: "Queued",
  calling: "On a call",
  retry_scheduled: "Calling again soon",
  callback_scheduled: "Callback scheduled",
  completed: "Answered",
  declined: "Written only",
  wrong_number: "Wrong number",
  unreachable: "Not reached",
  emailed: "Emailed",
  no_reply: "No reply",
};

export const OUTCOME_LABEL: Record<CallOutcome, string> = {
  completed: "Answered",
  callback_requested: "Asked for a callback",
  declined: "Written references only",
  wrong_number: "Wrong number",
  voicemail: "Voicemail",
  no_answer: "No answer",
  failed: "Call failed",
};

export const CHANNEL_LABEL: Record<CallChannel, string> = {
  simulated: "Simulated reference",
  browser: "Browser phone",
  twilio: "Real phone",
};

export function plural(n: number, word: string, pluralWord?: string): string {
  return `${n} ${n === 1 ? word : pluralWord ?? `${word}s`}`;
}
