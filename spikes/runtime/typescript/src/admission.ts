/**
 * Durable admission and lifecycle vocabulary for the OpenCode V2 runtime spike.
 *
 * Everything here mirrors the published V2 OpenAPI contract
 * (https://opencode.ai/v2/openapi.json) and the V2 client guide
 * (https://opencode.ai/v2/docs/build/client). No name or shape is invented:
 *
 *   Session.ID          string, pattern ^ses
 *   Session.Message.ID  string, pattern ^msg_
 *   session.prompt      -> Session.Inbox.User   (the durable admission record)
 *   session.inbox.list  -> Session.Inbox.Info[] (work enqueued but not yet delivered)
 *   session.message.list-> Session.Message.Info[] (includes a terminal "idle" message
 *                          carrying outcome: "succeeded" | "failed" | "interrupted")
 *   Session.Info.outcome-> "succeeded" | "failed" | "interrupted"
 *
 * The spike deliberately separates an *admission* from a *completion*: an admission
 * receipt only proves the server durably accepted one input. Completion is a later,
 * separately observed event.
 */

/** Session.Message.ID: `^msg_`. */
export type AdmissionId = string;
/** Session.ID: `^ses`. */
export type SessionId = string;

export const ADMISSION_ID_PATTERN = /^msg_/;
export const SESSION_ID_PATTERN = /^ses/;

export function isAdmissionId(value: string): boolean {
  return ADMISSION_ID_PATTERN.test(value);
}

export function isSessionId(value: string): boolean {
  return SESSION_ID_PATTERN.test(value);
}

/**
 * Client-supplied admission id. Deterministic callers may supply their own; this
 * helper produces one that satisfies the server-side Session.Message.ID pattern.
 */
export function newAdmissionId(): AdmissionId {
  return `msg_${globalThis.crypto.randomUUID()}`;
}

export function assertAdmissionId(value: string): AdmissionId {
  if (!isAdmissionId(value)) {
    throw new RangeError(
      `admission id ${JSON.stringify(value)} does not match Session.Message.ID (${ADMISSION_ID_PATTERN})`,
    );
  }
  return value;
}

export function assertSessionId(value: string): SessionId {
  if (!isSessionId(value)) {
    throw new RangeError(
      `session id ${JSON.stringify(value)} does not match Session.ID (${SESSION_ID_PATTERN})`,
    );
  }
  return value;
}

/** Delivery mode from Session.Inbox.Delivery. */
export type Delivery = "steer" | "queue";

/**
 * A durable admission receipt. `kind` is always the literal "admission" so a caller
 * cannot mistake it for a completion. It is a projection of Session.Inbox.User plus
 * the identity of the server that accepted it.
 */
export interface AdmissionReceipt {
  readonly kind: "admission";
  /** How the server recorded this admission. */
  readonly status: "accepted" | "duplicate-accepted";
  readonly sessionId: SessionId;
  readonly admissionId: AdmissionId;
  readonly delivery: Delivery;
  /** Session.Inbox.User.time.created (server clock). */
  readonly admittedAt: number;
  /** ServerInfo.version observed by the admitting client. */
  readonly serverVersion: string;
  /** True when the receipt came from reconciling a retry rather than a fresh accept. */
  readonly reconciled: boolean;
}

/**
 * The reconciled state of an admission. This is read from persisted server state
 * (inbox + message timeline), never inferred from a live event stream, because V2
 * event subscriptions are volatile and miss events while disconnected.
 */
export type Resolution =
  | { readonly state: "pending" }
  | { readonly state: "succeeded" }
  | { readonly state: "failed"; readonly detail?: string }
  | { readonly state: "interrupted" }
  | { readonly state: "unknown"; readonly reason: string };

export function isTerminal(resolution: Resolution): boolean {
  return resolution.state === "succeeded" || resolution.state === "failed" || resolution.state === "interrupted";
}
