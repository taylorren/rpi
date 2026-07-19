import { sqliteTable, text, integer, real, uniqueIndex, index } from "drizzle-orm/sqlite-core";
import { sql } from "drizzle-orm";

export const sources = sqliteTable(
  "sources",
  {
    id: text("id").primaryKey(),
    name: text("name").notNull(),
    feedUrl: text("feed_url").notNull(),
    homepageUrl: text("homepage_url").notNull(),
    sourceType: text("source_type").notNull().default("rss"),
    category: text("category"),
    language: text("language").notNull(),
    country: text("country"),
    reliabilityScore: real("reliability_score").notNull().default(0.5),
    biasNote: text("bias_note"),
    fetchIntervalMinutes: integer("fetch_interval_minutes").notNull().default(60),
    lastFetchedAt: text("last_fetched_at"),
    isActive: integer("is_active", { mode: "boolean" }).notNull().default(true),
    createdAt: text("created_at")
      .notNull()
      .default(sql`CURRENT_TIMESTAMP`),
    updatedAt: text("updated_at")
      .notNull()
      .default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [
    index("idx_sources_is_active").on(table.isActive),
    index("idx_sources_source_type").on(table.sourceType),
    index("idx_sources_last_fetched_at").on(table.lastFetchedAt),
  ]
);

export const articles = sqliteTable(
  "articles",
  {
    id: text("id").primaryKey(),
    sourceId: text("source_id")
      .notNull()
      .references(() => sources.id),
    url: text("url").notNull(),
    externalId: text("external_id"),
    headline: text("headline").notNull(),
    author: text("author"),
    publishedAt: text("published_at").notNull(),
    fetchedAt: text("fetched_at")
      .notNull()
      .default(sql`CURRENT_TIMESTAMP`),
    language: text("language").notNull(),
    rawExcerpt: text("raw_excerpt"),
    rawContent: text("raw_content"),
    contentHash: text("content_hash"),
    status: text("status").notNull().default("new"),
    failureReason: text("failure_reason"),
    createdAt: text("created_at")
      .notNull()
      .default(sql`CURRENT_TIMESTAMP`),
    updatedAt: text("updated_at")
      .notNull()
      .default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [
    index("idx_articles_source_id").on(table.sourceId),
    index("idx_articles_published_at").on(table.publishedAt),
    index("idx_articles_content_hash").on(table.contentHash),
    index("idx_articles_status").on(table.status),
  ]
);

export const events = sqliteTable(
  "events",
  {
    id: text("id").primaryKey(),
    title: text("title").notNull(),
    summary: text("summary").notNull(),
    category: text("category").notNull(),
    locationName: text("location_name").notNull(),
    country: text("country"),
    region: text("region"),
    latitude: real("latitude"),
    longitude: real("longitude"),
    scope: text("scope").notNull().default("local"),
    occurredAt: text("occurred_at").notNull(),
    detectedAt: text("detected_at")
      .notNull()
      .default(sql`CURRENT_TIMESTAMP`),
    status: text("status").notNull().default("emerging"),
    sourceCount: integer("source_count").notNull().default(1),
    canonicalArticleId: text("canonical_article_id"),
    createdAt: text("created_at")
      .notNull()
      .default(sql`CURRENT_TIMESTAMP`),
    updatedAt: text("updated_at")
      .notNull()
      .default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [
    index("idx_events_category").on(table.category),
    index("idx_events_status").on(table.status),
    index("idx_events_occurred_at").on(table.occurredAt),
    index("idx_events_detected_at").on(table.detectedAt),
    index("idx_events_scope").on(table.scope),
    index("idx_events_country").on(table.country),
  ]
);

export const eventSources = sqliteTable(
  "event_sources",
  {
    id: text("id").primaryKey(),
    eventId: text("event_id")
      .notNull()
      .references(() => events.id),
    articleId: text("article_id")
      .notNull()
      .references(() => articles.id),
    relationType: text("relation_type").notNull().default("primary"),
    confidence: real("confidence").notNull().default(0.5),
    createdAt: text("created_at")
      .notNull()
      .default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [
    index("idx_event_sources_event_id").on(table.eventId),
    index("idx_event_sources_article_id").on(table.articleId),
    uniqueIndex("idx_event_sources_event_article").on(table.eventId, table.articleId),
  ]
);

export const eventScores = sqliteTable(
  "event_scores",
  {
    id: text("id").primaryKey(),
    eventId: text("event_id")
      .notNull()
      .references(() => events.id),
    scoringVersion: text("scoring_version").notNull(),
    modelName: text("model_name"),
    sentiment: real("sentiment").notNull(),
    severity: real("severity").notNull(),
    scale: real("scale").notNull(),
    novelty: real("novelty").notNull(),
    confidence: real("confidence").notNull(),
    categoryWeight: real("category_weight").notNull(),
    durationWeight: real("duration_weight").notNull(),
    impactMultiplier: real("impact_multiplier").notNull(),
    durationHours: real("duration_hours").notNull(),
    decayRate: real("decay_rate").notNull(),
    rpiImpact: real("rpi_impact").notNull(),
    rationale: text("rationale").notNull(),
    uncertaintyNotes: text("uncertainty_notes"),
    rawAgentOutput: text("raw_agent_output"),
    isActive: integer("is_active", { mode: "boolean" }).notNull().default(true),
    createdAt: text("created_at")
      .notNull()
      .default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [
    index("idx_event_scores_event_id").on(table.eventId),
    index("idx_event_scores_scoring_version").on(table.scoringVersion),
    index("idx_event_scores_is_active").on(table.isActive),
    index("idx_event_scores_created_at").on(table.createdAt),
  ]
);

export const rpiSnapshots = sqliteTable(
  "rpi_snapshots",
  {
    id: text("id").primaryKey(),
    snapshotAt: text("snapshot_at").notNull(),
    indexScope: text("index_scope").notNull().default("global"),
    baselineRpi: real("baseline_rpi").notNull().default(1000),
    currentRpi: real("current_rpi").notNull(),
    changeSincePrevious: real("change_since_previous").notNull().default(0),
    positiveImpactSum: real("positive_impact_sum").notNull().default(0),
    negativeImpactSum: real("negative_impact_sum").notNull().default(0),
    activeEventCount: integer("active_event_count").notNull().default(0),
    calculationVersion: text("calculation_version").notNull(),
    createdAt: text("created_at")
      .notNull()
      .default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [
    index("idx_rpi_snapshots_snapshot_at").on(table.snapshotAt),
    index("idx_rpi_snapshots_index_scope").on(table.indexScope),
    uniqueIndex("idx_rpi_snapshots_snapshot_scope").on(table.snapshotAt, table.indexScope),
  ]
);

export const rpiSnapshotEvents = sqliteTable(
  "rpi_snapshot_events",
  {
    id: text("id").primaryKey(),
    snapshotId: text("snapshot_id")
      .notNull()
      .references(() => rpiSnapshots.id),
    eventId: text("event_id")
      .notNull()
      .references(() => events.id),
    eventScoreId: text("event_score_id")
      .notNull()
      .references(() => eventScores.id),
    currentImpact: real("current_impact").notNull(),
    hoursSinceDetected: real("hours_since_detected").notNull(),
    createdAt: text("created_at")
      .notNull()
      .default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [
    index("idx_rpi_snapshot_events_snapshot_id").on(table.snapshotId),
    index("idx_rpi_snapshot_events_event_id").on(table.eventId),
    index("idx_rpi_snapshot_events_event_score_id").on(table.eventScoreId),
  ]
);
