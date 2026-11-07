import { Plus } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { ApplicationSummary, SampleInfo } from "../api/types";
import NewApplication, { SampleList } from "../components/NewApplication";
import StatusPill from "../components/StatusPill";
import { DECISION_LABEL, money, requestLine, timeAgo } from "../lib/format";
import { useNow } from "../lib/hooks";

export default function Applications() {
  const [apps, setApps] = useState<ApplicationSummary[] | null>(null);
  const [samples, setSamples] = useState<SampleInfo[]>([]);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  useNow(30000);

  const load = useCallback(() => {
    api
      .applications()
      .then(setApps)
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    load();
    api.samples().then(setSamples).catch(() => undefined);
    const es = new EventSource("/api/stream");
    let t: number | null = null;
    es.onmessage = (msg) => {
      try {
        const ev = JSON.parse(msg.data) as { type: string };
        if (ev.type.startsWith("call.token") || ev.type === "call.transcript" || ev.type === "call.findings") return;
      } catch {
        return;
      }
      if (t === null)
        t = window.setTimeout(() => {
          t = null;
          load();
        }, 500);
    };
    return () => {
      es.close();
      if (t !== null) window.clearTimeout(t);
    };
  }, [load]);

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1>Credit applications</h1>
          <p className="sub">Vouch reads each application, checks the references, and drafts a decision for you to approve.</p>
        </div>
        {apps && apps.length > 0 ? (
          <button className="btn" onClick={() => setOpen(true)}>
            <Plus size={17} strokeWidth={2.2} />
            New application
          </button>
        ) : null}
      </div>

      {error ? <p className="error-text">Could not load applications: {error}</p> : null}

      {apps === null ? null : apps.length === 0 ? (
        <div className="start">
          <div className="start-copy">
            <h2>Run a sample application</h2>
            <p className="muted">
              Each sample is a scanned, hand-filled credit application. Vouch reads it, checks public records, calls the
              trade references, emails the bank, and writes the memo.
            </p>
            <button className="btn outline" onClick={() => setOpen(true)}>
              Upload your own
            </button>
          </div>
          <SampleList samples={samples} onStarted={(id) => navigate(`/applications/${id}`)} />
        </div>
      ) : (
        <div className="app-table">
          <div className="app-row head">
            <div>Applicant</div>
            <div>Request</div>
            <div>Status</div>
            <div>Recommendation</div>
            <div>Updated</div>
          </div>
          {apps.map((a) => (
            <button key={a.id} className="app-row" onClick={() => navigate(`/applications/${a.id}`)}>
              <div>
                <div className="app-name">{a.applicant_name ?? "Reading application"}</div>
                <div className="faint small">{a.source_filename}</div>
              </div>
              <div className="num">{requestLine(a.requested_limit, a.requested_terms).replace("Requested ", "")}</div>
              <div>
                <StatusPill status={a.status} />
              </div>
              <div>
                {a.approved_limit ? (
                  <span className="num">{money(a.approved_limit)} approved</span>
                ) : a.decision ? (
                  <span>
                    {DECISION_LABEL[a.decision]}
                    {a.recommended_limit ? <span className="muted num">, {money(a.recommended_limit)}</span> : null}
                  </span>
                ) : (
                  <span className="faint">
                    {a.flagged_references > 0 ? `${a.flagged_references} flagged so far` : "In progress"}
                  </span>
                )}
              </div>
              <div className="muted">{timeAgo(a.updated_at)}</div>
            </button>
          ))}
        </div>
      )}

      {open ? <NewApplication samples={samples} onClose={() => setOpen(false)} /> : null}
    </main>
  );
}
