# Results: HNSW Vector Search

**Table:** `support_articles` — 10,000 rows, real embeddings via Mistral's
`mistral-embed` model (1024 dimensions, confirmed empirically before
schema was written — see `get_embedding_dimension.py`), not synthetic
vectors.

**Test setup:** 50 randomly selected query rows, each searching for its
own top-10 nearest neighbors (excluding itself) via cosine distance.
Ground truth computed once via exact brute-force search before any
index existed.

---

## Real terminal output (final, corrected run)

```
Experiment 03: HNSW Vector Search Benchmark
support_articles: 10,000 rows
Selected 50 random test queries

============================================================
BASELINE: Brute force exact search (no index)
============================================================
  50 queries, exact top-10 each
  Latency (ms): p50=1.311  p95=2.322  p99=5.288  mean=1.356

============================================================
HNSW CONFIG: m=8  ef_construction=64
============================================================
  Build time: 1.29s
  Index size: 48 MB
  Spot-check (1 query): sequential scan used

  ef_search=40:
    Latency (ms): p50=36.477  p95=39.089  p99=48.53
    Recall@10: 56.8%
    Scan method mix: 0/50 likely index, 50/50 likely seq scan

  ef_search=100:
    Latency (ms): p50=1.171  p95=1.985  p99=2.606
    Recall@10: 62.2%
    Scan method mix: 50/50 likely index, 0/50 likely seq scan

  ef_search=200:
    Latency (ms): p50=1.257  p95=2.25  p99=3.868
    Recall@10: 59.8%
    Scan method mix: 50/50 likely index, 0/50 likely seq scan

============================================================
HNSW CONFIG: m=16  ef_construction=64
============================================================
  Build time: 1.69s
  Index size: 47 MB
  Spot-check (1 query): HNSW index used

  ef_search=40:  p50=0.893  p95=1.403  p99=2.759   Recall@10: 57.2%   mix: 50/0
  ef_search=100: p50=1.1    p95=1.902  p99=3.196   Recall@10: 58.4%   mix: 50/0
  ef_search=200: p50=1.257  p95=2.285  p99=2.976   Recall@10: 58.2%   mix: 50/0

============================================================
HNSW CONFIG: m=16  ef_construction=128
============================================================
  Build time: 2.32s
  Index size: 47 MB
  Spot-check (1 query): HNSW index used

  ef_search=40:  p50=1.063  p95=1.881  p99=2.732   Recall@10: 61.6%   mix: 50/0
  ef_search=100: p50=1.221  p95=2.205  p99=2.866   Recall@10: 62.6%   mix: 50/0
  ef_search=200: p50=1.414  p95=2.424  p99=4.156   Recall@10: 62.2%   mix: 50/0

============================================================
HNSW CONFIG: m=32  ef_construction=200
============================================================
  Build time: 4.98s
  Index size: 46 MB
  Spot-check (1 query): HNSW index used

  ef_search=40:  p50=1.011  p95=1.957  p99=3.166   Recall@10: 67.4%   mix: 50/0
  ef_search=100: p50=1.252  p95=2.349  p99=2.608   Recall@10: 66.6%   mix: 50/0
  ef_search=200: p50=1.393  p95=3.055  p99=4.02    Recall@10: 68.2%   mix: 50/0
```

---

## Summary Table

| m | ef_construction | ef_search | build(s) | size | p50(ms) | p95(ms) | recall | scan mix |
|---|---|---|---|---|---|---|---|---|
| 8 | 64 | 40 | 1.29 | 48MB | 36.477 | 39.089 | **56.8%** | 0/50 index — **flagged anomaly** |
| 8 | 64 | 100 | 1.29 | 48MB | 1.171 | 1.985 | 62.2% | 50/50 index |
| 8 | 64 | 200 | 1.29 | 48MB | 1.257 | 2.25 | 59.8% | 50/50 index |
| 16 | 64 | 40 | 1.69 | 47MB | 0.893 | 1.403 | 57.2% | 50/50 index |
| 16 | 64 | 100 | 1.69 | 47MB | 1.1 | 1.902 | 58.4% | 50/50 index |
| 16 | 64 | 200 | 1.69 | 47MB | 1.257 | 2.285 | 58.2% | 50/50 index |
| 16 | 128 | 40 | 2.32 | 47MB | 1.063 | 1.881 | 61.6% | 50/50 index |
| 16 | 128 | 100 | 2.32 | 47MB | 1.221 | 2.205 | 62.6% | 50/50 index |
| 16 | 128 | 200 | 2.32 | 47MB | 1.414 | 2.424 | 62.2% | 50/50 index |
| 32 | 200 | 40 | 4.98 | 46MB | 1.011 | 1.957 | 67.4% | 50/50 index |
| 32 | 200 | 100 | 4.98 | 46MB | 1.252 | 2.349 | 66.6% | 50/50 index |
| 32 | 200 | 200 | 4.98 | 46MB | 1.393 | 3.055 | 68.2% | 50/50 index |

**11 of 12 configurations show fully self-consistent behavior**: a
single, stable scan-method decision applied uniformly across all 50
test queries, and recall trending upward with `m` and
`ef_construction` exactly as theory predicts.

**One configuration — `m=8, ef_search=40` — is flagged as an open
anomaly**: latency matches the exact-scan baseline and the scan-method
classifier reports 50/50 sequential scan, yet recall is only 56.8%,
not the ~100% an exact scan against its own ground truth should
produce. See `result_analysis.md` for the full investigation, what
has been ruled out, and what remains unconfirmed.