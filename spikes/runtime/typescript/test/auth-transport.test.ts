import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { test } from "node:test";
import { ClientError } from "@opencode/client";
import {
  assertUsableEndpoint,
  authorizationHeader,
  createRuntimeClient,
  rejectRedirects,
  resolveEndpoint,
  RuntimeEndpointError,
  type AuthScheme,
} from "../src/client.ts";
import { MockServer } from "./support.ts";

const CONFIGURED = "2.2.0-test";
const URL = "http://127.0.0.1:4096";

function basicHeader(username: string, password: string): string {
  return `Basic ${Buffer.from(`${username}:${password}`, "utf8").toString("base64")}`;
}

function messageChain(error: unknown): string[] {
  const out: string[] = [];
  let current: unknown = error;
  for (let depth = 0; depth < 10 && current !== undefined && current !== null; depth += 1) {
    out.push(current instanceof Error ? `${current.name}: ${current.message}` : String(current));
    current = (current as { cause?: unknown }).cause;
  }
  return out;
}

test("bearer remains the default scheme and still authenticates", async () => {
  const server = new MockServer({ requireToken: "s3cret", version: CONFIGURED });
  const client = createRuntimeClient({ baseUrl: URL, token: "s3cret", fetch: server.fetch });
  const info = await client.server.info();
  assert.equal(info.version, CONFIGURED);
  assert.equal(server.requests[0]?.authorization, "Bearer s3cret");
});

test("basic auth sends `Basic base64(opencode:password)` and connects", async () => {
  const server = new MockServer({ requireBasic: { username: "opencode", password: "per-run" }, version: CONFIGURED });
  const client = createRuntimeClient({ baseUrl: URL, auth: "basic", password: "per-run", fetch: server.fetch });
  const info = await client.server.info();
  assert.equal(info.version, CONFIGURED);
  assert.equal(server.requests[0]?.authorization, basicHeader("opencode", "per-run"));
});

test("basic auth honours an explicit username", async () => {
  const server = new MockServer({ requireBasic: { username: "svc", password: "pw" }, version: CONFIGURED });
  const client = createRuntimeClient({ baseUrl: URL, auth: "basic", username: "svc", password: "pw", fetch: server.fetch });
  assert.equal((await client.server.info()).version, CONFIGURED);
  assert.equal(server.requests[0]?.authorization, basicHeader("svc", "pw"));
});

test("a wrong basic password surfaces the declared 401 error", async () => {
  const server = new MockServer({ requireBasic: { username: "opencode", password: "pw" } });
  const client = createRuntimeClient({ baseUrl: URL, auth: "basic", password: "wrong", fetch: server.fetch });
  await assert.rejects(client.server.info(), /invalid credentials|Unauthorized/);
});

test("basic without a password fails closed", () => {
  assert.throws(
    () => createRuntimeClient({ baseUrl: URL, auth: "basic" }),
    (error: unknown) => error instanceof RuntimeEndpointError && /unauthenticated/.test(error.message),
  );
});

test("a scheme never silently borrows the other scheme's credential", () => {
  assert.throws(() => createRuntimeClient({ baseUrl: URL, auth: "basic", token: "t", password: "p" }), /token/);
  assert.throws(() => createRuntimeClient({ baseUrl: URL, auth: "bearer", token: "t", password: "p" }), /password/);
});

test("an unknown auth scheme is rejected", () => {
  assert.throws(
    () => resolveEndpoint({ url: URL, auth: "digest" as AuthScheme }, {}),
    (error: unknown) => error instanceof RuntimeEndpointError && /unknown auth scheme/.test(error.message),
  );
  assert.throws(
    () => createRuntimeClient({ baseUrl: URL, auth: "digest" as AuthScheme, token: "t" }),
    /unknown auth scheme/,
  );
});

test("environment selects the scheme and reads the documented names", () => {
  const basic = resolveEndpoint({}, { OPENCODE_URL: URL, OPENCODE_AUTH: "basic", OPENCODE_BASIC_PASSWORD: "pw" });
  assert.equal(basic.auth, "basic");
  assert.equal(basic.password, "pw");
  assert.equal(authorizationHeader(basic), basicHeader("opencode", "pw"));

  // The historical bearer alias is untouched.
  const bearerAlias = resolveEndpoint({}, { OPENCODE_URL: URL, OPENCODE_PASSWORD: "pw" });
  assert.equal(bearerAlias.auth, "bearer");
  assert.equal(bearerAlias.token, "pw");

  // In basic mode OPENCODE_PASSWORD is not read as the password (no silent scheme guess).
  const ignored = resolveEndpoint({}, { OPENCODE_URL: URL, OPENCODE_AUTH: "basic", OPENCODE_PASSWORD: "pw" });
  assert.equal(ignored.token, undefined);
  assert.equal(ignored.password, undefined);
  assert.throws(() => assertUsableEndpoint(ignored), /unauthenticated/);
});

test("the transport rejects a redirect so credentials are not forwarded", async () => {
  const called: string[] = [];
  const base = (async (input: string | URL | Request) => {
    const target = input as unknown as { href?: string; url?: string };
    called.push(typeof input === "string" ? input : (target.href ?? target.url ?? ""));
    return new Response(null, { status: 302, headers: { location: "http://example.com/steal" } });
  }) as typeof globalThis.fetch;
  await assert.rejects(rejectRedirects(base)("http://127.0.0.1:4096/api/info"), /redirect/);
  assert.equal(called.length, 1, "the redirect must not be followed a second time");
});

test("a redirect through the client is refused and never followed", async () => {
  const server = new MockServer({ requireToken: "tok" });
  server.failNext(
    "GET",
    "/api/info",
    () => new Response(null, { status: 302, headers: { location: "http://example.com/steal" } }),
  );
  const client = createRuntimeClient({ baseUrl: URL, token: "tok", fetch: server.fetch });
  await assert.rejects(client.server.info(), (error: unknown) => {
    assert.ok(error instanceof ClientError);
    const chain = messageChain(error).join(" | ");
    assert.match(chain, /redirect/);
    return true;
  });
  assert.equal(server.requests.length, 1, "only the original request was made");
});
