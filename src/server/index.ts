import { existsSync } from "node:fs";
import { extname, resolve } from "node:path";
import { z } from "zod";
import type { LocalWorkspace } from "../core/workspace";
import { settingsPatchSchema, startScanSchema, triagePatchSchema } from "../shared/contracts";
import { dashboardFile } from "./static";
import { standalone } from "./embedded";
import { localRequestError, mutationRequestError, securityHeaders } from "./security";
import { DirectoryListingError, directoryListSchema, listDirectories } from "./directories";
import { FolderPickerError, pickNativeFolder, type FolderPicker } from "./folder-picker";
import { timingSafeEqual } from "node:crypto";
import { version } from "../../package.json";

export type DashboardWorkspace = Pick<LocalWorkspace,
  "getSettings" | "updateSettings" | "listScans" | "getScan" | "listFindings" | "listScanFindings" |
  "getFinding" | "updateFinding" | "startScan" | "resumeScan" | "cancelScan" |
  "configureCredential" | "removeCredential" | "refreshProviders"
>;

export interface DashboardOptions {
  workspace: DashboardWorkspace;
  port?: number;
  webDir?: string;
  /** Explicit development origins only. Production has no cross-origin access. */
  devOrigins?: readonly string[];
  /** Opens the native folder chooser; tests inject a fake. Defaults to the operating system dialog. */
  pickDirectory?: FolderPicker;
  lifecycle?: { home: string; instanceId: string; token: string; activeScans: () => number; shutdown: () => Promise<void> };
}

class HttpError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}

const resumeSchema = z.object({ steps: z.number().int().min(1).max(30).optional() }).strict();
const credentialSchema = z.object({ key: z.string().trim().min(1).max(8192).regex(/^[^\s\u0000-\u001f\u007f]+$/) }).strict();
const emptyBodySchema = z.object({}).strict();

async function body(request: Request): Promise<unknown> {
  const error = mutationRequestError(request);
  if (error) throw new HttpError(403, error);
  try { return await request.json(); }
  catch { throw new HttpError(400, "The request body must be valid JSON."); }
}

function routeId(value: string): string {
  if (!/^[a-zA-Z0-9-]+$/.test(value)) throw new HttpError(400, "Invalid scan or finding ID.");
  return value;
}

