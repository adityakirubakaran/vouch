import { FileText, Loader2, Upload, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { SampleInfo } from "../api/types";

export function SampleList({ samples, onStarted }: { samples: SampleInfo[]; onStarted: (id: string) => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(id: string) {
    setBusy(id);
    setError(null);
    try {
      const res = await api.fromSample(id);
      onStarted(res.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(null);
    }
  }

  return (
    <div className="samples">
      {samples.map((s) => (
        <div key={s.id} className="sample">
          <div className="sample-icon">
            <FileText size={20} />
          </div>
          <div className="sample-body">
            <h3>{s.title}</h3>
            <p className="muted">{s.description}</p>
            <a className="link-btn" href={s.url} target="_blank" rel="noreferrer">
              View the scan
            </a>
          </div>
          <button className="btn lime" disabled={!s.available || busy !== null} onClick={() => run(s.id)}>
            {busy === s.id ? <Loader2 size={16} className="spin" /> : null}
            Run
          </button>
        </div>
      ))}
      {error ? <p className="error-text">{error}</p> : null}
    </div>
  );
}

export default function NewApplication({ samples, onClose }: { samples: SampleInfo[]; onClose: () => void }) {
  const navigate = useNavigate();
  const input = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function upload(file: File | undefined) {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      const res = await api.upload(file);
      navigate(`/applications/${res.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setUploading(false);
    }
  }

  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog" role="dialog" aria-label="New application">
        <div className="row" style={{ marginBottom: 20 }}>
          <h2>New application</h2>
          <span className="spacer" />
          <button className="btn ghost small" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>
        <div
          className={`dropzone${drag ? " drag" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            void upload(e.dataTransfer.files[0]);
          }}
          onClick={() => input.current?.click()}
        >
          {uploading ? <Loader2 size={24} className="spin" /> : <Upload size={24} />}
          <div>
            <strong>{uploading ? "Uploading" : "Drop a credit application here"}</strong>
            <p className="muted">PDF, PNG, or JPG. Scans and handwriting are fine.</p>
          </div>
          <input
            ref={input}
            type="file"
            accept=".pdf,.png,.jpg,.jpeg,.webp,application/pdf,image/*"
            hidden
            onChange={(e) => void upload(e.target.files?.[0])}
          />
        </div>
        {error ? <p className="error-text" style={{ marginTop: 12 }}>{error}</p> : null}
        <h3 style={{ margin: "28px 0 12px" }}>Or run a sample</h3>
        <SampleList samples={samples} onStarted={(id) => navigate(`/applications/${id}`)} />
      </div>
    </div>
  );
}
