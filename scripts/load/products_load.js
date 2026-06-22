// L6 — load / SLO test (k6) for PaySpyre's public read surface.
//
// Hammers the latency-sensitive, idempotent public endpoints (the patient
// products catalog + clinic products) and enforces SLO thresholds. Read-only —
// it never writes, so it's safe to run repeatedly against staging without
// polluting data. The patient/clinic FLOW (writes) is covered by the e2e +
// concurrency smokes; this exists to validate latency + error budget under load.
//
// Run:  k6 run scripts/load/products_load.js
//   API_BASE   default https://payspyre-api-staging-5cflx.ondigitalocean.app/api
//   LOAD_VUS   peak virtual users (default 20)
//   P95_MS     p95 latency SLO in ms (default 1500 — staging is a dev-tier box)
import http from 'k6/http'
import { check, sleep } from 'k6'
import { Rate } from 'k6/metrics'

const API = __ENV.API_BASE || 'https://payspyre-api-staging-5cflx.ondigitalocean.app/api'
const PEAK = parseInt(__ENV.LOAD_VUS || '20', 10)
const P95 = parseInt(__ENV.P95_MS || '1500', 10)

// The "read" endpoints are rate-limited to 100 req / 60s PER IP. A single-source
// load test will (correctly) blow past that, so 429 is EXPECTED, graceful shedding —
// not a failure. What must hold under load: the server never 5xxes (stays up) and
// stays fast. We track server errors separately and require zero.
const serverErrors = new Rate('server_errors')
const okOr429 = http.expectedStatuses(200, 429)

export const options = {
  scenarios: {
    products: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '20s', target: PEAK }, // ramp up
        { duration: '40s', target: PEAK }, // hold at peak
        { duration: '10s', target: 0 }, // ramp down
      ],
    },
  },
  thresholds: {
    // Resilience SLO: the server must NEVER 5xx under load (excess is shed as 429).
    server_errors: ['rate<0.001'],
    // Error budget on real failures only (429 is treated as an expected status below,
    // so it does not count toward http_req_failed).
    http_req_failed: ['rate<0.01'],
    // Latency SLO: 95% of responses under P95_MS, and a hard ceiling on p99.
    http_req_duration: [`p(95)<${P95}`, `p(99)<${P95 * 2}`],
    // Every response is a graceful 200 or 429 (never a crash / unexpected status).
    checks: ['rate>0.99'],
  },
}

export default function () {
  // The patient products catalog is the genuinely PUBLIC (no-auth) read surface —
  // the first call every applicant makes. (Clinic /products requires a clinic JWT,
  // so it's exercised by the authenticated e2e smoke, not this no-auth load test.)
  const r = http.get(`${API}/applicant/v1/products`, {
    tags: { name: 'patient_products' },
    responseCallback: okOr429, // 429 = expected graceful throttle, not a failure
  })
  serverErrors.add(r.status >= 500)
  check(r, {
    'responded 200 or 429 (no crash)': (res) => res.status === 200 || res.status === 429,
    'a 200 carries a products list': (res) => {
      if (res.status !== 200) return true // throttled — nothing to validate
      try {
        return Array.isArray(res.json('products'))
      } catch {
        return false
      }
    },
  })

  sleep(1) // ~1 req/s per VU — a realistic browse cadence, not a thundering herd
}
