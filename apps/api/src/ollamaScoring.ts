import type { EventCategory } from "@rpi/shared";

export interface ArticleForAnalysis {
  url: string;
  headline: string;
  excerpt: string;
  publisher: string;
  publishedAt: string;
}

export interface OllamaAssessment {
  url: string;
  category: EventCategory;
  sentiment: number;
  severity: number;
  scale: number;
  confidence: number;
  durationHours: number;
  rationale: string;
}

const categories = new Set<EventCategory>([
  "disaster", "conflict", "health", "economy", "environment",
  "science", "technology", "rights", "culture", "governance"
]);

const SYSTEM_PROMPT = `You are a cautious world-news impact analyst for the Renpin Index (RPI), an experimental index of positive or negative pressure on the world.

Analyze only the provided headline and excerpt. Do not invent facts. Assess the likely net world impact, not the publisher's tone. Return only valid JSON in this exact shape:
{"assessments":[{"url":"input URL","category":"one allowed category","sentiment":-1 to 1,"severity":0 to 1,"scale":0 to 1,"confidence":0 to 1,"durationHours":positive number,"rationale":"one concise factual sentence"}]}

Allowed categories: disaster, conflict, health, economy, environment, science, technology, rights, culture, governance.
Use sentiment 0 when the information has no clear RPI direction. Confidence describes confidence in the interpretation from this limited source text, not trust in the publisher.`;

export async function assessWithOllama(articles: ArticleForAnalysis[]): Promise<Map<string, OllamaAssessment>> {
  const apiKey = process.env.OLLAMA_API_KEY;
  if (!apiKey) throw new Error("OLLAMA_API_KEY is not configured.");

  const response = await fetch("https://ollama.com/api/chat", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      model: process.env.OLLAMA_MODEL ?? "gpt-oss:120b",
      stream: false,
      options: { temperature: 0 },
      messages: [
        { role: "system", content: SYSTEM_PROMPT },
        { role: "user", content: JSON.stringify({ articles }) }
      ]
    }),
    signal: AbortSignal.timeout(90_000)
  });

  if (!response.ok) throw new Error(`Ollama Cloud request failed (${response.status}).`);
  const body = await response.json() as { message?: { content?: string } };
  const content = body.message?.content;
  if (!content) throw new Error("Ollama Cloud returned no analysis content.");

  const parsed = JSON.parse(stripCodeFence(content)) as { assessments?: unknown[] };
  if (!Array.isArray(parsed.assessments)) throw new Error("Ollama Cloud returned an invalid assessment list.");

  const requestedUrls = new Set(articles.map((article) => article.url));
  const results = new Map<string, OllamaAssessment>();
  for (const value of parsed.assessments) {
    const assessment = validateAssessment(value);
    if (assessment && requestedUrls.has(assessment.url)) results.set(assessment.url, assessment);
  }
  return results;
}

function validateAssessment(value: unknown): OllamaAssessment | undefined {
  if (!value || typeof value !== "object") return undefined;
  const input = value as Record<string, unknown>;
  if (typeof input.url !== "string" || typeof input.category !== "string" || !categories.has(input.category as EventCategory)) return undefined;
  if (typeof input.rationale !== "string") return undefined;
  const numbers = [input.sentiment, input.severity, input.scale, input.confidence, input.durationHours];
  if (!numbers.every((number) => typeof number === "number" && Number.isFinite(number))) return undefined;
  return {
    url: input.url,
    category: input.category as EventCategory,
    sentiment: clamp(input.sentiment as number, -1, 1),
    severity: clamp(input.severity as number, 0, 1),
    scale: clamp(input.scale as number, 0, 1),
    confidence: clamp(input.confidence as number, 0, 1),
    durationHours: clamp(input.durationHours as number, 1, 24 * 365),
    rationale: input.rationale.trim().slice(0, 600)
  };
}

function stripCodeFence(value: string): string {
  return value.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}
