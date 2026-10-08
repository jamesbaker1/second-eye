/**
 * The kill switch, flipped without a deploy.
 *
 * SERVICE_PAUSED is a var, so turning it on is a deploy, and a firm's IT
 * asks "how do we turn it off right now?" before anything else. This is the
 * answer: one row in the deployment's own D1 database,
 *
 *   edge_settings(key = 'service_paused', value = 'true' | 'false')
 *
 * written by `second-eye pause <firm>` / `second-eye resume <firm>` (src/secondeye/killswitch.py,
 * through `wrangler d1 execute` with the firm's own token), or by hand in the
 * D1 console of the firm's Cloudflare dashboard. It is read on every message,
 * every send and every cron run, so it takes effect on the next one.
 *
 * The var still works, and wins: SERVICE_PAUSED=true in the config is paused
 * whatever the row says. Either one paused is paused.
 *
 * Paused means what it always meant (mail.ts): mail from an admitted sender
 * is accepted and held sealed in R2, nothing is sent, and the held mail is
 * released when both switches are off.
 *
 * How the synchronous `paused(env)` in mail.ts sees the row: each entry point
 * (email, fetch, scheduled, a Workflow's send) calls `loadPause(env)` first,
 * which reads the row and remembers the answer for that env object.
 */

/** The table, created by whichever side writes it first. */
export const SETTINGS_SCHEMA =
  "CREATE TABLE IF NOT EXISTS edge_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, " +
  "updated_at TEXT NOT NULL, updated_by TEXT)";

export const PAUSE_KEY = "service_paused";

const TRUE = ["true", "1", "yes"];

/** The var: SERVICE_PAUSED in the deployment's config. */
export function varPaused(env: { SERVICE_PAUSED?: unknown }): boolean {
  return TRUE.includes(String(env.SERVICE_PAUSED ?? "").trim().toLowerCase());
}

// What the row said when this env last loaded it. Keyed on the env object an
// entry point was handed, so it lives exactly as long as that invocation's env.
const switched = new WeakMap<object, boolean>();

/** The runtime switch as last loaded for this env (false if never loaded). */
export function runtimePaused(env: object): boolean {
  return switched.get(env) ?? false;
}

/**
 * Read the row, remember it for this env, and say whether the service is
 * paused (the var or the row).
 *
 * A database that has never had the switch flipped has no table: that is
 * "not paused". Any other failure to read it is "paused": D1 being down is
 * the moment not to send, and mail held in R2 is released later, whereas
 * mail sent cannot be unsent.
 */
export async function loadPause(env: { DB?: D1Database; SERVICE_PAUSED?: unknown }): Promise<boolean> {
  let on = false;
  if (env.DB) {
    try {
      const row = await env.DB.prepare("SELECT value FROM edge_settings WHERE key = ?")
        .bind(PAUSE_KEY).first<{ value: string }>();
      on = TRUE.includes(String(row?.value ?? "").trim().toLowerCase());
    } catch (error) {
      if (!/no such table/i.test(String(error))) {
        console.error("could not read the kill switch; holding mail until it can be read", String(error));
        on = true;
      }
    }
  }
  switched.set(env, on);
  return varPaused(env) || on;
}

/** Set the row: what `second-eye pause` / `second-eye resume` do through wrangler. */
export async function setPause(db: D1Database, on: boolean, by: string): Promise<void> {
  await db.prepare(SETTINGS_SCHEMA).run();
  await db.prepare(
    "INSERT INTO edge_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) " +
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, " +
    "updated_by = excluded.updated_by",
  ).bind(PAUSE_KEY, on ? "true" : "false", new Date().toISOString(), by).run();
}
