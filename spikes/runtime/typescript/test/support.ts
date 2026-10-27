/**
 * Deterministic in-memory stand-in for an app-owned OpenCode V2 server.
 *
 * It implements the exact routes and envelope shapes the official client calls,
 * verified against the package's generated descriptors:
 *   GET  /api/info                       -> ServerInfo (not enveloped)
 *   POST /api/session                    -> { data: Session.Info }
 *   GET  /api/session/:id                -> { data: Session.Info }
 *   POST /api/session/:id/prompt         -> { data: Session.Inbox.User } | 409 ConflictError
 *   GET  /api/session/:id/inbox          -> { data: Session.Inbox.Info[] }
 *   GET  /api/session/:id/message        -> { data: Session.Message.Info[], cursor }
 *   POST /api/session/:id/interrupt      -> SessionInterruptResponse
 *   POST /api/experimental/generate/decisions -> { data: GenerateDecisionResult }
 *
 * This is a *local transport mock*. Passing tests here proves the client-side
 * logic and contract handling, not live server behaviour.
 */
import { createServer } from "node:http";
import type {
  GenerateDecisionsInput,
  GenerateDecisionResult,
  ServerInfo,
  SessionInboxUser,
  SessionInfo,
  SessionMessageAssistant,
  SessionMessageIdle,
  SessionMessageInfo,
  SessionMessageUser,
} from "@opencode/client";

export interface RecordedRequest {
  readonly method: string;
  readonly path: string;
  readonly body: unknown;
  readonly authorization: string | null;
}

interface MockSession {
  info: SessionInfo;
  inbox: SessionInboxUser[];
  messages: SessionMessageInfo[];
  admitted: Set<string>;
}

export interface MockServerOptions {
  readonly version?: string;
  /** Token the mock expects; requests without it get 401. Omit for no auth check. */
  readonly requireToken?: string;
}

export function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function envelope(data: unknown, status = 200): Response {
  return jsonResponse({ data }, status);
}

let idCounter = 0;
export function testAdmissionId(label: string): string {
  idCounter += 1;
  return `msg_${label}_${idCounter}`;
}

export class MockServer {
  readonly version: string;
  readonly requests: RecordedRequest[] = [];
  readonly #sessions = new Map<string, MockSession>();
  readonly #overrides = new Map<string, () => Response>();
  readonly #requireToken: string | undefined;
  #sessionCounter = 0;

  constructor(options: MockServerOptions = {}) {
    this.version = options.version ?? "2.2.0-test";
    this.#requireToken = options.requireToken;
  }

  /** The transport to hand to `OpenCode.make({ fetch })`. */
  readonly fetch: typeof globalThis.fetch = async (input, init) => this.#handle(input, init);

  /** Install a one-shot override for `METHOD /path` (used for fault injection). */
  failNext(method: string, path: string, responder: () => Response): void {
    this.#overrides.set(`${method.toUpperCase()} ${path}`, responder);
  }

  seedSession(input: {
    directory: string;
    agent?: string;
    model?: { id: string; providerID: string; variant?: string };
    title?: string;
    parentID?: string;
  }): SessionInfo {
    this.#sessionCounter += 1;
    const id = `ses_test_${this.#sessionCounter}`;
    const info: SessionInfo = {
      id,
      projectID: "proj_test",
      cost: 0,
      tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
      time: { created: Date.now(), updated: Date.now() },
      location: { directory: input.directory },
      ...(input.parentID === undefined ? {} : { parentID: input.parentID }),
      ...(input.agent === undefined ? {} : { agent: input.agent }),
      ...(input.model === undefined ? {} : { model: input.model }),
      ...(input.title === undefined ? {} : { title: input.title }),
    };
    this.#sessions.set(id, { info, inbox: [], messages: [], admitted: new Set() });
    return info;
  }

