import { randomUUID } from "node:crypto";
import { db, desc, rpiSnapshots } from "@rpi/db";
import { fetchAllSources, type FetchedArticle } from "@rpi/ingestion";
import { calculateCurrentImpact, createEventScore } from "@rpi/scoring";
import { assessWithOllama, type OllamaAssessment } from "./ollamaScoring.js";
import type {
  EventCategory,
  RpiHistoryPoint,
  RpiSnapshot,
  ScoredEvent,
  SourceArticle
} from "@rpi/shared";

const BASELINE_RPI = 1000;
const CACHE_TTL_MS = 5 * 60_000;
const MAX_HISTORY_POINTS = 500;
const MAX_ARTICLES_PER_RUN = 40;
const CALCULATION_VERSION = "rpi-calc-v1";

interface LiveRpiData {
  events: ScoredEvent[];
  articles: FetchedArticle[];
  snapshot: RpiSnapshot;
  history: RpiHistoryPoint[];
  refreshedAt: string;
}

let cached: LiveRpiData | undefined;
let refreshInFlight: Promise<LiveRpiData> | undefined;

export function getCachedLiveRpiData(): LiveRpiData | undefined {
  return cached;
}

const negativeTerms = [
  "war", "attack", "strike", "killed", "death", "dead", "injured", "crisis", "famine",
  "earthquake", "flood", "wildfire", "hurricane", "disaster", "outbreak", "disease",
  "pandemic", "shooting", "explosion", "collapse", "sanction", "recession", "protest",
  "violence", "abuse", "pollution", "climate"
];

const positiveTerms = [
  "ceasefire", "peace", "agreement", "rescue", "aid", "relief", "approved", "approval",
  "breakthrough", "discovery", "treatment", "vaccine", "cure", "protected", "restored",
  "recovery", "renewable", "conservation", "wins", "freed", "released", "donation"
];

const categoryTerms: Array<[EventCategory, string[]]> = [
  ["conflict", ["war", "attack", "strike", "ceasefire", "military", "violence", "hostage"]],
  ["disaster", ["earthquake", "flood", "wildfire", "hurricane", "storm", "disaster", "eruption"]],
  ["health", ["health", "disease", "outbreak", "treatment", "vaccine", "hospital", "medical"]],
  ["environment", ["climate", "pollution", "emissions", "environment", "wildlife", "renewable"]],
  ["economy", ["economy", "recession", "inflation", "trade", "market", "tariff", "jobs"]],
  ["science", ["science", "research", "discovery", "space", "study"]],
  ["technology", ["technology", "artificial intelligence", "cyber", "software", "internet"]],
  ["rights", ["rights", "refugee", "migrant", "election", "justice", "freed", "arrested"]],
  ["governance", ["government", "policy", "law", "court", "minister", "president"]],
  ["culture", ["culture", "sport", "film", "music"]]
];

export async function getLiveRpiData(forceRefresh = false): Promise<LiveRpiData> {
  if (!forceRefresh && cached && Date.now() - new Date(cached.refreshedAt).getTime() < CACHE_TTL_MS) {
    return cached;
  }

  if (!refreshInFlight) {
    refreshInFlight = buildLiveRpiData().finally(() => {
      refreshInFlight = undefined;
    });
  }

  cached = await refreshInFlight;
  return cached;
}

async function buildLiveRpiData(): Promise<LiveRpiData> {
  const refreshedAt = new Date();
  const sourceResults = await fetchAllSources();
  const articles = Array.from(sourceResults.values())
    .flat()
    .sort((a, b) => publishedTime(b) - publishedTime(a));
  const uniqueArticles = deduplicate(articles).slice(0, MAX_ARTICLES_PER_RUN);
  const assessments = await getAssessments(uniqueArticles);
  const events = uniqueArticles.map((article) => assessArticle(article, refreshedAt, assessments.get(article.url)));
  const activeEvents = events.filter((event) => event.score.sentiment !== 0);
  const { snapshot, history } = await createSnapshot(activeEvents, refreshedAt);

  return {
    articles,
    events: activeEvents.sort((a, b) => Math.abs(b.currentImpact) - Math.abs(a.currentImpact)),
    snapshot,
    history,
    refreshedAt: refreshedAt.toISOString()
  };
}

