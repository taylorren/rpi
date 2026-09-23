# Renpin Index (RPI)

**How is the world going today, in the eyes of an AI?**

**Live: <https://rpi.go4pro.org>**

---

## What this is

A small robot reads the world news, forms an opinion about each story, and publishes a single number that moves like a stock index.

It is genuinely that simple in concept:

1. Every hour it collects the latest world news from four English-language news outlets.
2. For each story it asks a language model three questions: *was this good or bad, how
   much does it matter, and who does it affect?*
3. It combines those answers into one number per story.
4. It adds those numbers up — giving more weight to recent news than to older news — to
   produce the Renpin Index.

The index starts at **100**. A number above 100 means recent news has been better than usual; below 100 means worse. It moves by small amounts, like a real index.

Renpin (人品) roughly means *character* or *reputation* — the sense of "how are things
going for you". Applied to the world, it's a deliberate joke with a serious mechanism
behind it.

## What this is not

- **Not a measurement.** It is one AI's opinion, aggregated. See the disclaimer at the
  bottom of the site.
- **Not a forecast.** It describes news that has already happened.
- **Not advice** of any kind.

---

## The idea in one paragraph

Most of the news on any given day is bad, and it is bad in roughly the same proportions
every day. So the interesting question isn't "is today's news bad?" — it always is. The
interesting question is **"is today's news unusually bad, or unusually good, compared with normal?"** The index is built to answer that second question, which is why it can rise on
a day when the headlines are still grim.

---

## Step 1: scoring a single story

Each story gets three judgements from the model:

| Question                                      | Answer                                         | Scale                              |
| --------------------------------------------- | ---------------------------------------------- | ---------------------------------- |
| **Tone** — are the consequences good or bad?  | positive / neutral / negative                  | one of three                       |
| **Magnitude** — how big are the consequences? | a number                                       | 0 (nothing) to 10 (world-historic) |
| **Reach** — how many people are affected?     | global / major regions / minor regions / local | one of four                        |

Magnitude is asked *independently* of tone, on purpose. "How much does this matter?" is a different question from "is it good?", and a model answers two simple questions more reliably than one tangled one. It also means a huge disaster and a huge breakthrough are scored as equally significant, and only the tone separates them.

### Turning three answers into one number

The three answers are multiplied together into a **signed impact**:

$s = \text{tone} \times \text{magnitude} \times \text{reach weight}$

where tone is `+1`, `0` or `−1`, and the reach weights are:

| Reach         | Weight | Reasoning                         |
| ------------- | ------ | --------------------------------- |
| global        | 1.0    | affects everyone                  |
| major regions | 0.7    | affects a large part of the world |
| minor regions | 0.4    | affects a country or a region     |
| local         | 0.2    | affects one place                 |

The result runs from roughly **−10** (worst conceivable story) through **0** to **+10**.

### Three real examples

Taken from the live site, so you can check the arithmetic against today's news list:

| Story                                                | Tone     | Magnitude | Reach               | Signed impact |
| ---------------------------------------------------- | -------- | --------- | ------------------- | ------------- |
| US–Iran conflict fuels mounting tensions             | negative | 6.41      | major regions (0.7) | **−4.49**     |
| Jamaica hails King's decision on slavery reparations | positive | 6.82      | minor regions (0.4) | **+2.73**     |
| US to expand military presence in Greenland          | neutral  | 4.99      | minor regions (0.4) | **0.00**      |

Two things are worth noticing there.

First, magnitude is not a whole number. The model doesn't just pick "6" or "7" — it
produces a *probability for every level*, and we use the weighted average. That matters
because the model is often torn between, say, "notable" and "significant", and averaging preserves that uncertainty instead of throwing it away.

Second, look at the neutral story. It carries a magnitude of 4.99 — a genuinely
notable event — yet contributes **exactly zero**. Neutral news still exists, it still
counts as news, and it still pulls the overall mood back toward the middle. It just
doesn't push the index in either direction. Stories are not discarded for being neutral;
they are simply worth zero on the day.

---

