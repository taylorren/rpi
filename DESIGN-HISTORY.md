# Design history

Why the index is the way it is, in the order the reasons arrived.

The README describes the design as it stands. This records how it got there: the
question that came up, what was measured to answer it, and what changed as a
result. Nothing here is a plan, and nothing is a guess - every number was
measured on the live corpus, and the tools that measured them are in `tools/`.

---

## 1. The projected freeze date kept moving

**Question.** `python -m rpi.calibrate` projected a date for freezing the
baseline `b`, and the date kept sliding: 5 October, then 7 October, then 18, 21,
23, 24, 29. Was the estimator broken, or was the world moving?

**Measurement.** `rpi.calibrate` reports one `tau_c` over the whole sample, so a
trend, a dominant story and genuine persistence all look the same.
`tools/window_dominance.py` was written to separate them: it re-measures `tau_c`
on trailing windows with each window's own mean removed, prints the polarity mix
beside it, and reports the whole sample with a straight line removed.

```
raw        tau 1.20 d  sd 0.2002  se 0.0763
detrended  tau 0.40 d  sd 0.1314  se 0.0288
trend      -0.0317/day = -0.222/week
```

**Finding.** The trend was 57% of the sample's variance and two thirds of the
measured correlation time. The date was moving because the estimator was reading
a drift as persistence. And no single story was doing it: the largest story held
0.2–0.3% of the decay weight, with about 800 of 2,400 stories effectively sharing
it.

**Change.** None yet - but the criterion was wrong. A date that re-fits itself
whenever the spread or the correlation time moves is not a countdown, and it was
being read as one.

---

## 2. The corpus was not the one the repository described

**Question.** While measuring the mix, the source list on the VPS turned out to
differ from the one in the repository.

**Measurement.** The two `fetcher/feeds.json` files differed by exactly two
booleans: `scmp` and `nyt-world` were enabled in production and disabled in the
repo. The VPS fetch log said `reading 6 feed(s)`, and the repo's own comment said
"Four sources". Measured on the live corpus:

| source | share of stories | mean signed impact |
| ------ | ---------------- | ------------------ |
| Guardian | 34% | −1.04 |
| SCMP | 25% | −0.08 |
| Al Jazeera | 19% | −0.90 |
| CGTN | 11% | **+0.76** |
| NYT | 7% | −1.11 |
| BBC | 5% | −0.74 |

**Finding.** Two outlets were 59% of the corpus, and the outlets disagree sharply
- their mean signed impacts span 1.87 points, on a scale where the index responds
to differences of about 0.2. Dropping The Guardian moves the mood by about +26%,
dropping SCMP by about −18%. Both are larger than the entire three-week drift the
index had shown.

**Change.** The source set was aligned to the six that actually run, the
divergence was recorded in `fetcher/feeds.json`, and the README now carries the
measured per-source table instead of a claim. This is also why the calibration
takes `--since 2026-09-23`: before that day the corpus had fewer sources, so `b`
would have been measured on a blend of two different instruments.

---

## 3. Freezing b

**Question.** What should `b` be, and how tight does the estimate have to be?

**Measurement.** The mood over the six-source era, and what the target standard
error is worth in index terms.

**Finding.** The criterion in the code was a *drift budget*: with `k = 0.02` an
error `d` in the mean mood drifts the index about `0.2 * d` percent per day, so a
standard error of 0.10 leaves at most ±7.6%/year against the ~30%/year the
correction removes. It had been 0.05, which buys ±3.7%/year for about four times
the wait.

**Change.** `TARGET_SE` became 0.10, the budget became a parameter
(`--target-se`), and `b` was frozen at the six-source mean. The rule that came out
of section 4 - never re-tune `b` to follow the news - was written into the code
and the README at the same time.

---

## 4. Is the instrument drifting? (No.)

**Question.** The mood had darkened over three weeks: the negative share of
stories rose from 61% to 70% and the mean mood fell from −0.42 to −0.67. With `b`
frozen, every change in how negative the news *sounds* lands on the index as a
change in how the world *is*. So: is the world darkening, or the scorer?

**Measurement.** `tools/rescore_drift.py` re-scores stored text and compares the
result with the score stored when the item was first analysed. The text is fixed,
so the difference carries no news - it is the instrument, measured. The sample is
stratified by *when each item was scored*, because an instrument that hardened
over time would shift the early items further than the recent ones, and it is
restricted to the six-source era, because mixing feed mixes would confound "the
model changed" with "the corpus changed".

