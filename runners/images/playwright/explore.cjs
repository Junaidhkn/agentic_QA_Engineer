#!/usr/bin/env node
// Bounded browser exploration (FR-8, agent-workflows §3 explore_ui).
//
// Breadth-first crawl of one origin with Playwright/Chromium inside the runner sandbox:
//   * every navigation, click and form submission costs one action; stops at the action or time budget;
//   * requests to any other origin are aborted (defence in depth on top of the egress proxy);
//   * denylisted actions (delete, payment, account closure, email send, logout…) are recorded as
//     skipped and never executed; forms with payment fields are never filled or submitted;
//   * forms are filled with synthetic values; a login form gets the provided test credentials.
// Output: pages (title, headings, links, forms), actions (incl. skipped ones), API calls seen.
//
// Usage: node explore.cjs config.json out.json
"use strict";
const fs = require("fs");

function loadChromium() {
  for (const mod of ["@playwright/test", "playwright"]) {
    try {
      return require(mod).chromium;
    } catch (_) {
      /* try the next one */
    }
  }
  throw new Error("Playwright is not installed");
}

const DEFAULT_DENY = [
  "delete", "remove", "destroy", "erase", "purge", "\\bdrop\\b", "\\bpay\\b", "payment", "purchase", "checkout",
  "\\bbuy\\b", "place order", "order now", "subscribe", "unsubscribe", "close (my )?account", "deactivate",
  "terminate", "cancel (my )?(subscription|plan|account)", "\\bsend\\b", "e-?mail", "invite", "transfer", "refund",
  "withdraw", "log ?out", "sign ?out", "reset password", "change password",
];
// Links are GETs, but some GETs still change state (one-click unsubscribe, "close account",
// confirmation links), so links get their own, stricter list.
const LINK_DENY =
  /log ?out|sign ?out|delete|remove|destroy|erase|purge|unsubscribe|deactivate|terminate|cancel|close|revoke|disable|reset|confirm|approve|reject|\bpay\b|refund|withdraw|transfer/i;
const PAYMENT_FIELD = /card|cc-?(num|number|exp|csc)|cvc|cvv|security code|iban|bic|swift|routing|account.?number|sort.?code|expir/i;
const ID_SEGMENT = /^(\d+|[0-9a-f]{8,}|[0-9a-f-]{36})$/i;

function shape(pathname) {
  return pathname.split("/").map((s) => (ID_SEGMENT.test(s) ? "{id}" : s)).join("/") || "/";
}

function syntheticValue(field, n, creds) {
  const t = (field.type || "").toLowerCase();
  const hint = `${field.name} ${field.label} ${field.placeholder}`.toLowerCase();
  if (t === "password") return creds.password || `Explore-${n}-pass!`;
  if (t === "email" || /e-?mail|user(name)?/.test(hint)) return creds.username || `explorer+${n}@example.com`;
  if (t === "number" || t === "range") return "1";
  if (t === "date") return "2026-01-15";
  if (t === "url") return "https://example.com/";
  if (t === "tel") return "+15555550100";
  if (/name|title/.test(hint)) return `Exploration ${n}`;
  return `exploration ${n}`;
}

