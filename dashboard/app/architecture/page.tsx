"use client";

import { PipelineDiagram } from "@/components/architecture/PipelineDiagram";

/**
 * The pipeline map: the simulated order-management architecture as an
 * interactive diagram.
 *
 * Reference material, not live data — it reads nothing from the backend and
 * persists nothing. What it documents is the topology the mock services imitate
 * (CLAUDE.md, "The simulated production system"), so an agent reading a journey
 * on the next page over can see where each service sits in the order's path.
 */
export default function ArchitecturePage() {
  return (
    <div>
      <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
        Architecture
      </h1>
      <p
        style={{
          fontSize: "16px",
          color: "var(--cc-grey-three)",
          marginTop: "4px",
          marginBottom: "24px",
        }}
      >
        The order pipeline end to end — hover a box for what it does, scroll to zoom, drag to pan
      </p>
      <PipelineDiagram />
    </div>
  );
}
