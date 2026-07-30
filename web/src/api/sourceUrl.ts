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
    const authority = value.match(/^[a-z][a-z\d+.-]*:\/\/([^/?#]*)/i)?.[1] ?? "";
    if (authority.includes("@")) {
      return false;
    }
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      url.port === "" &&
      url.username === "" &&
      url.password === "" &&
      url.search === "" &&
      url.hash === "" &&
      hosts.has(url.hostname)
    );
  } catch {
    return false;
  }
}
