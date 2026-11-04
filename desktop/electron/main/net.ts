/**
 * Network boundary helpers shared by config loading and the API client.
 *
 * The backend is loopback-only by design (`app/core/security.py`,
 * `app/core/config.py`). The desktop app mirrors that rule on its side:
 * it refuses to build an API origin that is not a literal loopback
 * address, so frontend work can never silently widen the backend's
 * trust boundary.
 */

const IPV4_LOOPBACK = /^127(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
const IPV6_LOOPBACK = new Set(["::1", "::ffff:127.0.0.1"]);

/**
 * True when `host` is a literal loopback address (127.0.0.0/8, ::1) or
 * the `localhost` name. Mirrors `backend/app/core/security.py`.
 */
export function isLoopbackHost(host: string | null | undefined): boolean {
  const value = host?.replace(/^\[/, "").replace(/\]$/, "").trim();
  if (!value) return false;
  const lower = value.toLowerCase();
  if (lower === "localhost") return true;
  if (IPV4_LOOPBACK.test(lower)) return true;
  return IPV6_LOOPBACK.has(lower);
}

/** True when `url` is an http(s) URL targeting the loopback interface. */
export function isLoopbackHttpUrl(url: URL): boolean {
  if (url.protocol !== "http:" && url.protocol !== "https:") return false;
  return isLoopbackHost(url.hostname);
}

/** True for a valid, loopback http(s) origin string. */
export function parseLoopbackHttpOrigin(value: string): URL | null {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (!isLoopbackHttpUrl(url)) return null;
  return url;
}
