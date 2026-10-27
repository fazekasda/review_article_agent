#!/usr/bin/env node
/**
 * Prototype CLI for the spike acceptance criteria.
 *
 *   check      authenticated server.info round-trip against an explicit URL
 *   submit     durably admit one input; prints a durable admission receipt and exits
 *   result     fresh client retrieves the persisted result after missed events
 *   reconcile  one-shot reconciliation of an admission
 *   critic     create a fresh critic session with pinned role/model
 *   decisions  build/print a typed decision request (live calls require --live)
 *
 * `submit` exits after printing the receipt: closing the CLI must not stop the
 * server-owned task, so a later `result` invocation is what recovers the outcome.
 */
import { createRuntimeClient, resolveEndpoint, type AuthScheme, type RuntimeEndpoint } from "./client.ts";
import { newAdmissionId, type AdmissionId, type Delivery, type SessionId } from "./admission.ts";
import {
  createCriticSession,
  describeError,
  reconcile,
  runDecisions,
  submitAdmission,
  waitForResolution,
  type DecisionPlan,
} from "./lifecycle.ts";
import type { GenerateDecisionsInput, ModelRef } from "@opencode/client";

interface ParsedArgs {
  readonly command: string | undefined;
  readonly positional: readonly string[];
  readonly flags: Readonly<Record<string, string>>;
}

const VALUE_FLAGS = new Set(["url", "auth", "token", "username", "password", "session", "id", "text", "delivery", "wait-ms", "dir", "agent", "model", "variant", "questions"]);

export function parseArgs(argv: readonly string[]): ParsedArgs {
  const [command, ...rest] = argv;
  const positional: string[] = [];
  const flags: Record<string, string> = {};
  for (let i = 0; i < rest.length; i += 1) {
    const token = rest[i] as string;
    if (token.startsWith("--")) {
      const key = token.slice(2);
      if (VALUE_FLAGS.has(key)) {
        const value = rest[i + 1];
        if (value === undefined) throw new Error(`--${key} requires a value`);
        flags[key] = value;
        i += 1;
      } else {
        flags[key] = "true";
      }
    } else {
      positional.push(token);
    }
  }
  return { command, positional, flags };
}

function endpointFrom(flags: Readonly<Record<string, string>>): RuntimeEndpoint {
  return resolveEndpoint({
    url: flags.url,
    // An unknown scheme is rejected by resolveEndpoint; the cast only satisfies
    // the union type, the runtime validation is the guard.
    auth: flags.auth as AuthScheme | undefined,
    token: flags.token,
    username: flags.username,
    password: flags.password,
  });
}

function print(value: unknown): void {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

function modelFromFlags(flags: Readonly<Record<string, string>>): ModelRef {
  const ref = flags.model;
  if (ref === undefined) throw new Error("--model provider/model is required");
  const slash = ref.indexOf("/");
  if (slash <= 0 || slash === ref.length - 1) {
    throw new Error(`--model must be provider/model, got ${JSON.stringify(ref)}`);
  }
  return {
    providerID: ref.slice(0, slash),
    id: ref.slice(slash + 1),
    ...(flags.variant === undefined ? {} : { variant: flags.variant }),
  };
}

async function main(): Promise<number> {
  const { command, flags } = parseArgs(process.argv.slice(2));
  if (command === undefined || command === "help" || flags.help === "true") {
    process.stdout.write(
      "usage: cli.ts <check|submit|result|reconcile|critic|decisions> [--url URL] [--auth bearer|basic] [--token TOKEN | --username USER --password PASS] ...\n",
    );
    return command === undefined ? 1 : 0;
  }

  const client = createRuntimeClient(endpointFrom(flags));

  switch (command) {
    case "check": {
      const info = await client.server.info();
      print({ ok: true, server: info });
      return 0;
    }
    case "submit": {
      const sessionId = required(flags, "session") as SessionId;
      const admissionId = (flags.id ?? newAdmissionId()) as AdmissionId;
      const text = required(flags, "text");
      const delivery = (flags.delivery ?? "queue") as Delivery;
      const receipt = await submitAdmission(client, { sessionId, admissionId, text, delivery });
      print(receipt);
      return 0;
    }
    case "result": {
      const sessionId = required(flags, "session") as SessionId;
      const admissionId = required(flags, "id") as AdmissionId;
      const waitMs = flags["wait-ms"] === undefined ? 0 : Number(flags["wait-ms"]);
      if (waitMs > 0) {
        const waited = await waitForResolution(client, sessionId, admissionId, { timeoutMs: waitMs });
        print({ admissionId, ...waited.resolution, timedOut: waited.timedOut, polls: waited.polls });
      } else {
        const resolution = await reconcile(client, sessionId, admissionId);
        print({ admissionId, ...resolution });
      }
      return 0;
    }
    case "reconcile": {
      const sessionId = required(flags, "session") as SessionId;
      const admissionId = required(flags, "id") as AdmissionId;
      print(await reconcile(client, sessionId, admissionId));
      return 0;
    }
    case "critic": {
      const directory = required(flags, "dir");
      const agent = required(flags, "agent");
      const model = modelFromFlags(flags);
      const critic = await createCriticSession(client, { directory, agent, model });
      print({ fresh: critic.fresh, session: critic.session });
      return critic.fresh ? 0 : 2;
    }
    case "decisions": {
      const plan: DecisionPlan = {
        model: modelFromFlags(flags),
        input: [{ type: "text", text: "placeholder passage" }],
        questions: [
          { type: "predicate", name: "is_original", instructions: "Is this the original source?" },
          {
            type: "choice",
            name: "study_design",
            instructions: "Which design?",
            choices: [{ value: "rct" }, { value: "cohort" }, { value: "other" }],
          },
          {
            type: "score",
            name: "confidence",
            instructions: "How confident?",
            levels: [{ label: "low" }, { label: "medium" }, { label: "high" }],
          },
        ],
      };
      const request: GenerateDecisionsInput = {
        model: plan.model,
        input: plan.input,
        questions: plan.questions,
      };
      if (flags.live !== "true") {
        print({ live: false, note: "dry run: no model call made", request });
        return 0;
      }
      const summary = await runDecisions(client, plan);
      print({ live: true, summary });
      return 0;
    }
    default:
      process.stderr.write(`unknown command ${JSON.stringify(command)}\n`);
      return 1;
  }
}

function required(flags: Readonly<Record<string, string>>, key: string): string {
  const value = flags[key];
  if (value === undefined) throw new Error(`--${key} is required`);
  return value;
}

main().then(
  (code) => {
    process.exitCode = code;
  },
  (error: unknown) => {
    process.stderr.write(`${describeError(error)}\n`);
    process.exitCode = 1;
  },
);
