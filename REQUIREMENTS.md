# Objective

This app serves as: 

1. A news RSS parser 
2. News analyser
3. RPI (Renpin Index) calculator


It parses news headlines from one or more RSS parser(s) and use an established API (see below) to evaluate the news to get responses including: positive/neutral/negative? impact value (0-10, where 0 means insignificant, and 10 means very very huge). It then calculate the overall RPI based on some algorithms (NOTE: to be explored and determined). 

Finally, it presents the RPI in an NYSE-index-like form to tell the world: How the world is going (in the eyes of an AI)?

# RSS source(s)

- Phase 1: One single source. 
- Phase 2: Multiple sources. 

When multiple sources are in place, app will need to de-duplicate same news using the same API. 

News fetching should be decoupled from the main function below. It could be running on Linux with a cron job and produces the news as output. 

## Source selection rules

**Language: English sources are preferred.** News text fed to the analyser should be
English wherever a reachable English source exists. Rationale: the analyser's
sentiment/impact judgements stay more consistent within a single language, and English
wires carry broader international coverage than domestic Chinese wires do.

**Network reality (GFW).** The fetcher runs from a mainland China network, so many
international outlets are unreachable. Reachability must be *tested*, never assumed,
and it can change over time.

A source is acceptable only if it passes all three checks:

1. **Reachable** - returns a feed document at all.
2. **Current** - `published` dates are actually recent. A feed can return HTTP 200
   while serving content frozen years ago.
3. **Usable** - descriptions contain real article text. A title-only feed leaves the
   analyser with almost nothing to judge.

### Deployment: the fetcher runs on an overseas VPS

**The GFW is a local-development constraint, not a project constraint.** The fetch script
is intended to run on an overseas VPS, where the full range of English sources is
reachable directly. Development happens on a mainland China machine behind the GFW, so
local testing sees a much smaller source pool than production will.

Consequences:

- **Do not choose sources by local reachability.** A feed that is blocked locally may be
  perfectly usable in production, and vice versa.
- **Do re-run the acceptance test on the deployment host.** Reachability turned out to be
  a red herring; the checks that actually matter - freshness and non-empty text - are
  network-independent, and must be verified where the fetcher will really run.
- Proxy support (`proxy` in `feeds.json`: `"auto"` | `"none"` | explicit URL) exists only
  as a local-testing convenience behind a VPN. A VPS needs no proxy; the `"auto"` default
  simply resolves to nothing and connects directly.

Sources marked "via VPN" below were verified through a local VPN, which is the closest
available approximation of production reachability.

### Verify with a command, not by eye

```powershell
python fetcher/fetch_rss.py --check-sources
```

Applies the three-part test to every configured feed (including disabled candidates) and
reports items, median summary length, age of the newest item, and how many items lack a
summary. Run this before trusting any source, and after any network change.

NOTE: sample size matters. A 3-item spot check suggested CGTN summaries were "242-999
chars"; the full feed showed a median of 995, the richest of any source. Check whole
feeds.

### Verified 2026-09-23 (via local VPN; expect a wider pool on the VPS)

| Source | Newest item | Median summary | Verdict |
| --- | --- | --- | --- |
| CGTN (world) | 4.3 h | **995 chars** | **current phase 1 source** |
| Guardian (world) | 0.3 h | 608 chars | strongest alternative |
| SCMP | 0.2 h | 500 chars | good |
| solidot (zh) | 3.1 h | 353 chars | tech-skewed, no VPN needed |
| NPR (world) | 9.4 h | 183 chars | good |
| NYT (world) | 2.0 h | 147 chars (5 of 59 empty) | usable |
| BBC (world) | 0.6 h | 115 chars | usable, terse |
| Al Jazeera (all) | 1.3 h | 109 chars | reachable via VPN; mixes video/sport into "all" |
| chinanews (zh) | 0.2 h | 86 chars | no VPN needed; weakest text |
| CNA | 0.8 h | 100 chars (**8 of 20 empty**) | rejected - too many empty summaries |
| TASS (en) | current | 48-223 chars | superseded now that stronger sources are reachable |
| Global Times | months old | **empty** | rejected - titles only |
| Xinhua English | **2017-2018** | 120-200 chars | rejected - dead feed |
| Sixth Tone | **no dates** | 95-164 chars | rejected - no timeline possible |
| people.com.cn | **frozen 2025-06-05** | mixed | rejected - stale |
| sspai.com | current | short | rejected - consumer-tech blog, not news |
| China Daily | 404 | - | RSS retired |
| Caixin Global (403), Shine, Bangkok Post, AP | - | - | unreachable even via the proxy |

