const ALLOWED_HOSTS: Record<string, ReadonlySet<string>> = {
  ccgp: new Set(["www.ccgp.gov.cn", "search.ccgp.gov.cn"]),
  ggzy: new Set(["www.ggzy.gov.cn"]),
};

/** Return true only for HTTPS URLs on the exact host allowlist for a source. */
export function isAllowedSourceUrl(source: string, value: string): boolean {
  const hosts = ALLOWED_HOSTS[source];
  if (!hosts) {
    return false;
  }

  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.port === "" && hosts.has(url.hostname);
  } catch {
    return false;
  }
}
