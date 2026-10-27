import assert from "node:assert/strict";
import { test } from "node:test";
import { createRuntimeClient } from "../src/client.ts";
import { reconcile, submitAdmission } from "../src/lifecycle.ts";
import { MockServer, testAdmissionId } from "./support.ts";

function clientFor(server: MockServer) {
  return createRuntimeClient({ baseUrl: "http://127.0.0.1:4096", token: "token", fetch: server.fetch });
}

test("an earlier turn's terminal outcome is not attributed to the target turn", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);

  // An earlier turn that already succeeded.
  server.appendUser(session.id, testAdmissionId("earlier"), "first");
  server.appendAssistant(session.id);
  server.appendIdle(session.id, "succeeded");

  // The target turn: delivered, but no terminal outcome recorded yet.
  const target = testAdmissionId("target");
  server.appendUser(session.id, target, "second");

  const resolution = await reconcile(client, session.id, target);
  assert.equal(resolution.state, "unknown", "a prior idle must not mark a later turn complete");
  assert.match(String((resolution as { reason: string }).reason), /without an authoritative idle|no terminal outcome/);
});

test("a later turn's terminal outcome is not attributed across the next user message", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);

  const target = testAdmissionId("target");
  server.appendUser(session.id, target, "target turn");
  server.appendAssistant(session.id);
  // A different turn begins here and is the one that succeeds.
  server.appendUser(session.id, testAdmissionId("next"), "next turn");
  server.appendAssistant(session.id);
  server.appendIdle(session.id, "succeeded");

  const resolution = await reconcile(client, session.id, target);
  assert.equal(resolution.state, "unknown", "the next user message ends the target window");
});

test("a synthetic message also ends the turn window", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);

  const target = testAdmissionId("target");
  server.appendUser(session.id, target, "target turn");
  server.appendSynthetic(session.id, "background work");
  server.appendIdle(session.id, "succeeded");

  const resolution = await reconcile(client, session.id, target);
  assert.equal(resolution.state, "unknown");
});

test("an assistant message with a completion time is not accepted as a shortcut", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);

  const target = testAdmissionId("target");
  server.appendUser(session.id, target, "target turn");
  server.appendAssistant(session.id); // has time.completed, but no idle marker

  const resolution = await reconcile(client, session.id, target);
  assert.equal(resolution.state, "unknown", "only an authoritative idle outcome counts");
});

test("an authoritative idle inside the target window is used", async () => {
  const server = new MockServer();
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);

  const target = testAdmissionId("target");
  server.appendUser(session.id, target, "target turn");
  server.appendAssistant(session.id);
  server.appendIdle(session.id, "interrupted");

  assert.deepEqual(await reconcile(client, session.id, target), { state: "interrupted" });
});

test("pagination finds an old target and then its terminal outcome on a later page", async () => {
  const server = new MockServer({ pageSize: 1 });
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);

  // Page 1: the oldest message is the target admission.
  const target = testAdmissionId("oldest");
  server.appendUser(session.id, target, "oldest turn");
  // Page 2: the assistant turn, page 3: the terminal idle.
  server.appendAssistant(session.id);
  server.appendIdle(session.id, "succeeded");

  const resolution = await reconcile(client, session.id, target);
  assert.deepEqual(resolution, { state: "succeeded" });
  const messageReads = server.requests.filter((request) => request.path.endsWith("/message"));
  assert.ok(messageReads.length >= 3, `expected to follow cursor.next across pages, saw ${messageReads.length}`);
});

test("a repeated pagination cursor is bounded and reported as unknown", async () => {
  const server = new MockServer({ pageSize: 1, stickyCursor: true });
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);

  const target = testAdmissionId("target");
  server.appendUser(session.id, target, "target turn");
  server.appendAssistant(session.id);

  const resolution = await reconcile(client, session.id, target);
  assert.equal(resolution.state, "unknown");
  assert.match(String((resolution as { reason: string }).reason), /could not be read completely/);

  // Bounded: it must not have read pages forever.
  const messageReads = server.requests.filter((request) => request.path.endsWith("/message"));
  assert.ok(messageReads.length < 12, `repeated cursor must stop the walk, saw ${messageReads.length} reads`);
});

test("a page cap is bounded and reported as unknown", async () => {
  const server = new MockServer({ pageSize: 1 });
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);

  const target = testAdmissionId("target");
  server.appendUser(session.id, target, "target turn");
  server.appendAssistant(session.id);
  server.appendIdle(session.id, "succeeded");

  const resolution = await reconcile(client, session.id, target, { maxPages: 1 });
  assert.equal(resolution.state, "unknown");
  const messageReads = server.requests.filter((request) => request.path.endsWith("/message"));
  assert.equal(messageReads.length, 1);
});

test("submit reconciles a delivered duplicate that appears only on a later page", async () => {
  const server = new MockServer({ pageSize: 1 });
  const session = server.seedSession({ directory: "/tmp/opencode" });
  const client = clientFor(server);

  // The target admission was delivered long ago and is the oldest message.
  const target = testAdmissionId("old");
  server.appendUser(session.id, target, "old turn");
  for (let i = 0; i < 3; i += 1) {
    server.appendUser(session.id, testAdmissionId(`noise${i}`), "later turn");
  }

  // A retry with the same id conflicts; the duplicate lookup must page to find it.
  const retry = await submitAdmission(client, { sessionId: session.id, admissionId: target, text: "old turn" });
  assert.equal(retry.status, "duplicate-accepted");
  assert.equal(retry.reconciled, true);
});
