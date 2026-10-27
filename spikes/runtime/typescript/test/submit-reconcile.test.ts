import assert from "node:assert/strict";
import { test } from "node:test";
import { newAdmissionId } from "../src/admission.ts";
import { createRuntimeClient } from "../src/client.ts";
import {
  isConflict,
  reconcile,
  statusOf,
  submitAdmission,
  waitForResolution,
} from "../src/lifecycle.ts";
import { MockServer, testAdmissionId } from "./support.ts";

function clientFor(server: MockServer) {
  return createRuntimeClient({ baseUrl: "http://127.0.0.1:4096", token: "token", fetch: server.fetch });
}

test("an admission receipt is durable and explicitly not a completion", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);
  const admissionId = testAdmissionId("receipt");

  const receipt = await submitAdmission(client, { sessionId: session.id, admissionId, text: "review this" });

  assert.equal(receipt.kind, "admission");
  assert.equal(receipt.status, "accepted");
  assert.equal(receipt.reconciled, false);
  assert.equal(receipt.admissionId, admissionId);
  assert.equal(receipt.serverVersion, server.version);
  assert.deepEqual(Object.keys(receipt).sort(), [
    "admissionId",
    "admittedAt",
    "delivery",
    "kind",
    "reconciled",
    "serverVersion",
    "sessionId",
    "status",
  ]);
  assert.equal("outcome" in receipt, false, "a receipt must not carry a completion outcome");

  // The input is durably enqueued; nothing has completed yet.
  assert.deepEqual(server.pendingAdmissions(session.id), [admissionId]);
  assert.deepEqual(await reconcile(client, session.id, admissionId), { state: "pending" });
});

test("retrying a client-supplied admission id yields one accepted input", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);
  const admissionId = testAdmissionId("retry");

  const first = await submitAdmission(client, { sessionId: session.id, admissionId, text: "once" });
  const second = await submitAdmission(client, { sessionId: session.id, admissionId, text: "once" });

  assert.equal(first.status, "accepted");
  assert.equal(second.status, "duplicate-accepted");
  assert.equal(second.reconciled, true);
  assert.equal(second.admissionId, admissionId);
  assert.deepEqual(server.pendingAdmissions(session.id), [admissionId], "exactly one enqueued input");

  // Two prompt attempts were made; the second was rejected with a conflict.
  const prompts = server.requests.filter((request) => request.path.endsWith("/prompt"));
  assert.equal(prompts.length, 2);
});

test("a retry after delivery reconciles against the persisted timeline", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);
  const admissionId = testAdmissionId("delivered");

  await submitAdmission(client, { sessionId: session.id, admissionId, text: "hello" });
  server.deliver(session.id, admissionId);
  server.finish(session.id, "succeeded");

  const retry = await submitAdmission(client, { sessionId: session.id, admissionId, text: "hello" });
  assert.equal(retry.status, "duplicate-accepted");
  assert.equal(retry.reconciled, true);
});

test("a second client recovers the result after missed events", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const admissionId = testAdmissionId("missed");

  // First client submits and "disconnects" (no event stream is ever opened).
  const firstClient = clientFor(server);
  const receipt = await submitAdmission(firstClient, { sessionId: session.id, admissionId, text: "run" });
  assert.equal(receipt.status, "accepted");

  // The server-owned task runs to completion while no client is attached.
  server.deliver(session.id, admissionId);
  server.finish(session.id, "succeeded");

  // A fresh, independent client reconciles from persisted state.
  const secondClient = clientFor(server);
  const resolution = await reconcile(secondClient, session.id, admissionId);
  assert.deepEqual(resolution, { state: "succeeded" });
  assert.equal(
    server.requests.some((request) => request.path.startsWith("/api/event")),
    false,
    "reconciliation must not depend on a live event subscription",
  );
});

test("bounded wait returns a terminal result without converting timeouts into success", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);
  const admissionId = testAdmissionId("wait");

  await submitAdmission(client, { sessionId: session.id, admissionId, text: "run" });

  const timedOut = await waitForResolution(client, session.id, admissionId, { timeoutMs: 30, pollMs: 10 });
  assert.equal(timedOut.timedOut, true);
  assert.deepEqual(timedOut.resolution, { state: "pending" });

  server.deliver(session.id, admissionId);
  server.finish(session.id, "succeeded");
  const finished = await waitForResolution(client, session.id, admissionId, { timeoutMs: 500, pollMs: 10 });
  assert.equal(finished.timedOut, false);
  assert.deepEqual(finished.resolution, { state: "succeeded" });
});

test("interrupted and failed outcomes are distinguished from success", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);

  const interruptedId = testAdmissionId("interrupted");
  await submitAdmission(client, { sessionId: session.id, admissionId: interruptedId, text: "a" });
  server.deliver(session.id, interruptedId);
  server.finish(session.id, "interrupted");
  assert.deepEqual(await reconcile(client, session.id, interruptedId), { state: "interrupted" });

  const failedId = testAdmissionId("failed");
  await submitAdmission(client, { sessionId: session.id, admissionId: failedId, text: "b" });
  server.deliver(session.id, failedId);
  server.finish(session.id, "failed");
  assert.deepEqual(await reconcile(client, session.id, failedId), { state: "failed" });
});

test("a restart that loses an in-flight turn reports unknown, never success", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);
  const admissionId = testAdmissionId("inflight");

  await submitAdmission(client, { sessionId: session.id, admissionId, text: "run" });
  server.deliver(session.id, admissionId);
  // No idle message is ever recorded: the service restarted mid-turn.
  const resolution = await reconcile(client, session.id, admissionId);
  assert.equal(resolution.state, "unknown");
  assert.match(String((resolution as { reason: string }).reason), /no terminal outcome/);
});

test("an admission absent from all persisted state reports unknown", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);
  const resolution = await reconcile(client, session.id, "msg_never_admitted");
  assert.equal(resolution.state, "unknown");
});

test("invalid admission ids are rejected before any request is made", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);
  await assert.rejects(submitAdmission(client, { sessionId: session.id, admissionId: "not-msg", text: "x" }), RangeError);
  await assert.rejects(reconcile(client, session.id, "not-msg"), RangeError);
  assert.equal(server.requests.length, 0, "local validation must precede network calls");
});

test("newAdmissionId satisfies the server-side message id pattern", () => {
  assert.match(newAdmissionId(), /^msg_/);
});

test("conflict helpers recognise the declared error", () => {
  assert.equal(isConflict({ name: "ConflictError" }), true);
  assert.equal(isConflict({ cause: { status: 409 } }), true);
  assert.equal(isConflict({ cause: { status: 500 } }), false);
  assert.equal(statusOf({ cause: { status: 500 } }), 500);
  assert.equal(statusOf(new Error("plain")), undefined);
});
