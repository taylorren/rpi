import cors from "@fastify/cors";
import Fastify from "fastify";
import { DEFAULT_SOURCES, fetchAllSources, fetchSource } from "@rpi/ingestion";
import { getCachedLiveRpiData, getLiveRpiData } from "./liveData.js";

const server = Fastify({
  logger: true
});

await server.register(cors, {
  origin: true
});

const startupRefresh = getLiveRpiData(true);

async function getCurrentLiveData() {
  await startupRefresh;
  const data = getCachedLiveRpiData();
  if (!data) throw new Error("RPI data is not ready.");
  return data;
}

server.get("/api/rpi/current", async () => (await getCurrentLiveData()).snapshot);

server.get("/api/rpi/history", async () => ({
  scope: "global",
  points: (await getCurrentLiveData()).history,
  methodology: "Persisted index snapshots. The first snapshot starts from the 1000 baseline; each later value applies the change in active event pressure to the previous snapshot."
}));

server.get("/api/events", async () => ({
  events: (await getCurrentLiveData()).events,
  nextCursor: null
}));

server.get<{ Params: { eventId: string } }>("/api/events/:eventId", async (request, reply) => {
  const event = (await getCurrentLiveData()).events.find((item) => item.id === request.params.eventId);

  if (!event) {
    return reply.code(404).send({
      error: {
        code: "event_not_found",
        message: "Event not found."
      }
    });
  }

  return event;
});

server.get("/api/events/top-movers", async () => {
  const events = (await getCurrentLiveData()).events;

  return {
    positive: events
      .filter((event) => event.currentImpact > 0)
      .sort((a, b) => b.currentImpact - a.currentImpact)
      .slice(0, 5),
    negative: events
      .filter((event) => event.currentImpact < 0)
      .sort((a, b) => a.currentImpact - b.currentImpact)
      .slice(0, 5)
  };
});

server.get("/api/articles", async () => {
  const { articles, refreshedAt } = await getCurrentLiveData();

  return {
    count: articles.length,
    refreshedAt,
    articles: articles.map((a) => ({
      publisher: a.source.name,
      headline: a.headline,
      url: a.url,
      publishedAt: a.publishedAt,
      excerpt: a.excerpt
    }))
  };
});

server.post("/api/refresh", async () => {
  const data = await getLiveRpiData(true);
  return { refreshedAt: data.refreshedAt, articleCount: data.articles.length, eventCount: data.events.length };
});

server.get("/api/sources", async () => ({
  sources: DEFAULT_SOURCES.map((source) => ({
    name: source.name,
    homepageUrl: source.homepageUrl,
    sourceType: source.sourceType,
    language: source.language,
    reliabilityScore: source.reliabilityScore,
    biasNote: source.biasNote ?? null
  }))
}));

server.get("/api/ingestion/fetch", async (request, reply) => {
  const query = request.query as { source?: string };

  try {
    if (query.source) {
      const source = DEFAULT_SOURCES.find(
        (s) => s.name.toLowerCase() === query.source!.toLowerCase()
      );

      if (!source) {
        return reply.code(404).send({
          error: {
            code: "source_not_found",
            message: `Source "${query.source}" not found. Available: ${DEFAULT_SOURCES.map((s) => s.name).join(", ")}`
          }
        });
      }

      const articles = await fetchSource(source);
      return { source: source.name, count: articles.length, articles };
    }

    const allResults = await fetchAllSources();
    const summary = Array.from(allResults.entries()).map(([name, articles]) => ({
      source: name,
      count: articles.length
    }));
    const allArticles = Array.from(allResults.values()).flat();

    return {
      sources: summary,
      totalCount: allArticles.length,
      articles: allArticles
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return reply.code(500).send({
      error: {
        code: "ingestion_failed",
        message
      }
    });
  }
});

const port = Number(process.env.PORT ?? 3000);
await server.listen({ port, host: "127.0.0.1" });

setInterval(() => {
  void getLiveRpiData(true).catch((error) => server.log.error(error, "Scheduled RPI refresh failed"));
}, 30 * 60_000).unref();
