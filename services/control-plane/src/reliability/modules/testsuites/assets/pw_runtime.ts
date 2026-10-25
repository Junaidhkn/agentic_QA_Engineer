// Reliability Engineer — Playwright runtime for Workflow IR (generated specs import this file).
// Workflows are data; this interpreter performs their browser steps. Locators use roles,
// labels and text (resilient), never generated CSS paths.
import { expect, type Locator, type Page } from "@playwright/test";

type Dict = Record<string, unknown>;
const TEMPLATE = /\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}/g;

export function render(value: string, vars: Dict): string {
  return value.replace(TEMPLATE, (_, name: string) => (vars[name] === undefined ? "" : String(vars[name])));
}

function secret(ref?: string): string {
  if (!ref) return "";
  return process.env[ref.split(":")[1]] ?? "";
}

function genVar(spec: Dict): unknown {
  if ("value" in spec) return spec.value;
  if ("secret" in spec) return process.env[String(spec.secret)];
  if ("range" in spec) {
    const [lo, hi] = spec.range as [number, number];
    return lo + Math.floor(Math.random() * (hi - lo + 1));
  }
  const rnd = Math.random().toString(16).slice(2, 12);
  switch (spec.faker) {
    case "internet.email": return `re-${rnd}@example.com`;
    case "person.name": return `Test User ${rnd.slice(0, 4)}`;
    case "string.uuid": return crypto.randomUUID();
    case "number.int": return Math.floor(Math.random() * 100000);
    default: return `word${rnd.slice(0, 4)}`;
  }
}

function locate(page: Page, loc: Dict, vars: Dict): Locator {
  const name = loc.name ? render(String(loc.name), vars) : undefined;
  if (loc.test_id) return page.getByTestId(String(loc.test_id));
  if (loc.role) return page.getByRole(loc.role as Parameters<Page["getByRole"]>[0], name ? { name } : {}).first();
  if (loc.label) return page.getByLabel(render(String(loc.label), vars)).first();
  if (loc.placeholder) return page.getByPlaceholder(render(String(loc.placeholder), vars)).first();
  if (loc.text) return page.getByText(render(String(loc.text), vars)).first();
  throw new Error("locator has no supported selector");
}

async function login(page: Page, wf: Dict) {
  for (const pre of (wf.preconditions as Dict[] | undefined) ?? []) {
    if (pre.type !== "login" || !pre.login_path) continue;
    await page.goto(String(pre.login_path));
    await page.getByLabel(/email|username/i).first().fill(secret(pre.username_ref as string));
    await page.getByLabel(/password/i).first().fill(secret(pre.credential_ref as string));
    await page.getByLabel(/password/i).first().press("Enter");
    await page.waitForLoadState("networkidle");
  }
}

export async function runWorkflow(page: Page, wf: Dict) {
  const vars: Dict = { base_url: process.env.BASE_URL ?? "" };
  const defs = ((wf.data as Dict | undefined)?.vars as Record<string, Dict> | undefined) ?? {};
  for (const [name, spec] of Object.entries(defs)) vars[name] = genVar(spec);
  await login(page, wf);
  for (const step of (wf.steps as Dict[] | undefined) ?? []) {
    if (step.request) continue;
    const loc = step.locator as Dict | undefined;
    switch (step.action) {
      case "navigate":
        await page.goto(render(String(step.url), vars));
        break;
      case "click":
        await locate(page, loc!, vars).click();
        break;
      case "fill":
        await locate(page, loc!, vars).fill(render(String(step.value ?? ""), vars));
        break;
      case "select":
        await locate(page, loc!, vars).selectOption(render(String(step.value ?? ""), vars));
        break;
      case "check":
        await locate(page, loc!, vars).check();
        break;
      case "press":
        await page.keyboard.press(String(step.key ?? "Enter"));
        break;
      case "wait_for_url":
        await page.waitForURL(new RegExp(render(String(step.url), vars)));
        break;
      case "expect": {
        const a = step.assertion as Dict;
        const value = a.value ? render(String(a.value), vars) : "";
        if (a.type === "text_visible") await expect(page.getByText(value).first()).toBeVisible();
        else if (a.type === "url_matches") await expect(page).toHaveURL(new RegExp(value));
        else if (a.type === "title_contains") await expect(page).toHaveTitle(new RegExp(value));
        else if (a.type === "element_visible") await expect(locate(page, a.locator as Dict, vars)).toBeVisible();
        else if (a.type === "element_hidden") await expect(locate(page, a.locator as Dict, vars)).toBeHidden();
        break;
      }
      default:
        throw new Error(`unsupported action ${String(step.action)}`);
    }
  }
}
