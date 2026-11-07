import { ArrowUpRight, ChevronDown, Loader2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { ApplicationDetail, EvidenceOut, MemoPoint } from "../api/types";
import { money } from "../lib/format";

function EvidenceChip({ ev, onJump }: { ev: EvidenceOut; onJump: (code: string) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <span className="chip-wrap" ref={ref}>
      <button className={`chip ${ev.kind}`} onClick={() => setOpen((o) => !o)}>
        {ev.label}
      </button>
      {open ? (
        <span className="chip-pop" role="dialog">
          <span className="chip-pop-detail">{ev.detail}</span>
          <button
            className="link-btn"
            onClick={() => {
              setOpen(false);
              onJump(ev.code);
            }}
          >
            Show source
          </button>
        </span>
      ) : null}
    </span>
  );
}

function Point({ point, evidence, onJump }: { point: MemoPoint; evidence: EvidenceOut[]; onJump: (c: string) => void }) {
  const chips = point.evidence_ids
    .map((id) => evidence.find((e) => e.code === id))
    .filter((e): e is EvidenceOut => Boolean(e));
  return (
    <li>
      <span>{point.text.replace(/\.;\s*/g, ". ")}</span>
      {chips.length > 0 ? (
        <span className="chips">
          {chips.map((ev) => (
            <EvidenceChip key={ev.code} ev={ev} onJump={onJump} />
          ))}
        </span>
      ) : null}
    </li>
  );
}

function ApproveForm({ app, onDone }: { app: ApplicationDetail; onDone: (a: ApplicationDetail) => void }) {
  const d = app.decision_detail!;
  const suggested = d.recommended_limit ?? d.requested_limit ?? 0;
  const [limit, setLimit] = useState(suggested ? String(Math.round(suggested)) : "");
  const [terms, setTerms] = useState(d.terms || "Net 30");
  const [mode, setMode] = useState<"idle" | "decline">("idle");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function act(name: string, fn: () => Promise<ApplicationDetail>) {
    setBusy(name);
    setError(null);
    try {
      onDone(await fn());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const needsInfo = d.decision === "request_info";
  const amount = Number(limit.replace(/[^0-9.]/g, ""));

  if (mode === "decline") {
    return (
      <div className="stack gap-12">
        <h3>Decline this application</h3>
        <p className="muted small">Vouch drafts the adverse action notice with the reason you give.</p>
        <textarea
          className="textarea"
          placeholder={d.reason}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        <div className="row gap-8">
          <button className="btn" disabled={busy !== null} onClick={() => act("decline", () => api.decline(app.id, reason || undefined))}>
            {busy === "decline" ? <Loader2 size={16} className="spin" /> : null}
            Decline and draft notice
          </button>
          <button className="btn ghost" onClick={() => setMode("idle")}>
            Cancel
          </button>
        </div>
        {error ? <p className="error-text">{error}</p> : null}
      </div>
    );
  }

  return (
    <div className="stack gap-16">
      <h3>{needsInfo ? "Your call" : "Approve terms"}</h3>
      <div className="approve-grid">
        <label>
          <span className="field-label">Credit limit</span>
          <span className="money-input">
            <span>$</span>
            <input className="input" inputMode="numeric" value={limit} onChange={(e) => setLimit(e.target.value)} />
          </span>
        </label>
        <label>
          <span className="field-label">Terms</span>
          <select className="select" value={terms} onChange={(e) => setTerms(e.target.value)}>
            {["Net 15", "Net 30", "Net 45", "Net 60"].map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </label>
      </div>
      {needsInfo ? (
        <button className="btn" disabled={busy !== null} onClick={() => act("info", () => api.requestInfo(app.id))}>
          {busy === "info" ? <Loader2 size={16} className="spin" /> : null}
          Ask the applicant for more
        </button>
      ) : null}
      <button
        className={needsInfo ? "btn outline" : "btn"}
        disabled={busy !== null || !amount}
        onClick={() => act("approve", () => api.approve(app.id, { limit: amount, terms }))}
      >
        {busy === "approve" ? <Loader2 size={16} className="spin" /> : null}
        {needsInfo ? `Approve ${money(amount)} anyway` : `Approve ${money(amount)} and sync`}
      </button>
      <div className="row gap-16">
        {!needsInfo ? (
          <button className="link-btn" disabled={busy !== null} onClick={() => act("info", () => api.requestInfo(app.id))}>
            Ask for more info
          </button>
        ) : null}
        <button className="link-btn" onClick={() => setMode("decline")}>
          Decline
        </button>
      </div>
      {error ? <p className="error-text">{error}</p> : null}
    </div>
  );
}

function Outcome({ app, onDone }: { app: ApplicationDetail; onDone: (a: ApplicationDetail) => void }) {
  const r = app.review!;
  const [busy, setBusy] = useState(false);
  if (r.action === "approved") {
    const sync = app.sync;
    return (
      <div className="stack gap-12">
        <h3>
          Approved {money(r.limit ?? null)} on {r.terms}
        </h3>
        <p className="muted small">
          By {r.reviewer}, {new Date(r.at).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" })}
        </p>
        {sync ? (
          sync.ok ? (
            <div className="sync-ok">
              <p>
                Added to {sync.provider === "quickbooks" ? "QuickBooks" : "the ledger"} as customer{" "}
                <span className="num">{sync.customer_id}</span>.
              </p>
              {sync.url ? (
                <a className="btn outline small" href={sync.url} target="_blank" rel="noreferrer">
                  Open in QuickBooks <ArrowUpRight size={14} />
                </a>
              ) : null}
            </div>
          ) : (
            <div className="stack gap-8">
              <p className="error-text">Sync failed: {sync.error}</p>
              <button
                className="btn small"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    onDone(await api.retrySync(app.id));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                Retry sync
              </button>
            </div>
          )
        ) : (
          <p className="muted row gap-8">
            <Loader2 size={15} className="spin" /> Creating the customer record
          </p>
        )}
      </div>
    );
  }
  if (r.action === "declined") {
    return (
      <div className="stack gap-12">
        <h3>Declined</h3>
        <p className="muted small">Adverse action notice, ready to send:</p>
        <pre className="letter">{r.notice}</pre>
      </div>
    );
  }
  const email = app.emails.find((e) => e.id === r.email_id);
  return (
    <div className="stack gap-12">
      <h3>Asked the applicant for more</h3>
      <p className="muted small">Sent to {email?.to_addr ?? "the applicant"}:</p>
      {email ? <pre className="letter">{email.body}</pre> : null}
    </div>
  );
}

export default function DecisionCard({
  app,
  onJump,
  onUpdate,
}: {
  app: ApplicationDetail;
  onJump: (code: string) => void;
  onUpdate: (a: ApplicationDetail) => void;
}) {
  const d = app.decision_detail;
  const [showMath, setShowMath] = useState(false);
  if (!d) return null;
  const hasLimit = d.recommended_limit !== null && d.decision !== "request_info" && d.decision !== "decline";
  const figureTitle: Record<string, string> = {
    request_info: "More information needed",
    decline: "Recommend decline",
    manual_review: "Needs a credit manager",
  };
  return (
    <section className="decision">
      <div className="decision-main card">
        <div className="decision-figure">
          {hasLimit ? (
            <>
              <span className="muted">Recommended limit</span>
              <span className="big num">{money(d.recommended_limit)}</span>
              <span className="decision-terms">
                on {d.terms}
                {d.requested_limit && d.requested_limit !== d.recommended_limit ? (
                  <span className="muted num">, requested {money(d.requested_limit)}</span>
                ) : null}
              </span>
            </>
          ) : (
            <span className="big-text">{figureTitle[d.decision] ?? d.decision}</span>
          )}
        </div>
        <p className="headline">{d.narrative.headline}</p>
        <ul className="points">
          {d.narrative.points.map((p, i) => (
            <Point key={i} point={p} evidence={app.evidence} onJump={onJump} />
          ))}
        </ul>
        {d.missing.length > 0 ? (
          <div className="missing">
            <h4>What to ask for</h4>
            <ul>
              {d.missing.map((m) => (
                <li key={m}>{m}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {d.narrative.risks.length > 0 ? (
          <div className="risks">
            <h4>Watch out for</h4>
            <ul className="points">
              {d.narrative.risks.map((p, i) => (
                <Point key={i} point={p} evidence={app.evidence} onJump={onJump} />
              ))}
            </ul>
          </div>
        ) : null}
        {d.math.length > 0 ? (
          <div className="math">
            <button className="math-toggle" onClick={() => setShowMath((s) => !s)} aria-expanded={showMath}>
              How the limit was set
              <ChevronDown size={16} style={{ transform: showMath ? "rotate(180deg)" : undefined }} />
            </button>
            {showMath ? (
              <ol>
                {d.math.map((line) => (
                  <li key={line} className="num">
                    {line}
                  </li>
                ))}
              </ol>
            ) : null}
          </div>
        ) : null}
      </div>
      <aside className="decision-actions card white">
        {app.review ? <Outcome app={app} onDone={onUpdate} /> : <ApproveForm app={app} onDone={onUpdate} />}
      </aside>
    </section>
  );
}
