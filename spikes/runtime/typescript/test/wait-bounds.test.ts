import assert from "node:assert/strict";
import { test } from "node:test";
import { createRuntimeClient } from "../src/client.ts";
import { submitAdmission, waitForResolution } from "../src/lifecycle.ts";
import { MockServer, testAdmissionId } from "./support.ts";

function clientFor(server: MockServer) {
  return createRuntimeClient({ baseUrl: "http://127.0.0.1:4096", token: "token", fetch: server.fetch });
}

/** A transport that never settles unless (and until) its request signal aborts. */
const hangingFetch: typeof globalThis.fetch = (_input, init) =>
  new Promise<Response>((_resolve, reject) => {
    const signal = init?.signal;
    const fail = (): void => reject(new DOMException("The operation was aborted.", "AbortError"));
    if (signal === undefined || signal === null) return; // deliberately never settles
    if (signal.aborted) {
      fail();
      return;
    }
    signal.addEventListener("abort", fail, { once: true });
  });

test("a stalled HTTP request is bounded by the per-request deadline", async () => {
  const client = createRuntimeClient({ baseUrl: "http://127.0.0.1:4096", token: "token", fetch: hangingFetch });
  const started = Date.now();

  const result = await waitForResolution(client, "ses_x", "msg_y", {
    timeoutMs: 5000,
    requestTimeoutMs: 60,
    pollMs: 10,
  });

  const elapsed = Date.now() - started;
  assert.equal(result.resolution.state, "unknown");
  assert.equal(result.cancelled, false);
  assert.match(String((result.resolution as { reason: string }).reason), /read failed or timed out/);
  assert.ok(elapsed < 2000, `the wait must not hang; elapsed ${elapsed}ms`);
});

test("a directly cancelled wait returns cancelled without issuing any request", async () => {
  const server = new MockServer();
  const client = clientFor(server);
  const controller = new AbortController();
  controller.abort();

  const result = await waitForResolution(client, "ses_x", "msg_y", {
    timeoutMs: 1000,
    signal: controller.signal,
  });

  assert.equal(result.cancelled, true);
  assert.equal(result.polls, 0);
  assert.deepEqual(result.resolution, { state: "unknown", reason: "wait cancelled" });
  assert.equal(server.requests.length, 0, "cancellation must precede any read");
});

test("cancelling during the wait stops it promptly", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const admissionId = testAdmissionId("cancel");
  const client = clientFor(server);
  await submitAdmission(client, { sessionId: session.id, admissionId, text: "x" });

  const controller = new AbortController();
  const started = Date.now();
  const waiting = waitForResolution(client, session.id, admissionId, {
    timeoutMs: 5000,
    pollMs: 20,
    signal: controller.signal,
  });
  setTimeout(() => controller.abort(), 30);

  const result = await waiting;
  assert.equal(result.cancelled, true);
  assert.equal(result.resolution.state, "unknown");
  assert.ok(Date.now() - started < 1500, "abort must stop the wait");
});

test("a 200 idempotent retry still yields exactly one accepted input", async () => {
  const server = new MockServer({ duplicatePromptStatus: 200 });
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);
  const admissionId = testAdmissionId("idem");

  const first = await submitAdmission(client, { sessionId: session.id, admissionId, text: "once" });
  const second = await submitAdmission(client, { sessionId: session.id, admissionId, text: "once" });

  assert.equal(first.status, "accepted");
  // A 200 retry is indistinguishable from a first accept in that response alone.
  assert.equal(second.status, "accepted");
  assert.deepEqual(server.pendingAdmissions(session.id), [admissionId], "exactly one enqueued input");
  assert.equal(server.requests.filter((request) => request.path.endsWith("/prompt")).length, 2);
});
