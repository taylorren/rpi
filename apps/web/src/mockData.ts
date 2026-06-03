import type { RpiHistoryPoint, RpiSnapshot, ScoredEvent } from "@rpi/shared";

export const snapshot: RpiSnapshot = {
  scope: "global",
  currentRpi: 1001.86,
  changeSincePrevious: 1.06,
  snapshotAt: "2026-06-03T03:00:00Z",
  activeEventCount: 3,
  positiveImpactSum: 5.94,
  negativeImpactSum: -4.08
};

export const history: RpiHistoryPoint[] = [
  { snapshotAt: "2026-06-02T21:00:00Z", currentRpi: 998.9, changeSincePrevious: -1.1 },
  { snapshotAt: "2026-06-02T22:00:00Z", currentRpi: 999.4, changeSincePrevious: 0.5 },
  { snapshotAt: "2026-06-02T23:00:00Z", currentRpi: 1001.2, changeSincePrevious: 1.8 },
  { snapshotAt: "2026-06-03T00:00:00Z", currentRpi: 1002.1, changeSincePrevious: 0.9 },
  { snapshotAt: "2026-06-03T01:00:00Z", currentRpi: 999.7, changeSincePrevious: -2.4 },
  { snapshotAt: "2026-06-03T02:00:00Z", currentRpi: 1000.8, changeSincePrevious: 1.1 },
  { snapshotAt: "2026-06-03T03:00:00Z", currentRpi: 1001.86, changeSincePrevious: 1.06 }
];

export const events: ScoredEvent[] = [
  {
    id: "evt_treatment",
    title: "Breakthrough treatment approved",
    summary: "A new treatment received approval after trials showed strong patient benefit.",
    category: "health",
    locationName: "Global",
    scope: "global",
    occurredAt: "2026-06-02T14:00:00Z",
    detectedAt: "2026-06-02T14:30:00Z",
    status: "confirmed",
    sourceCount: 4,
    currentImpact: 3.54,
    sources: [],
    score: {
      eventId: "evt_treatment",
      scoringVersion: "rpi-score-v1",
      category: "health",
      sentiment: 0.9,
      severity: 0.7,
      scale: 0.8,
      novelty: 0.95,
      confidence: 0.75,
      categoryWeight: 1.1,
      durationHours: 720,
      durationWeight: 1.36,
      decayRate: 0.00139,
      impactMultiplier: 10,
      rpiImpact: 5.38,
      rationale: "The event is positive because it may improve health outcomes at broad scale.",
      uncertaintyNotes: ["Long-term access and affordability are not yet clear."]
    }
  },
  {
    id: "evt_ceasefire",
    title: "Ceasefire agreement reduces regional violence",
    summary: "Opposing sides agreed to a temporary ceasefire with international monitoring.",
    category: "conflict",
    locationName: "Example Region",
    scope: "regional",
    occurredAt: "2026-06-03T00:00:00Z",
    detectedAt: "2026-06-03T00:20:00Z",
    status: "emerging",
    sourceCount: 3,
    currentImpact: 2.4,
    sources: [],
    score: {
      eventId: "evt_ceasefire",
      scoringVersion: "rpi-score-v1",
      category: "conflict",
      sentiment: 0.65,
      severity: 0.7,
      scale: 0.55,
      novelty: 0.9,
      confidence: 0.62,
      categoryWeight: 1.2,
      durationHours: 240,
      durationWeight: 1.25,
      decayRate: 0.00417,
      impactMultiplier: 10,
      rpiImpact: 2.63,
      rationale: "The event is positive because reduced violence improves safety, though the agreement is still early.",
      uncertaintyNotes: ["The ceasefire may not hold."]
    }
  },
  {
    id: "evt_earthquake",
    title: "Major earthquake affects coastal city",
    summary: "A strong earthquake caused infrastructure damage and emergency response activity.",
    category: "disaster",
    locationName: "Example City",
    country: "Example Country",
    scope: "regional",
    occurredAt: "2026-06-03T01:20:00Z",
    detectedAt: "2026-06-03T01:35:00Z",
    status: "confirmed",
    sourceCount: 5,
    currentImpact: -4.08,
    sources: [],
    score: {
      eventId: "evt_earthquake",
      scoringVersion: "rpi-score-v1",
      category: "disaster",
      sentiment: -0.9,
      severity: 0.8,
      scale: 0.6,
      novelty: 0.95,
      confidence: 0.84,
      categoryWeight: 1.1,
      durationHours: 168,
      durationWeight: 1.12,
      decayRate: 0.00595,
      impactMultiplier: 10,
      rpiImpact: -4.21,
      rationale: "The event is negative because it caused harm, infrastructure damage, and emergency response needs.",
      uncertaintyNotes: ["Damage estimates may change as reporting develops."]
    }
  }
];
