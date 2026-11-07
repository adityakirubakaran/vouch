import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ActivityEvent, LiveEvent } from "../api/types";
import { clock } from "../lib/format";

export default function ActivityTab({ appId, lastEvent }: { appId: string; lastEvent: LiveEvent | null }) {
  const [events, setEvents] = useState<ActivityEvent[] | null>(null);
  useEffect(() => {
    api
      .events(appId)
      .then(setEvents)
      .catch(() => setEvents([]));
  }, [appId, lastEvent]);
  if (events === null) return null;
  const shown = events.filter((e) => e.message);
  return (
    <div>
      <div className="section-head">
        <div>
          <h2>Activity</h2>
          <p className="sub">Everything Vouch did on this application, in order.</p>
        </div>
      </div>
      {shown.length === 0 ? (
        <p className="muted">Nothing yet.</p>
      ) : (
        <ol className="activity">
          {shown.map((e, i) => (
            <li key={e.id ?? i}>
              <span className="activity-time num">{clock(e.ts)}</span>
              <span>{e.message}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
