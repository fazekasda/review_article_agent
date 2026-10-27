/**
 * Explicit-URL, authenticated client construction for the spike.
 *
 * Design constraints from the task and docs/specification.md:
 *
 *  - The spike must talk to an *app-owned* OpenCode V2 server, never the user's
 *    ordinary shared coding service. We therefore never import
 *    `@opencode/client/service` and never call `Service.ensure()`/`Service.discover()`.
 *    The base URL and credential are supplied by the caller (CLI arg or environment).
 *  - No private state is parsed: we do not read service.json, the SQLite database,
 *    or any keychain. Only an explicitly provided URL/auth/directory is used.
 *  - Refuse an unauthenticated connection by default, and refuse a non-loopback
 *    endpoint unless the caller opts in explicitly.
 *  - The Authorization scheme is explicit: `bearer` (default, backward compatible)
 *    or `basic` (the disposable 2.2.0 harness answers HTTP Basic
 *    `opencode:<per-run-password>`). Redirects are refused at the transport so the
 *    credential is never forwarded to a redirect target.
 */
import { OpenCode } from "@opencode/client";
import type { OpenCodeClient } from "@opencode/client";
import { Buffer } from "node:buffer";

/** Explicit Authorization scheme. `bearer` is the default (backward compatible). */
export type AuthScheme = "bearer" | "basic";

/** Default Basic username, matching the disposable 2.2.0 harness (`opencode`). */
export const BASIC_DEFAULT_USERNAME = "opencode";

