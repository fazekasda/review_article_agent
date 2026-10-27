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
  MessageListInput,
  ModelRef,
  OpenCodeClient,
  SessionInboxInfo,
  SessionInboxUser,
  SessionInfo,
  SessionMessageIdle,
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

/**
 * Options shared by reconciliation and its callers. Every option exists because a
 * single unbounded read can hang a caller or wander a hostile timeline.
 */
export interface ReconcileOptions {
  /** Caller cancellation; combined with the per-request deadline. */
  readonly signal?: AbortSignal | undefined;
  /** Per-HTTP-request deadline so one stalled request cannot block a bounded wait. */
  readonly requestTimeoutMs?: number | undefined;
  /** Safety cap on timeline pages walked before giving up and reporting unknown. */
  readonly maxPages?: number | undefined;
  /** Page size requested per call. */
  readonly pageLimit?: number | undefined;
}

function requestOptionsFor(options: ReconcileOptions): { signal: AbortSignal } | undefined {
  const signals: AbortSignal[] = [];
  if (options.signal !== undefined) signals.push(options.signal);
  if (options.requestTimeoutMs !== undefined) signals.push(AbortSignal.timeout(options.requestTimeoutMs));
  if (signals.length === 0) return undefined;
  const signal = signals.length === 1 ? (signals[0] as AbortSignal) : AbortSignal.any(signals);
  return { signal };
}

type Visit = (message: SessionMessageInfo) => "continue" | "stop";

interface WalkResult {
  /** True only when the timeline was followed through to its end. */
  readonly complete: boolean;
  /** True when the visitor asked to stop (a deliberate, known boundary). */
  readonly stopped: boolean;
  /** Why a non-complete walk ended without a deliberate stop. */
  readonly reason?: string;
}

/**
 * Walk the message timeline oldest-first, following `cursor.next`.
 *
 * The read is bounded in three ways so a caller can never hang or loop forever:
 * a page cap, a repeated-cursor guard, and a per-request deadline. When any of
 * them triggers, the walk reports `complete: false`; callers must then treat the
 * result as ambiguous rather than guessing.
 */
async function walkTimeline(
  client: OpenCodeClient,
  sessionId: SessionId,
  visit: Visit,
  options: ReconcileOptions,
): Promise<WalkResult> {
  const maxPages = options.maxPages ?? 100;
  const requested = new Set<string>();
  const visited = new Set<string>();
  let cursor: string | undefined;
  for (let page = 1; ; page += 1) {
    if (cursor !== undefined) {
      if (requested.has(cursor)) {
        return { complete: false, stopped: false, reason: "the server repeated a pagination cursor" };
      }
      requested.add(cursor);
    }
    const input: MessageListInput = {
      sessionID: sessionId,
      ...(options.pageLimit === undefined ? {} : { limit: options.pageLimit }),
      // The contract says not to combine a cursor with an explicit order.
      ...(cursor === undefined ? { order: "asc" } : { cursor }),
    };
    const response = await client.message.list(input, requestOptionsFor(options));
    for (const message of response.data) {
      if (visited.has(message.id)) {
        return { complete: false, stopped: false, reason: "the timeline repeated a message id" };
      }
      visited.add(message.id);
      if (visit(message) === "stop") return { complete: false, stopped: true };
    }
    const next = response.cursor?.next;
    if (next === undefined || next === null || next === "") return { complete: true, stopped: false };
    if (page >= maxPages) {
      return { complete: false, stopped: false, reason: `the page cap of ${maxPages} was reached` };
    }
    cursor = next;
  }
}