```
band                                n     stored  re-scored     delta   flips    repeat
early (2026-09-23 .. 2026-09-30)   12    -0.8004    -0.8004   +0.0000   0/12     0.0000
late  (2026-10-01 .. 2026-10-08)   12    -0.8835    -0.8835   +0.0000   0/12     0.0000
```

**Finding.** Every stored score was reproduced exactly, repeat noise included, and
a single-item cross-check reproduced the very first item ever scored
(`negative 6.4115824719825705`, sixteen days later, to thirteen decimal places).
The scorer is deterministic - one forward pass over the candidate answer tokens,
no sampling - and unchanged. So the pipeline is a fixed function of the incoming
news, and the darkening is in the news, not in the instrument.

**Change.** The "re-calibrate weekly" advice was wrong and was removed. A frozen
`b` cannot tell a darkening world from a hardening instrument, so re-applying it to
follow the drift would absorb the very movement the index exists to show. `b`
should change only when the *instrument* changes, and `tools/rescore_drift.py` is
how the last of those is tested.

---

## 5. What the index is for, stated as a rule

**Question.** What does a story's *persistence* mean, and should the index model
it?

**Statement.** A news item's lasting influence is carried by *later* news about the
same thing. A discovery is followed by manufacturers acting on it; a war is
followed by more reporting about it. Detecting that relatedness ourselves would be
both unnecessary and presumptuous - the world works in ways we do not model. So
the index should reflect what a report changed **on the day it appeared**, and
trust the follow-on reports to carry the rest, each scored on its own day.

**Consequences.** Three things follow, and all three were then built:

* the score is what this report changed, as of this report - not the topic it
  discusses (an opinion piece changes nothing) and not the situation it refers
  back to (a condemnation is scored as a condemnation, because the attacks it
  condemns were their own earlier reports);
* a warning or a forecast has not changed anything yet, and if the storm kills,
  those reports will arrive and score on their own;
* the decay kernel is not a model of persistence to be tuned. It is a *default*
  persistence for stories that nothing follows up on. Removing it does not
  delegate persistence to the news, it grants every story permanent influence -
  see "What was rejected".

---

## 6. Four impact rubrics

**Question.** The magnitude scale was not discriminating. Could the anchors fix
it?

**Diagnosis.** From the stored probability distributions and the impact histogram
over 2,957 stories:

```
 2   16.5%      6    2.2%
 3   26.3%      7    0.8%
 4   38.8%      8    0.2%
 5   14.7%      9    0.0%
                10   0.0%
```