  /** Simulate the server delivering a queued admission to the agent loop. */
  deliver(sessionId: string, admissionId: string): void {
    const session = this.#must(sessionId);
    const index = session.inbox.findIndex((item) => item.id === admissionId);
    if (index === -1) throw new Error(`no pending admission ${admissionId}`);
    const [item] = session.inbox.splice(index, 1);
    const user: SessionMessageUser = {
      id: admissionId,
      time: { created: item?.time.created ?? Date.now() },
      text: item?.payload.text ?? "",
      type: "user",
    };
    session.messages.push(user);
  }

  /** Append an assistant turn and a terminal idle message with the given outcome. */
  finish(sessionId: string, outcome: SessionMessageIdle["outcome"], text = "done"): void {
    const session = this.#must(sessionId);
    const assistant: SessionMessageAssistant = {
      id: testAdmissionId("assistant"),
      time: { created: Date.now(), completed: Date.now() },
      type: "assistant",
      agent: session.info.agent ?? "build",
      model: session.info.model ?? { id: "test-model", providerID: "test" },
      content: [],
    };
    const idle: SessionMessageIdle = {
      id: testAdmissionId("idle"),
      time: { created: Date.now() },
      type: "idle",
      outcome,
    };
    session.messages.push(assistant, idle);
    session.info = { ...session.info, outcome: outcome === "succeeded" ? "succeeded" : outcome === "interrupted" ? "interrupted" : "failed" };
    void text;
  }

  pendingAdmissions(sessionId: string): string[] {
    return this.#must(sessionId).inbox.map((item) => item.id);
  }

  timeline(sessionId: string): SessionMessageInfo[] {
    return [...this.#must(sessionId).messages];
  }

  #must(sessionId: string): MockSession {
    const session = this.#sessions.get(sessionId);
    if (session === undefined) throw new Error(`mock has no session ${sessionId}`);
    return session;
  }

  async #handle(input: string | URL | Request, init?: RequestInit): Promise<Response> {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
    const request = input instanceof Request ? input : undefined;
    const method = (request?.method ?? init?.method ?? "GET").toUpperCase();
    const headers = new Headers(request?.headers ?? init?.headers);
    const authorization = headers.get("authorization");
    const rawBody =
      request !== undefined ? await request.clone().text() : typeof init?.body === "string" ? init.body : undefined;
    const body: unknown = rawBody === undefined || rawBody === "" ? undefined : JSON.parse(rawBody);
    this.requests.push({ method, path: url.pathname, body, authorization });

    const override = this.#overrides.get(`${method} ${url.pathname}`);
    if (override !== undefined) {
      this.#overrides.delete(`${method} ${url.pathname}`);
      return override();
    }

    if (this.#requireToken !== undefined && authorization !== `Bearer ${this.#requireToken}`) {
      return jsonResponse({ _tag: "UnauthorizedError", message: "missing or invalid bearer token" }, 401);
    }

    if (method === "GET" && url.pathname === "/api/info") {
      const info: ServerInfo = {
        version: this.version,
        pid: 4242,
        urls: [`http://127.0.0.1:4096`],
        paths: { tmp: "/tmp/opencode" },
      };
      return jsonResponse(info);
    }

    if (method === "POST" && url.pathname === "/api/session") {
      const input = (body ?? {}) as {
        location?: { directory?: string };
        agent?: string;
        model?: { id: string; providerID: string };
        title?: string;
        id?: string;
        parentID?: string;
      };
      const directory = input.location?.directory ?? "/workspace";
      const created = this.seedSession({
        directory,
        ...(input.parentID === undefined ? {} : { parentID: input.parentID }),
        ...(input.agent === undefined ? {} : { agent: input.agent }),
        ...(input.model === undefined ? {} : { model: input.model }),
        ...(input.title === undefined ? {} : { title: input.title }),
      });
      return envelope(created);
    }

