import assert from "node:assert/strict";
import { test } from "node:test";
import type { GenerateDecisionsInput } from "@opencode/client";
import { createRuntimeClient } from "../src/client.ts";
import { createCriticSession, runDecisions, summarizeDecisions } from "../src/lifecycle.ts";
import { jsonResponse, MockServer } from "./support.ts";

function clientFor(server: MockServer) {
  return createRuntimeClient({ baseUrl: "http://127.0.0.1:4096", token: "token", fetch: server.fetch });
}

test("a critic session is fresh, independent, and role/model pinned", async () => {
  const server = new MockServer();
  const writer = server.seedSession({ directory: "/workspace", agent: "build", model: { providerID: "openrouter", id: "writer-model" } });
  const client = clientFor(server);

  const critic = await createCriticSession(client, {
    directory: "/workspace",
    agent: "critic",
    model: { providerID: "openrouter", id: "typesafe/jev-1.14" },
    title: "critic",
  });

  assert.equal(critic.fresh, true);
  assert.notEqual(critic.session.id, writer.id);
  assert.equal(critic.session.fork, undefined);
  assert.equal(critic.session.parentID, undefined);
  assert.equal(critic.session.agent, "critic");
  assert.deepEqual(critic.session.model, { providerID: "openrouter", id: "typesafe/jev-1.14" });

  const createRequest = server.requests.find((request) => request.method === "POST" && request.path === "/api/session");
  assert.deepEqual(createRequest?.body, {
    location: { directory: "/workspace" },
    agent: "critic",
    model: { providerID: "openrouter", id: "typesafe/jev-1.14" },
    title: "critic",
  });
});

test("a writer-history fork is not accepted as a fresh critic", async () => {
  const server = new MockServer();
  const client = clientFor(server);
  const writer = server.seedSession({ directory: "/workspace", agent: "build" });

  // A fork inherits the writer's context; the client never requests one for a critic.
  const forked = await client.session.create({
    location: { directory: "/workspace" },
    parentID: writer.id,
  });
  assert.equal(forked.parentID, writer.id);
  const fresh = forked.fork === undefined && forked.parentID === undefined;
  assert.equal(fresh, false);
});

test("typed predicate/choice/score answers are separated", async () => {
  const server = new MockServer();
  const client = clientFor(server);

  const summary = await runDecisions(client, {
    model: { providerID: "openrouter", id: "typesafe/jev-1.14" },
    input: [{ type: "text", text: "A passage to classify." }],
    questions: [
      { type: "predicate", name: "is_original", instructions: "Original research?" },
      {
        type: "choice",
        name: "design",
        instructions: "Study design?",
        choices: [{ value: "rct" }, { value: "cohort" }],
      },
      {
        type: "score",
        name: "confidence",
        instructions: "Confidence?",
        levels: [{ label: "low" }, { label: "medium" }, { label: "high" }],
      },
    ],
  });

  assert.equal(summary.errors.length, 0);
  assert.equal(summary.answers.predicate.length, 1);
  assert.equal(summary.answers.choice.length, 1);
  assert.equal(summary.answers.score.length, 1);
  assert.equal(summary.answers.predicate[0]?.probability, 0.7);
  assert.equal(summary.answers.choice[0]?.choice, "rct");
  assert.equal(summary.answers.score[0]?.score, 3);
  assert.equal(summary.byName["confidence"]?.type, "score");
});

test("a per-question failure is isolated from successful answers", () => {
  const summary = summarizeDecisions({
    answers: [
      { type: "predicate", name: "is_original", probability: 0.5 },
      { type: "error", name: "design", reason: "unparseable", message: "model returned prose" },
      { type: "score", name: "confidence", score: 2, probabilities: [], confidence: 0.4 },
    ],
  });

  assert.equal(summary.errors.length, 1);
  assert.equal(summary.errors[0]?.name, "design");
  assert.equal(summary.errors[0]?.reason, "unparseable");
  assert.equal(summary.answers.choice.length, 0);
  assert.equal(summary.answers.predicate.length, 1);
  assert.equal(summary.answers.score.length, 1);
});

test("the decision request is sent to the documented route", async () => {
  const server = new MockServer();
  const client = clientFor(server);
  const plan: GenerateDecisionsInput = {
    model: { providerID: "openrouter", id: "typesafe/jev-1.14" },
    input: [{ type: "text", text: "x" }],
    questions: [{ type: "predicate", name: "is_original", instructions: "?" }],
  };
  await client.generate.decisions(plan);
  const call = server.requests.find((request) => request.path === "/api/experimental/generate/decisions");
  assert.equal(call?.method, "POST");
});

test("a declared decision-route error is surfaced", async () => {
  const server = new MockServer();
  server.failNext("POST", "/api/experimental/generate/decisions", () => jsonResponse({ _tag: "ModelNotFoundError", message: "no such model" }, 404));
  const client = clientFor(server);
  await assert.rejects(
    runDecisions(client, {
      model: { providerID: "openrouter", id: "nope" },
      input: [{ type: "text", text: "x" }],
      questions: [{ type: "predicate", name: "p", instructions: "?" }],
    }),
    (error: unknown) => {
      assert.equal((error as { name?: string }).name, "ModelNotFoundError");
      return true;
    },
  );
});
