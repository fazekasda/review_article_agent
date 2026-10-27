import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { MockServer, startHttpServer } from "./support.ts";

const CLI = fileURLToPath(new URL("../src/cli.ts", import.meta.url));
const PACKAGE_ROOT = fileURLToPath(new URL("..", import.meta.url));

interface CliResult {
  readonly status: number | null;
  readonly stdout: string;
  readonly stderr: string;
}

/**
 * Spawn the CLI as a genuinely separate process. This is asynchronous on purpose:
 * the in-process mock server must stay responsive while the child talks to it.
 */
function runCli(args: readonly string[]): Promise<CliResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [CLI, ...args], {
      cwd: PACKAGE_ROOT,
      env: {
        ...process.env,
        OPENCODE_URL: "",
        OPENCODE_TOKEN: "",
        OPENCODE_AUTH: "",
        OPENCODE_BASIC_USERNAME: "",
        OPENCODE_BASIC_PASSWORD: "",
      },
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString("utf8");
    });
    child.on("error", reject);
    child.on("close", (status) => resolve({ status, stdout, stderr }));
  });
}

test("a CLI that exits after submit does not stop the server-owned task", async () => {
  const server = new MockServer({ requireToken: "tok" });
  const http = await startHttpServer(server);
  try {
    const session = server.seedSession({ directory: "/tmp/opencode" });
    const admissionId = "msg_cli_1";

    // Process 1: submit and exit. A receipt is printed, not a completion.
    const submit = await runCli([
      "submit",
      "--url",
      http.url,
      "--token",
      "tok",
      "--session",
      session.id,
      "--id",
      admissionId,
      "--text",
      "review the changes",
    ]);
    assert.equal(submit.status, 0, submit.stderr);
    const receipt = JSON.parse(submit.stdout) as { kind: string; status: string; admissionId: string };
    assert.equal(receipt.kind, "admission");
    assert.equal(receipt.status, "accepted");
    assert.equal(receipt.admissionId, admissionId);

    // Process 2: read persisted state while the admission is still queued.
    const pending = await runCli(["result", "--url", http.url, "--token", "tok", "--session", session.id, "--id", admissionId]);
    assert.equal(pending.status, 0, pending.stderr);
    assert.equal(JSON.parse(pending.stdout).state, "pending");

    // The server-owned task runs to completion with no client attached.
    server.deliver(session.id, admissionId);
    server.finish(session.id, "succeeded");

    // Process 3: a second, independent client recovers the result.
    const finished = await runCli(["result", "--url", http.url, "--token", "tok", "--session", session.id, "--id", admissionId]);
    assert.equal(finished.status, 0, finished.stderr);
    const resolution = JSON.parse(finished.stdout) as { state: string };
    assert.equal(resolution.state, "succeeded");

    // Retrying the same admission id through the CLI is still one input.
    const retry = await runCli([
      "submit",
      "--url",
      http.url,
      "--token",
      "tok",
      "--session",
      session.id,
      "--id",
      admissionId,
      "--text",
      "review the changes",
    ]);
    assert.equal(retry.status, 0, retry.stderr);
    assert.equal(JSON.parse(retry.stdout).status, "duplicate-accepted");
    assert.deepEqual(server.pendingAdmissions(session.id), []);
  } finally {
    await http.close();
  }
});

test("the CLI refuses an unauthenticated or implicit endpoint", async () => {
  const noUrl = await runCli(["result", "--session", "ses_x", "--id", "msg_y"]);
  assert.equal(noUrl.status, 1);
  assert.match(noUrl.stderr, /explicit server URL required/);

  const noToken = await runCli(["result", "--url", "http://127.0.0.1:4096", "--session", "ses_x", "--id", "msg_y"]);
  assert.equal(noToken.status, 1);
  assert.match(noToken.stderr, /refusing unauthenticated/);
});

test("the CLI authenticates with Basic when asked", async () => {
  const server = new MockServer({ requireBasic: { username: "opencode", password: "pw" } });
  const http = await startHttpServer(server);
  try {
    const ok = await runCli(["check", "--url", http.url, "--auth", "basic", "--password", "pw"]);
    assert.equal(ok.status, 0, ok.stderr);
    assert.equal((JSON.parse(ok.stdout) as { ok: boolean }).ok, true);

    const bad = await runCli(["check", "--url", http.url, "--auth", "basic", "--password", "wrong"]);
    assert.equal(bad.status, 1);
    assert.match(bad.stderr, /Unauthorized|credentials/);
  } finally {
    await http.close();
  }
});

test("the decisions command is dry-run by default (no paid call)", async () => {
  const dry = await runCli([
    "decisions",
    "--url",
    "http://127.0.0.1:4096",
    "--token",
    "tok",
    "--model",
    "openrouter/typesafe/jev-1.14",
  ]);
  assert.equal(dry.status, 0, dry.stderr);
  const output = JSON.parse(dry.stdout) as { live: boolean; request: { questions: unknown[] } };
  assert.equal(output.live, false);
  assert.equal(output.request.questions.length, 3);
});