    const sessionMatch = /^\/api\/session\/([^/]+)(\/prompt|\/inbox|\/message|\/interrupt)?$/.exec(url.pathname);
    if (sessionMatch !== null) {
      const sessionId = decodeURIComponent(sessionMatch[1] as string);
      const suffix = sessionMatch[2] ?? "";
      const session = this.#sessions.get(sessionId);
      if (session === undefined) {
        return jsonResponse({ _tag: "SessionNotFoundError", message: `no session ${sessionId}` }, 404);
      }
      if (method === "GET" && suffix === "") return envelope(session.info);
      if (method === "GET" && suffix === "/inbox") return envelope(session.inbox);
      if (method === "GET" && suffix === "/message") {
        return jsonResponse({ data: session.messages, cursor: {} });
      }
      if (method === "POST" && suffix === "/prompt") {
        const prompt = (body ?? {}) as { id?: string; text?: string; delivery?: "steer" | "queue" };
        const admissionId = prompt.id ?? testAdmissionId("auto");
        if (session.admitted.has(admissionId)) {
          return jsonResponse({ _tag: "ConflictError", message: `admission ${admissionId} already accepted` }, 409);
        }
        session.admitted.add(admissionId);
        const item: SessionInboxUser = {
          id: admissionId,
          sessionID: sessionId,
          time: { created: Date.now() },
          type: "user",
          payload: { text: prompt.text ?? "" },
          delivery: prompt.delivery ?? "queue",
        };
        session.inbox.push(item);
        return envelope(item);
      }
      if (method === "POST" && suffix === "/interrupt") {
        return jsonResponse({ interrupted: session.messages.length > 0 });
      }
    }

    if (method === "POST" && url.pathname === "/api/experimental/generate/decisions") {
      const request = body as GenerateDecisionsInput;
      const result: GenerateDecisionResult = {
        answers: request.questions.map((question, index) => {
          if (question.type === "predicate") {
            return { type: "predicate", name: question.name, probability: 0.7 };
          }
          if (question.type === "choice") {
            const first = question.choices[0]?.value ?? "unknown";
            return {
              type: "choice",
              name: question.name,
              choice: first,
              probabilities: question.choices.map((choice) => ({ value: choice.value, probability: 1 / question.choices.length })),
              confidence: 0.6,
            };
          }
          void index;
          return {
            type: "score",
            name: question.name,
            score: question.levels.length,
            probabilities: question.levels.map((level, position) => ({
              value: position,
              label: level.label,
              probability: 1 / question.levels.length,
            })),
            confidence: 0.5,
          };
        }),
        cost: 0,
        tokens: { input: 10, output: 20 },
      };
      return envelope(result);
    }

    return jsonResponse({ _tag: "InvalidRequestError", message: `unhandled ${method} ${url.pathname}` }, 400);
  }
}

export interface RunningHttpServer {
  readonly url: string;
  close(): Promise<void>;
}

/**
 * Bridge the in-memory mock onto a real loopback HTTP listener so the compiled
 * CLI can be exercised as a separate process. Used only by the integration test.
 */
export async function startHttpServer(mock: MockServer): Promise<RunningHttpServer> {
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      const raw = chunks.length === 0 ? undefined : Buffer.concat(chunks);
      void (async () => {
        const init: RequestInit = {
          method: request.method ?? "GET",
          headers: request.headers as Record<string, string>,
          ...(raw === undefined ? {} : { body: raw }),
        };
        const bridged = await mock.fetch(new Request(`http://127.0.0.1${request.url ?? "/"}`, init));
        response.statusCode = bridged.status;
        bridged.headers.forEach((value, key) => response.setHeader(key, value));
        response.end(await bridged.text());
      })();
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("failed to bind loopback server");
  return {
    url: `http://127.0.0.1:${address.port}`,
    close: () =>
      new Promise<void>((resolve, reject) => {
        // Undici keeps connections alive; drop them so close() resolves promptly.
        server.closeAllConnections();
        server.close((error) => (error ? reject(error) : resolve()));
      }),
  };
}
