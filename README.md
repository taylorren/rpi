# Renpin Index (RPI)

**How is the world going today, in the eyes of an AI?**

**Live: <https://rpi.go4pro.org>**

---

## What this is

A small robot reads the world news, forms an opinion about each story, and publishes a single number that moves like a stock index.

It is genuinely that simple in concept:

1. Every hour it collects the latest world news from six English-language news outlets.
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

The index reads the mood **compared with normal**:

$RPI = 100 \times \exp\left(c \times (S(t) - b)\right)$

| Symbol | Value   | Meaning                                                    |
| ------ | ------- | ---------------------------------------------------------- |
| $c$    | 0.16    | sensitivity — how far a deviation moves the level          |
| $S(t)$ | varies  | the mood, −10 to +10                                       |
| $b$    | −0.3314 | the baseline: what counts as "normal news"                 |
| $\tau$ | 36 h    | decay half-life — news older than this counts half as much |

### Reading it in plain English

- **If the mood is normal** — that is, $S(t)$ equals $b$ — the level is exactly **100**, however much news there is.
- **A one-standard-deviation mood** (about 0.11 on the live corpus) is a move of about **1.7%**.
- **The mood at its observed extreme** (about ±0.3 from normal) puts the index about **5%** either side of 100.

So the index measures **how unusual the news is**, not how bad. This is the whole design.

### A thermometer, not an odometer

The level is a *reading*, not a running total. Nothing carries over from one update to the
next, so the index revisits a level whenever the news does, and a bad month shows as a low
reading rather than as a line that has slid away.

That is deliberate, and it is what makes a day's news visible. An index that *integrates*
the mood has to average it across hundreds of stories and then accumulate the result, and
the two together move too slowly to read: on the live corpus the integrated version covered
**0.44% in a month**, in one smooth arc with no days visible in it. The same news read as a
thermometer covers 3–4% in a week, and the days are in it.

What it gives up is memory — it cannot show that the world has been getting worse for a
month. The persistence of an event is carried by later reports about it instead, each
scored on its own day. That is the same rule the scoring follows: a warning or a forecast
has not changed anything yet, so it scores low, and if the storm kills, those reports
arrive and score on their own.

### Why the opening days are missing

The first snapshots are not published. Coverage builds up: the opening days of the live
corpus held one to fifteen scored stories a day, against one to two hundred once every
feed was running, and with a handful of stories in the decay window the mood is one or two
headlines rather than an average. A thermometer shows its input directly, so those days
would have drawn the level to 85 and 116 — a fact about coverage, not about the world.
`rpi.calculator` therefore starts the series at the first window holding at least
`MIN_WINDOW_ITEMS` stories, the same floor `rpi.calibrate` uses to trim its sample.

### Why the baseline $b$ exists

This is the least obvious part, and the most important.

News is not neutral. About **70%** of the stories the index reads are negative, and that
proportion is roughly constant from day to day. So the mood sits persistently below zero.
Over the first six days of data it averaged **−0.16**, and never wandered outside roughly
**±1.5**, even though the scale allows ±10. Left at zero, $b$ would hold the whole chart
below 100 — on the current series about **7% below**, every day of the year — so the index
would read "the world is worse than normal" with no change whatsoever in the world.

That is not a signal about the world. It's a property of what news *is*.

(A caution on that number: it is the average over a short sample, so treat it as an
indication of the size of the effect, not a fixed constant. The live series has averaged
between **−0.33** and **−0.52** depending on which weeks are included, and in the
thermometer that difference is a level about **3%** apart.)

The baseline corrects for it. $b$ is meant to be set to the average mood over a long
settling-in period, so that the index responds to news being *unusual* rather than to news being *news*.

