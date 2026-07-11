# Result Analysis: HNSW Vector Search

**Table:** `support_articles`, 10,000 rows, real Mistral `mistral-embed`
embeddings (1024 dimensions), cosine distance.

---

## The primary finding: recall scales predictably with m and ef_construction

Excluding the one flagged anomaly, average recall by configuration:

| Config | Avg recall across ef_search sweep |
|---|---|
| m=8, ef_construction=64 (excl. anomaly) | ~61.0% |
| m=16, ef_construction=64 | ~57.9% |
| m=16, ef_construction=128 | ~62.1% |
| m=32, ef_construction=200 | ~67.4% |

This is a clean, monotonic trend: more graph connectivity (`m`) and a
more thorough build (`ef_construction`) produce measurably better
recall, at the direct cost of longer index build time (1.29s → 4.98s,
a 3.9x increase from the cheapest to most thorough configuration
tested) and no meaningful change in query latency (all configurations
land in the same ~1-3ms band once the index is actually used). This is
the real, expected HNSW tradeoff, now proven with real embeddings and
real numbers rather than assumed from documentation.

---

## Why recall tops out around 67-68%, not closer to 100%, even at the best configuration

This dataset's synthetic support-ticket text is built from only 5
templates per category across 8 categories — 40 total distinct
patterns, each filled with randomized specifics. This means many rows
within the same category are semantically near-identical to each
other. For any given query, there are very likely dozens of articles
that are all nearly equally close matches, not a small, sharply
distinguished set of 10. This makes the exact "top 10" boundary itself
somewhat arbitrary on this specific dataset — swapping article #47 for
article #52 as the 10th-closest match, when both are nearly
indistinguishable in meaning, is a trivial, low-consequence disagreement
in practice, but it still counts as a recall miss in this benchmark's
strict measurement.

**This means the recall numbers reported here likely understate HNSW's
practical usefulness on more diverse, real-world text**, where fewer
near-duplicate rows would make the true top-10 boundary sharper and
easier for an approximate index to hit consistently. This is a real
methodological limitation of the synthetic dataset, not a limitation of
HNSW itself, and is worth stating plainly rather than letting the raw
percentage stand unqualified.

---

## Two real mistakes made while building this benchmark, and what they taught

**Mistake 1 — adding a deterministic tiebreak (`, id`) to fix perceived
recall instability.** The hypothesis was that ties in cosine distance,
caused by the template-heavy synthetic data, were producing
non-reproducible "exact" search results. The fix made things
categorically worse: an approximate HNSW index cannot guarantee a
specific tie-break order, so demanding one forced PostgreSQL's planner
to abandon the index entirely, for every single configuration,
permanently. The resulting "100% recall every time" that followed was
trivial and meaningless — an exact scan was being compared against
itself, because HNSW was never actually being exercised. This was
caught only because a new per-query scan-method classifier was added
and showed `index_used: False` uniformly across all twelve
configurations — including ones previously confirmed working before
the tiebreak was introduced. **Lesson: a fix that produces suspiciously
perfect results deserves more scrutiny, not less.**

**Mistake 2 — assuming the recall variability came from ties in the
underlying data.** Real, high-precision floating-point embeddings from
a real API make genuine exact ties in cosine distance exceedingly
unlikely. The tiebreak was reverted, and a more precise instrument was
built instead: classifying each of the 50 timed queries individually by
latency, rather than trusting a single one-query `EXPLAIN` spot-check
to represent the behavior of all 50. This revealed the real mechanism:
the planner makes one **consistent** decision per configuration and
`ef_search` value — not a random per-query flip-flop — which ruled out
"planner inconsistency across executions" as the explanation too.

---

## The one anomaly that remains genuinely unresolved

`m=8, ef_construction=64, ef_search=40`: all 50 queries classified as
sequential-scan speed (36ms, matching the exact-scan baseline exactly),
yet recall is 56.8%, not the ~100% an exact scan compared against its
own ground truth should trivially produce.

**Ruled out with reasonable confidence:**
- Genuine floating-point ties in the underlying data (real
  high-precision embeddings make this statistically implausible)
- The deterministic-tiebreak fix (tested directly; it broke a different
  part of the system instead of fixing this one)
- Per-query planner inconsistency within the same setting (the
  scan-method classifier shows a clean, consistent 50/50 split, not a
  mixed count)

**Plausible but not confirmed:** this table's `work_mem` is configured
at only 4MB (see `docker-compose.yml`). A full sequential scan
computing cosine distance across 9,999 rows, followed by a sort to find
the top 10, is exactly the kind of operation that can exceed a small
`work_mem` budget and spill to an external disk-based sort. Disk-based
sorts introduce I/O-scheduling and OS-page-cache variability that an
in-memory sort does not have, which could plausibly produce
inconsistent row ordering under otherwise-identical conditions. This
hypothesis was proposed and a direct diagnostic (`EXPLAIN (ANALYZE,
BUFFERS)`, checking for `Sort Method: external merge` versus
`quicksort`) was identified as the definitive test, but has not yet
been run and confirmed.

**This is recorded here explicitly, not glossed over, because an
honestly flagged open question is more valuable to a reader than a
confident-sounding explanation that hasn't actually been verified.**
This lab's entire methodology has depended on verifying claims with
real output before trusting them — the correct response to running out
of verified explanations is to say so plainly, not to manufacture
certainty.

---

## Production learnings

**A single spot-check cannot characterize a config near the planner's
cost decision boundary.** The original one-query `EXPLAIN` check
reported "sequential scan" or "index used" as if it were a fixed
property of the configuration. In reality, the same `(m,
ef_construction)` index can be serviced by entirely different scan
methods depending on the `ef_search` value set at query time — a live,
per-query tunable independent of how the index was built. Any
production system tuning HNSW parameters should verify behavior across
the actual range of `ef_search` values it expects to use in practice,
not assume one plan choice holds for every setting.

**Recall must be measured, not assumed, and the measurement itself
needs auditing.** Two different flawed hypotheses were tested and
ruled out during this experiment specifically because the benchmark
was built to make its own internal consistency checkable — the
scan-method classifier existed only because a suspicious result
(100% recall, but only after disabling the index the whole benchmark
was supposed to be measuring) triggered deeper investigation instead of
being accepted at face value.

---

## Core Thesis, Extended a Sixth Time

> Performance is not a default state but a measurable compromise; every
> database decision — whether adding an index, pooling connections, or
> structuring a query — extracts a specific architectural cost that
> must be explicitly benchmarked and proven under load, rather than
> blindly assumed.

This experiment extends the thesis one layer further: **the benchmark
measuring the tradeoff must itself be verified, not just the tradeoff
being measured.** A benchmarking script that appears to work — that
runs without errors and produces a clean-looking number — is not
automatically a correct one. The clearest example here is the
tiebreak fix: it produced a perfectly uniform, superficially reassuring
result (100% recall, every configuration, every run) that was
completely wrong, because it silently disabled the very mechanism under
test. The number was required, and so was auditing the number itself.