import { Check, Clock, Loader2, Minus, X } from "lucide-react";
import type { ApplicationDetail, StepOut, StepStatus } from "../api/types";
import { DECISION_LABEL, countdown, plural } from "../lib/format";
import { useNow } from "../lib/hooks";

type NodeState = "pending" | "active" | "waiting" | "done" | "failed" | "skipped";

interface Node {
  key: string;
  label: string;
  state: NodeState;
  detail: string;
}

function fromStep(step: StepOut | undefined): NodeState {
  if (!step) return "pending";
  const map: Record<StepStatus, NodeState> = {
    pending: step.next_run_at ? "waiting" : "pending",
    running: "active",
    waiting: "waiting",
    done: "done",
    failed: "failed",
    skipped: "skipped",
  };
  return map[step.status];
}

function secondsUntil(iso: string | null, now: number): number | null {
  if (!iso) return null;
  return (new Date(iso).getTime() - now) / 1000;
}

export function buildNodes(app: ApplicationDetail, now: number): Node[] {
  const step = (kind: string) => app.steps.find((s) => s.kind === kind);
  const extract = step("extract");
  const verify = step("verify");
  const bank = step("bank");
  const memo = step("memo");
  const syncs = app.steps.filter((s) => s.kind === "sync");
  const sync = syncs[syncs.length - 1];
  const calls = app.steps.filter((s) => s.kind === "call");

  const nodes: Node[] = [];

  const ex = fromStep(extract);
  nodes.push({
    key: "read",
    label: "Read",
    state: ex,
    detail:
      ex === "done"
        ? `${plural(app.extracted?.trade_references.length ?? 0, "reference")} found`
        : ex === "failed"
          ? "Could not read the file"
          : ex === "active"
            ? "Reading the scan"
            : "",
  });

  const ve = fromStep(verify);
  const flagged = app.references.filter((r) => r.flagged).length;
  nodes.push({
    key: "verify",
    label: "Verify",
    state: ve,
    detail: ve === "done" ? (flagged ? `${flagged} flagged` : "All check out") : ve === "active" ? "Checking records" : "",
  });

  let callState: NodeState = "pending";
  let callDetail = "";
  if (calls.length > 0) {
    const states = calls.map(fromStep);
    const finished = calls.filter((c) => ["done", "skipped", "failed"].includes(c.status)).length;
    if (finished === calls.length) {
      callState = "done";
      const answered = app.references.filter((r) => r.status === "completed").length;
      callDetail = `${answered} of ${calls.length} answered`;
    } else if (states.includes("active") || calls.some((c) => c.status === "waiting")) {
      callState = "active";
      const live = calls.find((c) => c.status === "waiting" || c.status === "running");
      const ref = app.references.find((r) => r.id === live?.reference_id);
      callDetail = ref ? `On the phone with ${ref.company}` : "Calling";
    } else {
      callState = "waiting";
      const next = calls
        .map((c) => secondsUntil(c.next_run_at, now))
        .filter((s): s is number => s !== null)
        .sort((a, b) => a - b)[0];
      callDetail = next !== undefined ? `Next call in ${countdown(next)}` : "Queued";
    }
  } else if (ve === "done") {
    callState = "skipped";
    callDetail = "No references to call";
  }
  nodes.push({ key: "calls", label: "Call references", state: callState, detail: callDetail });

  const ba = fromStep(bank);
  nodes.push({
    key: "bank",
    label: "Bank reply",
    state: ba === "waiting" ? "active" : ba,
    detail:
      ba === "done"
        ? app.bank?.status === "no_reply"
          ? "No reply"
          : "Replied"
        : ba === "waiting"
          ? "Email sent, waiting"
          : ba === "skipped"
            ? "No bank listed"
            : "",
  });

  const me = fromStep(memo);
  nodes.push({
    key: "decide",
    label: "Decide",
    state: me,
    detail:
      me === "done" && app.decision
        ? DECISION_LABEL[app.decision]
        : me === "active"
          ? "Writing the memo"
          : "",
  });

  const sy = fromStep(sync);
  nodes.push({
    key: "sync",
    label: "Sync",
    state: app.sync && !app.sync.ok ? "failed" : sy,
    detail: app.sync?.ok
      ? app.sync.provider === "quickbooks"
        ? "In QuickBooks"
        : "In the ledger"
      : app.sync && !app.sync.ok
        ? "Sync failed"
        : sy === "active"
          ? "Creating customer"
          : app.status === "ready_for_review"
            ? "After you approve"
            : "",
  });
  return nodes;
}

function Icon({ state }: { state: NodeState }) {
  switch (state) {
    case "done":
      return <Check size={14} strokeWidth={2.6} />;
    case "active":
      return <Loader2 size={14} strokeWidth={2.4} className="spin" />;
    case "waiting":
      return <Clock size={14} strokeWidth={2.2} />;
    case "failed":
      return <X size={14} strokeWidth={2.6} />;
    case "skipped":
      return <Minus size={14} strokeWidth={2.4} />;
    default:
      return null;
  }
}

export default function Pipeline({ app }: { app: ApplicationDetail }) {
  const now = useNow(1000);
  const nodes = buildNodes(app, now);
  return (
    <ol className="pipeline">
      {nodes.map((n) => (
        <li key={n.key} className={`pnode ${n.state}`}>
          <div className="pnode-top">
            <span className="pnode-icon">
              <Icon state={n.state} />
            </span>
            <span className="pnode-line" />
          </div>
          <div className="pnode-label">{n.label}</div>
          <div className="pnode-detail">{n.detail}</div>
        </li>
      ))}
    </ol>
  );
}
