/**
 * Helpers for the real-API journeys: sign-in through the login form, a second browser context per
 * person (an approver decides in their own session), the state the journeys share (the workspace the
 * first journey creates), and screenshots of key screens.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test as base, expect, type Browser, type BrowserContext, type Locator, type Page } from "@playwright/test";

export const LIVE = process.env.LIVE_BASE_URL;
export const PASSWORD = process.env.LIVE_PASSWORD ?? "ChangeMe123!";
export const ADMIN = "admin@analystos.local";
export const ANALYST = "analyst@analystos.local";
export const APPROVER = "approver@analystos.local";

const HERE = dirname(fileURLToPath(import.meta.url));
// Outside test-results-live, which Playwright empties at the start of every run.
const STATE = join(HERE, ".live-state.json");
export const SHOTS = join(HERE, "screenshots");

export const test = base;
export { expect };

/** Every live journey is skipped unless LIVE_BASE_URL points at a running stack (call in each describe). */
export function requireLive(): void {
  test.skip(!LIVE, "LIVE_BASE_URL is not set: the live journeys need a running stack");
}

export interface LiveState {
  ws?: string;
  wsName?: string;
  run?: string;
  schema?: string;
  stamp?: string;
}

export function readState(): LiveState {
  try {
    return JSON.parse(readFileSync(STATE, "utf8")) as LiveState;
  } catch {
    return {};
  }
}

export function writeState(patch: LiveState): LiveState {
  const next = { ...readState(), ...patch };
  mkdirSync(dirname(STATE), { recursive: true });
  writeFileSync(STATE, JSON.stringify(next, null, 2));
  return next;
}

/** Sign in through the real login form, then land on `next`. */
export async function signIn(page: Page, email: string, next = "/"): Promise<void> {
  await page.goto(next);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeHidden();
}

/** A separate browser session for another person (its own token, its own tab). */
export async function personPage(browser: Browser, email: string, next: string): Promise<{ ctx: BrowserContext; page: Page }> {
  const ctx = await browser.newContext({ baseURL: LIVE, viewport: { width: 1400, height: 1000 } });
  const page = await ctx.newPage();
  await signIn(page, email, next);
  return { ctx, page };
}

/** Approve the first pending card whose text matches, as the approver in their own session. */
export async function approveAs(browser: Browser, ws: string, action: RegExp, reason = "reviewed in the live journey"): Promise<void> {
  const { ctx, page } = await personPage(browser, APPROVER, `/w/${ws}/operate/approvals`);
  const list = page.getByRole("complementary", { name: "Approval proposals" });
  const pending = list.getByRole("button", { name: action });
  // the inbox loads once: a proposal made a moment ago appears on the next load, as it would for a person
  await expect(async () => {
    if (!(await pending.first().isVisible())) await page.reload();
    await expect(pending.first()).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 45_000 });
  const before = await pending.count();
  await pending.first().click(); // newest first: the request this journey just made
  const card = page.locator("article.approval-pending").first();
  await card.getByLabel("Reason").fill(reason);
  await card.getByRole("button", { name: "Approve" }).click();
  await expect(pending).toHaveCount(before - 1);
  await ctx.close();
}

/**
 * A status badge by its raw status: `data-status` where the badge carries it, else the badge's text
 * (older bundles print the raw status; newer ones plain words such as "waiting for you").
 */
export function badge(scope: Page | Locator, status: string): Locator {
  return scope.locator(`.badge[data-status="${status}" i], .badge:text-matches("^\\\\s*${status}\\\\s*$", "i")`);
}

export async function shot(page: Page, name: string): Promise<void> {
  if (!existsSync(SHOTS)) mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: false });
}

/** Main-navigation links; job kinds and panels must never add one. */
export async function navEntries(page: Page): Promise<number> {
  await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Outputs" })).toBeVisible();
  return page.getByRole("navigation", { name: "Main" }).getByRole("link").count();
}
