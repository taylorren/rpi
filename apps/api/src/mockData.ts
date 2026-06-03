import type { RpiHistoryPoint, RpiSnapshot, ScoredEvent } from "@rpi/shared";
import { calculateCurrentImpact, createEventScore } from "@rpi/scoring";

const detectedAt = "2026-06-03T01:35:00Z";

const events: ScoredEvent[] = [
  {
    id: "evt_earthquake",
    title: "Major earthquake affects coastal city",
    summary: "A strong earthquake caused infrastructure damage and emergency response activity.",
    category: "disaster",
    locationName: "Example City",
    country: "Example Country",
    scope: "regional",
    occurredAt: "2026-06-03T01:20:00Z",
    detectedAt,
    status: "confirmed",
    sourceCount: 5,
    sources: [
      {
        publisher: "Example News",
        headline: "Earthquake strikes coastal city",
        url: "https://example.com/earthquake",
        publishedAt: "2026-06-03T01:30:00Z",
        excerpt: "Emergency crews responded after a strong earthquake caused damage.",
        reliabilityScore: 0.8
      }
    ],
    score: createEventScore({
      eventId: "evt_earthquake",
      category: "disaster",
      sentiment: -0.9,
      severity: 0.8,
      scale: 0.6,
      novelty: 0.95,
      confidence: 0.84,
      durationHours: 168,
      rationale:
        "The event is negative because it caused harm, infrastructure damage, and emergency response needs.",
      uncertaintyNotes: ["Damage estimates may change as reporting develops."]
    }),
    currentImpact: 0
  },
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
    sources: [
      {
        publisher: "Medical Example",
        headline: "New treatment receives approval",
        url: "https://example.com/treatment",
        publishedAt: "2026-06-02T14:20:00Z",
        excerpt: "Regulators approved a treatment following strong trial results.",
        reliabilityScore: 0.86
      }
    ],
    score: createEventScore({
      eventId: "evt_treatment",
      category: "health",
      sentiment: 0.9,
      severity: 0.7,
      scale: 0.8,
      novelty: 0.95,
      confidence: 0.75,
      durationHours: 720,
      rationale:
        "The event is positive because it may improve health outcomes at broad scale.",
      uncertaintyNotes: ["Long-term access and affordability are not yet clear."]
    }),
    currentImpact: 0
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
    sources: [
      {
        publisher: "World Example",
        headline: "Temporary ceasefire announced",
        url: "https://example.com/ceasefire",
        publishedAt: "2026-06-03T00:15:00Z",
        excerpt: "International monitors are expected to observe the ceasefire.",
        reliabilityScore: 0.78
      }
    ],
    score: createEventScore({
      eventId: "evt_ceasefire",
      category: "conflict",
      sentiment: 0.65,
      severity: 0.7,
      scale: 0.55,
      novelty: 0.9,
      confidence: 0.62,
      durationHours: 240,
      rationale:
        "The event is positive because reduced violence improves safety, though the agreement is still early.",
      uncertaintyNotes: ["The ceasefire may not hold."]
    }),
    currentImpact: 0
  }
];

export function getMockEvents(now = new Date("2026-06-03T03:00:00Z")): ScoredEvent[] {
  return events.map((event) => {
    const hoursSinceDetected =
      (now.getTime() - new Date(event.detectedAt).getTime()) / 3_600_000;
    const currentImpact = calculateCurrentImpact(
      event.score.rpiImpact,
      event.score.decayRate,
      hoursSinceDetected
    );

    return {
      ...event,
      currentImpact: Math.round(currentImpact * 100) / 100
    };
  });
}

export function getMockSnapshot(): RpiSnapshot {
  const activeEvents = getMockEvents();
  const positiveImpactSum = activeEvents
    .filter((event) => event.currentImpact > 0)
    .reduce((sum, event) => sum + event.currentImpact, 0);
  const negativeImpactSum = activeEvents
    .filter((event) => event.currentImpact < 0)
    .reduce((sum, event) => sum + event.currentImpact, 0);
  const currentRpi = 1000 + positiveImpactSum + negativeImpactSum;

  return {
    scope: "global",
    currentRpi: Math.round(currentRpi * 100) / 100,
    changeSincePrevious: 1.86,
    snapshotAt: "2026-06-03T03:00:00Z",
    activeEventCount: activeEvents.length,
    positiveImpactSum: Math.round(positiveImpactSum * 100) / 100,
    negativeImpactSum: Math.round(negativeImpactSum * 100) / 100
  };
}

export const mockHistory: RpiHistoryPoint[] = [
  { snapshotAt: "2026-06-02T21:00:00Z", currentRpi: 998.9, changeSincePrevious: -1.1 },
  { snapshotAt: "2026-06-02T22:00:00Z", currentRpi: 999.4, changeSincePrevious: 0.5 },
  { snapshotAt: "2026-06-02T23:00:00Z", currentRpi: 1001.2, changeSincePrevious: 1.8 },
  { snapshotAt: "2026-06-03T00:00:00Z", currentRpi: 1002.1, changeSincePrevious: 0.9 },
  { snapshotAt: "2026-06-03T01:00:00Z", currentRpi: 999.7, changeSincePrevious: -2.4 },
  { snapshotAt: "2026-06-03T02:00:00Z", currentRpi: 1000.8, changeSincePrevious: 1.1 },
  { snapshotAt: "2026-06-03T03:00:00Z", currentRpi: 1001.86, changeSincePrevious: 1.06 }
];
