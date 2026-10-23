// Reliability Engineer — k6 runtime for Workflow IR (generated bundles import this file).
// Workflows are data (JSON); this interpreter executes their request steps. No workflow
// string is ever evaluated as code.
import http from "k6/http";
import { check, group, sleep } from "k6";
import exec from "k6/execution";

export const BASE_URL = (__ENV.BASE_URL || "").replace(/\/$/, "");
const THINK_MIN = parseFloat(__ENV.THINK_MIN_S || "1");
const THINK_MAX = parseFloat(__ENV.THINK_MAX_S || "3");
const TEMPLATE = /\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}/g;

function hex(n) {
  let s = "";
  for (let i = 0; i < n; i++) s += Math.floor(Math.random() * 16).toString(16);
  return s;
}

export function traceparent() {
  return `00-${hex(32)}-${hex(16)}-01`;
}

export function render(value, vars) {
  if (typeof value === "string") {
    const whole = value.match(/^\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}$/);
    if (whole && vars[whole[1]] !== undefined && typeof vars[whole[1]] !== "string") return vars[whole[1]];
    return value.replace(TEMPLATE, (_, name) => (vars[name] === undefined ? "" : String(vars[name])));
  }
  if (Array.isArray(value)) return value.map((v) => render(v, vars));
  if (value && typeof value === "object") {
    const out = {};
    for (const k of Object.keys(value)) out[k] = render(value[k], vars);
    return out;
  }
  return value;
}

export function jsonGet(obj, path) {
  const parts = path.replace(/^\$\.?/, "").match(/[^.[\]]+/g) || [];
  let cur = obj;
  for (const p of parts) {
    if (cur === null || cur === undefined) return undefined;
    cur = cur[/^\d+$/.test(p) ? parseInt(p, 10) : p];
  }
  return cur;
}

const WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"];
export function genVar(spec) {
  if ("value" in spec) return spec.value;
  if ("secret" in spec) return __ENV[spec.secret];
  if ("range" in spec) return spec.range[0] + Math.floor(Math.random() * (spec.range[1] - spec.range[0] + 1));
  switch (spec.faker) {
    case "internet.email": return `re-${hex(10)}@example.test`;
    case "person.name": return `Test ${WORDS[Math.floor(Math.random() * WORDS.length)]}`;
    case "string.uuid": return `${hex(8)}-${hex(4)}-4${hex(3)}-a${hex(3)}-${hex(12)}`;
    case "lorem.word": return WORDS[Math.floor(Math.random() * WORDS.length)];
    case "number.int": return Math.floor(Math.random() * 100000);
    default: return "";
  }
}

export function think(range) {
  const [lo, hi] = range || [THINK_MIN, THINK_MAX];
  sleep(lo + Math.random() * Math.max(0, hi - lo));
}

function buildQuery(q) {
  if (!q) return "";
  const parts = Object.keys(q).map((k) => `${encodeURIComponent(k)}=${encodeURIComponent(String(q[k]))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

export function request(wf, step, vars) {
  const req = render(step.request, vars);
  const tp = traceparent();
  const headers = Object.assign({ traceparent: tp, "User-Agent": "ReliabilityEngineer-k6" }, req.headers || {});
  if (vars.__auth_header) headers.Authorization = vars.__auth_header;
  let body = null;
  if (req.json !== undefined) {
    body = JSON.stringify(req.json);
    headers["Content-Type"] = "application/json";
  } else if (req.form) {
    body = req.form;
  }
  const endpoint = `${step.request.method} ${step.request.path}`;
  const res = http.request(req.method, `${BASE_URL}${req.path}${buildQuery(req.query)}`, body, {
    headers,
    tags: { workflow: wf.key, step: step.id, endpoint, trace_id: tp.split("-")[1] },
    responseCallback: http.expectedStatuses({ min: 200, max: 399 }),
  });
  const expect = step.expect || {};
  const checks = {};
  const statuses = expect.status || null;
  checks[`${wf.key}/${step.id} status`] = (r) => (statuses ? statuses.indexOf(r.status) >= 0 : r.status < 400);
  if (expect.body_contains) checks[`${wf.key}/${step.id} body`] = (r) => String(r.body || "").indexOf(expect.body_contains) >= 0;
  if (expect.max_duration_ms) checks[`${wf.key}/${step.id} duration`] = (r) => r.timings.duration <= expect.max_duration_ms;
  check(res, checks, { workflow: wf.key, step: step.id });
  if (step.extract) {
    let parsed = null;
    try { parsed = res.json(); } catch (e) { parsed = null; }
    for (const name of Object.keys(step.extract)) vars[name] = parsed === null ? undefined : jsonGet(parsed, step.extract[name]);
  }
  return res;
}

export function initVars(wf) {
  const vars = { base_url: BASE_URL, vu: exec.vu.idInTest, iter: exec.vu.iterationInScenario };
  const defs = (wf.data && wf.data.vars) || {};
  for (const name of Object.keys(defs)) vars[name] = genVar(defs[name]);
  return vars;
}

const loginState = {};

export function login(wf, vars) {
  for (const pre of wf.preconditions || []) {
    if (pre.type !== "login" || !pre.api) continue;
    const key = `${wf.key}`;
    if (!loginState[key]) {
      const loginVars = Object.assign({}, vars);
      if (pre.username_ref) loginVars.username = __ENV[pre.username_ref.split(":")[1]];
      if (pre.credential_ref) loginVars.password = __ENV[pre.credential_ref.split(":")[1]];
      request(wf, pre.api, loginVars);
      const extracted = {};
      for (const name of Object.keys(pre.api.extract || {})) extracted[name] = loginVars[name];
      loginState[key] = extracted;
    }
    Object.assign(vars, loginState[key]);
    if (vars.token && !vars.__auth_header) vars.__auth_header = `Bearer ${vars.token}`;
  }
}

export function apiSteps(wf) {
  if (wf.api_equivalent && wf.api_equivalent.length) return wf.api_equivalent;
  return (wf.steps || []).filter((s) => s.request);
}

export function runWorkflow(wf) {
  const vars = initVars(wf);
  login(wf, vars);
  group(wf.key, () => {
    const steps = apiSteps(wf);
    for (let i = 0; i < steps.length; i++) {
      request(wf, steps[i], vars);
      if (i < steps.length - 1) think((wf.load && wf.load.think_time_s) || null);
    }
  });
  think((wf.load && wf.load.think_time_s) || null);
}

export function findStep(wf, id) {
  return apiSteps(wf).filter((s) => s.id === id)[0];
}
