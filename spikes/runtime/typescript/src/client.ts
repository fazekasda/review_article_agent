/**
 * Explicit-URL, authenticated client construction for the spike.
 *
 * Design constraints from the task and docs/specification.md:
 *
 *  - The spike must talk to an *app-owned* OpenCode V2 server, never the user's
 *    ordinary shared coding service. We therefore never import
 *    `@opencode/client/service` and never call `Service.ensure()`/`Service.discover()`.
 *    The base URL and token are supplied by the caller (CLI arg or environment).
 *  - No private state is parsed: we do not read service.json, the SQLite database,
 *    or any keychain. Only an explicitly provided URL/token/directory is used.
 *  - Refuse an unauthenticated connection by default, and refuse a non-loopback
 *    endpoint unless the caller opts in explicitly.
 */
import { OpenCode } from "@opencode/client";
import type { OpenCodeClient } from "@opencode/client";

export interface RuntimeEndpoint {
  /** Base URL of the app-owned server, e.g. http://127.0.0.1:4096. Required. */
  readonly baseUrl: string;
  /** Bearer token for the app-owned server. Required unless the local opt-in is set. */
  readonly token?: string | undefined;
  /**
   * Allow an unauthenticated connection to a loopback endpoint. Off by default so
   * "forgot the token" fails loudly instead of silently reaching a server.
   */
  readonly allowUnauthenticatedLocal?: boolean;
  /** Allow a non-loopback endpoint. Off by default. */
  readonly allowNonLoopback?: boolean;
  /** Injectable transport, used by the deterministic tests. */
  readonly fetch?: typeof globalThis.fetch;
}

export class RuntimeEndpointError extends Error {
  override readonly name = "RuntimeEndpointError";
}

export function isLoopbackHost(hostname: string): boolean {
  const host = hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (host === "localhost" || host === "::1" || host === "0:0:0:0:0:0:0:1") return true;
  return /^127(?:\.\d{1,3}){3}$/.test(host);
}

export interface EndpointInputs {
  readonly url?: string | undefined;
  readonly token?: string | undefined;
  readonly allowUnauthenticatedLocal?: boolean | undefined;
  readonly allowNonLoopback?: boolean | undefined;
  readonly fetch?: typeof globalThis.fetch | undefined;
}

/**
 * Resolve an endpoint from explicit arguments, falling back to environment
 * variables. Environment values are only ever URL/token/opt-ins; nothing else on
 * disk is consulted.
 */
export function resolveEndpoint(
  inputs: EndpointInputs,
  env: Record<string, string | undefined> = process.env,
): RuntimeEndpoint {
  const baseUrl = inputs.url ?? env.OPENCODE_URL;
  if (baseUrl === undefined || baseUrl.trim() === "") {
    throw new RuntimeEndpointError(
      "explicit server URL required: pass --url or set OPENCODE_URL (the spike never discovers or starts the shared service)",
    );
  }
  const token = inputs.token ?? env.OPENCODE_TOKEN ?? env.OPENCODE_PASSWORD;
  const allowUnauthenticatedLocal =
    inputs.allowUnauthenticatedLocal ?? env.OPENCODE_ALLOW_UNAUTHENTICATED === "1";
  const allowNonLoopback = inputs.allowNonLoopback ?? env.OPENCODE_ALLOW_NON_LOOPBACK === "1";
  return {
    baseUrl,
    token,
    allowUnauthenticatedLocal,
    allowNonLoopback,
    fetch: inputs.fetch,
  };
}

export function assertUsableEndpoint(endpoint: RuntimeEndpoint): URL {
  let url: URL;
  try {
    url = new URL(endpoint.baseUrl);
  } catch {
    throw new RuntimeEndpointError(`invalid server URL ${JSON.stringify(endpoint.baseUrl)}`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new RuntimeEndpointError(`unsupported server URL scheme ${JSON.stringify(url.protocol)}`);
  }
  const loopback = isLoopbackHost(url.hostname);
  if (!loopback && endpoint.allowNonLoopback !== true) {
    throw new RuntimeEndpointError(
      `refusing non-loopback endpoint ${url.hostname}: pass allowNonLoopback/OPENCODE_ALLOW_NON_LOOPBACK=1 to override`,
    );
  }
  const hasToken = endpoint.token !== undefined && endpoint.token !== "";
  if (!hasToken && !(loopback && endpoint.allowUnauthenticatedLocal === true)) {
    throw new RuntimeEndpointError(
      "refusing unauthenticated connection: pass --token or set OPENCODE_TOKEN (for a loopback server with authentication disabled, set allowUnauthenticatedLocal/OPENCODE_ALLOW_UNAUTHENTICATED=1)",
    );
  }
  return url;
}

/**
 * Build the official V2 client against an explicit base URL and token.
 * The custom `fetch` (when supplied) is the only injection point the tests use.
 */
export function createRuntimeClient(endpoint: RuntimeEndpoint): OpenCodeClient {
  const url = assertUsableEndpoint(endpoint);
  const headers: Record<string, string> = {};
  if (endpoint.token !== undefined && endpoint.token !== "") {
    headers.authorization = `Bearer ${endpoint.token}`;
  }
  return OpenCode.make({
    baseUrl: url.origin + (url.pathname === "/" ? "" : url.pathname),
    headers,
    ...(endpoint.fetch === undefined ? {} : { fetch: endpoint.fetch }),
  });
}
