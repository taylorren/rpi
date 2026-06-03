<script setup lang="ts">
import { computed } from "vue";
import { events, history, snapshot } from "./mockData";

const positiveEvents = computed(() =>
  events.filter((event) => event.currentImpact > 0).sort((a, b) => b.currentImpact - a.currentImpact)
);

const negativeEvents = computed(() =>
  events.filter((event) => event.currentImpact < 0).sort((a, b) => a.currentImpact - b.currentImpact)
);

const chartPoints = computed(() => {
  const values = history.map((point) => point.currentRpi);
  const min = Math.min(...values) - 1;
  const max = Math.max(...values) + 1;

  return history
    .map((point, index) => {
      const x = (index / Math.max(history.length - 1, 1)) * 100;
      const y = 100 - ((point.currentRpi - min) / (max - min)) * 100;
      return `${x},${y}`;
    })
    .join(" ");
});

function formatChange(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}
</script>

<template>
  <main class="app-shell">
    <section class="top-bar">
      <div>
        <p class="eyebrow">Renpin Index</p>
        <h1>Global RPI</h1>
      </div>
      <div class="timestamp">Snapshot {{ snapshot.snapshotAt }}</div>
    </section>

    <section class="index-panel">
      <div class="index-value">
        <span>{{ snapshot.currentRpi.toFixed(2) }}</span>
        <strong :class="{ positive: snapshot.changeSincePrevious >= 0 }">
          {{ formatChange(snapshot.changeSincePrevious) }}
        </strong>
      </div>
      <div class="index-stats">
        <div>
          <span>Active events</span>
          <strong>{{ snapshot.activeEventCount }}</strong>
        </div>
        <div>
          <span>Positive pressure</span>
          <strong class="positive">{{ formatChange(snapshot.positiveImpactSum) }}</strong>
        </div>
        <div>
          <span>Negative pressure</span>
          <strong>{{ snapshot.negativeImpactSum.toFixed(2) }}</strong>
        </div>
      </div>
      <svg class="line-chart" viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="RPI history chart">
        <polyline :points="chartPoints" />
      </svg>
    </section>

    <section class="movement-grid">
      <div>
        <h2>Positive Movers</h2>
        <article v-for="event in positiveEvents" :key="event.id" class="event-row">
          <div>
            <h3>{{ event.title }}</h3>
            <p>{{ event.summary }}</p>
          </div>
          <strong class="positive">{{ formatChange(event.currentImpact) }}</strong>
        </article>
      </div>

      <div>
        <h2>Negative Movers</h2>
        <article v-for="event in negativeEvents" :key="event.id" class="event-row">
          <div>
            <h3>{{ event.title }}</h3>
            <p>{{ event.summary }}</p>
          </div>
          <strong>{{ event.currentImpact.toFixed(2) }}</strong>
        </article>
      </div>
    </section>

    <section class="timeline">
      <h2>Event Timeline</h2>
      <article v-for="event in events" :key="event.id" class="timeline-item">
        <div class="timeline-meta">
          <span>{{ event.category }}</span>
          <span>{{ event.status }}</span>
          <span>{{ event.sourceCount }} sources</span>
        </div>
        <h3>{{ event.title }}</h3>
        <p>{{ event.score.rationale }}</p>
      </article>
    </section>
  </main>
</template>