async function main() {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const outPath = process.argv[3];
  const base = new URL(cfg.base_url);
  const deny = new RegExp((cfg.denylist && cfg.denylist.length ? cfg.denylist : DEFAULT_DENY).join("|"), "i");
  const maxActions = Math.max(1, Math.min(cfg.max_actions || 150, 1000));
  const deadline = Date.now() + Math.max(5, Math.min(cfg.max_seconds || 900, 3600)) * 1000;
  const creds = cfg.credentials || {};
  const out = { base_url: base.origin, pages: [], actions: [], api_calls: [], blocked_requests: 0, stats: {} };
  const apiSeen = new Map();
  let actions = 0;
  let n = 0;
  const budgetLeft = () => actions < maxActions && Date.now() < deadline;

  const browser = await loadChromium().launch();
  const context = await browser.newContext({ ignoreHTTPSErrors: false, userAgent: "ReliabilityEngineer-Explorer/1" });
  await context.route("**/*", (route) => {
    const u = new URL(route.request().url());
    if (u.origin !== base.origin && !u.protocol.startsWith("data")) {
      out.blocked_requests++;
      return route.abort();
    }
    return route.continue();
  });
  context.on("request", (req) => {
    const type = req.resourceType();
    if (["fetch", "xhr"].includes(type) || (type === "document" && req.method() !== "GET")) {
      const u = new URL(req.url());
      if (u.origin !== base.origin) return;
      const key = `${req.method()} ${shape(u.pathname)}`;
      apiSeen.set(key, (apiSeen.get(key) || 0) + 1);
    }
  });
  const page = await context.newPage();
  page.setDefaultTimeout(8000);

  async function open(url) {
    const resp = await page.goto(url, { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle", { timeout: 4000 }).catch(() => {});
    return resp ? resp.status() : null;
  }

  async function snapshot() {
    return page.evaluate(() => {
      const text = (el) => (el.innerText || el.value || el.getAttribute("aria-label") || el.title || "").trim().slice(0, 120);
      const labelFor = (el) => {
        if (el.id) {
          const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
          if (l) return l.innerText.trim();
        }
        const wrap = el.closest("label");
        return wrap ? wrap.innerText.trim() : el.getAttribute("aria-label") || "";
      };
      return {
        title: document.title.slice(0, 200),
        headings: [...document.querySelectorAll("h1,h2")].map((h) => h.innerText.trim().slice(0, 120)).slice(0, 10),
        links: [...document.querySelectorAll("a[href]")].map((a) => ({ href: a.href, text: text(a) })).slice(0, 200),
        buttons: [...document.querySelectorAll("button, [role=button], input[type=button]")]
          .filter((b) => !b.closest("form"))
          .map((b, i) => ({ index: i, text: text(b) }))
          .slice(0, 50),
        forms: [...document.forms].map((f, i) => ({
          index: i,
          method: (f.getAttribute("method") || "get").toLowerCase(),
          action: f.getAttribute("action") || "",
          text: f.innerText.trim().slice(0, 300),
          submit: text(f.querySelector("button[type=submit], button:not([type]), input[type=submit]") || f),
          fields: [...f.querySelectorAll("input, textarea, select")]
            .filter((el) => !["hidden", "submit", "button", "reset", "file"].includes(el.type))
            .map((el) => ({
              name: el.name || el.id || "",
              type: el.type || el.tagName.toLowerCase(),
              label: labelFor(el).slice(0, 120),
              placeholder: el.placeholder || "",
              required: el.required,
              autocomplete: el.autocomplete || "",
            })),
        })),
      };
    });
  }

  const queue = [{ url: new URL(cfg.start_path || "/", base).href, depth: 0 }];
  const seenPages = new Set();
  const triedForms = new Set();

  while (queue.length && budgetLeft()) {
    const { url, depth } = queue.shift();
    const u = new URL(url);
    const key = shape(u.pathname) + u.search;
    if (seenPages.has(key) || u.origin !== base.origin) continue;
    seenPages.add(key);
    actions++;
    let status;
    try {
      status = await open(url);
    } catch (e) {
      out.actions.push({ type: "navigate", page: u.pathname, target: u.pathname, outcome: "error", error: String(e).slice(0, 200) });
      continue;
    }
    const snap = await snapshot();
    const here = new URL(page.url());
    out.pages.push({ url: here.pathname + here.search, path: shape(here.pathname), status, depth, ...snap });
    out.actions.push({ type: "navigate", page: u.pathname, target: u.pathname, outcome: "ok", status, url_after: here.pathname });

    for (const link of snap.links) {
      let lu;
      try {
        lu = new URL(link.href);
      } catch (_) {
        continue;
      }
      if (lu.origin !== base.origin || lu.hash && lu.pathname === here.pathname) continue;
      if (LINK_DENY.test(`${link.text} ${lu.pathname}`)) {
        out.actions.push({ type: "navigate", page: here.pathname, target: lu.pathname, label: link.text, outcome: "skipped", blocked: "denylisted link" });
        continue;
      }
      if (depth + 1 <= (cfg.max_depth || 6)) queue.push({ url: lu.href, depth: depth + 1 });
    }

    for (const form of snap.forms) {
      if (!budgetLeft()) break;
      const formKey = `${shape(here.pathname)}#${form.index}:${form.action}`;
      if (triedForms.has(formKey)) continue;
      triedForms.add(formKey);
      const label = form.submit || form.action || "form";
      const isLogin = form.fields.some((f) => f.type === "password") && form.fields.length <= 3;
      const payment = form.fields.some((f) => PAYMENT_FIELD.test(`${f.name} ${f.label} ${f.placeholder} ${f.autocomplete}`));
      const denied = !isLogin && deny.test(`${label} ${form.action} ${form.text}`);
      if (payment || denied) {
        out.actions.push({
          type: "submit", page: here.pathname, label, outcome: "skipped",
          blocked: payment ? "payment form" : "denylisted action", fields: form.fields.map((f) => f.name),
        });
        continue;
      }
      if (isLogin && !(creds.username && creds.password)) {
        out.actions.push({ type: "submit", page: here.pathname, label, outcome: "skipped", blocked: "login form without test credentials" });
        continue;
      }
      actions++;
      n++;
      const before = apiSeen.size;
      try {
        await open(here.href);
        const f = page.locator("form").nth(form.index);
        const filled = [];
        for (const field of form.fields) {
          const value = syntheticValue(field, n, creds);
          const input = field.name ? f.locator(`[name="${field.name}"], #${CSS_ESCAPE(field.name)}`).first() : null;
          if (!input) continue;
          if (field.type === "checkbox" || field.type === "radio") await input.check().catch(() => {});
          else if (field.type === "select-one" || field.type === "select") await input.selectOption({ index: 1 }).catch(() => {});
          else await input.fill(field.type === "password" ? value : value).catch(() => {});
          filled.push({ name: field.name, label: field.label, placeholder: field.placeholder, type: field.type,
                        value: field.type === "password" ? "<secret>" : value });
        }
        const submit = f.locator("button[type=submit], button:not([type]), input[type=submit]").first();
        // Wait for the submission's navigation, or the next page load would abort the request.
        const nav = page.waitForNavigation({ timeout: 6000 }).catch(() => {});
        if (await submit.count()) await submit.click();
        else await f.evaluate((el) => el.requestSubmit());
        await nav;
        await page.waitForLoadState("networkidle", { timeout: 4000 }).catch(() => {});
        const after = new URL(page.url());
        out.actions.push({
          type: "submit", page: here.pathname, label, method: form.method, outcome: "ok", login: isLogin,
          fields: filled, url_after: after.pathname, new_api_calls: apiSeen.size - before,
        });
        if (after.origin === base.origin) queue.push({ url: after.href, depth: depth + 1 });
      } catch (e) {
        out.actions.push({ type: "submit", page: here.pathname, label, outcome: "error", error: String(e).slice(0, 200) });
      }
    }

    for (const button of snap.buttons) {
      if (!budgetLeft()) break;
      if (!button.text || deny.test(button.text)) {
        out.actions.push({ type: "click", page: here.pathname, label: button.text, outcome: "skipped", blocked: button.text ? "denylisted action" : "unlabelled button" });
        continue;
      }
      actions++;
      try {
        await open(here.href);
        await page.locator("button, [role=button], input[type=button]").filter({ hasText: button.text }).first().click();
        await page.waitForLoadState("networkidle", { timeout: 4000 }).catch(() => {});
        const after = new URL(page.url());
        out.actions.push({ type: "click", page: here.pathname, label: button.text, outcome: "ok", url_after: after.pathname });
        if (after.origin === base.origin && after.pathname !== here.pathname) queue.push({ url: after.href, depth: depth + 1 });
      } catch (e) {
        out.actions.push({ type: "click", page: here.pathname, label: button.text, outcome: "error", error: String(e).slice(0, 200) });
      }
    }
  }

  out.api_calls = [...apiSeen.entries()].map(([k, count]) => ({ key: k, count })).sort((a, b) => a.key.localeCompare(b.key));
  out.stats = {
    actions_used: actions,
    action_budget: maxActions,
    pages: out.pages.length,
    skipped: out.actions.filter((a) => a.outcome === "skipped").length,
    budget_exhausted: actions >= maxActions,
    timed_out: Date.now() >= deadline,
  };
  await browser.close();
  fs.writeFileSync(outPath, JSON.stringify(out));
}

function CSS_ESCAPE(s) {
  return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