/** Search the whole (bounded) timeline for the admission's delivered user message. */
async function findDeliveredAdmission(
  client: OpenCodeClient,
  sessionId: SessionId,
  admissionId: AdmissionId,
  options: ReconcileOptions,
): Promise<{ readonly found: boolean; readonly createdAt?: number; readonly complete: boolean }> {
  let createdAt: number | undefined;
  const walk = await walkTimeline(
    client,
    sessionId,
    (message) => {
      if (message.type === "user" && message.id === admissionId) {
        createdAt = message.time.created;
        return "stop";
      }
      return "continue";
    },
    options,
  );
  return {
    found: createdAt !== undefined,
    ...(createdAt === undefined ? {} : { createdAt }),
    complete: walk.complete,
  };
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
 * On success the server returns the `Session.Inbox.User` it holds. A repeated
 * admission id is answered by the server either with `200` (idempotent: it returns
 * the already-held inbox item) or with `409 ConflictError`. This was observed live:
 * a real harness retry of the same id returned `200`, not always `409`. Both mean
 * exactly one accepted input, and neither may be reported as a completion. Because
 * a `200` retry is indistinguishable from a first accept in that response alone,
 * `status` is `duplicate-accepted` only when the conflict path reconciled a
 * pre-existing record; a `200` retry is reported as `accepted`. The single-input
 * guarantee is the server's; the client never enqueues a second time.
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
    // The timeline is paginated and bounded, so this cannot miss an old admission
    // on a later page or loop forever.
    const delivered = await findDeliveredAdmission(client, sessionId, admissionId, {});
    if (delivered.found) {
      return {
        kind: "admission",
        status: "duplicate-accepted",
        sessionId,
        admissionId,
        delivery: input.delivery ?? "queue",
        admittedAt: delivered.createdAt ?? Date.now(),
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
 * Rebuild the state of one admission from persisted server state.
 *
 * Event subscriptions are live-only, so a client that was disconnected (or a CLI
 * that exited) has no way to observe what happened. Reading the inbox and the
 * message timeline reconstructs it:
 *   - still in the inbox                      -> pending
 *   - authoritative `idle` inside this turn   -> succeeded / failed / interrupted
 *   - turn ended (next user/synthetic or end)  -> unknown (no terminal outcome)
 *   - target absent from a fully-read timeline -> unknown
 *   - timeline unreadable/truncated/ambiguous  -> unknown
 *
 * The turn window starts at the target user message and ends at the next
 * `user`/`synthetic` message (exclusive) or the end of the timeline. An `idle`
 * that belongs to an earlier turn (before the target) or a later turn (after the
 * next user message) is never attributed to the target.
 *
 * Completion is taken only from an authoritative `idle` outcome. An assistant
 * message that looks finished (`time.completed`) is deliberately *not* accepted
 * as a shortcut, because the server records the authoritative terminal marker
 * separately.
 */
export async function reconcile(
  client: OpenCodeClient,
  sessionId: SessionId,
  admissionId: AdmissionId,
  options: ReconcileOptions = {},
): Promise<Resolution> {
  assertSessionId(sessionId);
  assertAdmissionId(admissionId);

  const inbox = await client.session.inbox.list({ sessionID: sessionId }, requestOptionsFor(options));
  if (userInboxItem(inbox, admissionId) !== undefined) return { state: "pending" };

  let found = false;
  let outcome: Resolution | undefined;
  let windowEnded = false;

  const walk = await walkTimeline(
    client,
    sessionId,
    (message) => {
      if (!found) {
        if (message.type === "user" && message.id === admissionId) found = true;
        return "continue";
      }
      // Inside the target turn. The next user/synthetic message begins another turn.
      if (message.type === "user" || message.type === "synthetic") {
        windowEnded = true;
        return "stop";
      }
      if (message.type === "idle") {
        outcome = classifyIdle(message);
        windowEnded = true;
        return "stop";
      }
      return "continue";
    },
    options,
  );

  if (outcome !== undefined) return outcome;

  if (!found) {
    return walk.complete
      ? {
          state: "unknown",
          reason: `admission ${admissionId} is neither enqueued nor present in the message timeline`,
        }
      : {
          state: "unknown",
          reason: `the message timeline could not be read completely while locating admission ${admissionId} (${walk.reason ?? "truncated"})`,
        };
  }

  if (windowEnded) {
    return {
      state: "unknown",
      reason: `admission ${admissionId} ended its turn without an authoritative idle outcome`,
    };
  }

  return walk.complete
    ? {
        state: "unknown",
        reason: `admission ${admissionId} was delivered but no terminal outcome was recorded (in flight or interrupted by a restart)`,
      }
    : {
        state: "unknown",
        reason: `the message timeline could not be read completely while locating the outcome for admission ${admissionId} (${walk.reason ?? "truncated"})`,
      };
}

function classifyIdle(message: SessionMessageIdle): Resolution {
  if (message.outcome === "succeeded") return { state: "succeeded" };
  if (message.outcome === "interrupted") return { state: "interrupted" };
  const detail = message.error === undefined ? undefined : structuredErrorSummary(message.error);
  return detail === undefined ? { state: "failed" } : { state: "failed", detail };
}

function structuredErrorSummary(error: SessionStructuredError): string {
  const message = (error as { message?: unknown }).message;
  return typeof message === "string" ? message : JSON.stringify(error);
}

export interface WaitOptions {
  readonly timeoutMs: number;
  readonly pollMs?: number;
  /**
   * Deadline applied to each individual HTTP request. Bounds a single stalled
   * request; defaults to the remaining wait budget.
   */
  readonly requestTimeoutMs?: number;
  /** Caller cancellation. Resolves as `cancelled` rather than hanging. */
  readonly signal?: AbortSignal;
  readonly maxPages?: number;
  readonly pageLimit?: number;
}

export interface WaitResult {
  readonly resolution: Resolution;
  readonly timedOut: boolean;
  readonly cancelled: boolean;
  readonly polls: number;
}

/**
 * Optional bounded wait. Returns as soon as the admission reaches a terminal state,
 * the total timeout elapses, the caller cancels, or a read fails or times out.
 * Never converts a timeout or a failed read into a success.
 */
export async function waitForResolution(
  client: OpenCodeClient,
  sessionId: SessionId,
  admissionId: AdmissionId,
  options: WaitOptions,
): Promise<WaitResult> {
  const pollMs = options.pollMs ?? 500;
  const deadline = Date.now() + Math.max(0, options.timeoutMs);
  const base: ReconcileOptions = {
    ...(options.maxPages === undefined ? {} : { maxPages: options.maxPages }),
    ...(options.pageLimit === undefined ? {} : { pageLimit: options.pageLimit }),
  };
  let polls = 0;
  let last: Resolution = { state: "unknown", reason: "no poll completed before the deadline" };

  for (;;) {
    if (options.signal?.aborted === true) return cancelled(polls);
    const remaining = deadline - Date.now();
    if (remaining <= 0) return { resolution: last, timedOut: true, cancelled: false, polls };

    const budget = options.requestTimeoutMs === undefined ? remaining : Math.max(1, Math.min(options.requestTimeoutMs, remaining));
    polls += 1;
    try {
      last = await reconcile(client, sessionId, admissionId, {
        ...base,
        ...(options.signal === undefined ? {} : { signal: options.signal }),
        requestTimeoutMs: budget,
      });
    } catch (error) {
      return {
        resolution: { state: "unknown", reason: `read failed or timed out: ${describeError(error)}` },
        timedOut: false,
        cancelled: Boolean(options.signal?.aborted),
        polls,
      };
    }

    if (last.state !== "pending" && last.state !== "unknown") {
      return { resolution: last, timedOut: false, cancelled: false, polls };
    }
    if (Date.now() + pollMs > deadline) return { resolution: last, timedOut: true, cancelled: false, polls };
    if (await sleep(pollMs, options.signal)) return cancelled(polls);
  }
}

function cancelled(polls: number): WaitResult {
  return { resolution: { state: "unknown", reason: "wait cancelled" }, timedOut: false, cancelled: true, polls };
}

/** Resolves `true` when the signal aborted before the delay elapsed. */
function sleep(ms: number, signal?: AbortSignal): Promise<boolean> {
  return new Promise((resolve) => {
    if (signal?.aborted === true) {
      resolve(true);
      return;
    }
    const onAbort = (): void => {
      clearTimeout(timer);
      resolve(true);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve(false);
    }, ms);
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
