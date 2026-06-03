export type EventCategory =
  | "disaster"
  | "conflict"
  | "health"
  | "economy"
  | "environment"
  | "science"
  | "technology"
  | "rights"
  | "culture"
  | "governance";

export type EventScope = "local" | "national" | "regional" | "global";

export type EventStatus =
  | "emerging"
  | "confirmed"
  | "updated"
  | "resolved"
  | "disputed";

export interface SourceArticle {
  publisher: string;
  headline: string;
  url: string;
  publishedAt: string;
  excerpt: string;
  reliabilityScore: number;
  biasNote?: string | null;
}

export interface RpiEvent {
  id: string;
  title: string;
  summary: string;
  category: EventCategory;
  locationName: string;
  country?: string;
  scope: EventScope;
  occurredAt: string;
  detectedAt: string;
  status: EventStatus;
  sourceCount: number;
}

export interface EventScore {
  eventId: string;
  scoringVersion: string;
  category: EventCategory;
  sentiment: number;
  severity: number;
  scale: number;
  novelty: number;
  confidence: number;
  categoryWeight: number;
  durationHours: number;
  durationWeight: number;
  decayRate: number;
  impactMultiplier: number;
  rpiImpact: number;
  rationale: string;
  uncertaintyNotes: string[];
}

export interface ScoredEvent extends RpiEvent {
  score: EventScore;
  currentImpact: number;
  sources: SourceArticle[];
}

export interface RpiSnapshot {
  scope: "global" | string;
  currentRpi: number;
  changeSincePrevious: number;
  snapshotAt: string;
  activeEventCount: number;
  positiveImpactSum: number;
  negativeImpactSum: number;
}

export interface RpiHistoryPoint {
  snapshotAt: string;
  currentRpi: number;
  changeSincePrevious: number;
}
