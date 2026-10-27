#!/usr/bin/env node
/**
 * TypeScript-side lifecycle worker for the step-00 integration spike.
 *
 * One command per invocation so the orchestrator can run each step in a separate
 * process (the disconnect criterion: the submitting process exits after printing
 * its durable admission receipt, the server-owned turn keeps running, and a
 * different process recovers the outcome after missing every event).
 *
 * It reuses the untouched TypeScript prototype modules and their real
 * `@opencode/client` construction — nothing here re-implements transport.
 *
 * Endpoint/auth come from `--url` plus the documented environment variables
 * (`OPENCODE_AUTH=basic`, `OPENCODE_BASIC_PASSWORD`, optional
 * `OPENCODE_BASIC_USERNAME`), so the password is never passed on the command
 * line. All output is a single JSON object on stdout.
 */
import { createRuntimeClient, resolveEndpoint, type RuntimeEndpoint } from "../typescript/src/client.ts";
import { newAdmissionId, type AdmissionId, type SessionId } from "../typescript/src/admission.ts";
import {
  createCriticSession,
  describeError,
  reconcile,
  submitAdmission,
} from "../typescript/src/lifecycle.ts";

/** Structural model reference, derived from the prototype's own signature. */
type Model = Parameters<typeof createCriticSession>[1]["model"];

/** Minimal message projection used for the evidence counts. */
interface MessageInfo {
  readonly type: string;
  readonly id: string;
  readonly outcome?: string;
}

interface Args {
  readonly command: string;
  readonly flags: Readonly<Record<string, string>>;
}

function parse(argv: readonly string[]): Args {
  const [command, ...rest] = argv;
  if (command === undefined) throw new Error("usage: ts_lifecycle.ts <info|create|submit|reconcile|critic> ...");
  const flags: Record<string, string> = {};
  for (let i = 0; i < rest.length; i += 1) {
    const token = rest[i] as string;
    if (token.startsWith("--")) {
      const key = token.slice(2);
      const value = rest[i + 1];
      if (value === undefined) throw new Error(`--${key} requires a value`);
      flags[key] = value;
      i += 1;
    }
  }
  return { command, flags };
}

function required(flags: Readonly<Record<string, string>>, key: string): string {
  const value = flags[key];
  if (value === undefined) throw new Error(`--${key} is required`);
  return value;
}

function endpointOf(flags: Readonly<Record<string, string>>): RuntimeEndpoint {
  return resolveEndpoint({ url: flags.url });
}

function modelFrom(value: string): Model {
  const slash = value.indexOf("/");
  if (slash <= 0 || slash === value.length - 1) throw new Error(`--model must be provider/id, got ${value}`);
  return { providerID: value.slice(0, slash), id: value.slice(slash + 1) };
}

function emit(payload: unknown): void {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

async function main(): Promise<number> {
  const { command, flags } = parse(process.argv.slice(2));
  const client = createRuntimeClient(endpointOf(flags));

  switch (command) {
    case "info": {
      const info = await client.server.info();
      emit({ prototype: "typescript", version: info.version, pid: info.pid });
      return 0;
    }
    case "create": {
      const session = await client.session.create({
        location: { directory: required(flags, "directory") },
        agent: required(flags, "agent"),
        model: modelFrom(required(flags, "model")),
        ...(flags.title === undefined ? {} : { title: flags.title }),
      });
      emit({ prototype: "typescript", session, fresh: session.parentID === undefined && session.fork === undefined });
      return 0;
    }
    case "submit": {
      const receipt = await submitAdmission(client, {
        sessionId: required(flags, "session") as SessionId,
        admissionId: (flags.id ?? newAdmissionId()) as AdmissionId,
        text: required(flags, "text"),
        delivery: (flags.delivery ?? "queue") as "queue" | "steer",
      });
      emit({
        prototype: "typescript",
        admissionId: receipt.admissionId,
        status: receipt.status,
        reconciled: receipt.reconciled,
        receipt,
        // A receipt is never a completion: it carries no outcome, only admission facts.
      });
      return 0;
    }
    case "reconcile": {
      const resolution = await reconcile(
        client,
        required(flags, "session") as SessionId,
        required(flags, "id") as AdmissionId,
      );
      emit({ prototype: "typescript", admissionId: flags.id, ...resolution });
      return 0;
    }
    case "messages": {
      const sessionId = required(flags, "session") as SessionId;
      const messages: MessageInfo[] = [];
      let cursor: string | undefined;
      for (let page = 0; page < 100; page += 1) {
        const response = await client.message.list({
          sessionID: sessionId,
          ...(cursor === undefined ? { order: "asc" as const } : { cursor }),
        });
        messages.push(...response.data);
        const next = response.cursor?.next;
        if (next === undefined || next === null || next === "") break;
        cursor = next;
      }
      const payload: Record<string, unknown> = {
        prototype: "typescript",
        count: messages.length,
        types: messages.map((message) => message.type),
        ids: messages.map((message) => message.id),
        idleOutcomes: messages.filter((message) => message.type === "idle").map((message) => message.outcome),
      };
      const admissionId = flags["admission-id"];
      if (admissionId !== undefined) {
        payload.userInputsForId = messages.filter((message) => message.type === "user" && message.id === admissionId).length;
      }
      emit(payload);
      return 0;
    }
    case "critic": {
      const critic = await createCriticSession(client, {
        directory: required(flags, "dir"),
        agent: required(flags, "agent"),
        model: modelFrom(required(flags, "model")),
      });
      const history = await client.message.list({ sessionID: critic.session.id, order: "asc" });
      emit({
        prototype: "typescript",
        session: critic.session,
        fresh: critic.fresh,
        historyLength: history.data.length,
      });
      return critic.fresh ? 0 : 2;
    }
    default:
      throw new Error(`unknown command ${JSON.stringify(command)}`);
  }
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
