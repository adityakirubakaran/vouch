import { Mic, MicOff, Phone as PhoneIcon, PhoneOff, Send, Volume2, VolumeX } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { RingingCall } from "../api/types";
import { countdown, duration } from "../lib/format";
import { useNow } from "../lib/hooks";

type Phase = "idle" | "incoming" | "in_call" | "ended";

interface Line {
  role: "agent" | "me";
  text: string;
}

// Minimal Web Speech API typings (not in lib.dom for all browsers).
interface SpeechRecognitionResultLike {
  isFinal: boolean;
  0: { transcript: string };
}
interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: ArrayLike<SpeechRecognitionResultLike>;
}
interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((e: SpeechRecognitionEventLike) => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error: string }) => void) | null;
}

function recognitionCtor(): (new () => SpeechRecognitionLike) | null {
  const w = window as unknown as Record<string, unknown>;
  return (w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null) as (new () => SpeechRecognitionLike) | null;
}

export default function Phone() {
  const now = useNow(1000);
  const [phase, setPhase] = useState<Phase>("idle");
  const [ringing, setRinging] = useState<RingingCall[]>([]);
  const [active, setActive] = useState<RingingCall | null>(null);
  const [lines, setLines] = useState<Line[]>([]);
  const [draft, setDraft] = useState("");
  const [speaking, setSpeaking] = useState(false);
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [text, setText] = useState("");
  const [speakerOn, setSpeakerOn] = useState(true);
  const [micOn, setMicOn] = useState(true);
  const [startedAt, setStartedAt] = useState<string | null>(null);
  const [declined, setDeclined] = useState<Set<string>>(new Set());

  const ws = useRef<WebSocket | null>(null);
  const rec = useRef<SpeechRecognitionLike | null>(null);
  const wantListen = useRef(false);
  const pendingSpeech = useRef(0);
  const sentenceBuf = useRef("");
  const agentLine = useRef("");
  const ending = useRef(false);
  const speakerRef = useRef(speakerOn);
  const micRef = useRef(micOn);
  speakerRef.current = speakerOn;
  micRef.current = micOn;
  const sttSupported = recognitionCtor() !== null;

  // ------------------------------------------------------------ polling

  useEffect(() => {
    if (phase !== "idle" && phase !== "incoming") return;
    let alive = true;
    const tick = () =>
      api
        .ringing()
        .then((calls) => {
          if (!alive) return;
          const open = calls.filter((c) => !declined.has(c.id));
          setRinging(open);
          setPhase((p) => (p === "idle" && open.length > 0 ? "incoming" : p === "incoming" && open.length === 0 ? "idle" : p));
          setActive((a) => (a && open.some((c) => c.id === a.id) ? a : open[0] ?? null));
        })
        .catch(() => undefined);
    void tick();
    const t = window.setInterval(tick, 1500);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [phase, declined]);

  // ----------------------------------------------------------- listening

  const stopListening = useCallback(() => {
    wantListen.current = false;
    setListening(false);
    setInterim("");
    try {
      rec.current?.stop();
    } catch {
      /* not started */
    }
  }, []);

  const startListening = useCallback(() => {
    if (!rec.current || !micRef.current || ending.current) return;
    wantListen.current = true;
    try {
      rec.current.start();
    } catch {
      /* already started */
    }
    setListening(true);
  }, []);

  // ------------------------------------------------------------ speaking

  const finishCall = useCallback(() => {
    stopListening();
    try {
      window.speechSynthesis?.cancel();
    } catch {
      /* ignore */
    }
    ws.current?.close();
    ws.current = null;
    setPhase("ended");
    window.setTimeout(() => {
      setPhase("idle");
      setLines([]);
      setDraft("");
      setStartedAt(null);
      setActive(null);
    }, 2500);
  }, [stopListening]);


  const maybeResume = useCallback(() => {
    if (pendingSpeech.current > 0) return;
    setSpeaking(false);
    if (ending.current) {
      finishCall();
      return;
    }
    startListening();
  }, [startListening, finishCall]);

  const speak = useCallback(
    (chunk: string) => {
      const clean = chunk.trim();
      if (!clean) return;
      if (!speakerRef.current || !("speechSynthesis" in window)) return;
      pendingSpeech.current += 1;
      setSpeaking(true);
      stopListening();
      const u = new SpeechSynthesisUtterance(clean);
      u.rate = 1.04;
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        pendingSpeech.current = Math.max(0, pendingSpeech.current - 1);
        maybeResume();
      };
      u.onend = finish;
      u.onerror = finish;
      // Some browsers never fire onend; don't get stuck.
      window.setTimeout(finish, 1500 + clean.split(" ").length * 450);
      window.speechSynthesis.speak(u);
    },
    [maybeResume, stopListening],
  );

  // ------------------------------------------------------------ the call
  const sendPrompt = useCallback(
    (said: string) => {
      const value = said.trim();
      if (!value || !ws.current || ws.current.readyState !== WebSocket.OPEN) return;
      if (pendingSpeech.current > 0) {
        window.speechSynthesis?.cancel();
        pendingSpeech.current = 0;
        setSpeaking(false);
        ws.current.send(JSON.stringify({ type: "interrupt", utteranceUntilInterrupt: agentLine.current }));
      }
      setLines((l) => [...l, { role: "me", text: value }]);
      ws.current.send(JSON.stringify({ type: "prompt", voicePrompt: value, lang: "en-US", last: true }));
    },
    [],
  );

  useEffect(() => {
    const Ctor = recognitionCtor();
    if (!Ctor) return;
    const r = new Ctor();
    r.continuous = true;
    r.interimResults = true;
    r.lang = "en-US";
    r.onresult = (e) => {
      let finalText = "";
      let interimText = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        if (res.isFinal) finalText += res[0].transcript;
        else interimText += res[0].transcript;
      }
      setInterim(interimText);
      if (finalText.trim()) sendPrompt(finalText);
    };
    r.onend = () => {
      if (wantListen.current) {
        try {
          r.start();
        } catch {
          /* ignore */
        }
      }
    };
    r.onerror = (e) => {
      if (["not-allowed", "service-not-allowed", "audio-capture", "network"].includes(e.error)) {
        wantListen.current = false;
        setListening(false);
        setMicOn(false);
      }
    };
    rec.current = r;
    return () => {
      wantListen.current = false;
      try {
        r.abort();
      } catch {
        /* ignore */
      }
    };
  }, [sendPrompt]);

  function answer(call: RingingCall) {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${proto}://${window.location.host}/ws/phone/${call.id}`);
    ws.current = socket;
    ending.current = false;
    sentenceBuf.current = "";
    agentLine.current = "";
    setLines([]);
    setDraft("");
    setActive(call);
    setPhase("in_call");
    setStartedAt(new Date().toISOString());
    socket.onopen = () => {
      socket.send(JSON.stringify({ type: "setup", callSid: `BROWSER-${call.id}`, customParameters: { call_id: call.id } }));
    };
    socket.onmessage = (msg) => {
      let data: { type: string; token?: string; last?: boolean };
      try {
        data = JSON.parse(msg.data);
      } catch {
        return;
      }
      if (data.type === "text") {
        const token = data.token ?? "";
        agentLine.current += token;
        sentenceBuf.current += token;
        setDraft(agentLine.current);
        const m = sentenceBuf.current.match(/^([\s\S]*[.?!])(\s[\s\S]*)?$/);
        if (m && !data.last) {
          speak(m[1]);
          sentenceBuf.current = m[2] ?? "";
        }
        if (data.last) {
          speak(sentenceBuf.current);
          sentenceBuf.current = "";
          const full = agentLine.current.trim();
          agentLine.current = "";
          setDraft("");
          if (full) setLines((l) => [...l, { role: "agent", text: full }]);
          if (pendingSpeech.current === 0) maybeResume();
        }
      } else if (data.type === "end") {
        ending.current = true;
        if (pendingSpeech.current === 0) finishCall();
      }
    };
    socket.onclose = () => {
      if (ws.current === socket) {
        ending.current = true;
        if (pendingSpeech.current === 0) finishCall();
      }
    };
  }

  function hangUp() {
    ending.current = true;
    finishCall();
  }

  // ------------------------------------------------------------------ ui

  const transcriptEnd = useRef<HTMLDivElement>(null);
  useEffect(() => {
    transcriptEnd.current?.scrollIntoView({ block: "end" });
  }, [lines.length, draft, interim]);

  const ringLeft = active?.ring_deadline ? (new Date(active.ring_deadline).getTime() - now) / 1000 : null;

  return (
    <main className="page phone-page">
      <div className="phone-intro">
        <h1>Reference phone</h1>
        <p className="muted">
          Keep this page open while Vouch runs with calls going to the browser phone. When it calls a reference, the call
          rings here and you answer as that business.
        </p>
      </div>

      <div className="phone">
        {phase === "idle" ? (
          <div className="phone-idle">
            <span className="phone-pulse" />
            <h2>Waiting for a call</h2>
            <p className="muted-dark">
              Open an application and send its calls to the browser phone. If no one answers here, Vouch tries again later.
            </p>
          </div>
        ) : null}

        {phase === "incoming" && active ? (
          <div className="phone-incoming">
            <p className="muted-dark">Incoming call to</p>
            <h2 className="phone-company">{active.reference_company}</h2>
            <p className="phone-caller">
              Vouch, calling for {active.seller}'s credit team about {active.applicant ?? "an applicant"}
            </p>
            {ringLeft !== null && ringLeft > 0 ? <p className="muted-dark num">Rings for {countdown(ringLeft)}</p> : null}
            <div className="phone-actions">
              <button
                className="round decline"
                aria-label="Decline"
                onClick={() => {
                  setDeclined((d) => new Set([...d, active.id]));
                  setPhase("idle");
                }}
              >
                <PhoneOff size={24} />
              </button>
              <button className="round answer" aria-label="Answer" onClick={() => answer(active)}>
                <PhoneIcon size={24} />
              </button>
            </div>
            {ringing.length > 1 ? (
              <div className="phone-others">
                <p className="muted-dark small">Also ringing</p>
                {ringing
                  .filter((c) => c.id !== active.id)
                  .map((c) => (
                    <button key={c.id} className="btn small on-dark outline" onClick={() => setActive(c)}>
                      {c.reference_company}
                    </button>
                  ))}
              </div>
            ) : null}
          </div>
        ) : null}

        {(phase === "in_call" || phase === "ended") && active ? (
          <div className="phone-call">
            <div className="phone-call-head">
              <div>
                <strong>{active.reference_company}</strong>
                <p className="muted-dark small num">
                  {phase === "ended" ? "Call ended" : `On a call with Vouch ${duration(startedAt, null)}`}
                </p>
              </div>
              <span className="spacer" />
              <button className="icon-btn" aria-label={speakerOn ? "Mute speaker" : "Unmute speaker"} onClick={() => setSpeakerOn((s) => !s)}>
                {speakerOn ? <Volume2 size={18} /> : <VolumeX size={18} />}
              </button>
              {sttSupported ? (
                <button
                  className="icon-btn"
                  aria-label={micOn ? "Mute microphone" : "Unmute microphone"}
                  onClick={() => {
                    const next = !micOn;
                    setMicOn(next);
                    micRef.current = next;
                    if (!next) stopListening();
                    else if (!speaking) startListening();
                  }}
                >
                  {micOn ? <Mic size={18} /> : <MicOff size={18} />}
                </button>
              ) : null}
            </div>
            <div className="phone-transcript">
              {lines.map((l, i) => (
                <div key={i} className={`pbubble ${l.role}`}>
                  {l.text}
                </div>
              ))}
              {draft ? <div className="pbubble agent">{draft}</div> : null}
              {interim ? <div className="pbubble me interim">{interim}</div> : null}
              <div ref={transcriptEnd} />
            </div>
            <p className="phone-state muted-dark small">
              {phase === "ended"
                ? "Thanks. Going back to waiting."
                : speaking
                  ? "Vouch is speaking"
                  : listening
                    ? "Listening. Answer out loud or type below."
                    : sttSupported
                      ? "Type your answer, or turn on the microphone."
                      : "This browser has no speech recognition, so type your answers."}
            </p>
            <form
              className="phone-input"
              onSubmit={(e) => {
                e.preventDefault();
                sendPrompt(text);
                setText("");
              }}
            >
              <input
                className="input"
                placeholder="Type what you would say"
                value={text}
                onChange={(e) => setText(e.target.value)}
                disabled={phase !== "in_call"}
                aria-label="Your answer"
              />
              <button className="btn lime" type="submit" disabled={phase !== "in_call" || !text.trim()} aria-label="Send">
                <Send size={16} />
              </button>
            </form>
            <button className="btn hangup" onClick={hangUp} disabled={phase !== "in_call"}>
              <PhoneOff size={16} /> Hang up
            </button>
          </div>
        ) : null}
      </div>
    </main>
  );
}
