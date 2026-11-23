/** Only the dashboard served by this loopback process may issue browser requests. */
export function localRequestError(request: Request, port: number, devOrigins: readonly string[] = []): string | undefined {
  const hosts = new Set([`127.0.0.1:${port}`, `localhost:${port}`]);
  if (port === 80) {
    hosts.add("127.0.0.1");
    hosts.add("localhost");
  }
  const host = request.headers.get("host") ?? new URL(request.url).host;
  if (!hosts.has(host.toLowerCase())) return "This dashboard only accepts requests addressed to localhost.";

  const origin = request.headers.get("origin");
  if (origin && ![...hosts].some(value => origin === `http://${value}`) && !devOrigins.includes(origin)) {
    return "Requests from another website are not allowed.";
  }
  if (request.headers.get("sec-fetch-site") === "cross-site" && !(origin && devOrigins.includes(origin))) {
    return "Requests from another website are not allowed.";
  }
  return undefined;
}

export function mutationRequestError(request: Request): string | undefined {
  if (request.headers.get("x-openhack-client") !== "dashboard") {
    return "The X-OpenHack-Client dashboard header is required.";
  }
  if (request.headers.get("content-type")?.split(";")[0]?.trim().toLowerCase() !== "application/json") {
    return "Use application/json for dashboard changes.";
  }
  return undefined;
}

export const securityHeaders = {
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
} satisfies Record<string, string>;