export interface RuntimeEndpoint {
  /** Base URL of the app-owned server, e.g. http://127.0.0.1:4096. Required. */
  readonly baseUrl: string;
  /** Authorization scheme. Defaults to `bearer`. */
  readonly auth?: AuthScheme | undefined;
  /** Bearer token (auth `bearer`). Required unless the local opt-in is set. */
  readonly token?: string | undefined;
  /** Basic username (auth `basic`). Defaults to `opencode`. */
  readonly username?: string | undefined;
  /** Basic password (auth `basic`). Required unless the local opt-in is set. */
  readonly password?: string | undefined;
  /**
   * Allow an unauthenticated connection to a loopback endpoint. Off by default so
   * "forgot the credential" fails loudly instead of silently reaching a server.
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
  readonly auth?: AuthScheme | undefined;
  readonly token?: string | undefined;
  readonly username?: string | undefined;
  readonly password?: string | undefined;
  readonly allowUnauthenticatedLocal?: boolean | undefined;
  readonly allowNonLoopback?: boolean | undefined;
  readonly fetch?: typeof globalThis.fetch | undefined;
}

/** True when `value` is a supported Authorization scheme. */
export function isAuthScheme(value: unknown): value is AuthScheme {
  return value === "bearer" || value === "basic";
}

/**
 * Resolve an endpoint from explicit arguments, falling back to environment
 * variables. Environment values are only ever URL/auth/opt-ins; nothing else on
 * disk is consulted (no service discovery, no private credential parsing).
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
  const rawAuth = inputs.auth ?? env.OPENCODE_AUTH;
  if (rawAuth !== undefined && rawAuth !== "" && !isAuthScheme(rawAuth)) {
    throw new RuntimeEndpointError(
      `unknown auth scheme ${JSON.stringify(rawAuth)}: use "bearer" or "basic"`,
    );
  }
  const auth: AuthScheme = rawAuth === undefined || rawAuth === "" ? "bearer" : rawAuth;
  // OPENCODE_PASSWORD is the historical bearer alias; it is only read for bearer,
  // so setting it can never silently become a Basic password.
  const token =
    auth === "bearer"
      ? (inputs.token ?? env.OPENCODE_TOKEN ?? env.OPENCODE_PASSWORD)
      : (inputs.token ?? env.OPENCODE_TOKEN);
  const username = (inputs.username ?? env.OPENCODE_BASIC_USERNAME) || undefined;
  const password = (inputs.password ?? env.OPENCODE_BASIC_PASSWORD) || undefined;
  const allowUnauthenticatedLocal =
    inputs.allowUnauthenticatedLocal ?? env.OPENCODE_ALLOW_UNAUTHENTICATED === "1";
  const allowNonLoopback = inputs.allowNonLoopback ?? env.OPENCODE_ALLOW_NON_LOOPBACK === "1";
  return {
    baseUrl,
    auth,
    token,
    username,
    password,
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
  const auth = endpoint.auth ?? "bearer";
  if (!isAuthScheme(auth)) {
    throw new RuntimeEndpointError(`unknown auth scheme ${JSON.stringify(auth)}: use "bearer" or "basic"`);
  }
  const unauthenticatedOk = loopback && endpoint.allowUnauthenticatedLocal === true;
  const hasToken = endpoint.token !== undefined && endpoint.token !== "";
  const hasPassword = endpoint.password !== undefined && endpoint.password !== "";
  if (auth === "basic") {
    // A token has no meaning under Basic; refuse so the scheme is never guessed.
    if (hasToken) {
      throw new RuntimeEndpointError(
        'auth "basic" must not be given a bearer token: pass --password or OPENCODE_BASIC_PASSWORD (unset OPENCODE_TOKEN)',
      );
    }
    if (!hasPassword && !unauthenticatedOk) {
      throw new RuntimeEndpointError(
        'refusing unauthenticated connection: auth "basic" needs --password or OPENCODE_BASIC_PASSWORD (for a loopback server with authentication disabled, set allowUnauthenticatedLocal/OPENCODE_ALLOW_UNAUTHENTICATED=1)',
      );
    }
  } else {
    if (hasPassword) {
      throw new RuntimeEndpointError(
        'auth "bearer" must not be given a password: pass --token or OPENCODE_TOKEN',
      );
    }
    if (!hasToken && !unauthenticatedOk) {
      throw new RuntimeEndpointError(
        "refusing unauthenticated connection: pass --token or set OPENCODE_TOKEN (for a loopback server with authentication disabled, set allowUnauthenticatedLocal/OPENCODE_ALLOW_UNAUTHENTICATED=1)",
      );
    }
  }
  return url;
}

/** The Authorization header value for the endpoint's explicit scheme, or undefined. */
export function authorizationHeader(endpoint: RuntimeEndpoint): string | undefined {
  const auth = endpoint.auth ?? "bearer";
  if (auth === "basic") {
    if (endpoint.password === undefined || endpoint.password === "") return undefined;
    const username =
      endpoint.username === undefined || endpoint.username === "" ? BASIC_DEFAULT_USERNAME : endpoint.username;
    return `Basic ${Buffer.from(`${username}:${endpoint.password}`, "utf8").toString("base64")}`;
  }
  if (endpoint.token === undefined || endpoint.token === "") return undefined;
  return `Bearer ${endpoint.token}`;
}

/**
 * Wrap a fetch so a redirect is refused instead of followed.
 *
 * The credential is attached by the client to the outbound request; a followed
 * redirect could forward it to another origin. Forcing `redirect: "manual"` makes
 * the transport return the 3xx response, which this wrapper turns into a loud
 * error. Node/undici returns a 3xx response for manual redirects; a browser
 * reports `type: "opaqueredirect"`. Both are refused.
 */
export function rejectRedirects(baseFetch: typeof globalThis.fetch): typeof globalThis.fetch {
  return async (input, init) => {
    const response = await baseFetch(input, { ...(init ?? {}), redirect: "manual" });
    if (response.type === "opaqueredirect" || (response.status >= 300 && response.status < 400)) {
      throw new RuntimeEndpointError(
        `server answered with a redirect (${response.status}); refusing to follow it and forward credentials`,
      );
    }
    return response;
  };
}

/**
 * Build the official V2 client against an explicit base URL and credential.
 * The custom `fetch` (when supplied) is the only injection point the tests use;
 * it is always wrapped so redirects cannot leak the credential.
 */
export function createRuntimeClient(endpoint: RuntimeEndpoint): OpenCodeClient {
  const url = assertUsableEndpoint(endpoint);
  const headers: Record<string, string> = {};
  const authorization = authorizationHeader(endpoint);
  if (authorization !== undefined) headers.authorization = authorization;
  return OpenCode.make({
    baseUrl: url.origin + (url.pathname === "/" ? "" : url.pathname),
    headers,
    fetch: rejectRedirects(endpoint.fetch ?? globalThis.fetch),
  });
}
