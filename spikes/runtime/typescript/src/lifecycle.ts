/**
 * Deterministic lifecycle helpers over the official V2 client.
 *
 * These functions express the spike's acceptance criteria directly:
 *  - submitAdmission: durably admit one input; retries reconcile instead of
 *    double-admitting; the receipt is explicitly not a completion.
 *  - reconcile / waitForResolution: read persisted inbox + message timeline to
 *    recover a result after missed events, or report pending/interrupted/unknown.
 *  - createCriticSession: a fresh, independent critic session with pinned agent
 *    and model, never a fork of the writer's history.
 *  - runDecisions: typed predicate/choice/score handling with per-question errors.
 */
import type {
  GenerateDecisionAnswer,
  GenerateDecisionResult,
  GenerateDecisionsInput,
  GenerateErrorAnswer,
  ModelRef,
  OpenCodeClient,
  SessionInboxInfo,
  SessionInboxUser,
  SessionInfo,
  SessionMessageInfo,
  SessionStructuredError,
} from "@opencode/client";
import {
  assertAdmissionId,
  assertSessionId,
  type AdmissionId,
  type AdmissionReceipt,
  type Delivery,
  type Resolution,
  type SessionId,
} from "./admission.ts";

/** HTTP status carried by a declared/undeclared client error, when present. */
export function statusOf(error: unknown): number | undefined {
  const cause = (error as { cause?: unknown } | null)?.cause;
  if (cause !== null && typeof cause === "object" && "status" in cause) {
    const status = (cause as { status?: unknown }).status;
    if (typeof status === "number") return status;
  }
  return undefined;
}

/** The V2 client names declared errors from the response `_tag`; 409 is `ConflictError`. */
export function isConflict(error: unknown): boolean {
  const name = (error as { name?: unknown } | null)?.name;
  return name === "ConflictError" || statusOf(error) === 409;
}

export function describeError(error: unknown): string {
  if (error instanceof Error) return `${error.name}: ${error.message}`;
  return String(error);
}

function receiptFromInbox(
  inbox: SessionInboxUser,
  serverVersion: string,
  status: AdmissionReceipt["status"],
): AdmissionReceipt {
  return {
    kind: "admission",
    status,
    sessionId: inbox.sessionID,
    admissionId: inbox.id,
    delivery: inbox.delivery,
    admittedAt: inbox.time.created,
    serverVersion,
    reconciled: status === "duplicate-accepted",
  };
}

function userInboxItem(items: readonly SessionInboxInfo[], admissionId: AdmissionId): SessionInboxUser | undefined {
  for (const item of items) {
    if (item.type === "user" && item.id === admissionId) return item;
  }
  return undefined;
}

function userMessage(items: readonly SessionMessageInfo[], admissionId: AdmissionId): SessionMessageInfo | undefined {
  for (const item of items) {
    if (item.type === "user" && item.id === admissionId) return item;
  }
  return undefined;
}

export interface SubmitInput {
  readonly sessionId: SessionId;
  readonly admissionId: AdmissionId;
  readonly text: string;
  readonly delivery?: Delivery;
  readonly resume?: boolean;
}

/**
 * Durably admit one session input.
 *
 * On success the server returns the `Session.Inbox.User` it enqueued. If the same
 * admission id is submitted again the server replies 409 Conflict; we then reconcile
 * and report a `duplicate-accepted` receipt, proving exactly one accepted input
 * rather than a second enqueue.
 */
