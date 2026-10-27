import assert from "node:assert/strict";
import { test } from "node:test";
import { ClientError } from "@opencode/client";
import { createRuntimeClient } from "../src/client.ts";
import { reconcile } from "../src/lifecycle.ts";
import { jsonResponse, MockServer } from "./support.ts";

function clientFor(server: MockServer) {
  return createRuntimeClient({ baseUrl: "http://127.0.0.1:4096", token: "token", fetch: server.fetch });
}

test("an undeclared HTTP status surfaces as an UnexpectedStatus ClientError", async () => {
  const server = new MockServer();
  server.failNext("GET", "/api/info", () => jsonResponse({ _tag: "UnknownError" }, 500));
  const client = clientFor(server);
  await assert.rejects(client.server.info(), (error: unknown) => {
    assert.ok(error instanceof ClientError);
    assert.equal(error.reason, "UnexpectedStatus");
    return true;
  });
});

test("a malformed JSON body surfaces as a MalformedResponse ClientError", async () => {
  const server = new MockServer();
  server.failNext("GET", "/api/info", () => new Response("{not json", { status: 200, headers: { "content-type": "application/json" } }));
  const client = clientFor(server);
  await assert.rejects(client.server.info(), (error: unknown) => {
    assert.ok(error instanceof ClientError);
    assert.equal(error.reason, "MalformedResponse");
    return true;
  });
});

test("a non-JSON content type surfaces as an UnsupportedContentType ClientError", async () => {
  const server = new MockServer();
  server.failNext("GET", "/api/info", () => new Response("<html></html>", { status: 200, headers: { "content-type": "text/html" } }));
  const client = clientFor(server);
  await assert.rejects(client.server.info(), (error: unknown) => {
    assert.ok(error instanceof ClientError);
    assert.equal(error.reason, "UnsupportedContentType");
    return true;
  });
});

test("a transport failure surfaces as a Transport ClientError", async () => {
  const failing: typeof globalThis.fetch = async () => {
    throw new TypeError("connection refused");
  };
  const client = createRuntimeClient({ baseUrl: "http://127.0.0.1:4096", token: "token", fetch: failing });
  await assert.rejects(client.server.info(), (error: unknown) => {
    assert.ok(error instanceof ClientError);
    assert.equal(error.reason, "Transport");
    return true;
  });
});

test("reconciling an unknown session surfaces the declared 404 error", async () => {
  const server = new MockServer();
  const client = clientFor(server);
  await assert.rejects(reconcile(client, "ses_missing", "msg_x"), (error: unknown) => {
    assert.equal((error as { name?: string }).name, "SessionNotFoundError");
    return true;
  });
});
