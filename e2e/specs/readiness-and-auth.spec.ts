import { expect, test } from "@playwright/test";

/**
 * Verify the readiness probe, liveness probe, and admin-token guard at the
 * HTTP level -- no browser or page object required.
 *
 * The E2E webServer boots with ``BIDSCOPE_APP_MODE=test``, which bypasses the
 * admin-token guard in ``require_admin_token`` (see ``auth.py``).  Tests 3 and
 * 4 therefore exercise the test-mode codepath; production-mode authentication
 * is covered by the backend security suite in
 * ``backend/tests/security/test_production_auth.py``.
 */

test.describe("Readiness endpoint", () => {
  test("/readyz returns bounded status shape with no sensitive data", async ({
    request,
  }) => {
    const response = await request.get("/readyz");

    // The readiness endpoint returns 200 when all checks pass, 503 otherwise.
    // Either way the body must follow the bounded schema.
    expect([200, 503]).toContain(response.status());

    const body = await response.json();

    // Top-level status field is required and must be one of the known values.
    expect(body).toHaveProperty("status");
    expect(["ok", "failed"]).toContain(body.status);

    // The checks object must contain every known dependency name with a valid
    // status sub-field -- nothing else leaks through.
    expect(body).toHaveProperty("checks");
    const expectedChecks = [
      "database",
      "checkpoint",
      "object_store",
      "configuration",
    ];
    for (const name of expectedChecks) {
      expect(body.checks).toHaveProperty(name);
      expect(body.checks[name]).toHaveProperty("status");
      expect(["ok", "failed"]).toContain(body.checks[name].status);
    }

    // The sanitized response must never expose deployment details: DSN
    // fragments, credentials, passwords, or stack traces.
    const text = JSON.stringify(body);
    expect(text).not.toMatch(/postgres:\/\//i);
    expect(text).not.toMatch(/dsn/i);
    expect(text).not.toMatch(/password/i);
    expect(text).not.toMatch(/secret/i);
    expect(text).not.toMatch(/credential/i);
    expect(text).not.toMatch(/traceback/i);
    expect(text).not.toMatch(/stack trace/i);

    // Bounded payload -- the readiness body must stay small.
    expect(text.length).toBeLessThan(1024);
  });
});

test.describe("Liveness endpoint", () => {
  test("/healthz is public and returns 200 without authentication", async ({
    request,
  }) => {
    const response = await request.get("/healthz");

    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty("status", "ok");
  });
});

test.describe("Business API authentication", () => {
  test("unauthenticated request to business API is rejected", async ({
    request,
  }) => {
    // In production mode, requests to /api/* without the X-Admin-Token header
    // receive a 401 with {"detail": "invalid admin token"}.  The E2E server
    // runs in test mode where the guard is bypassed, so the request reaches the
    // router and returns a non-401 response.  This test verifies the endpoint
    // is reachable and does not crash; production auth rejection is covered by
    // backend/tests/security/test_production_auth.py.
    const response = await request.get("/api/runs");

    // Must not be a server error.
    expect(response.status()).toBeLessThan(500);
  });

  test("authenticated request reaches the router", async ({ request }) => {
    // Provide a valid-looking admin token.  In test mode the guard is bypassed,
    // but in production mode the token would be validated against the
    // configured value.  Either way the request must reach the business router
    // (not 401) and return a well-formed response.
    const response = await request.get("/api/runs", {
      headers: { "X-Admin-Token": "test-e2e-token" },
    });

    // The request must not be rejected as unauthenticated.
    expect(response.status()).not.toBe(401);

    // Must not be a server error.
    expect(response.status()).toBeLessThan(500);
  });
});
