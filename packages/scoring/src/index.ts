import type { EventCategory, EventScore } from "@rpi/shared";

export const SCORING_VERSION = "rpi-score-v1";
export const DEFAULT_IMPACT_MULTIPLIER = 10;

export const CATEGORY_WEIGHTS: Record<EventCategory, number> = {
  disaster: 1.1,
  conflict: 1.2,
  health: 1.1,
  economy: 1,
  environment: 1.1,
  science: 0.9,
  technology: 0.9,
  rights: 1.1,
  culture: 0.7,
  governance: 1
};

export interface ScoreInput {
  eventId: string;
  category: EventCategory;
  sentiment: number;
  severity: number;
  scale: number;
  novelty: number;
  confidence: number;
  durationHours: number;
  rationale: string;
  uncertaintyNotes?: string[];
}

export function calculateDurationWeight(durationHours: number): number {
  return clamp(Math.log10(durationHours + 10) / 2, 0.5, 1.5);
}

export function calculateDecayRate(durationHours: number): number {
  return 1 / Math.max(durationHours, 1);
}

export function calculateCurrentImpact(
  rpiImpact: number,
  decayRate: number,
  hoursSinceDetected: number
): number {
  return rpiImpact * Math.exp(-decayRate * Math.max(hoursSinceDetected, 0));
}

export function createEventScore(input: ScoreInput): EventScore {
  const categoryWeight = CATEGORY_WEIGHTS[input.category];
  const durationWeight = calculateDurationWeight(input.durationHours);
  const decayRate = calculateDecayRate(input.durationHours);
  const rpiImpact =
    input.sentiment *
    input.severity *
    input.scale *
    input.novelty *
    input.confidence *
    categoryWeight *
    durationWeight *
    DEFAULT_IMPACT_MULTIPLIER;

  return {
    eventId: input.eventId,
    scoringVersion: SCORING_VERSION,
    category: input.category,
    sentiment: input.sentiment,
    severity: input.severity,
    scale: input.scale,
    novelty: input.novelty,
    confidence: input.confidence,
    categoryWeight,
    durationHours: input.durationHours,
    durationWeight,
    decayRate,
    impactMultiplier: DEFAULT_IMPACT_MULTIPLIER,
    rpiImpact: round(rpiImpact),
    rationale: input.rationale,
    uncertaintyNotes: input.uncertaintyNotes ?? []
  };
}

export function validateScore(score: EventScore): string[] {
  const errors: string[] = [];

  if (!isBetween(score.sentiment, -1, 1)) errors.push("sentiment must be between -1 and 1.");
  if (!isBetween(score.severity, 0, 1)) errors.push("severity must be between 0 and 1.");
  if (!isBetween(score.scale, 0, 1)) errors.push("scale must be between 0 and 1.");
  if (!isBetween(score.novelty, 0, 1)) errors.push("novelty must be between 0 and 1.");
  if (!isBetween(score.confidence, 0, 1)) errors.push("confidence must be between 0 and 1.");
  if (score.durationHours <= 0) errors.push("durationHours must be positive.");
  if (!score.rationale.trim()) errors.push("rationale is required.");

  return errors;
}

function isBetween(value: number, min: number, max: number): boolean {
  return Number.isFinite(value) && value >= min && value <= max;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}