> **Current status: $b$ is frozen at −0.3314** — the mean mood measured over the era in which all six sources were present, on the rubric in use, written into `rpi.config.json` once the estimate came inside its budget. The banner that said "$b$ is still 0" is gone, and the level is no longer provisional in the way it was. It is not permanent, but it is also not meant to be re-tuned to follow the news: a frozen $b$ cannot tell a darkening world from a hardening instrument, so re-applying it whenever the mood drifts would absorb the very movement the index exists to show. It should change only when the *instrument* changes — a source added or dropped, or the scoring model or schema replaced — and `tools/rescore_drift.py` is how the last of those is tested.
> 
> **How long is "while the data accumulates"? Shorter than it looks, and the target is a choice.** The mood is a smoothed average with a half-life measured in days, so consecutive readings carry almost the same information. Measured on the live series it decorrelates in about half a day, so a week of running buys roughly a dozen independent observations, however many readings the snapshot count suggests. How long it then takes is decided by how tightly you insist on pinning the mean, because the target is a drift budget rather than a statistical threshold: at the default of **0.10** in $S$ units — about ±7.6% a year of residual drift, against roughly 30% a year of bias removed on the current series — a few weeks of history is enough. Halving that target quadruples the wait, for drift that is already invisible on the chart. The banner quotes a projected date for it: read it as a range, because it is re-fitted as data arrives and moves while the sample is young (on the evidence so far, anywhere from late 2026 to late 2027).
> 
> The first days of the series are excluded from that estimate on purpose. Coverage builds up: the opening days carried one to fifteen scored stories a day against one to two hundred once everything was running, which makes $S(t)$ one or two headlines rather than an average. Those readings otherwise dominate the sample's spread and drag the projected date around; `python -m rpi.calibrate` prints how much history it dropped.
> 
> A quick estimate is worse than none. Calibrating from a day or two would leave residual drift of roughly 20% a year, which is comparable to the bias it was meant to remove — so it would replace a known error with an equally large unknown one. The rule is that a calibration is only worth applying once what it leaves behind is clearly smaller than what it removes. `python -m rpi.calibrate` reports where things stand, shows the residual drift each candidate target would leave, and refuses to apply
> an estimate until it is worth applying.
> 
> With $b$ set, read the level and the change figures directly. The structural bias is gone, and what is left is real: the scoring instrument has been verified deterministic and unchanged — **24 of 24 stored scores reproduced exactly**, including the first item ever scored — so the pipeline is a fixed function of the incoming news, and any movement in the index is movement in the news. Whether the *world* darkened or the *newsrooms* did is not something this instrument can see: it measures the news. And because the level is a reading rather than a running total, the uncertainty in $b$ is **bounded**: its own standard error (0.068) is a level offset of about **1.1%**, not a drift that accumulates.

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

Six English-language outlets, chosen to include both state-affiliated and independent
editorial voices. Both how much each one publishes and how it scores are measured, not
assumed:

| Source       |                                          | Share of stories | Mean signed impact |
| ------------ | ---------------------------------------- | ---------------- | ------------------ |
| The Guardian | UK, independent                          | 34%              | −1.04              |
| SCMP         | Hong Kong, Alibaba-owned                 | 25%              | −0.08              |
| Al Jazeera   | Qatar-based                              | 19%              | −0.90              |
| CGTN         | China's international broadcaster        | 11%              | **+0.76**          |
| NYT          | US                                       | 7%               | −1.11              |
| BBC          | UK public service                        | 5%               | −0.74              |

This mix matters more than it might seem. When the index was built on a single source it read **70% positive**; across the current six it reads about **30% positive**. The first figure
was a fact about one newsroom's editorial choices, not about the world. That is the single strongest argument for using more than one source.

The same argument cuts the other way once there are several. The outlets disagree sharply — their
mean signed impacts span 1.87 points, on a scale where the index responds to differences of about
0.2 — and their volumes are very uneven, with two of the six making up 59% of the corpus between
them. So **the mood depends on which outlet happens to publish most**: dropping The Guardian moves
$S(t)$ by about +26%, dropping SCMP by about −18%, either of which is larger than the entire
three-week drift the index has shown so far. That is a property of the instrument, not a fault in
the arithmetic, and it is why a change to the source list is a change to what the index *measures*
— and to what the baseline was calibrated against.

The same story is frequently covered by several of them. Before scoring, the stories are grouped so that one event counts once, however many outlets reported it — and the grouping is done by asking the model whether two headlines describe the same real-world event, with the local text comparison used only to narrow down which pairs are worth asking about.

---

## Known limitations

Stated plainly, because the alternative is pretending.

- **The baseline is uncalibrated**, as described above. The level is provisional.
- **The model is wrong sometimes**, and confidently wrong on occasion. It has scored a story about renaming AI as the largest positive event of the day, and rated a military build-up as entirely neutral. It does better on major news than on unusual wording.
- **Magnitude is bunched.** Asked for 0–10, the model leans toward 5 for a lot of stories. Using the probability-weighted average rather than its single favourite answer recovers most of the lost resolution, but not all of it.
- **The editorial mix is still narrow, and unevenly weighted.** Six English-language
  outlets, two of them state-affiliated, are not the world — and because two of them are
  59% of the corpus by volume, the index moves when an outlet's *publishing rate* changes,
  with no change in the world at all.
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