function deduplicate(articles: FetchedArticle[]): FetchedArticle[] {
  const byHeadline = new Map<string, FetchedArticle>();
  for (const article of articles) {
    const key = article.headline.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    const existing = byHeadline.get(key);
    if (!existing || article.source.reliabilityScore > existing.source.reliabilityScore) {
      byHeadline.set(key, article);
    }
  }
  return [...byHeadline.values()].slice(0, 80);
}

async function getAssessments(articles: FetchedArticle[]): Promise<Map<string, OllamaAssessment>> {
  try {
    return await assessWithOllama(articles.map((article) => ({
      url: article.url,
      headline: article.headline,
      excerpt: stripHtml(article.excerpt),
      publisher: article.source.name,
      publishedAt: article.publishedAt
    })));
  } catch (error) {
    console.error("Ollama scoring failed; using deterministic fallback:", error);
    return new Map();
  }
}

function assessArticle(
  article: FetchedArticle,
  now: Date,
  modelAssessment?: OllamaAssessment
): ScoredEvent {
  const text = `${article.headline} ${stripHtml(article.excerpt)}`.toLowerCase();
  const negativeHits = countMatches(text, negativeTerms);
  const positiveHits = countMatches(text, positiveTerms);
  const fallbackSentiment = clamp((positiveHits - negativeHits) / Math.max(positiveHits + negativeHits, 1), -1, 1);
  const fallbackCategory = categoryTerms.find(([, terms]) => countMatches(text, terms) > 0)?.[0] ?? "governance";
  const fallbackSeverity = clamp(0.25 + Math.max(positiveHits, negativeHits) * 0.13 + severityBonus(text), 0.2, 0.95);
  const fallbackScale = /global|world|international|nationwide|millions|countries/.test(text) ? 0.8 :
    /national|region|state|city|thousands/.test(text) ? 0.55 : 0.35;
  const ageHours = Math.max(0, (now.getTime() - publishedTime(article)) / 3_600_000);
  const novelty = clamp(1 - ageHours / (7 * 24), 0.2, 0.95);
  const sentiment = modelAssessment?.sentiment ?? fallbackSentiment;
  const category = modelAssessment?.category ?? fallbackCategory;
  const severity = modelAssessment?.severity ?? fallbackSeverity;
  const scale = modelAssessment?.scale ?? fallbackScale;
  const durationHours = modelAssessment?.durationHours ?? durationFor(category, text);
  const source: SourceArticle = {
    publisher: article.source.name,
    headline: article.headline,
    url: article.url,
    publishedAt: new Date(publishedTime(article)).toISOString(),
    excerpt: stripHtml(article.excerpt),
    reliabilityScore: article.source.reliabilityScore,
    biasNote: article.source.biasNote ?? null
  };
  const id = `rss_${hash(article.url)}`;
  const score = createEventScore({
    eventId: id,
    category,
    sentiment,
    severity,
    scale,
    novelty,
    confidence: modelAssessment
      ? clamp(modelAssessment.confidence * article.source.reliabilityScore, 0, 1)
      : article.source.reliabilityScore,
    durationHours,
    rationale: modelAssessment?.rationale ?? rationale(sentiment, category, negativeHits, positiveHits),
    uncertaintyNotes: [modelAssessment
      ? "Assessed by an Ollama Cloud model from RSS headline and excerpt; not human-reviewed."
      : "Ollama Cloud scoring was unavailable; deterministic keyword fallback used."]
  });
  const detectedAt = new Date(publishedTime(article)).toISOString();
  const currentImpact = calculateCurrentImpact(score.rpiImpact, score.decayRate, ageHours);

  return {
    id,
    title: article.headline,
    summary: source.excerpt || "No RSS excerpt was provided by the publisher.",
    category,
    locationName: "Not extracted from RSS",
    scope: scale >= 0.8 ? "global" : scale >= 0.55 ? "regional" : "local",
    occurredAt: detectedAt,
    detectedAt,
    status: "emerging",
    sourceCount: 1,
    sources: [source],
    score,
    currentImpact: round(currentImpact)
  };
}

