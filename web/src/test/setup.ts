import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach } from "vitest";
import { cleanup } from "@testing-library/react";
import { setAdminToken } from "../auth/adminToken";
import { server } from "./mockServer";

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => setAdminToken("test-admin-token"));
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  localStorage.clear();
  server.resetHandlers();
});
afterAll(() => server.close());
