import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { AppConfig, ApplicationDetail, LiveEvent, ReferenceFindings, TranscriptTurn } from "../api/types";

/** Re-render every `ms` milliseconds (for countdowns and timers). */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

let configCache: AppConfig | null = null;

export function useConfig(): AppConfig | null {
  const [config, setConfig] = useState<AppConfig | null>(configCache);
  useEffect(() => {
    let alive = true;
    api
      .config()
      .then((c) => {
        configCache = c;
        if (alive) setConfig(c);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);
  return config;
}

export interface LiveCallState {
  /** Agent words streamed since the last finished line. */
  draft: string;
  /** Turns received over the stream, used until the next fetch catches up. */
  turns: TranscriptTurn[];
  findings: ReferenceFindings | null;
}

export interface LiveApplication {
  app: ApplicationDetail | null;
  error: string | null;
  calls: Record<string, LiveCallState>;
  lastEvent: LiveEvent | null;
  refresh: () => void;
  setApp: (a: ApplicationDetail) => void;
}

/** Fetch an application and keep it current from the event stream. */
export function useLiveApplication(id: string): LiveApplication {
  const [app, setApp] = useState<ApplicationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [calls, setCalls] = useState<Record<string, LiveCallState>>({});
  const [lastEvent, setLastEvent] = useState<LiveEvent | null>(null);
  const timer = useRef<number | null>(null);
  const inflight = useRef(false);
  const again = useRef(false);

  const load = useCallback(async () => {
    if (inflight.current) {
      again.current = true;
      return;
    }
    inflight.current = true;
    try {
      const next = await api.application(id);
      setApp(next);
      setError(null);
      // Drop streamed turns the server now has.
      setCalls((prev) => {
        const out: Record<string, LiveCallState> = {};
        const allCalls = [...next.references, ...(next.bank ? [next.bank] : [])].flatMap((r) => r.calls);
        for (const [callId, state] of Object.entries(prev)) {
          const call = allCalls.find((c) => c.id === callId);
          const server = call?.transcript ?? [];
          const ended = call?.status === "ended";
          out[callId] = {
            draft: ended ? "" : state.draft,
            turns: state.turns.filter((t) => !inServer(server, t)),
            findings: state.findings,
          };
        }
        return out;
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      inflight.current = false;
      if (again.current) {
        again.current = false;
        void load();
      }
    }
  }, [id]);

  const schedule = useCallback(
    (delay = 250) => {
      if (timer.current !== null) return;
      timer.current = window.setTimeout(() => {
        timer.current = null;
        void load();
      }, delay);
    },
    [load],
  );

  useEffect(() => {
    setApp(null);
    setCalls({});
    void load();
    const es = new EventSource(`/api/stream?application_id=${encodeURIComponent(id)}`);
    es.onmessage = (msg) => {
      let ev: LiveEvent;
      try {
        ev = JSON.parse(msg.data) as LiveEvent;
      } catch {
        return;
      }
      const data = ev.data as Record<string, unknown>;
      const callId = typeof data.call_id === "string" ? data.call_id : null;
      if (ev.type === "call.token" && callId) {
        const token = String(data.token ?? "");
        setCalls((prev) => {
          const cur = prev[callId] ?? { draft: "", turns: [], findings: null };
          return { ...prev, [callId]: { ...cur, draft: cur.draft + token } };
        });
        return;
      }
      if (ev.type === "call.transcript" && callId) {
        const turn: TranscriptTurn = {
          role: data.role === "agent" ? "agent" : "reference",
          text: String(data.text ?? ""),
          ts: String(data.ts ?? ev.ts),
        };
        setCalls((prev) => {
          const cur = prev[callId] ?? { draft: "", turns: [], findings: null };
          return { ...prev, [callId]: { ...cur, draft: turn.role === "agent" ? "" : cur.draft, turns: [...cur.turns, turn] } };
        });
        schedule(400);
        return;
      }
      if (ev.type === "call.findings" && callId) {
        const findings = data.findings as ReferenceFindings;
        setCalls((prev) => {
          const cur = prev[callId] ?? { draft: "", turns: [], findings: null };
          return { ...prev, [callId]: { ...cur, findings } };
        });
        return;
      }
      setLastEvent(ev);
      schedule();
    };
    // Safety net in case an event is missed while the tab sleeps.
    const poll = window.setInterval(() => void load(), 15000);
    return () => {
      es.close();
      window.clearInterval(poll);
      if (timer.current !== null) {
        window.clearTimeout(timer.current);
        timer.current = null;
      }
    };
  }, [id, load, schedule]);

  return { app, error, calls, lastEvent, refresh: () => void load(), setApp };
}

function inServer(server: TranscriptTurn[], turn: TranscriptTurn): boolean {
  return server.slice(-8).some((t) => t.role === turn.role && t.text === turn.text);
}

/** Merge the server transcript with turns that only arrived over the stream. */
export function mergedTranscript(server: TranscriptTurn[], live?: LiveCallState): TranscriptTurn[] {
  if (!live || live.turns.length === 0) return server;
  return [...server, ...live.turns.filter((t) => !inServer(server, t))];
}
