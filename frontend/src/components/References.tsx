import { ArrowUpRight, Check, ChevronDown, Loader2, Minus, PhoneCall, Plus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { AppConfig, ApplicationDetail, CallChannel, CallOut, ReferenceFindings, ReferenceOut, ReferenceScore } from "../api/types";
import { CHANNEL_LABEL, OUTCOME_LABEL, REF_STATUS_LABEL, countdown, duration, money } from "../lib/format";
import { type LiveCallState, mergedTranscript, useNow } from "../lib/hooks";
import { heldForPresenter } from "./Pipeline";

// --------------------------------------------------------------- channel

export function ChannelPicker({
  appId,
  config,
  channel,
  onChange,
}: {
  appId: string;
  config: AppConfig | null;
  channel: CallChannel;
  onChange: (c: CallChannel) => void;
}) {
  const twilioReady = Boolean(config?.calls.twilio_configured && config?.calls.public_base_url);
  return (
    <div className="stack gap-8 channel-picker">
      <div className="row gap-12">
        <span className="muted small">Calls go to</span>
        <div className="segmented" role="radiogroup">
          {(["simulated", "browser", "twilio"] as CallChannel[]).map((c) => (
            <button
              key={c}
              className={channel === c ? "on" : ""}
              disabled={c === "twilio" && !twilioReady}
              title={c === "twilio" && !twilioReady ? "Add Twilio credentials and PUBLIC_BASE_URL in .env" : undefined}
              onClick={() => {
                onChange(c);
                void api.setCallChannel(appId, c).catch(() => undefined);
              }}
            >
              {CHANNEL_LABEL[c]}
            </button>
          ))}
        </div>
      </div>
      {channel === "browser" ? (
        <p className="muted small hint">
          Answer as the reference from the{" "}
          <a href="/phone" target="_blank" rel="noreferrer">
            Phone page <ArrowUpRight size={13} />
          </a>{" "}
          in another window.
        </p>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------- verification

const BAND_LABEL: Record<ReferenceScore["band"], string> = {
  likely_genuine: "Likely genuine",
  unconfirmed: "Unconfirmed",
  likely_fake: "Likely fake",
};

function ScoreBlock({ score }: { score: ReferenceScore }) {
  return (
    <div className={`vscore band-${score.band}`}>
      <div className="vscore-top">
        <span className="vscore-num num">{score.score}</span>
        <span className="vscore-meta">
          <span className="vscore-band">{BAND_LABEL[score.band]}</span>
          <span className="vscore-label">Authenticity score</span>
        </span>
      </div>
      <div className="vscore-meter" aria-hidden>
        <span style={{ width: `${score.score}%` }} />
      </div>
    </div>
  );
}

function Factors({ score, startOpen }: { score: ReferenceScore; startOpen: boolean }) {
  const [open, setOpen] = useState(startOpen);
  const against = score.factors.filter((f) => f.weight < 0).length;
  const support = score.factors.length - against;
  if (score.factors.length === 0) return <p className="factors-empty">No outside evidence found for or against this reference.</p>;
  return (
    <div className="factors">
      <button className="factors-toggle" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <span>
          {against > 0 ? `${against} against` : ""}
          {against > 0 && support > 0 ? ", " : ""}
          {support > 0 ? `${support} in favor` : ""}
        </span>
        <ChevronDown size={15} className={open ? "flip" : ""} />
      </button>
      {open ? (
        <ul className="factor-list">
          {score.factors.map((f) => (
            <li key={f.code} className={`factor ${f.weight < 0 ? "against" : "for"}`}>
              <span className="factor-mark">{f.weight < 0 ? <Minus size={13} strokeWidth={2.6} /> : <Plus size={13} strokeWidth={2.6} />}</span>
              <span className="factor-body">
                <span className="factor-text">{f.label}</span>
                <span className="factor-source">{f.source}</span>
              </span>
              <span className={`factor-impact ${f.impact}`} title={`${f.impact} effect`}>
                <i />
                <i />
                <i />
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function VerificationCard({ r, upNext = false }: { r: ReferenceOut; upNext?: boolean }) {
  const rows = r.checks?.rows ?? [];
  const score = r.checks?.score;
  const signals = (r.checks?.signals ?? []).filter((s) => s.severity !== "info");
  return (
    <article id={`ref-${r.id}`} className={`vcard${r.flagged ? " flagged" : ""}`}>
      <div className="row gap-8">
        {r.checks ? (
          <span className={`pill ${r.flagged ? "lime" : "ink"}`}>{r.flagged ? "Flagged" : "Verified"}</span>
        ) : upNext ? (
          <span className="pill outline">Not checked yet</span>
        ) : (
          <span className="pill outline">
            <Loader2 size={12} className="spin" /> Checking
          </span>
        )}
        <span className="spacer" />
        {!r.flagged && r.checks ? <span className="vcard-status">{REF_STATUS_LABEL[r.status] ?? r.status}</span> : null}
      </div>
      <h3 className="vcard-name">{r.company}</h3>
      <p className="vcard-contact">
        {[r.contact_name, r.phone_display].filter(Boolean).join(", ")}
      </p>
      {score ? <ScoreBlock score={score} /> : null}
      <div className="vrows">
        {rows.map((row) => (
          <div key={row.label} className="vrow">
            <span className="vrow-label">{row.label}</span>
            <span className={`vrow-value${row.ok === false ? " bad" : ""}${row.ok === null ? " unknown" : ""}`}>{row.value}</span>
          </div>
        ))}
      </div>
      {score ? (
        <Factors score={score} startOpen={r.flagged} />
      ) : r.flagged && signals.length > 0 ? (
        <ul className="signals">
          {signals.map((s) => (
            <li key={s.rule}>{s.message}</li>
          ))}
        </ul>
      ) : null}
      {r.flagged ? <p className="vcard-note">Vouch will not call this reference.</p> : null}
    </article>
  );
}

// ------------------------------------------------------------------ calls

const LINE_LABEL: Record<CallChannel, string> = { twilio: "Real phone", simulated: "Simulated", browser: "Browser" };

const FINDING_ROWS: { key: keyof ReferenceFindings; label: string; fmt: (v: unknown) => string }[] = [
  { key: "customer_since", label: "Customer since", fmt: (v) => String(v) },
  { key: "terms", label: "Terms", fmt: (v) => String(v) },
  { key: "high_credit", label: "High credit", fmt: (v) => money(v as number) },
  { key: "current_balance", label: "Current balance", fmt: (v) => money(v as number) },
  { key: "past_due_amount", label: "Past due", fmt: (v) => ((v as number) === 0 ? "None" : money(v as number)) },
  { key: "avg_days_to_pay", label: "Days to pay", fmt: (v) => `${Math.round(v as number)} on average` },
  { key: "would_extend_more_credit", label: "Would extend more", fmt: (v) => (v ? "Yes" : "No") },
];

function Waveform({ live }: { live: boolean }) {
  const bars = [0.35, 0.6, 0.9, 0.5, 0.75, 1, 0.65, 0.4, 0.8, 0.95, 0.55, 0.3, 0.7, 0.9, 0.6, 0.45, 0.85, 0.7, 0.4];
  return (
    <span className={`wave${live ? " live" : ""}`} aria-hidden>
      {bars.map((h, i) => (
        <span key={i} style={{ height: `${Math.round(h * 22)}px`, animationDelay: `${(i % 7) * 0.11}s` }} />
      ))}
    </span>
  );
}

function Transcript({ call, live }: { call: CallOut; live?: LiveCallState }) {
  const turns = mergedTranscript(call.transcript, live);
  const draft = call.status !== "ended" ? live?.draft ?? "" : "";
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = box.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns.length, draft]);
  if (turns.length === 0 && !draft) {
    return (
      <div className="bubbles empty-bubbles" ref={box}>
        {call.status === "ringing" ? (
          <p className="muted-dark row gap-8">
            <PhoneCall size={15} /> Ringing {call.dialed_number ?? ""}
          </p>
        ) : call.status === "queued" ? (
          <p className="muted-dark">Dialing</p>
        ) : (
          <p className="muted-dark">{call.result?.summary ?? "No one picked up."}</p>
        )}
      </div>
    );
  }
  return (
    <div className="bubbles" ref={box}>
      {turns.map((t, i) =>
        t.voicemail ? (
          <div key={i} className="bubble machine">
            <span className="bubble-label">Voicemail greeting</span>
            {t.text}
          </div>
        ) : (
          <div key={i} className={`bubble ${t.role}${t.voicemail_message ? " left-message" : ""}`}>
            {t.voicemail_message ? <span className="bubble-label">Message left on voicemail</span> : null}
            {t.text}
            {t.interrupted ? <span className="interrupted"> (interrupted)</span> : null}
          </div>
        ),
      )}
      {draft ? <div className="bubble agent typing">{draft}</div> : null}
    </div>
  );
}

function Findings({ call, live }: { call: CallOut | undefined; live?: LiveCallState }) {
  const final = call?.status === "ended" && call.result?.outcome === "completed" ? call.result.findings : null;
  const findings: Partial<ReferenceFindings> = final ?? live?.findings ?? call?.live_findings ?? {};
  return (
    <div className="findings">
      <h4>Credit file</h4>
      <div className="frows">
        {FINDING_ROWS.map((row) => {
          const v = findings[row.key];
          const has = v !== null && v !== undefined && v !== "";
          return (
            <div key={row.key} className={`frow${has ? " has" : ""}`}>
              <span className="frow-label">{row.label}</span>
              <span className="frow-value num">{has ? row.fmt(v) : ""}</span>
              <span className="frow-check">{has ? <Check size={15} strokeWidth={2.6} /> : null}</span>
            </div>
          );
        })}
      </div>
      {call?.status === "ended" && call.result ? <p className="findings-summary">{call.result.summary}</p> : null}
    </div>
  );
}

export function CallPanel({
  r,
  live,
  channel,
  highlightCall,
  twilioReady,
}: {
  r: ReferenceOut;
  live: Record<string, LiveCallState>;
  channel: CallChannel;
  highlightCall?: string | null;
  twilioReady: boolean;
}) {
  const now = useNow(1000);
  // This reference's line: its own choice, else the application's. Kept locally so
  // the switch responds instantly; the server stores it for future calls.
  const [lineChoice, setLineChoice] = useState<CallChannel | null>(null);
  useEffect(() => setLineChoice(null), [r.call_channel]);
  const line: CallChannel = lineChoice ?? r.call_channel ?? channel;
  function changeLine(c: CallChannel) {
    setLineChoice(c);
    void api.setReferenceChannel(r.id, c === channel ? null : c).catch(() => undefined);
  }
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const calls = r.calls;
  const latest = calls[calls.length - 1];
  useEffect(() => {
    if (highlightCall && calls.some((c) => c.id === highlightCall)) setSelected(highlightCall);
  }, [highlightCall, calls]);
  const call = calls.find((c) => c.id === selected) ?? latest;
  const step = r.step;
  const retryIn = step?.status === "pending" && step.next_run_at ? (new Date(step.next_run_at).getTime() - now) / 1000 : null;
  const inProgress = call && call.status !== "ended";
  // A machine answered: the greeting came from voicemail, not a person.
  const isVoicemail = Boolean(call && mergedTranscript(call.transcript, live[call.id]).some((t) => t.voicemail));
  const canCallNow = step && (step.status === "pending" || (step.status === "done" && r.status !== "completed"));

  async function callNow() {
    setBusy(true);
    setError(null);
    try {
      await api.callNow(r.id, line);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <article id={`call-${r.id}`} className="callpanel">
      <header className="callpanel-head">
        <div>
          <h3>{r.company}</h3>
          <p className="muted small">{[r.contact_name, r.phone_display].filter(Boolean).join(", ")}</p>
          <div className="line-picker row gap-8">
            <span className="muted small">Line</span>
            <div className="segmented small" role="radiogroup" aria-label={`Line for ${r.company}`}>
              {(["twilio", "simulated", "browser"] as CallChannel[]).map((c) => (
                <button
                  key={c}
                  className={line === c ? "on" : ""}
                  disabled={(c === "twilio" && !twilioReady) || Boolean(inProgress)}
                  title={c === "twilio" && !twilioReady ? "Add Twilio credentials and PUBLIC_BASE_URL in .env" : undefined}
                  onClick={() => changeLine(c)}
                >
                  {LINE_LABEL[c]}
                </button>
              ))}
            </div>
          </div>
        </div>
        <span className="spacer" />
        {calls.length > 0 ? (
          <div className="attempts">
            {calls.map((c) => (
              <button key={c.id} className={c.id === call?.id ? "on" : ""} onClick={() => setSelected(c.id)}>
                Attempt {c.attempt}
                <span className="attempt-outcome">
                  {c.status === "ended" ? (c.outcome ? OUTCOME_LABEL[c.outcome] : "Ended") : c.status === "in_progress" ? "Live" : "Ringing"}
                </span>
              </button>
            ))}
          </div>
        ) : null}
      </header>

      {retryIn !== null || (canCallNow && !inProgress) || calls.length === 0 ? (
        <div className="retry-bar">
          <span>
            {retryIn !== null
              ? retryIn > 600
                ? `${step?.wait_reason ?? "Follow-up scheduled"}.` // a callback later today or another day: show when
                : `${(step?.wait_reason ?? "Waiting").replace(/, trying again$/, "")}. Calling again in ${countdown(retryIn)}.`
              : calls.length === 0
                ? "Waiting to call."
                : r.summary ?? REF_STATUS_LABEL[r.status]}
          </span>
          <span className="spacer" />
          {canCallNow ? (
            <button className="btn small lime" disabled={busy} onClick={callNow}>
              {busy ? <Loader2 size={14} className="spin" /> : <PhoneCall size={14} />}
              Call now
            </button>
          ) : null}
        </div>
      ) : null}
      {error ? <p className="error-text" style={{ marginBottom: 12 }}>{error}</p> : null}

      {call && call.status === "ended" && call.transcript.length === 0 ? (
        <div className="attempt-note">
          <span className="live-dot" />
          <span>
            Attempt {call.attempt}, {CHANNEL_LABEL[call.channel].toLowerCase()}: {call.result?.summary ?? "No one picked up."}
          </span>
        </div>
      ) : call ? (
        <div className="call-body">
          <div className="call-live">
            <div className="call-status">
              <span className={`live-dot${inProgress ? " on" : ""}`} />
              <span>
                {call.outcome === "voicemail" || (call.status !== "ended" && isVoicemail)
                  ? `${call.status === "ended" ? "Reached voicemail" : "Voicemail"}${call.started_at ? `, ${duration(call.started_at, call.status === "ended" ? call.ended_at : null)}` : ""}`
                  : call.status === "in_progress"
                  ? `Live call ${duration(call.started_at, null)}`
                  : call.status === "ringing"
                    ? "Ringing"
                    : call.status === "queued"
                      ? "Dialing"
                      : `Call ended${call.started_at ? `, ${duration(call.started_at, call.ended_at)}` : ""}`}
              </span>
              <span className="muted-dark">{CHANNEL_LABEL[call.channel]}</span>
              <span className="spacer" />
              <Waveform live={call.status === "in_progress"} />
            </div>
            <Transcript call={call} live={live[call.id]} />
          </div>
          <Findings call={call} live={live[call.id]} />
        </div>
      ) : null}
    </article>
  );
}

export default function ReferencesTab({
  app,
  config,
  live,
  highlightCall,
}: {
  app: ApplicationDetail;
  config: AppConfig | null;
  live: Record<string, LiveCallState>;
  highlightCall: string | null;
}) {
  // The server owns the choice; keep a local copy so the picker responds instantly.
  const [channel, setChannel] = useState<CallChannel>(app.call_channel);
  useEffect(() => {
    setChannel(app.call_channel);
  }, [app.call_channel]);
  const change = (c: CallChannel) => setChannel(c);
  const called = app.references.filter((r) => !r.flagged && r.checks);
  return (
    <div>
      <div className="section-head">
        <div>
          <h2>Reference checks</h2>
          <p className="sub">Every reference is checked against public records before Vouch calls it.</p>
        </div>
      </div>
      {app.references.length === 0 ? (
        <p className="muted">No trade references yet.</p>
      ) : (
        <div className="ref-grid">
          {app.references.map((r) => (
            <VerificationCard key={r.id} r={r} upNext={app.steps.some((s) => s.kind === "verify" && heldForPresenter(s))} />
          ))}
        </div>
      )}

      <div className="section">
        <div className="section-head">
          <div>
            <h2>Calls</h2>
            <p className="sub">Vouch says it is an AI on a recorded line, asks the standard questions, and fills in the credit file.</p>
          </div>
          <ChannelPicker appId={app.id} config={config} channel={channel} onChange={change} />
        </div>
        {called.length === 0 ? (
          <p className="muted">{app.references.some((r) => r.checks) ? "No references to call." : "Calls start once the records check finishes."}</p>
        ) : (
          <div className="stack gap-24">
            {called.map((r) => (
              <CallPanel
                key={r.id}
                r={r}
                live={live}
                channel={channel}
                highlightCall={highlightCall}
                twilioReady={Boolean(config?.calls.twilio_configured && config?.calls.public_base_url)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
