export const ADMIN_TOKEN_STORAGE_KEY = "bidscope.adminToken";

type AuthListener = () => void;

const unauthorizedListeners = new Set<AuthListener>();
const tokenChangeListeners = new Set<AuthListener>();

export class UnauthorizedError extends Error {
  constructor() {
    super("Unauthorized");
    this.name = "UnauthorizedError";
  }
}

function storage(): Storage | null {
  try {
    return sessionStorage;
  } catch {
    return null;
  }
}

export function getAdminToken(): string | null {
  const value = storage()?.getItem(ADMIN_TOKEN_STORAGE_KEY) ?? null;
  return value?.trim() || null;
}

export function setAdminToken(value: string): boolean {
  const token = value.trim();
  if (!token) return false;

  const target = storage();
  if (!target) return false;
  target.setItem(ADMIN_TOKEN_STORAGE_KEY, token);
  notify(tokenChangeListeners);
  return true;
}

export function clearAdminToken(): void {
  const hadToken = getAdminToken() !== null;
  storage()?.removeItem(ADMIN_TOKEN_STORAGE_KEY);
  if (hadToken) notify(tokenChangeListeners);
}

export function onUnauthorized(listener: AuthListener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

export function onAdminTokenChange(listener: AuthListener): () => void {
  tokenChangeListeners.add(listener);
  return () => tokenChangeListeners.delete(listener);
}

export function notifyUnauthorized(): void {
  notify(unauthorizedListeners);
}

export function buildAuthHeaders(init?: HeadersInit): Headers {
  const headers = new Headers(init);
  const token = getAdminToken();
  if (token) {
    headers.set("X-Admin-Token", token);
  } else {
    headers.delete("X-Admin-Token");
  }
  return headers;
}

function notify(listeners: Set<AuthListener>): void {
  for (const listener of listeners) {
    listener();
  }
}
