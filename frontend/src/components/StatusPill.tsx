import type { AppStatus } from "../api/types";
import { STATUS_LABEL, statusTone } from "../lib/format";

export default function StatusPill({ status }: { status: AppStatus }) {
  const tone = statusTone(status);
  return (
    <span className={`pill ${tone.cls}`}>
      {tone.live ? <span className="dot live" /> : null}
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}