async function createSnapshot(
  events: ScoredEvent[],
  at: Date
): Promise<{ snapshot: RpiSnapshot; history: RpiHistoryPoint[] }> {
  const previous = await db
    .select()
    .from(rpiSnapshots)
    .orderBy(desc(rpiSnapshots.snapshotAt))
    .limit(1)
    .then((rows) => rows[0] ?? null);

  const impacts = events.map((event) => event.currentImpact);
  const positiveImpactSum = impacts.filter((impact) => impact > 0).reduce((sum, impact) => sum + impact, 0);
  const negativeImpactSum = impacts.filter((impact) => impact < 0).reduce((sum, impact) => sum + impact, 0);
  const currentPressure = positiveImpactSum + negativeImpactSum;
  const previousPressure = previous
    ? previous.positiveImpactSum + previous.negativeImpactSum
    : 0;
  const isFirstSnapshot = !previous;
  const changeSincePrevious = isFirstSnapshot ? 0 : currentPressure - previousPressure;
  const currentRpi = isFirstSnapshot
    ? BASELINE_RPI + currentPressure
    : previous.currentRpi + changeSincePrevious;

  const snapshot: RpiSnapshot = {
    scope: "global",
    currentRpi: round(currentRpi),
    changeSincePrevious: round(changeSincePrevious),
    snapshotAt: at.toISOString(),
    activeEventCount: events.length,
    positiveImpactSum: round(positiveImpactSum),
    negativeImpactSum: round(negativeImpactSum)
  };

  await db.insert(rpiSnapshots).values({
    id: randomUUID(),
    snapshotAt: snapshot.snapshotAt,
    indexScope: "global",
    baselineRpi: BASELINE_RPI,
    currentRpi: snapshot.currentRpi,
    changeSincePrevious: snapshot.changeSincePrevious,
    positiveImpactSum: snapshot.positiveImpactSum,
    negativeImpactSum: snapshot.negativeImpactSum,
    activeEventCount: snapshot.activeEventCount,
    calculationVersion: CALCULATION_VERSION
  });

  const historyRows = await db
    .select({
      snapshotAt: rpiSnapshots.snapshotAt,
      currentRpi: rpiSnapshots.currentRpi,
      changeSincePrevious: rpiSnapshots.changeSincePrevious
    })
    .from(rpiSnapshots)
    .orderBy(rpiSnapshots.snapshotAt);

  const history = historyRows.slice(-MAX_HISTORY_POINTS);

  return { snapshot, history };
}

function countMatches(text: string, terms: string[]): number {
  return terms.reduce((count, term) => count + (text.includes(term) ? 1 : 0), 0);
}

function severityBonus(text: string): number {
  return /mass|major|deadly|urgent|catastrophic|devastating|historic/.test(text) ? 0.2 : 0;
}

function durationFor(category: EventCategory, text: string): number {
  if (/war|climate|pandemic|recession/.test(text)) return 24 * 60;
  if (["conflict", "health", "environment", "economy"].includes(category)) return 24 * 14;
  return 24 * 3;
}

function rationale(sentiment: number, category: EventCategory, negativeHits: number, positiveHits: number): string {
  if (sentiment === 0) return "No directional RPI signal was detected in the available RSS headline and excerpt.";
  const direction = sentiment > 0 ? "positive" : "negative";
  const signals = sentiment > 0 ? positiveHits : negativeHits;
  return `Automated assessment found ${signals} ${direction} ${category} signal${signals === 1 ? "" : "s"} in the RSS headline and excerpt.`;
}

function publishedTime(article: FetchedArticle): number {
  const value = new Date(article.publishedAt).getTime();
  return Number.isFinite(value) ? value : Date.now();
}

function stripHtml(value: string): string {
  return value.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
}

function hash(value: string): string {
  let result = 0;
  for (let index = 0; index < value.length; index += 1) result = (result * 31 + value.charCodeAt(index)) | 0;
  return Math.abs(result).toString(36);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}
