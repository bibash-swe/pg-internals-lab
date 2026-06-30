// k6_raw.js — Experiment 04: Connection Pooling Under Load
//
// Hammers /raw/{id}, which opens a brand new asyncpg connection per
// request with no pooling. Expected behavior: once concurrent
// requests exceed the container's max_connections (20), PostgreSQL
// rejects new connections outright. This should appear in k6's
// output as a spike in HTTP 5xx responses, NOT as increased latency.
//
// Run:
//   k6 run k6_raw.js
//
// Requires app.py running on localhost:8000.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

// Custom metrics so we can report exactly what matters for this
// experiment: error rate and latency distribution, not just k6's
// generic defaults.
const errorRate = new Rate('errors');
const requestDuration = new Trend('request_duration_ms');

export const options = {
    stages: [
        { duration: '10s', target: 200 },  // ramp 0 -> 200 VUs over 10s
        { duration: '30s', target: 200 },  // hold at 200 VUs for 30s
        { duration: '5s', target: 0 },     // ramp down
    ],
    thresholds: {
        // We are NOT asserting these must pass -- we WANT to see
        // the raw endpoint's failure mode clearly, not have the
        // test runner treat it as a hard failure. Thresholds here
        // are informational markers in the summary, not pass/fail
        // gates for this particular run.
        http_req_duration: ['p(95)<5000'],
        errors: ['rate<1.0'],
    },
};

// Job IDs span the seeded 500,000 rows in transactions_five_indexes.
const MAX_TXN_ID = 500_000;

export default function () {
    const txnId = Math.floor(Math.random() * MAX_TXN_ID) + 1;
    const res = http.get(`http://localhost:8000/raw/${txnId}`);

    const ok = check(res, {
        'status is 200': (r) => r.status === 200,
    });

    errorRate.add(!ok);
    requestDuration.add(res.timings.duration);

    sleep(0.05);  // small think-time, keeps VUs from being purely
                  // CPU-bound on k6's side, more representative of
                  // real client request pacing
}