## Step 2: from stories to a mood

If we simply averaged every story ever, the index would barely move and would never forget anything. Instead, **older news fades**. A story's influence halves roughly every 36
hours, which is the *half-life*: $S(t) = \frac{\sum_i s_i \cdot 2^{-(t - t_i)/36}}{\sum_i 2^{-(t - t_i)/36}}$

In words: a decay-weighted average of every recent story's signed impact, where a story from 36 hours ago counts half as much as one from now, and a story from three days ago counts about a quarter as much.

$S(t)$, pronounced "the mood", is the answer to *"how is the world doing right now?"* on
the same −10 to +10 scale. It is worth looking at on its own — it's the far-right column of the stats on the site.

If no news arrives at all, there is nothing to average, so the mood falls back to "normal"
and the index holds still. **Silence never moves the index.**

---

## Step 3: from mood to index

The final step is the subtle one, and it is what makes the index behave like an index
rather than a scoreboard.

The index does **not** use the mood directly. It uses the mood **compared with normal**:

$RPI_{\text{now}} = RPI_{\text{before}} \times \exp\left(k \times \frac{\Delta t}{1\ \text{day}} \times \frac{S(t) - b}{10}\right)$

| Symbol     | Value       | Meaning                                                    |
| ---------- | ----------- | ---------------------------------------------------------- |
| $k$        | 0.02        | sensitivity — how much a full-scale day moves the index    |
| $\Delta t$ | varies      | time since the last update                                 |
| $S(t)$     | varies      | the mood, −10 to +10                                       |
| $\tau$     | 36 h        | decay half-life — news older than this counts half as much |
| $b$        | 0 (for now) | the baseline: what counts as "normal news"                 |

### Reading it in plain English

- **If the mood is normal** — that is, $S(t)$ equals $b$ — the exponent is zero and the
  index does not move at all, no matter how much news there is.
- **If the mood is as good as it can possibly be** ($S(t) = +10$, an entire day of
  world-historic good news) the index rises about **2%** in a day.
- **A typical day** moves it by about **0.1%**, in whichever direction the news leans.

So the index measures **how unusual the news is**, not how bad. This is the whole design.

### Why divide by time?

$\Delta t$ is the gap since the previous update. Without it, the same news would produce a
bigger change if we checked more often — checking every 15 minutes would give a wildly different chart from checking every hour, and changing the schedule would silently rewrite history. Dividing by the elapsed time makes the result depend only on the news, not on how often we look. That is why the site can update hourly and the history stays comparable.

### Why the baseline $b$ exists

This is the least obvious part, and the most important.

News is not neutral. About **70%** of the stories the index reads are negative, and that
proportion is roughly constant from day to day. So the mood sits persistently below zero.
Over the first six days of data it averaged **−0.16**, and never wandered outside roughly
**±1.5**, even though the scale allows ±10. If $b$ were left at zero, the index would sink
steadily — around **0.03% a day, or about 11% a year, with no change whatsoever in the world.** Give it a few years and it would read as the apocalypse proceeding on a smooth schedule.

That is not a signal about the world. It's a property of what news *is*.

(A caution on that number: it is the average over a short sample, so treat it as an
indication of the size of the effect, not a fixed constant. A single unusually quiet or
alarming week moves it noticeably.)

The baseline corrects for it. $b$ is meant to be set to the average mood over a long
settling-in period, so that the index responds to news being *unusual* rather than to news being *news*.

> **Current status: $b$ is still 0, and the site says so in a banner on every page.** The index is therefore drifting slowly downward while the data accumulates, and the level should be treated as provisional.
> 
> **How long is "while the data accumulates"? Much longer than it looks.** The mood is a smoothed average with a half-life measured in days, so consecutive readings carry almost the same information. After six days of running, there are only about **four independent
> observations** to average — not the six hundred that the number of readings suggests.
> Pinning the mean mood tightly enough to make the correction worthwhile needs on the order of **a year** of history.
> 
> A quick estimate is worse than none. Calibrating from a fortnight would leave residual drift of roughly 20% a year, which is *larger* than the 11% bias it was meant to remove — so it would replace a known small error with a bigger unknown one. The rule is that a calibration is only worth applying once what it leaves behind is clearly smaller than what it removes. `python -m rpi.calibrate` reports where things stand and refuses to apply
> an estimate until it is worth applying.
> 
> Until then, read the **change** figures rather than the absolute level. The drift
> contributes only about 0.03% to a day's movement, which is small next to a typical day's real move, so the day-on-day readings are meaningful even while the level is not.

