"use client";

import { useState } from "react";
import { alerts } from "@/lib/fixtures";
import { AlertCard } from "@/components/alerts/AlertCard";
import { AlertDetailDrawer } from "@/components/alerts/AlertDetailDrawer";
import type { ProcessedAlert } from "@/lib/types";

export default function AlertFeedPage() {
  const [selected, setSelected] = useState<ProcessedAlert | null>(null);

  const sorted = [...alerts].sort(
    (a, b) => new Date(b.emitted_at).getTime() - new Date(a.emitted_at).getTime()
  );

  return (
    <div>
      <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
        Alert Feed
      </h1>
      <p style={{ fontSize: "16px", color: "var(--cc-grey-three)", marginTop: "4px", marginBottom: "24px" }}>
        Real-time WARN / ERROR alerts, explained in plain English
      </p>
      <div>
        {sorted.map((alert) => (
          <AlertCard key={alert.alert_id} alert={alert} onOpen={setSelected} />
        ))}
      </div>
      <AlertDetailDrawer alert={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
