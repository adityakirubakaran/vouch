import { ArrowLeft, ArrowRight, Loader2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { ApplicationDetail } from "../api/types";
import { Link, useParams } from "react-router-dom";
import ActivityTab from "../components/ActivityTab";
import ApplicationTab from "../components/ApplicationTab";
import BankTab from "../components/BankTab";
import DecisionCard from "../components/DecisionCard";
import Pipeline from "../components/Pipeline";
import ReferencesTab from "../components/References";
import StatusPill from "../components/StatusPill";
import { requestLine } from "../lib/format";
import { useConfig, useLiveApplication } from "../lib/hooks";

type Tab = "references" | "bank" | "application" | "activity";

/** Presenter mode: the demo waits at each stage until the presenter moves it on, by
 * clicking here or pressing the right arrow key, so it keeps pace with the voiceover. */
function PresenterNext({ app, onStarted }: { app: ApplicationDetail; onStarted: (what: string) => void }) {
  const [busy, setBusy] = useState(false);
  const paused = app.presenter?.paused && !busy;
  const go = useCallback(async () => {
    if (!app.presenter?.paused || busy) return;
    setBusy(true);
    try {
      const res = await api.advance(app.id);
      onStarted(res.started);
    } catch {
      /* already moving; the next refresh shows where it is */
    } finally {
      window.setTimeout(() => setBusy(false), 1200);
    }
  }, [app.id, app.presenter?.paused, busy, onStarted]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
      if (e.key === "ArrowRight" && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        void go();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go]);
  if (!paused || !app.presenter.next) return null;
  return (
    <button className="presenter-next" onClick={() => void go()} title="Or press the right arrow key">
      {app.presenter.next}
      <ArrowRight size={16} strokeWidth={2.4} />
    </button>
  );
}

export default function ApplicationPage() {
  const { id = "" } = useParams();
  const { app, error, calls, lastEvent, setApp } = useLiveApplication(id);
  const config = useConfig();
  const [tab, setTab] = useState<Tab>("references");
  const [highlightCall, setHighlightCall] = useState<string | null>(null);

  const jump = useCallback(
    (code: string) => {
      if (!app) return;
      const ev = app.evidence.find((e) => e.code === code);
      let target: Tab = "references";
      let elementId = "";
      const refMatch = code.match(/-ref-(\d+)$/);
      if (refMatch) {
        const ref = app.references.find((r) => r.position === Number(refMatch[1]) - 1);
        if (code.startsWith("call-") && ref) {
          elementId = `call-${ref.id}`;
          setHighlightCall(ev?.source_id ?? null);
        } else if (ref) {
          elementId = `ref-${ref.id}`;
        }
      } else if (code === "bank") {
        target = "bank";
        elementId = ev?.source_id ? `email-${ev.source_id}` : "bank";
      } else {
        target = "application";
        elementId = "application";
      }
      setTab(target);
      window.setTimeout(() => {
        const el = document.getElementById(elementId);
        if (!el) return;
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.remove("flash");
        void el.offsetWidth;
        el.classList.add("flash");
        window.setTimeout(() => el.classList.remove("flash"), 1700);
      }, 60);
    },
    [app],
  );

  if (error && !app) {
    return (
      <main className="page">
        <Link to="/" className="back">
          <ArrowLeft size={16} /> Applications
        </Link>
        <p className="error-text" style={{ marginTop: 24 }}>
          {error}
        </p>
      </main>
    );
  }
  if (!app) {
    return (
      <main className="page">
        <p className="muted row gap-8">
          <Loader2 size={16} className="spin" /> Loading
        </p>
      </main>
    );
  }

  const inProgress = ["received", "reading", "verifying", "checking_references", "deciding"].includes(app.status);
  const bankEmails = app.emails.filter((e) => e.reference_id === app.bank?.id).length;

  return (
    <main className="page">
      <Link to="/" className="back">
        <ArrowLeft size={16} /> Applications
      </Link>
      <div className="app-head">
        <div>
          <h1>{app.applicant_name ?? "Reading the application"}</h1>
          <p className="app-head-sub num">{requestLine(app.requested_limit, app.requested_terms)}</p>
        </div>
        <div className="app-head-side">
          {app.presenter?.paused ? <span className="pill outline">Paused</span> : <StatusPill status={app.status} />}
          {app.presenter?.enabled ? (
            <PresenterNext app={app} onStarted={(what) => setTab(what === "Email the bank" ? "bank" : "references")} />
          ) : null}
        </div>
      </div>
      {app.llm_mode === "fake" ? (
        <p className="offline-note">
          Offline mode: Claude's responses are scripted. Add ANTHROPIC_API_KEY to .env to read your own applications live.
        </p>
      ) : null}

      <Pipeline app={app} />

      {app.status === "failed" ? (
        <div className="card fail-card">
          <h3>Vouch could not finish this application</h3>
          <p className="muted">{app.error}</p>
        </div>
      ) : app.decision_detail ? (
        <DecisionCard app={app} onJump={jump} onUpdate={setApp} />
      ) : inProgress ? (
        <div className="card waiting-card">
          <Loader2 size={18} className="spin" />
          <p>The credit memo appears here once the reference calls and the bank reply are in.</p>
        </div>
      ) : null}

      <div className="tabs" role="tablist">
        {(
          [
            ["references", "References", app.reference_count],
            ["bank", "Bank", bankEmails || null],
            ["application", "Application", null],
            ["activity", "Activity", null],
          ] as [Tab, string, number | null][]
        ).map(([key, label, count]) => (
          <button key={key} role="tab" className={tab === key ? "on" : ""} onClick={() => setTab(key)}>
            {label}
            {count ? <span className="count">{count}</span> : null}
          </button>
        ))}
      </div>

      {tab === "references" ? <ReferencesTab app={app} config={config} live={calls} highlightCall={highlightCall} /> : null}
      {tab === "bank" ? <BankTab app={app} /> : null}
      {tab === "application" ? <ApplicationTab app={app} /> : null}
      {tab === "activity" ? <ActivityTab appId={app.id} lastEvent={lastEvent} /> : null}
    </main>
  );
}
