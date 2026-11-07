import { Loader2, Mail } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import type { ApplicationDetail, BankFindings, EmailOut } from "../api/types";
import { REF_STATUS_LABEL, clock } from "../lib/format";

function EmailCard({ email }: { email: EmailOut }) {
  const sent = email.direction === "outbound";
  const via: Record<EmailOut["delivery"], string> = {
    outbox: "Held in the outbox (email sending is off)",
    smtp: "Sent by email",
    simulated: "Simulated reply",
    webhook: "Received by email",
  };
  return (
    <article id={`email-${email.id}`} className="email card white">
      <div className="email-head">
        <Mail size={16} />
        <div className="stack">
          <strong>{email.subject}</strong>
          <span className="muted small">
            {sent ? `To ${email.to_addr ?? "the bank"}` : `From ${email.from_addr ?? "the bank"}`}, {clock(email.created_at)}
          </span>
        </div>
        <span className="spacer" />
        <span className="pill outline">{via[email.delivery]}</span>
      </div>
      <pre className="email-body">{email.body}</pre>
    </article>
  );
}

const BANK_ROWS: { key: keyof BankFindings; label: string; fmt: (v: unknown) => string }[] = [
  { key: "account_open_since", label: "Account open since", fmt: (v) => String(v) },
  { key: "average_balance", label: "Average balance", fmt: (v) => String(v) },
  { key: "nsf_or_overdrafts_last_12mo", label: "NSF items, last 12 months", fmt: (v) => String(v) },
  { key: "has_loans_or_lines", label: "Loans or lines", fmt: (v) => (v ? "Yes" : "No") },
  { key: "loans_in_good_standing", label: "In good standing", fmt: (v) => (v ? "Yes" : "No") },
  { key: "relationship_rating", label: "Relationship", fmt: (v) => String(v).replace(/^\w/, (c) => c.toUpperCase()) },
];

export default function BankTab({ app }: { app: ApplicationDetail }) {
  const bank = app.bank;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!bank) return <p className="muted">The application does not list a bank.</p>;
  const emails = app.emails.filter((e) => e.reference_id === bank.id);
  const waiting = bank.step?.status === "waiting";
  const outbox = emails.some((e) => e.direction === "outbound" && e.delivery === "outbox");
  const findings = bank.findings as Partial<BankFindings> | null;

  return (
    <div id="bank" className="bank">
      <div className="section-head">
        <div>
          <h2>{bank.company}</h2>
          <p className="sub">
            {[bank.contact_name, bank.email, bank.phone_display].filter(Boolean).join(", ")}
          </p>
        </div>
        <span className="pill">{REF_STATUS_LABEL[bank.status] ?? bank.status}</span>
      </div>
      <div className="bank-grid">
        <div className="stack gap-16">
          {emails.length === 0 ? <p className="muted">The request goes out once the application is read.</p> : null}
          {emails.map((e) => (
            <EmailCard key={e.id} email={e} />
          ))}
          {waiting && outbox ? (
            <div className="retry-bar light">
              <span>Waiting for the bank to reply.</span>
              <span className="spacer" />
              <button
                className="btn small outline"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setError(null);
                  try {
                    await api.simulateBankReply(app.id);
                  } catch (e) {
                    setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {busy ? <Loader2 size={14} className="spin" /> : null}
                Simulate bank reply
              </button>
            </div>
          ) : null}
          {error ? <p className="error-text">{error}</p> : null}
        </div>
        <div className="card">
          <h3 style={{ marginBottom: 8 }}>What the bank said</h3>
          {findings && bank.status === "completed" ? (
            <>
              <div className="kv">
                {BANK_ROWS.map((row) => {
                  const v = findings[row.key];
                  return [
                    <div key={`${row.key}-l`}>{row.label}</div>,
                    <div key={`${row.key}-v`}>{v === null || v === undefined ? <span className="faint">Not stated</span> : row.fmt(v)}</div>,
                  ];
                })}
              </div>
              {findings.summary ? <p className="muted small" style={{ marginTop: 14 }}>{findings.summary}</p> : null}
            </>
          ) : (
            <p className="muted">Nothing yet. Vouch reads the reply as soon as it arrives.</p>
          )}
        </div>
      </div>
    </div>
  );
}