function errorStatus(error: Error): number {
  if (error instanceof HttpError) return error.status;
  if (error instanceof DirectoryListingError) return error.status;
  if (error instanceof FolderPickerError) return error.status;
  if (error instanceof z.ZodError) return 400;
  if (/not found|no saved scan/i.test(error.message)) return 404;
  if (/shutting down/i.test(error.message)) return 503;
  if (/already running|already complete|another process|scan lock|currently running|not running|no resumable checkpoint|storage is busy/i.test(error.message)) return 409;
  if (/invalid|must |requires?|cannot |can't |not a directory|no folder at|ENOENT|not resumable|API.key|no source/i.test(error.message)) return 400;
  return 500;
}

function defaultWebDir(): string {
  const bundled = resolve(import.meta.dir, "web");
  return existsSync(bundled) ? bundled : resolve(import.meta.dir, "../../dist/web");
}

export function startDashboard(options: DashboardOptions): Bun.Server<void> {
  const workspace = options.workspace;
  const webDir = options.webDir ?? defaultWebDir();
  const devOrigins = options.devOrigins ?? [];
  for (const origin of devOrigins) {
    const url = new URL(origin);
    if (url.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(url.hostname) || url.origin !== origin) {
      throw new Error("Development origins must be exact local HTTP origins.");
    }
  }
  const pickDirectory = options.pickDirectory ?? pickNativeFolder;
  let picking = false;
  const port = options.port ?? 6969;
  let shutdownRequested = false;
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error("Invalid dashboard port.");

  return Bun.serve<void>({
    hostname: "127.0.0.1",
    port,
    maxRequestBodySize: 64 * 1024,
    async fetch(request, server) {
      const headers = new Headers({ ...securityHeaders, "Cache-Control": "no-store" });
      const json = (value: unknown, status = 200) => Response.json(value, { status, headers });
      const requestError = localRequestError(request, server.port!, devOrigins);
      if (requestError) return json({ error: requestError }, 403);
      const origin = request.headers.get("origin");
      if (origin && devOrigins.includes(origin)) {
        headers.set("Access-Control-Allow-Origin", origin);
        headers.set("Vary", "Origin");
        if (request.method === "OPTIONS") {
          headers.set("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE");
          headers.set("Access-Control-Allow-Headers", "Content-Type, X-OpenHack-Client");
          return new Response(null, { status: 204, headers });
        }
      }
      try {
        const url = new URL(request.url);
        const path = url.pathname;
        const method = request.method;
        if (path === "/api/health" && method === "GET") {
          const home = options.lifecycle?.home ?? (await workspace.getSettings()).home;
          return json({ app: "openhack", version, home, ...(options.lifecycle ? {instanceId: options.lifecycle.instanceId, activeScans: options.lifecycle.activeScans()} : {}) });
        }
        if (path === "/api/shutdown") {
          if (method !== "POST") throw new HttpError(405, "Method not allowed.");
          const input = z.object({ instanceId: z.string() }).strict().parse(await body(request));
          const lifecycle = options.lifecycle;
          const supplied = Buffer.from(request.headers.get("x-openhack-token") ?? "");
          const expected = Buffer.from(lifecycle?.token ?? "");
          if (!lifecycle || input.instanceId !== lifecycle.instanceId || supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
            throw new HttpError(403, "This dashboard can only be stopped by its local owner.");
          }
          if (!shutdownRequested) {
            shutdownRequested = true;
            // Return the acknowledgement before gracefully closing the listening server.
            setTimeout(() => { void lifecycle.shutdown().catch(() => console.error("OpenHack could not finish shutting down cleanly.")); }, 50);
          }
          return json({ stopping: true }, 202);
        }
        if (path === "/api/scans") {
          if (method === "GET") return json({ scans: await workspace.listScans() });
          if (method === "POST") return json({ scan: await workspace.startScan(startScanSchema.parse(await body(request))) }, 202);
          throw new HttpError(405, "Method not allowed.");
        }
        const scanFindingsRoute = path.match(/^\/api\/scans\/([^/]+)\/findings$/);
        if (scanFindingsRoute) {
          if (method !== "GET") throw new HttpError(405, "Method not allowed.");
          return json({ findings: await workspace.listScanFindings(routeId(decodeURIComponent(scanFindingsRoute[1]!))) });
        }
        const scanRoute = path.match(/^\/api\/scans\/([^/]+)(?:\/(resume|cancel))?$/);
        if (scanRoute) {
          const id = routeId(decodeURIComponent(scanRoute[1]!));
          const action = scanRoute[2];
          if (!action && method === "GET") return json({ scan: await workspace.getScan(id) });
          if (action && method === "POST") {
            const input = await body(request);
            if (action === "resume") return json({ scan: await workspace.resumeScan(id, resumeSchema.parse(input)) }, 202);
            z.object({}).strict().parse(input);
            return json({ scan: await workspace.cancelScan(id) });
          }
          throw new HttpError(405, "Method not allowed.");
        }
        if (path === "/api/findings") {
          if (method === "GET") return json({ findings: await workspace.listFindings() });
          throw new HttpError(405, "Method not allowed.");
        }
        const findingRoute = path.match(/^\/api\/findings\/([^/]+)$/);
        if (findingRoute) {
          const parts = decodeURIComponent(findingRoute[1]!).split(":");
          if (parts.length !== 2) throw new HttpError(400, "Invalid finding key.");
          const scanId = routeId(parts[0]!);
          const findingId = routeId(parts[1]!);
          if (method === "GET") return json({ finding: await workspace.getFinding(scanId, findingId) });
          if (method !== "PATCH") throw new HttpError(405, "Method not allowed.");
          return json({ finding: await workspace.updateFinding(scanId, findingId, triagePatchSchema.parse(await body(request))) });
        }
        if (path === "/api/settings") {
          if (method === "GET") return json({ settings: await workspace.getSettings() });
          if (method === "PATCH") return json({ settings: await workspace.updateSettings(settingsPatchSchema.parse(await body(request))) });
          throw new HttpError(405, "Method not allowed.");
        }
        if (path === "/api/providers/refresh") {
          if (method !== "POST") throw new HttpError(405, "Method not allowed.");
          if (!emptyBodySchema.safeParse(await body(request)).success) throw new HttpError(400, "Provider refresh expects an empty JSON object.");
          return json({ providers: await workspace.refreshProviders() });
        }
        const credentialRoute = path.match(/^\/api\/providers\/(openai|anthropic)\/credentials$/);
        if (credentialRoute) {
          if (method !== "POST" && method !== "DELETE") throw new HttpError(405, "Method not allowed.");
          const provider = credentialRoute[1] as "openai" | "anthropic";
          const input = await body(request);
          if (method === "DELETE") {
            if (!emptyBodySchema.safeParse(input).success) throw new HttpError(400, "Removing saved credentials expects an empty JSON object.");
            try { return json({ settings: await workspace.removeCredential(provider) }); }
            catch { throw new HttpError(500, "Could not finish removing the saved API key. Check the OpenHack data directory and retry."); }
          }
          const parsed = credentialSchema.safeParse(input);
          if (!parsed.success) throw new HttpError(400, "Provide a nonempty API key of at most 8192 characters, without whitespace or control characters.");
          try { return json({ settings: await workspace.configureCredential(provider, parsed.data.key) }); }
          catch { throw new HttpError(500, "Could not finish saving the API key. Check the OpenHack data directory and retry."); }
        }
        if (path === "/api/directories/list") {
          if (method !== "POST") throw new HttpError(405, "Method not allowed.");
          return json(await listDirectories(directoryListSchema.parse(await body(request))));
        }
        if (path === "/api/directories/pick") {
          if (method !== "POST") throw new HttpError(405, "Method not allowed.");
          if (!emptyBodySchema.safeParse(await body(request)).success) throw new HttpError(400, "Choosing a folder expects an empty JSON object.");
          if (picking) throw new FolderPickerError(409, "A folder chooser is already open. Finish or cancel it first.");
          picking = true;
          // The user may keep the native dialog open well past the default idle timeout.
          server.timeout(request, 0);
          try {
            const chosen = await pickDirectory(request.signal);
            return json({ path: typeof chosen === "string" && chosen ? chosen : null });
          } finally { picking = false; }
        }
        if (path === "/api" || path.startsWith("/api/")) throw new HttpError(404, "API route not found.");
        if (method !== "GET" && method !== "HEAD") throw new HttpError(405, "Method not allowed.");
        const file = await dashboardFile(webDir, path, standalone && options.webDir === undefined);
        if (!file) {
          if (path === "/" && !existsSync(resolve(webDir, "index.html"))) {
            return new Response("The dashboard has not been built. Run `bun run build`, then start OpenHack again.", { status: 503, headers });
          }
          throw new HttpError(404, "File not found.");
        }
        const asset = Bun.file(file);
        headers.set("Content-Type", asset.type || "application/octet-stream");
        if (extname(file) !== ".html" && path.startsWith("/assets/")) headers.set("Cache-Control", "public, max-age=31536000, immutable");
        return new Response(method === "HEAD" ? null : asset, { headers });
      } catch (value) {
        const error = value instanceof Error ? value : new Error("The local request failed.");
        const status = errorStatus(error);
        const message = error instanceof z.ZodError
          ? "Invalid request data. Check the supplied fields and values."
          : error instanceof URIError ? "The URL is malformed."
          : status >= 500 && !(error instanceof HttpError) && !(error instanceof FolderPickerError) ? "The local request could not be completed. Check the OpenHack data directory and try again."
          : error.message;
        return json({ error: message }, error instanceof URIError ? 400 : status);
      }
    },
  });
}