NOTE: CGTN is state-affiliated. Now that independent sources (Guardian, SCMP, NYT, BBC,
NPR, Al Jazeera) are reachable through the proxy, the earlier concern about building the
index solely from state media is **resolvable** - at the cost of needing de-duplication,
because these outlets cover the same stories. This was confirmed empirically: the
"US expands military presence in Greenland" story appeared in both CGTN and BBC on the
same day, so with two sources enabled it would be counted twice.

Also NOTE: a VPN-dependent pipeline inherits the VPN's availability. If the VPN drops, the
fetcher silently degrades to the small direct-reachability set. `chinanews-world` and
`solidot` are retained in the config precisely as no-VPN fallbacks.

# Dependencies

**Decision: no required third-party package anywhere.** Every stage runs on the standard
library alone - the fetcher on the VPS (Python 3.12, where `pip` is not even installed),
the pipeline on the workstation's system Python, and the front end in the browser. There
is no `requirements.txt`, and one is not planned: a pipeline that can be dropped onto a
bare host is worth more than the convenience of a dependency.

One optional exception, confined to a single file:

| File | Optional dependency | Purpose | Without it |
| --- | --- | --- | --- |
| `rpi/calibrate.py` | NumPy | FFT-based autocorrelation | falls back to the naive `O(n * lag)` loop, same result |

Constraints this implies:

- **The fallback is the reference implementation, not a degraded mode.** Both paths were
  checked against each other and against a brute-force reference, on the live snapshot
  series and on synthetic ones; they return identical values. Any future change must keep
  that property, or an optional dependency has quietly become a behaviour switch.
- **Nothing in the hourly pipeline reads NumPy.** `run_once.py` does not import
  `rpi.calibrate`, and the interpreter the schedule uses does not have NumPy installed -
  so a missing or broken NumPy cannot affect production, only a manual calibration run.
- **The import must never be fatal.** It is guarded, so an absent NumPy degrades to the
  fallback rather than failing the tool.

Measured at a 15-minute snapshot cadence (the naive worst case assumes the search runs to
its 800-lag cap, and results are identical either way):

| History | Snapshots | Naive | NumPy |
| --- | --- | --- | --- |
| 6.4 days (current) | 615 | 3.1 - 3.5 ms | 2.0 ms |
| 1 year | 35,040 | 0.8 - 3.0 s | 7.7 ms |
| 5 years | 175,200 | 3.4 - 15.6 s | 41 ms |

Conclusion recorded at the time: NumPy is **headroom, not a fix**. At the sizes this
project has, and is likely to have, the fallback is not slow enough for anyone to notice,
so installing it is optional in the strongest sense.

# News analyser

The API server can answer questions like: 

- Is this news positive? (positive/negative/neutral)
- How big is the impact of this news itself? (A weighted number, say 4.5)
- How big is the impact of this news in terms areas effected？ （Global, Important Regions, Trivial Regions, Local, etc)

Detailed usage can be found in [API Doc](API.md). 

NOTE: The questions to be analysed can be run in parellel. 

NOTE: You will find that API can also answer questions like "Are these two news the same?" using some contructions in the context. 

NOTE: Consider the de-dupe effort. If we have 20 news in each source, and assuming there is no duplicated news in any one source alone, we will have 20X20 questions. The API is fast, but sitll worth noting. 

Parsed/Analysed news should be marked for record to prevent re-analyse. 

This item needs much deliberation before execution plans. 

# RPI calculator and presenter

Pure UI implementation. 

Must haves: 

- RPI chart, with window like today, 1 week, 1 month, etc. 
- A list of the news behind the index, each entry linking to its original article. 

Good to have:

- Typical stock index indicators, and plotted. 

## Analysed news list

The index must be **auditable**: every value shown should be traceable back to the
individual news items that produced it. An index whose inputs cannot be inspected is
unfalsifiable, and an odd-looking reading becomes indistinguishable from a bug.

Requirements:

- Each analysed item is listed with its **title linked to the original article** (the
  feed's canonical link).
- Each entry shows the analysis that drove the index - sentiment, impact, scope, and
  the resulting signed contribution - so a reader can see *why* an item moved the
  index and by how much.
- The list respects the selected chart window (today / 1 week / 1 month), so the
  visible news always corresponds to the visible curve.
- Newest first, labelled with its source, so provenance is explicit once phase 2
  introduces multiple sources.
- Links open in a new tab and must not leak the app's referrer.

NOTE: this is the user-facing counterpart of the stored `analyses` rows. The
provenance already exists in the database (`items.link`, `items.source`,
`analyses.*`); the requirement is about surfacing it, not storing it.