export async function submitAdmission(
  client: OpenCodeClient,
  input: SubmitInput,
  dependencies: { readonly serverVersion?: string } = {},
): Promise<AdmissionReceipt> {
  const sessionId = assertSessionId(input.sessionId);
  const admissionId = assertAdmissionId(input.admissionId);
  const serverVersion = dependencies.serverVersion ?? (await client.server.info()).version;

  try {
    const admitted = await client.session.prompt({
      sessionID: sessionId,
      id: admissionId,
      text: input.text,
      ...(input.delivery === undefined ? {} : { delivery: input.delivery }),
      ...(input.resume === undefined ? {} : { resume: input.resume }),
    });
    return receiptFromInbox(admitted, serverVersion, "accepted");
  } catch (error) {
    if (!isConflict(error)) throw error;

    const inbox = await client.session.inbox.list({ sessionID: sessionId });
    const enqueued = userInboxItem(inbox, admissionId);
    if (enqueued !== undefined) {
      return receiptFromInbox(enqueued, serverVersion, "duplicate-accepted");
    }

    // Already delivered: the admission id now exists as a persisted user message.
    const messages = await client.message.list({ sessionID: sessionId, order: "asc" });
    if (userMessage(messages.data, admissionId) !== undefined) {
      return {
        kind: "admission",
        status: "duplicate-accepted",
        sessionId,
        admissionId,
        delivery: input.delivery ?? "queue",
        admittedAt: (userMessage(messages.data, admissionId) as { time: { created: number } }).time.created,
        serverVersion,
        reconciled: true,
      };
    }

    // Conflict but no surviving record: do not silently invent an admission.
    throw new Error(
      `admission ${admissionId} was rejected as a conflict but is absent from the inbox and message timeline`,
      { cause: error },
    );
  }
}

/**
 * Rebuild the state of an admission from persisted server state.
 *
 * Event subscriptions are live-only, so a client that was disconnected (or a CLI
 * that exited) has no way to observe what happened. Reading the inbox and the
 * message timeline reconstructs it:
 *   - still in the inbox           -> pending
 *   - user message + idle message  -> succeeded / failed / interrupted
 *   - user message, no idle        -> unknown (in flight, or lost to a restart)
 *   - no record at all             -> unknown
 */
export async function reconcile(
  client: OpenCodeClient,
  sessionId: SessionId,
  admissionId: AdmissionId,
): Promise<Resolution> {
  assertSessionId(sessionId);
  assertAdmissionId(admissionId);

  const inbox = await client.session.inbox.list({ sessionID: sessionId });
  if (userInboxItem(inbox, admissionId) !== undefined) return { state: "pending" };

  const messages = await client.message.list({ sessionID: sessionId, order: "asc" });
  const index = messages.data.findIndex((message) => message.type === "user" && message.id === admissionId);
  if (index === -1) {
    return {
      state: "unknown",
      reason: `admission ${admissionId} is neither enqueued nor present in the message timeline`,
    };
  }

  for (const message of messages.data.slice(index + 1)) {
    if (message.type === "idle") {
      if (message.outcome === "succeeded") return { state: "succeeded" };
      if (message.outcome === "interrupted") return { state: "interrupted" };
      const detail = message.error === undefined ? undefined : structuredErrorSummary(message.error);
      return detail === undefined ? { state: "failed" } : { state: "failed", detail };
    }
  }

  return {
    state: "unknown",
    reason: `admission ${admissionId} was delivered but no terminal outcome was recorded (in flight or interrupted by a restart)`,
  };
}

function structuredErrorSummary(error: SessionStructuredError): string {
  const message = (error as { message?: unknown }).message;
  return typeof message === "string" ? message : JSON.stringify(error);
}

export interface WaitOptions {
  readonly timeoutMs: number;
  readonly pollMs?: number;
  readonly signal?: AbortSignal;
}

export interface WaitResult {
  readonly resolution: Resolution;
  readonly timedOut: boolean;
  readonly polls: number;
}

/**
 * Optional bounded wait. Returns as soon as the admission reaches a terminal state
 * or the timeout elapses. Never converts a timeout into a success.
 */
