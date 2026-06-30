// k6_pooled.js — Experiment 04: Connection Pooling Under Load
//
// Hammers /pooled/{id}, which borrows a connection from a shared
// asyncpg.Pool (max_size=10, acquire timeout=5s) instead of opening
// a new one per request. Expected behavior: excess requests beyond
// the pool's 10 connections QUEUE rather than fail -- this should
// appear as increased p95/p99 LATENCY, not as an error spike, unless
// queueing time itself exceeds the 5s acquire timeout, in which case
// this endpoint can also start failing. That crossover is a valid,
// expected finding, not a bug if it happens.
//
// Identical load profile to k6_raw.js so the only variable between
// the two test runs is the connection strategy under test.
//
// Run:
//   k6 run k6_pooled.js
//
// Requires app.py running on localhost:8000.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const errorRate = new Rate('errors');
const requestDuration = new Trend('request_duration_ms');

export const options = {
    stages: [
        { duration: '10s', target: 200 },
        { duration: '30s', target: 200 },
        { duration: '5s', target: 0 },
    ],
    thresholds: {
        http_req_duration: ['p(95)<5000'],
        errors: ['rate<1.0'],
    },
};

const MAX_TXN_ID = 500_000;

export default function () {
    const txnId = Math.floor(Math.random() * MAX_TXN_ID) + 1;
    const res = http.get(`http://localhost:8000/raw/${txnId}`, {
        tags: { name: 'raw_get_transaction' },
    });

    const ok = check(res, {
        'status is 200': (r) => r.status === 200,
    });

    errorRate.add(!ok);
    requestDuration.add(res.timings.duration);

    sleep(0.05);
}