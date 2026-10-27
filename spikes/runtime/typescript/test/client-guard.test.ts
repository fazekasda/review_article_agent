import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import {
  createRuntimeClient,
  isLoopbackHost,
  resolveEndpoint,
  RuntimeEndpointError,
} from "../src/client.ts";
import { MockServer } from "./support.ts";

const CONFIGURED = "2.2.0-test";

test("explicit URL is required; the spike never falls back to service discovery", () => {
  assert.throws(
    () => resolveEndpoint({}, {}),
    (error: unknown) => error instanceof RuntimeEndpointError && /explicit server URL required/.test(error.message),
  );
  assert.throws(
    () => resolveEndpoint({ url: "   " }, { OPENCODE_URL: "http://127.0.0.1:1" }),
    RuntimeEndpointError,
  );
});

test("URL and token come from arguments first, environment second", () => {
  const fromEnv = resolveEndpoint({}, { OPENCODE_URL: "http://127.0.0.1:4096", OPENCODE_TOKEN: "env-token" });
  assert.equal(fromEnv.baseUrl, "http://127.0.0.1:4096");
  assert.equal(fromEnv.token, "env-token");

  const fromArgs = resolveEndpoint(
    { url: "http://127.0.0.1:5000", token: "arg-token" },
    { OPENCODE_URL: "http://127.0.0.1:4096", OPENCODE_TOKEN: "env-token" },
  );
  assert.equal(fromArgs.baseUrl, "http://127.0.0.1:5000");
  assert.equal(fromArgs.token, "arg-token");
});

test("unauthenticated connections are refused by default, even on loopback", () => {
  assert.throws(
    () => createRuntimeClient({ baseUrl: "http://127.0.0.1:4096" }),
    (error: unknown) => error instanceof RuntimeEndpointError && /refusing unauthenticated/.test(error.message),
  );
  // Explicit local opt-in for a loopback server that has authentication disabled.
  assert.doesNotThrow(() =>
    createRuntimeClient({ baseUrl: "http://127.0.0.1:4096", allowUnauthenticatedLocal: true }),
  );
});

test("non-loopback endpoints are refused unless explicitly allowed", () => {
  assert.throws(
    () => createRuntimeClient({ baseUrl: "http://10.0.0.5:4096", token: "x" }),
    (error: unknown) => error instanceof RuntimeEndpointError && /refusing non-loopback/.test(error.message),
  );
  assert.doesNotThrow(() =>
    createRuntimeClient({ baseUrl: "http://10.0.0.5:4096", token: "x", allowNonLoopback: true }),
  );
});

test("loopback detection is narrow", () => {
  assert.equal(isLoopbackHost("localhost"), true);
  assert.equal(isLoopbackHost("127.0.0.1"), true);
  assert.equal(isLoopbackHost("127.9.9.9"), true);
  assert.equal(isLoopbackHost("[::1]"), true);
  assert.equal(isLoopbackHost("example.com"), false);
  assert.equal(isLoopbackHost("127.0.0.1.evil.com"), false);
});

test("the token is sent as a bearer header", async () => {
  const server = new MockServer({ requireToken: "s3cret", version: CONFIGURED });
  const client = createRuntimeClient({ baseUrl: "http://127.0.0.1:4096", token: "s3cret", fetch: server.fetch });
  const info = await client.server.info();
  assert.equal(info.version, CONFIGURED);
  assert.equal(server.requests[0]?.authorization, "Bearer s3cret");
});

test("a wrong token surfaces the declared 401 error", async () => {
  const server = new MockServer({ requireToken: "s3cret", version: CONFIGURED });
  const client = createRuntimeClient({ baseUrl: "http://127.0.0.1:4096", token: "wrong", fetch: server.fetch });
  await assert.rejects(client.server.info(), /invalid bearer token|Unauthorized/);
});

test("the client module never imports the shared-service API", () => {
  const raw = readFileSync(fileURLToPath(new URL("../src/client.ts", import.meta.url)), "utf8");
  // Check executable code, not the explanatory comments that name the forbidden API.
  const source = raw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
  assert.equal(
    /from\s+["']@opencode\/client\/service["']/.test(source),
    false,
    "must not import @opencode/client/service",
  );
  assert.equal(/Service\.(ensure|discover|stop)\s*\(/.test(source), false, "must not call Service lifecycle helpers");
  // No private state parsing either.
  for (const forbidden of ["service.json", "opencode.db", ".local/share", ".local/state"]) {
    assert.equal(source.includes(forbidden), false, `must not reference ${forbidden}`);
  }
});
