// E2E database provisioning: reset + migrate + seed the dedicated
// ``bidscope_e2e`` database so every suite run starts deterministically.
//
// Invoked from the ``webServer.command`` in playwright.config.ts BEFORE the
// API server starts (chained with ``&&``), which guarantees the schema and
// seed data are ready before the server's lifespan runs. Running this in
// globalSetup instead would race with the concurrently-started webServer.
//
// Plain JavaScript (not TypeScript) so it runs directly under ``node`` without
// a ts-node/tsx loader.
//
// Required env: ``BIDSCOPE_TEST_CONTROL_TOKEN`` (validated here so a missing
// token fails fast with a clear message instead of an opaque 401 mid-run).
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");

const E2E_DATABASE_URL =
  "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_e2e";
const E2E_CHECKPOINT_DATABASE_URL =
  "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_e2e";
// Raw psycopg (unlike SQLAlchemy) wants a plain postgresql:// DSN.
const E2E_PSYCOPG_DSN =
  "postgresql://bidscope:bidscope@localhost:5432/bidscope_e2e";
const BATCH_1_PATH = resolve(root, "data", "demo", "batch-1");

const RESET_SQL =
  "DROP SCHEMA IF EXISTS public CASCADE; " +
  "CREATE SCHEMA public; " +
  "GRANT ALL ON SCHEMA public TO bidscope; " +
  "GRANT ALL ON SCHEMA public TO public;";

function log(label) {
  process.stdout.write(`[e2e:db-setup] ${label}\n`);
}

function run(label, command, args, extraEnv) {
  log(`${label}: ${command} ${args.join(" ")}`);
  try {
    execFileSync(command, args, {
      stdio: "inherit",
      cwd: root,
      env: { ...process.env, ...extraEnv },
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(
      `[e2e:db-setup] ${label} failed.\n  command: ${command} ${args.join(" ")}\n  error: ${message}`,
    );
  }
}

function resetDatabase() {
  // Drop and recreate the public schema so every suite run starts clean. This
  // is essential because several specs depend on import/tick side effects that
  // are idempotent once present (e.g. re-importing batch-2 after it was
  // already imported creates no new notice versions, so no material_change
  // events would be emitted). The checkpoint tables also live in public and
  // are recreated by ``checkpoints setup``.
  const program =
    "import os, psycopg; " +
    "conn = psycopg.connect(os.environ['E2E_PSYCOPG_DSN']); " +
    "conn.autocommit = True; " +
    "conn.execute(os.environ['E2E_RESET_SQL']); " +
    "conn.close(); " +
    "print('reset bidscope_e2e public schema')";
  run("reset bidscope_e2e schema", "uv", [
    "run", "--offline", "python", "-c", program,
  ], { E2E_PSYCOPG_DSN, E2E_RESET_SQL: RESET_SQL });
}

function main() {
  const token = process.env.BIDSCOPE_TEST_CONTROL_TOKEN;
  if (!token) {
    throw new Error(
      "[e2e:db-setup] BIDSCOPE_TEST_CONTROL_TOKEN is not set. Export it before running E2E, e.g.:\n" +
        '  BIDSCOPE_TEST_CONTROL_TOKEN="e2e-$(date +%s)" npm run test:e2e',
    );
  }
  if (!existsSync(BATCH_1_PATH)) {
    throw new Error(
      `[e2e:db-setup] demo batch-1 bundle not found at ${BATCH_1_PATH}.`,
    );
  }

  const dbEnv = {
    BIDSCOPE_APP_MODE: "test",
    BIDSCOPE_DATABASE_URL: E2E_DATABASE_URL,
    BIDSCOPE_CHECKPOINT_DATABASE_URL: E2E_CHECKPOINT_DATABASE_URL,
    BIDSCOPE_TEST_CONTROL_TOKEN: token,
  };

  resetDatabase();
  run("alembic upgrade head", "uv", ["run", "--offline", "alembic", "upgrade", "head"], dbEnv);
  run("checkpoints setup", "uv", ["run", "--offline", "bidscope", "checkpoints", "setup"], dbEnv);
  run("snapshots import batch-1", "uv", [
    "run", "--offline", "bidscope", "snapshots", "import", BATCH_1_PATH,
  ], dbEnv);
  log("complete");
}

try {
  main();
} catch (error) {
  process.stderr.write((error instanceof Error ? error.message : String(error)) + "\n");
  process.exit(1);
}
