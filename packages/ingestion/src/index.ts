import type { SourceArticle } from "@rpi/shared";
import RssParser from "rss-parser";

export interface NewsSource {
  name: string;
  feedUrl: string;
  homepageUrl: string;
  sourceType: "rss";
  language: string;
  reliabilityScore: number;
  biasNote?: string;
}

export interface FetchedArticle {
  source: NewsSource;
  url: string;
  headline: string;
  publishedAt: string;
  excerpt: string;
}

export const DEFAULT_SOURCES: NewsSource[] = [
  {
    name: "BBC World",
    feedUrl: "http://feeds.bbci.co.uk/news/world/rss.xml",
    homepageUrl: "https://www.bbc.com/news/world",
    sourceType: "rss",
    language: "en",
    reliabilityScore: 0.8,
    biasNote: "UK public broadcaster"
  },
  {
    name: "NPR World",
    feedUrl: "https://feeds.npr.org/1004/rss.xml",
    homepageUrl: "https://www.npr.org/sections/world/",
    sourceType: "rss",
    language: "en",
    reliabilityScore: 0.8,
    biasNote: "US public radio"
  },
  {
    name: "Al Jazeera",
    feedUrl: "https://www.aljazeera.com/xml/rss/all.xml",
    homepageUrl: "https://www.aljazeera.com",
    sourceType: "rss",
    language: "en",
    reliabilityScore: 0.75,
    biasNote: "Qatar-based, broader Middle East perspective"
  }
];

const parser = new RssParser({
  timeout: 15_000,
  headers: {
    "User-Agent": "RPI-Ingestion/0.1"
  }
});

export async function fetchSource(source: NewsSource): Promise<FetchedArticle[]> {
  const feed = await parser.parseURL(source.feedUrl);

  return (feed.items ?? [])
    .filter((item) => item.link && item.title)
    .map((item) => ({
      source,
      url: item.link!,
      headline: item.title!,
      publishedAt: item.pubDate ?? new Date().toISOString(),
      excerpt: item.contentSnippet ?? item.content ?? ""
    }));
}

export async function fetchAllSources(
  sources: NewsSource[] = DEFAULT_SOURCES
): Promise<Map<string, FetchedArticle[]>> {
  const results = new Map<string, FetchedArticle[]>();

  const fetches = sources.map(async (source) => {
    try {
      const articles = await fetchSource(source);
      results.set(source.name, articles);
    } catch (error) {
      console.error(`Failed to fetch ${source.name}:`, error);
      results.set(source.name, []);
    }
  });

  await Promise.allSettled(fetches);
  return results;
}

export function toSourceArticle(fetched: FetchedArticle): SourceArticle {
  return {
    publisher: fetched.source.name,
    headline: fetched.headline,
    url: fetched.url,
    publishedAt: fetched.publishedAt,
    excerpt: fetched.excerpt,
    reliabilityScore: fetched.source.reliabilityScore,
    biasNote: fetched.source.biasNote ?? null
  };
}