export async function waitForResolution(
  client: OpenCodeClient,
  sessionId: SessionId,
  admissionId: AdmissionId,
  options: WaitOptions,
): Promise<WaitResult> {
  const pollMs = options.pollMs ?? 500;
  const deadline = Date.now() + options.timeoutMs;
  let polls = 0;
  for (;;) {
    polls += 1;
    const resolution = await reconcile(client, sessionId, admissionId);
    const terminal = resolution.state !== "pending" && resolution.state !== "unknown";
    if (terminal) return { resolution, timedOut: false, polls };
    if (Date.now() + pollMs > deadline) return { resolution, timedOut: true, polls };
    await delay(pollMs, options.signal);
  }
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted === true) {
      reject(new Error("aborted"));
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(new Error("aborted"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export interface CriticOptions {
  readonly directory: string;
  /** Pinned role (Agent.ID). */
  readonly agent: string;
  /** Pinned model. */
  readonly model: ModelRef;
  readonly title?: string;
}

export interface CriticSession {
  readonly session: SessionInfo;
  /** True only when the session is a brand-new head, not a fork or child of writer history. */
  readonly fresh: boolean;
}

/**
 * Create a fresh critic session with a pinned role and model.
 *
 * A writer-history fork would inherit the writer's context; that is not a clean
 * critic. We call session.create with no parentID and no fork boundary, so the
 * server starts an independent session at the requested directory.
 */
export async function createCriticSession(client: OpenCodeClient, options: CriticOptions): Promise<CriticSession> {
  const session = await client.session.create({
    location: { directory: options.directory },
    agent: options.agent,
    model: options.model,
    ...(options.title === undefined ? {} : { title: options.title }),
  });
  const fresh = session.fork === undefined && session.parentID === undefined;
  return { session, fresh };
}

export interface DecisionPlan {
  readonly model: ModelRef;
  readonly input: GenerateDecisionsInput["input"];
  /** Predicate, choice, and score questions; each answer type is handled below. */
  readonly questions: GenerateDecisionsInput["questions"];
}

export function buildDecisionRequest(plan: DecisionPlan): GenerateDecisionsInput {
  return { model: plan.model, input: plan.input, questions: plan.questions };
}

export interface DecisionSummary {
  readonly all: readonly GenerateDecisionAnswer[];
  readonly answers: {
    readonly predicate: readonly Extract<GenerateDecisionAnswer, { type: "predicate" }>[];
    readonly choice: readonly Extract<GenerateDecisionAnswer, { type: "choice" }>[];
    readonly score: readonly Extract<GenerateDecisionAnswer, { type: "score" }>[];
  };
  /** Per-question failures. A failure here does not invalidate the other answers. */
  readonly errors: readonly GenerateErrorAnswer[];
  readonly byName: Readonly<Record<string, GenerateDecisionAnswer>>;
  readonly cost?: number;
}

export async function runDecisions(
  client: OpenCodeClient,
  plan: DecisionPlan,
): Promise<DecisionSummary> {
  const result: GenerateDecisionResult = await client.generate.decisions(buildDecisionRequest(plan));
  return summarizeDecisions(result);
}

export function summarizeDecisions(result: GenerateDecisionResult): DecisionSummary {
  const byName: Record<string, GenerateDecisionAnswer> = {};
  const predicate: Extract<GenerateDecisionAnswer, { type: "predicate" }>[] = [];
  const choice: Extract<GenerateDecisionAnswer, { type: "choice" }>[] = [];
  const score: Extract<GenerateDecisionAnswer, { type: "score" }>[] = [];
  const errors: GenerateErrorAnswer[] = [];
  for (const answer of result.answers) {
    byName[answer.name] = answer;
    if (answer.type === "predicate") predicate.push(answer);
    else if (answer.type === "choice") choice.push(answer);
    else if (answer.type === "score") score.push(answer);
    else errors.push(answer);
  }
  return {
    all: result.answers,
    answers: { predicate, choice, score },
    errors,
    byName,
    ...(result.cost === undefined ? {} : { cost: result.cost }),
  };
}
