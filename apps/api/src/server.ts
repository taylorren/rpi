import cors from "@fastify/cors";
import Fastify from "fastify";
import { getMockEvents, getMockSnapshot, mockHistory } from "./mockData.js";

const server = Fastify({
  logger: true
});

await server.register(cors, {
  origin: true
});

server.get("/api/rpi/current", async () => getMockSnapshot());

server.get("/api/rpi/history", async () => ({
  scope: "global",
  points: mockHistory
}));

server.get("/api/events", async () => ({
  events: getMockEvents(),
  nextCursor: null
}));

server.get<{ Params: { eventId: string } }>("/api/events/:eventId", async (request, reply) => {
  const event = getMockEvents().find((item) => item.id === request.params.eventId);

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
  const events = getMockEvents();

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

const port = Number(process.env.PORT ?? 3000);
await server.listen({ port, host: "127.0.0.1" });