---

## Reading the chart

**The solid line** is the index. Green means it has risen over the period you've selected,
red means it has fallen.

**The dashed line** is the 24-hour average. News arrives in lumps, and a single dramatic
story can swing the index for a day or two. The average is the honest way to read the
trend; the solid line is the noise. When the two diverge sharply, you are looking at one
story talking, not the world changing.

**The bars at the bottom** are volume — how many stories were published in each period.
They are worth glancing at, because a quiet news day and a busy one mean different things for how much the line can be trusted. A big index move on low volume is a couple of stories; the same move on high volume is a real shift in the world's news.

**The stat row** summarises the current state: the level, the change over the selected
window and over 24 hours, the mood $S(t)$, the baseline, volume, how many stories have been analysed, how many duplicates were merged, and the balance of positive versus negative stories.

**The news list** under the chart shows every story behind the number, each linked to the original article, with the exact arithmetic that produced its contribution. This exists
because an index whose inputs you cannot inspect is not worth trusting. If a number looks wrong, the list is where you go to find out whether the world was strange or the AI was.

Stories covered by more than one outlet are shown once, with a badge showing how many reports were merged, and which other outlets carried it.

---

## Where the news comes from

Four English-language outlets, chosen to include both state-affiliated and independent
editorial voices:

| Source       |                                   |
| ------------ | --------------------------------- |
| CGTN         | China's international broadcaster |
| Al Jazeera   | Qatar-based                       |
| BBC          | UK public service                 |
| The Guardian | UK, independent                   |

This mix matters more than it might seem. When the index was built on a single source it read **70% positive**; with four sources it reads about **30% positive**. The first figure
was a fact about one newsroom's editorial choices, not about the world. That is the single strongest argument for using more than one source.

The same story is frequently covered by all four. Before scoring, the stories are grouped so that one event counts once, however many outlets reported it — and the grouping is done by asking the model whether two headlines describe the same real-world event, with the local text comparison used only to narrow down which pairs are worth asking about.

---

## Known limitations

Stated plainly, because the alternative is pretending.

- **The baseline is uncalibrated**, as described above. The level is provisional.
- **The model is wrong sometimes**, and confidently wrong on occasion. It has scored a story about renaming AI as the largest positive event of the day, and rated a military build-up as entirely neutral. It does better on major news than on unusual wording.
- **Magnitude is bunched.** Asked for 0–10, the model leans toward 5 for a lot of stories. Using the probability-weighted average rather than its single favourite answer recovers most of the lost resolution, but not all of it.
- **The editorial mix is still narrow.** Four English-language outlets, two of them
  state-affiliated, are not the world.
- **History is short.** The index only knows what those feeds currently publish — a few
  days, not years.
- **The uncertainty estimate stops looking back at 8 days.** The mood's correlation time
  is only searched that far, so if it were ever longer, the tool would read 8 days and
  report *less* uncertainty than is really there. Today it is about 16 hours, so the
  ceiling is not being reached.
- **Volume is a proxy for attention, not importance.** A story covered by seven outlets is not seven times as important, though it does enter the index once either way.

---

## Where to look next

| Document            | For                                                                |
| ------------------- | ------------------------------------------------------------------ |
| `REQUIREMENTS.md`   | what the project set out to do, and what was decided along the way |
| `deploy/README.md`  | how it runs: the server, the schedule, the alerts                  |
| `fetcher/README.md` | how news is collected and cleaned                                  |
| `API.md`            | the scoring service this depends on                                |

The scoring model runs locally and offline. No news text leaves the machine building the index.
