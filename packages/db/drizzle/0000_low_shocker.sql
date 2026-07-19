CREATE TABLE `articles` (
	`id` text PRIMARY KEY NOT NULL,
	`source_id` text NOT NULL,
	`url` text NOT NULL,
	`external_id` text,
	`headline` text NOT NULL,
	`author` text,
	`published_at` text NOT NULL,
	`fetched_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL,
	`language` text NOT NULL,
	`raw_excerpt` text,
	`raw_content` text,
	`content_hash` text,
	`status` text DEFAULT 'new' NOT NULL,
	`failure_reason` text,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL,
	`updated_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL,
	FOREIGN KEY (`source_id`) REFERENCES `sources`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE INDEX `idx_articles_source_id` ON `articles` (`source_id`);--> statement-breakpoint
CREATE INDEX `idx_articles_published_at` ON `articles` (`published_at`);--> statement-breakpoint
CREATE INDEX `idx_articles_content_hash` ON `articles` (`content_hash`);--> statement-breakpoint
CREATE INDEX `idx_articles_status` ON `articles` (`status`);--> statement-breakpoint
CREATE TABLE `event_scores` (
	`id` text PRIMARY KEY NOT NULL,
	`event_id` text NOT NULL,
	`scoring_version` text NOT NULL,
	`model_name` text,
	`sentiment` real NOT NULL,
	`severity` real NOT NULL,
	`scale` real NOT NULL,
	`novelty` real NOT NULL,
	`confidence` real NOT NULL,
	`category_weight` real NOT NULL,
	`duration_weight` real NOT NULL,
	`impact_multiplier` real NOT NULL,
	`duration_hours` real NOT NULL,
	`decay_rate` real NOT NULL,
	`rpi_impact` real NOT NULL,
	`rationale` text NOT NULL,
	`uncertainty_notes` text,
	`raw_agent_output` text,
	`is_active` integer DEFAULT true NOT NULL,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL,
	FOREIGN KEY (`event_id`) REFERENCES `events`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE INDEX `idx_event_scores_event_id` ON `event_scores` (`event_id`);--> statement-breakpoint
CREATE INDEX `idx_event_scores_scoring_version` ON `event_scores` (`scoring_version`);--> statement-breakpoint
CREATE INDEX `idx_event_scores_is_active` ON `event_scores` (`is_active`);--> statement-breakpoint
CREATE INDEX `idx_event_scores_created_at` ON `event_scores` (`created_at`);--> statement-breakpoint
CREATE TABLE `event_sources` (
	`id` text PRIMARY KEY NOT NULL,
	`event_id` text NOT NULL,
	`article_id` text NOT NULL,
	`relation_type` text DEFAULT 'primary' NOT NULL,
	`confidence` real DEFAULT 0.5 NOT NULL,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL,
	FOREIGN KEY (`event_id`) REFERENCES `events`(`id`) ON UPDATE no action ON DELETE no action,
	FOREIGN KEY (`article_id`) REFERENCES `articles`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE INDEX `idx_event_sources_event_id` ON `event_sources` (`event_id`);--> statement-breakpoint
CREATE INDEX `idx_event_sources_article_id` ON `event_sources` (`article_id`);--> statement-breakpoint
CREATE UNIQUE INDEX `idx_event_sources_event_article` ON `event_sources` (`event_id`,`article_id`);--> statement-breakpoint
CREATE TABLE `events` (
	`id` text PRIMARY KEY NOT NULL,
	`title` text NOT NULL,
	`summary` text NOT NULL,
	`category` text NOT NULL,
	`location_name` text NOT NULL,
	`country` text,
	`region` text,
	`latitude` real,
	`longitude` real,
	`scope` text DEFAULT 'local' NOT NULL,
	`occurred_at` text NOT NULL,
	`detected_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL,
	`status` text DEFAULT 'emerging' NOT NULL,
	`source_count` integer DEFAULT 1 NOT NULL,
	`canonical_article_id` text,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL,
	`updated_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL
);
--> statement-breakpoint
CREATE INDEX `idx_events_category` ON `events` (`category`);--> statement-breakpoint
CREATE INDEX `idx_events_status` ON `events` (`status`);--> statement-breakpoint
CREATE INDEX `idx_events_occurred_at` ON `events` (`occurred_at`);--> statement-breakpoint
CREATE INDEX `idx_events_detected_at` ON `events` (`detected_at`);--> statement-breakpoint
CREATE INDEX `idx_events_scope` ON `events` (`scope`);--> statement-breakpoint
CREATE INDEX `idx_events_country` ON `events` (`country`);--> statement-breakpoint
CREATE TABLE `rpi_snapshot_events` (
	`id` text PRIMARY KEY NOT NULL,
	`snapshot_id` text NOT NULL,
	`event_id` text NOT NULL,
	`event_score_id` text NOT NULL,
	`current_impact` real NOT NULL,
	`hours_since_detected` real NOT NULL,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL,
	FOREIGN KEY (`snapshot_id`) REFERENCES `rpi_snapshots`(`id`) ON UPDATE no action ON DELETE no action,
	FOREIGN KEY (`event_id`) REFERENCES `events`(`id`) ON UPDATE no action ON DELETE no action,
	FOREIGN KEY (`event_score_id`) REFERENCES `event_scores`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE INDEX `idx_rpi_snapshot_events_snapshot_id` ON `rpi_snapshot_events` (`snapshot_id`);--> statement-breakpoint
CREATE INDEX `idx_rpi_snapshot_events_event_id` ON `rpi_snapshot_events` (`event_id`);--> statement-breakpoint
CREATE INDEX `idx_rpi_snapshot_events_event_score_id` ON `rpi_snapshot_events` (`event_score_id`);--> statement-breakpoint
CREATE TABLE `rpi_snapshots` (
	`id` text PRIMARY KEY NOT NULL,
	`snapshot_at` text NOT NULL,
	`index_scope` text DEFAULT 'global' NOT NULL,
	`baseline_rpi` real DEFAULT 1000 NOT NULL,
	`current_rpi` real NOT NULL,
	`change_since_previous` real DEFAULT 0 NOT NULL,
	`positive_impact_sum` real DEFAULT 0 NOT NULL,
	`negative_impact_sum` real DEFAULT 0 NOT NULL,
	`active_event_count` integer DEFAULT 0 NOT NULL,
	`calculation_version` text NOT NULL,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL
);
--> statement-breakpoint
CREATE INDEX `idx_rpi_snapshots_snapshot_at` ON `rpi_snapshots` (`snapshot_at`);--> statement-breakpoint
CREATE INDEX `idx_rpi_snapshots_index_scope` ON `rpi_snapshots` (`index_scope`);--> statement-breakpoint
CREATE UNIQUE INDEX `idx_rpi_snapshots_snapshot_scope` ON `rpi_snapshots` (`snapshot_at`,`index_scope`);--> statement-breakpoint
CREATE TABLE `sources` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`feed_url` text NOT NULL,
	`homepage_url` text NOT NULL,
	`source_type` text DEFAULT 'rss' NOT NULL,
	`category` text,
	`language` text NOT NULL,
	`country` text,
	`reliability_score` real DEFAULT 0.5 NOT NULL,
	`bias_note` text,
	`fetch_interval_minutes` integer DEFAULT 60 NOT NULL,
	`last_fetched_at` text,
	`is_active` integer DEFAULT true NOT NULL,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL,
	`updated_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL
);
--> statement-breakpoint
CREATE INDEX `idx_sources_is_active` ON `sources` (`is_active`);--> statement-breakpoint
CREATE INDEX `idx_sources_source_type` ON `sources` (`source_type`);--> statement-breakpoint
CREATE INDEX `idx_sources_last_fetched_at` ON `sources` (`last_fetched_at`);