96.8% of stories sat in 2–5, the model's entropy over the eleven levels was 1.40
bits - about 2.6 effective levels - and nothing had ever reached 10. The scale was
*bimodal*, not merely compressed: about 1% of stories sat at 7–9.5 while level 6,
the middle, held 2.2%. The v1 anchors mixed three dimensions into one scale:
reach ("affects few people", "changes conditions for many", "sustained regional
impact"), media attention ("widely reported" - the strongest attractor for news of
any kind, and the reason level 5 held a third of the model's probability mass),
and severity ("severe", "grave", "world-historic"). Reach is already asked
separately as `scope`.

**Measurement.** `tools/anchor_ab.py` scores the same stored text under two
rubrics, so the comparison has no news in it. Two 100-story samples: one evenly
spaced across the era, one of the 100 highest-impact stories.

| rubric | body sd | tail sd | tail levels | failure mode |
| ------ | ------- | ------- | ----------- | ------------ |
| v1 (in use) | 0.99 | 0.71 | 4 | 67 of 100 top stories crammed on level 6 |
| v2 | 1.03 | 1.47 | 9 | judged the transience of the reported act |
| v3 | 0.97 | 1.25 | 8 | promoted human-interest features |
| **v4** | **1.28** | **1.52** | **10** | none observed |

**Finding.** Wording can roughly double the spread of the *tail* - and v4, which
states the rule from section 5 plainly, was the first to move the *body* of the
distribution at all (sd 0.99 to 1.28). It also demoted exactly the right things:
an opinion piece (−4.64), a call for a ceasefire (−4.70), a storm forecast
(−4.52), a lettuce recall (−4.54).

**A trap worth recording.** A 24-story sample had suggested the spread doubled. At
100 stories the body was unchanged. Use `--limit 100` for decisions; the "biggest
moves" window is where a rubric change bites, and it is five items wide.

**A coupling worth recording.** A rubric change is not isolated: scope answers
moved on 14–15 of 100 stories in every sample, because every field shares one
prompt. The schema is one instrument, not three.

**Change.** v4 became the rubric in use, the retired one is kept as
`IMPACT_ANCHORS_PREVIOUS` so the comparison stays auditable, and `SCHEMA_VERSION`
went to 2. Every stored score became incomparable, so the corpus was re-scored.

---

## 7. Re-scoring 3,095 stories

**Question.** The whole corpus had to be scored again under the new rubric. At
about 1.9 seconds a story that is a hundred minutes, which is longer than the gap
between scheduled pipeline runs.

**Approach.** Newest first, in date-sized windows (`--newest-first`, `--since`,
`--until`), so the recent end - the part anyone is looking at - is finished first
and the older end fills in behind it. Both orders select the same items, so an
interruption loses nothing either way, and the windows cannot overlap.

The scheduled task was disabled for the duration. Its analyse step has no limit
and starts at the *oldest* end, so it would have contended for the same
serialised scorer while working in the opposite direction. It was re-enabled
afterwards; the next run pulls the backlog, which is delayed but never lost - the
fetcher on the VPS keeps writing inbox files, and the pull transfers by size and
mtime.

**Result.** 3,095 stories, 0 errors. The baseline was re-measured on the new
scale: mean −0.3314, sd 0.1150, correlation time 2.85 days, standard error
0.0679.

---

## 8. The chart did not move

**Question.** With everything re-scored and calibrated, the live line was almost
flat. It was not a rendering problem and not a cache - the payload was current and
the axis auto-scales to the visible data. There was simply nothing to see.

**Measurement.** The published series, by window:

| window | level range | span |
| ------ | ----------- | ---- |
| today | 99.9881 .. 99.9997 | **0.012%** |
| 1 week | 99.9881 .. 100.1417 | **0.154%** |
| 1 month | 99.7048 .. 100.1416 | **0.438%** |

The median daily move was 0.016%. The month's shape was one smooth arc - a climb
to 1 October and a fall after it - with no days visible in it.

**Finding.** Two things multiplied. The new rubric scores most reports as changing
little, so the mood is very steady (sd 0.106), and the integrator then smoothed it
further. An index that averages the mood across hundreds of stories *and*
accumulates the result cannot show a day.

**Change.** The integrator was replaced by the thermometer:

```
level = base * exp(c * (S(t) - b))
```

On the same scores this covers 3–4% in a week at about 0.5% a day, with the days
visible in it. `CONFIG_VERSION` went to 3.

Two consequences came with it. First, the thin opening days are no longer
published: a thermometer shows its input directly, so the days that held one to
fifteen stories drew the level to 85 and 116, which is a fact about coverage and
not about the world. The series now begins at the first window holding
`MIN_WINDOW_ITEMS` (50) stories - the same floor the calibration uses to trim its
sample. Second, an error in `b` became a *bounded* level offset, `exp(c * se)`
rather than a drift that accumulates, which is what made section 10 necessary.

---

## 9. Impact weighting

**Question.** With the thermometer in place, could one day's big story move the
level?

**Measurement.** The dilution first. The decay window holds about 2,400 stories
with a total weight near 400, so one story is 1/413 of the mood, and the largest
story of a day held **2.4%** of the day's weight. No event could move the level
much, because it was competing with the crowd.

The lever is to weight each story by its own consequence - `|signed|^p` on top of
the decay - which is also what the scoring rule already implies: a report that
changed nothing should not count the same as one that changed a great deal.

| weighting | largest story's share of a day | mood spread |
| --------- | ------------------------------ | ----------- |
| p = 0 (plain mean) | 2.4% | 0.59 |
| **p = 2** | **8.0%** | 2.00 |
| p = 3 | 17.4% | 5.31 |

**Change.** `weight_power = 2`. A neutral story now contributes exactly nothing
instead of diluting the mood toward zero. `b` was re-measured (+0.4968) and `c`
re-set (0.0243), because the weighted mood's spread is 4.6x wider.
`CONFIG_VERSION` went to 4.

**A finding that followed, and is not about the weighting.** The weighted mood
averages **+0.50** where the plain mood averages **−0.33**:

| by \|signed\| | stories | %positive | %negative | mean signed |
| ------------- | ------- | --------- | --------- | ----------- |
| top 20 | 20 | **80%** | 20% | +4.14 |
| top 100 | 100 | **75%** | 25% | +2.57 |
| rest | 2,009 | 21% | 66% | −0.45 |

The top twenty are four Nobel prizes, a Starship launch, and Chinese economic and
diplomatic achievements. The scorer rates achievement-style news as more
consequential than wars and disasters (the highest positive score seen is +9.64,
the highest negative −8.08). The weighting only amplifies that faithfully. It is
a scoring property, of the same family as "magnitude is bunched", and it is the
next thing to look at on the backend.

---

## 10. The criterion had to change too

**Question.** The calibration refused to apply the new baseline: standard error
0.213 against a target of 0.10.

**Measurement.** With a thermometer, an error in `b` is a bounded multiplicative
offset: `exp(c * se) - 1`, which for `c = 0.0243` and `se = 0.213` is **0.5% of
level** - and it does not grow.

**Finding.** The verdict was still the *integrator's* drift budget, which no
longer exists. The question is not "how fast does the chart slide" but "how far
off can the whole chart be".

**Change.** The hardcoded target standard error was replaced by `LEVEL_BUDGET` -
two percent of level - divided by `c`, so the budget is stated in the units that
actually matter and follows the thermometer's sensitivity. The verdict table now
compares level offsets instead of annualised drifts. The live estimate's 0.213 is
a 0.5% offset, comfortably inside a 2% budget, so the verdict is ready.

---

## 11. What the clustering hides, and why it was accepted

**Question.** The index consumes one item per cluster, preferring the
representative. Is one report a fair summary of the story it stands for?

**Measurement.** `tools/cluster_audit.py`, written for this. It scores a sample
of non-representative members and compares them with their representatives - and
it prints the reference that number has to be read against, because "73%" means
nothing on its own.

| | |
| --- | --- |
| clusters / multi-member / members | 3,131 / 769 / 5,193 |
| members that would flip their story's sign | **27%** (16 of 60) |
| agreement, by drift band | 67 / 80 / 73 / 73% |
| two arbitrary same-day stories | **49%** (1,561 stored pairs) |

**Finding.** The clustering is doing real work: 73% against a 49% same-day
reference means members of a cluster are genuinely more alike than two stories
drawn from the same day. But the rate is **flat across the drift bands**, so the
guess this tool was built to test - that long, theme-glued clusters are where the
loss lives - is wrong. The cost of one score per cluster is paid everywhere,
because a cluster holds a spread of framings of one event (CGTN averages +0.76
signed, NYT -1.11) and the index takes one draw from that spread.

**Whose problem this is.** The flat rate is the evidence, and it points outward.
Loose clustering would show up as visibly worse agreement in the long-drift bands;
it does not, so tightening the dedupe would not buy anything. What is left is the
corpus - the sources frame one event differently, which is what +0.76 against
-1.11 measures, and a feed's summary is often a hook for a different fact than its
headline: the NYT report of the Pillay award carries a one-line description of a UN
commission's genocide finding, which is why the award scored -6.66. Neither is
under this project's control.

That leaves exactly one design decision - *which* report stands for an event - and
the flat rate says it is not what moves the number. So the rule stays.

The first run said otherwise. At 24 calls it produced a clean 100 / 100 / 83 / 67
gradient, which is the story this section was going to tell. At 60 calls the
gradient was gone. That is the sampling trap already recorded under "The tools
this produced", walked into again by the person who wrote it down.

**Decision: accept.** The disagreement is real, bounded, and cheaper to live with
than to remove:

* it is measured rather than assumed, and measured against a reference that says
  the clustering still carries most of the signal;
* removing it means scoring every duplicate member - 1,714 calls on this corpus -
  and taking a consensus per cluster. That trades a known 27% spread for an
  unknown amount of index churn, and it would put the index's score for a story
  at odds with the one report a reader can actually click through to;
* the direction is not knowable. A member and a representative can each be the
  better reading of the same event, so "the member disagrees" is not "the index
  is wrong".

What changes is the claim, not the number. The UI's `×2` badge said "same event
reported by other sources", which implies the others agree. It now says the score
is this report's and that the others are not scored, so their reading can differ.
The index itself is untouched.

---

## What was rejected

**Removing the decay kernel** - the proposal to score each story once and never
model its persistence. It sounds like delegating persistence to the news flow, but
it does the opposite: with no kernel every story keeps its influence forever,
whether or not anything follows it up. It also makes `b`'s error unbounded - a
standard error of 0.077 would move the level 0.2% *per day, indefinitely*, about
thirteen times the integrator's sensitivity, with no forgetting to absorb it.

**A long kernel, to model persistence.** The kernel is a *default* persistence for
stories that nothing follows up on, not a model of how events propagate. Lengthening
it re-introduces the same assumption in slower motion.

**Detecting relatedness between stories.** That would mean modelling how the world
works, which is exactly what the rule in section 5 exists to avoid.

**The integrator's drift budget.** Replaced in section 10.

**Rubrics v2 and v3.** v2 fixed the tail but judged the transience of the reported
act; v3 fixed neither and promoted human-interest features. Both are recorded in
section 6, and the retired v1 rubric is still in `rpi/schema.py` so the comparison
stays reproducible.

**The scorer library's fitted temperature.** The library fits a temperature for the
original release (T = 2.179) and applies it *in the scorer*, not in the weights, so
a third-party runtime has to reproduce it; this one uses the original published
helper, which softmaxes the raw logits. Applying the fitted value was measured and
rejected - it flattens the distribution and empties the top of the scale:

| temperature | expected-score spread | max | stories at 8-10 |
| ----------- | --------------------- | --- | --------------- |
| 1.0 (ships) | 1.037 | 9.46 | 7 |
| 2.179 (fitted) | 0.762 | 7.92 | **0** |

The library optimises "how often is the answer right"; the index needs spread. The
two pull in opposite directions, and for this use the uncalibrated setting is the
better one.

**An overnight batch average, for a market-style open and close.** Unnecessary: the
decay-weighted mean has already digested the overnight news by the time a market
would open, so the 08:00 reading *is* the overnight digest. And batching the
scoring changes nothing at all, because the index keys on an item's event time, not
on when the analysis happened.

---

## The tools this produced

| tool | the question it answers |
| ---- | ----------------------- |
| `tools/window_dominance.py` | Is the measured correlation a trend, one dominant story, or persistence? |
| `tools/rescore_drift.py` | Is the instrument drifting, or is the news? |
| `tools/anchor_ab.py` | Does a rubric change buy anything, and does it move the wrong stories? |
| `tools/calibration_forecast.py` | How has the projected date moved as the sample grew? |
| `tools/cluster_audit.py` | Is one report a fair summary of the story it stands for? |

All five are read-only, and none of them writes to the database. Adopting a change
is always a separate, deliberate step - which is why every one of them prints what
it measured and stops.

Two habits they share, both learned the hard way:

* **sample at 100, not 25.** A 24-story sample said the spread doubled; 100 stories
  said the body was unchanged. The extremes move first and they are not the
  distribution.
* **stratify by the thing under test.** Re-scores are split by scoring date, because
  an instrument that hardened over time would shift the old items further; and the
  corpus is restricted to one source era, because otherwise "the model changed" and
  "the corpus changed" cannot be told apart.

---

## Open

* **The scorer's magnitude asymmetry.** The biggest stories are 80% positive, so an
  impact-weighted index sits above 100 on average. Whether that is the world or the
  scorer is not something this instrument can see - but the *asymmetry*
  (achievements outscoring disasters) is a scoring property, and it is where the
  next look should go.
* **The clustering's spread.** One score per cluster discards a 27% sign
  disagreement that is now measured, accepted, and stated in the UI. The untried
  alternative is a consensus across a cluster's members - 1,714 scoring calls on
  this corpus - which is the only way to find out whether the index would read
  better for it.
* **`p = 3`.** More event dominance, at the cost of being more hostage to a single
  story, including a mis-scored one: the largest story of a day would hold 17% of
  the weight.
* **The index has no memory.** It cannot show that the world has been getting worse
  for a month, because it only reads the present. That is the trade that made it
  readable; the rejected alternative is above.
* **The schema is one instrument.** A change to one field's wording moves the other
  fields a little - 14 to 15 of 100 scope answers moved in every sample taken.
* **`k` is dead weight.** It survives in the config only because the diagnostic tools
  still express a legacy drift through it.
