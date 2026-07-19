<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from "vue";
import * as echarts from "echarts";

interface EventItem {
  id: string;
  title: string;
  summary: string;
  category: string;
  status: string;
  sourceCount: number;
  currentImpact: number;
  score: { rationale: string };
  sources: Array<{ url: string }>;
}

interface Snapshot {
  currentRpi: number;
  changeSincePrevious: number;
  snapshotAt: string;
  activeEventCount: number;
  positiveImpactSum: number;
  negativeImpactSum: number;
}

interface HistoryPoint {
  snapshotAt: string;
  currentRpi: number;
}

const apiBaseUrl = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:3000";
const liveEvents = ref<EventItem[]>([]);
const history = ref<HistoryPoint[]>([]);
const snapshot = ref<Snapshot | null>(null);
const loading = ref(true);
const error = ref("");
const trendChartElement = ref<HTMLDivElement | null>(null);
let trendChart: echarts.ECharts | undefined;

onMounted(async () => {
  try {
    const [eventsRes, snapshotRes, historyRes] = await Promise.all([
      fetch(`${apiBaseUrl}/api/events`),
      fetch(`${apiBaseUrl}/api/rpi/current`),
      fetch(`${apiBaseUrl}/api/rpi/history`)
    ]);
    if (![eventsRes, snapshotRes, historyRes].every((response) => response.ok)) {
      throw new Error("The live RPI service is unavailable.");
    }
    const [eventsData, snapshotData, historyData] = await Promise.all([
      eventsRes.json(), snapshotRes.json(), historyRes.json()
    ]);
    liveEvents.value = eventsData.events ?? [];
    snapshot.value = snapshotData;
    history.value = historyData.points ?? [];
    await nextTick();
    renderTrendChart();
  } catch {
    error.value = "Live news could not be loaded. Start the API and try again.";
  } finally {
    loading.value = false;
  }
});

onUnmounted(() => {
  window.removeEventListener("resize", resizeTrendChart);
  trendChart?.dispose();
  trendChart = undefined;
});

const positiveEvents = computed(() =>
  liveEvents.value.filter((event) => event.currentImpact > 0).sort((a, b) => b.currentImpact - a.currentImpact)
);

const negativeEvents = computed(() =>
  liveEvents.value.filter((event) => event.currentImpact < 0).sort((a, b) => a.currentImpact - b.currentImpact)
);

function formatChange(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}

function sourceUrl(event: EventItem): string | undefined {
  return event.sources[0]?.url;
}

function formatSnapshotTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short"
  }).format(new Date(value));
}

function renderTrendChart(): void {
  if (!trendChartElement.value || history.value.length === 0) return;

  trendChart?.dispose();
  trendChart = echarts.init(trendChartElement.value, undefined, { renderer: "canvas" });
  trendChart.setOption({
    animationDuration: 650,
    grid: { top: 28, right: 20, bottom: 30, left: 48 },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#18212f",
      borderWidth: 0,
      padding: [10, 12],
      textStyle: { color: "#ffffff" },
      formatter: (params: Array<{ dataIndex: number }>) => {
        const point = history.value[params[0]?.dataIndex ?? 0];
        return point ? `${formatSnapshotTime(point.snapshotAt)}<br/><strong>RPI ${point.currentRpi.toFixed(2)}</strong>` : "";
      }
    },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: history.value.map((point) => new Date(point.snapshotAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })),
      axisLine: { lineStyle: { color: "#dce5eb" } },
      axisLabel: { color: "#607084", fontSize: 11, interval: 5 },
      axisTick: { show: false }
    },
    yAxis: {
      type: "value",
      scale: true,
      axisLabel: { color: "#607084", fontSize: 11, formatter: (value: number) => value.toFixed(0) },
      splitLine: { lineStyle: { color: "#edf2f5" } }
    },
    series: [{
      name: "RPI",
      type: "line",
      smooth: false,
      data: history.value.map((point) => point.currentRpi),
      symbol: "circle",
      symbolSize: 7,
      lineStyle: { color: "#246b88", width: 3 },
      itemStyle: { color: "#246b88", borderColor: "#ffffff", borderWidth: 2 },
      areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
        { offset: 0, color: "rgba(36, 107, 136, 0.28)" },
        { offset: 1, color: "rgba(36, 107, 136, 0.02)" }
      ]) }
    }]
  });
  window.addEventListener("resize", resizeTrendChart, { passive: true });
}

function resizeTrendChart(): void {
  trendChart?.resize();
}
</script>

<template>
  <main class="app-shell">
    <section class="top-bar">
      <div>
        <p class="eyebrow">Renpin Index</p>
        <h1>Global RPI</h1>
      </div>
      <div class="timestamp">{{ snapshot ? `Live snapshot ${snapshot.snapshotAt}` : "Loading live snapshot…" }}</div>
    </section>

    <section v-if="snapshot" class="index-panel">
      <div class="index-value">
        <span>{{ snapshot.currentRpi.toFixed(2) }}</span>
        <strong :class="{ positive: snapshot.changeSincePrevious >= 0 }">
          {{ formatChange(snapshot.changeSincePrevious) }}
        </strong>
        <small>change since previous snapshot</small>
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
      <div ref="trendChartElement" class="line-chart" role="img" aria-label="Interactive RPI history chart. Hover or click a point to see its time and RPI value." />
    </section>

    <p v-if="error" class="loading-text">{{ error }}</p>

    <section class="movement-grid">
      <div class="mover-panel positive-panel">
        <div class="mover-heading">
          <span class="mover-signal" aria-hidden="true">↑</span>
          <div>
            <p>Improving pressure</p>
            <h2>Positive Movers</h2>
          </div>
        </div>
        <article v-for="event in positiveEvents" :key="event.id" class="event-row">
          <div>
            <h3><a v-if="sourceUrl(event)" :href="sourceUrl(event)" target="_blank" rel="noopener">{{ event.title }}</a><template v-else>{{ event.title }}</template></h3>
            <p>{{ event.summary }}</p>
          </div>
          <strong class="positive">{{ formatChange(event.currentImpact) }}</strong>
        </article>
      </div>

      <div class="mover-panel negative-panel">
        <div class="mover-heading">
          <span class="mover-signal" aria-hidden="true">↓</span>
          <div>
            <p>Worsening pressure</p>
            <h2>Negative Movers</h2>
          </div>
        </div>
        <article v-for="event in negativeEvents" :key="event.id" class="event-row">
          <div>
            <h3><a v-if="sourceUrl(event)" :href="sourceUrl(event)" target="_blank" rel="noopener">{{ event.title }}</a><template v-else>{{ event.title }}</template></h3>
            <p>{{ event.summary }}</p>
          </div>
          <strong>{{ event.currentImpact.toFixed(2) }}</strong>
        </article>
      </div>
    </section>

    <footer class="site-footer">
      <p>Persisted RPI snapshots start at 1000 once; each later update applies new event pressure to the preceding index value.</p>
      <p><strong>Renpin Index (RPI)</strong> is an experimental, automated estimate based on RSS headlines and excerpts.</p>
      <p>It is not news reporting, investment advice, or a measure of any individual or group. Scores can be incomplete or wrong and should be checked against the linked source articles.</p>
      <p>© {{ new Date().getFullYear() }} RPI. Data attribution remains with the original publishers.</p>
    </footer>
  </main>
</template>
