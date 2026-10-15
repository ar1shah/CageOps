// k6 script for the CageOps API load test (D-046). One file, four shapes, chosen by SCENARIO:
//
//   cold   every /predict is a pair the cache has never seen            (closed loop, VUS)
//   warm   /predict over a small set of pairs that was warmed first      (closed loop, VUS)
//   mixed  /predict 60%, search 20%, profile 10%, upcoming 10%           (closed loop, VUS)
//   rate   the warm or cold mix at a FIXED arrival rate, so latency is   (open model, RATE)
//          what a client sees and not what a slowing loop lets through
//
// Inputs come from /results/inputs.json (written by make_inputs.py): unique same-division pairs,
// ids and search terms taken from the real database. Nothing here invents a fighter.
//
// Environment: SCENARIO, MIX (predict|mixed), SET (cold|warm), VUS, DURATION, RATE, PHASE.
import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';
import { SharedArray } from 'k6/data';

const inputs = new SharedArray('inputs', () => [JSON.parse(open('/results/inputs.json'))])[0];

const BASE = __ENV.BASE_URL || 'http://api:8000';
const SCENARIO = __ENV.SCENARIO || 'warm';
const MIX = __ENV.MIX || (SCENARIO === 'mixed' ? 'mixed' : 'predict');
const SET = __ENV.SET || (SCENARIO === 'cold' ? 'cold' : 'warm');
const VUS = parseInt(__ENV.VUS || '16');
const DURATION = __ENV.DURATION || '30s';
const RATE = parseInt(__ENV.RATE || '100');
const PHASE = __ENV.PHASE || 'measure';
const WARM_ITERATIONS = parseInt(__ENV.WARM_ITERATIONS || String(inputs.warm_pairs));
const COLD_OFFSET = parseInt(__ENV.COLD_OFFSET || String(inputs.warm_pairs));

const JSON_HEADERS = { 'Content-Type': 'application/json' };

export const options = (() => {
  let scenarios;
  if (PHASE === 'warmup') {
    // one pass over the warm set, nothing measured: after it every pair is in Redis
    scenarios = {
      warmup: { executor: 'shared-iterations', vus: 8, iterations: WARM_ITERATIONS, maxDuration: '5m' },
    };
  } else if (SCENARIO === 'rate') {
    scenarios = {
      measure: {
        executor: 'constant-arrival-rate',
        rate: RATE,
        timeUnit: '1s',
        duration: DURATION,
        preAllocatedVUs: Math.max(VUS, 32),
        maxVUs: 512,
      },
    };
  } else {
    scenarios = { measure: { executor: 'constant-vus', vus: VUS, duration: DURATION } };
  }
  const thresholds = {};
  // Thresholds that cannot fail exist so that k6 exports per-endpoint percentiles.
  for (const name of ['predict', 'search', 'profile', 'upcoming']) {
    thresholds[`http_req_duration{name:${name}}`] = ['max>=0'];
    thresholds[`http_reqs{name:${name}}`] = ['count>=0'];
  }
  return {
    scenarios,
    thresholds,
    summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
    noConnectionReuse: false,
  };
})();

function predictBody(index) {
  const pair = inputs.pairs[index % inputs.pairs.length];
  return JSON.stringify({ fighter_a_id: pair[0], fighter_b_id: pair[1] });
}

function pickPredictIndex(i, fromWarm) {
  if (fromWarm) return i % inputs.warm_pairs; // the same few pairs, over and over
  // a pair never asked before in this run (the cache was flushed)
  return COLD_OFFSET + (i % (inputs.pairs.length - COLD_OFFSET));
}

function request(i) {
  let kind = 'predict';
  let fromWarm = SET === 'warm';
  if (MIX === 'mixed') {
    const slot = i % 10;
    if (slot <= 5) {
      kind = 'predict';
      fromWarm = i % 5 !== 0; // 80% of predictions from the warm set, 20% brand new
    } else if (slot <= 7) kind = 'search';
    else if (slot === 8) kind = 'profile';
    else kind = 'upcoming';
  }
  switch (kind) {
    case 'predict': {
      const res = http.post(`${BASE}/predict`, predictBody(pickPredictIndex(i, fromWarm)), {
        headers: JSON_HEADERS,
        tags: { name: 'predict' },
      });
      check(res, { 'predict 200': (r) => r.status === 200 });
      return res;
    }
    case 'search': {
      const term = inputs.search_terms[i % inputs.search_terms.length];
      const res = http.get(`${BASE}/fighters?search=${encodeURIComponent(term)}&limit=20`, {
        tags: { name: 'search' },
      });
      check(res, { 'search 200': (r) => r.status === 200 });
      return res;
    }
    case 'profile': {
      const id = inputs.fighter_ids[i % inputs.fighter_ids.length];
      const res = http.get(`${BASE}/fighters/${id}`, { tags: { name: 'profile' } });
      check(res, { 'profile 200': (r) => r.status === 200 });
      return res;
    }
    default: {
      const res = http.get(`${BASE}/events/upcoming`, { tags: { name: 'upcoming' } });
      check(res, { 'upcoming 200': (r) => r.status === 200 });
      return res;
    }
  }
}

export default function () {
  // iterationInTest is unique across every VU, so "unique pair per request" holds under concurrency
  request(exec.scenario.iterationInTest);
}
