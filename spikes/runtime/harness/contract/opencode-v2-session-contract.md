# OpenCode V2 session contract (spike reference)

Curated, self-contained contract snippets for the independent TypeScript and
Python prototypes (tasks A-ts / A-py). Everything here was observed against a
disposable app-owned `opencode serve` instance started by this harness, not
against the ordinary shared service.

Tested versions (verified):

| Surface | Value |
| --- | --- |
| CLI | `opencode --version` → `opencode v2.2.0` |
| `GET /api/info` → `version` | `2.2.0` |
| `GET /openapi.json` → `info.version` | `0.0.1` (`title: "opencode HttpApi"`) |
| Client library | `@opencode/client` (not installed here; harness is dependency-free) |

`/openapi.json` is the authoritative machine contract (156 operations, 339
schemas at v2.2.0). This file is a convenience excerpt; fetch `/openapi.json`
from a running harness instance when generating client types.

## Discovery

The harness owns its endpoint and writes a descriptor at
`$HARNESS_ROOT/run/instance.json` (default
`/tmp/opencode/spike-harness/run/instance.json`). Shape:

```json
{
  "descriptorVersion": 1,
  "kind": "app-owned-disposable",
  "binary": "<absolute opencode path>",
  "opencodeVersion": "2.2.0",
  "url": "http://127.0.0.1:<port>",
  "pid": 12345,
  "auth": { "scheme": "http-basic", "username": "opencode", "passwordFile": "<absolute path, mode 600>" },
  "dirs": { "home": "…", "xdgConfigHome": "…", "xdgDataHome": "…", "xdgStateHome": "…", "xdgCacheHome": "…", "cwd": "…" },
  "documentedRegistration": "~/.local/state/opencode/service.json",
  "note": "This instance writes no standard service registration."
}
```

Prototypes should read `url` and the Basic password from `passwordFile`. Do not
print the password. This deliberately avoids OpenCode's private
`~/.local/state/opencode/service.json` registration (see README), so do **not**
use `Service.discover()` / `Service.ensure()` from `@opencode/client/service`
against this harness — those read the standard path and could start or target
the ordinary shared service.

## Authentication

HTTP Basic, username `opencode`, password = the value passed to the server as
`OPENCODE_PASSWORD`. Without a correct password the server returns
`401 {"_tag":"UnauthorizedError","message":"Authentication required"}`.

```sh
curl -u "opencode:$(cat "$passwordFile")" "$url/api/info"
```

If `OPENCODE_PASSWORD` is unset, `opencode serve` generates one and prints
`server password <token>` on stdout — the harness always sets it explicitly so
discovery is deterministic.

`GET /api/info` →

```json
{"version":"2.2.0","pid":35751,"hostname":"…","urls":["http://127.0.0.1:33443"],
 "paths":{"tmp":"/tmp/opencode/opencode"},"capabilities":{"persistentPty":true}}
```

## Create a session

`POST /api/session`

```jsonc
// request
{ "location": { "directory": "<absolute path, must be an app-owned workspace>" },
  "title": "optional", "agent": "optional", "model": {"providerID":"…","id":"…"} }
// 200 →
{ "data": { "id": "ses_…", "projectID": "…", "cost": 0,
            "tokens": {"input":0,"output":0,"reasoning":0,"cache":{"read":0,"write":0}},
            "time": {"created": 1793130161725, "updated": 1793130161725},
            "title": "…", "location": {"directory": "<echoed>"} } }
```

`Session.ID` pattern: `^ses`.

## Prompt admission (client-supplied admission ID)

`POST /api/session/{sessionID}/prompt`

```jsonc
// request — `text` is required; `id` is the client-supplied admission ID
{ "id": "msg_<your-id>", "text": "…",
  "delivery": "queue" | "steer",   // optional
  "resume": true,                  // optional
  "files": [], "agents": [], "skills": [], "metadata": {} }  // optional
// 200 → durable admission receipt
{ "data": { "id": "msg_<your-id>", "sessionID": "ses_…",
            "time": {"created": 1793130163100}, "type": "user",
            "payload": {"text": "…"}, "delivery": "steer" } }
```

* `Session.Message.ID` pattern: `^msg`. A bad `id` → `400 InvalidRequestError`
  ("Expected a string starting with \"msg_\"").
* `delivery` defaults to `"steer"`; an explicit value is echoed back.
* **Admission ≠ completion.** The 200 receipt only means the input was durably
  admitted. Verified: admitting with no provider credentials and no event
  subscriber produced zero model/generate log activity.
* **Idempotent by admission ID.** Retrying the same `id` returned 200 again yet
  `GET /api/session/{sid}/message` contained exactly one `msg_<id>` entry — one
  accepted input per client-supplied ID. (No `409` was returned for this case.)

## Retrieval

* `GET /api/session/{sessionID}/message` → `{ "data": [ Session.Message.Info … ], "cursor": {…} }`
  where a user message is
  `{ "id":"msg_…","type":"user","text":"…","time":{"created":…} }`
  and assistant messages carry `type":"assistant"`.
* `GET /api/session/{sessionID}/inbox` → `{ "data": [ Session.Inbox.Info … ] }`
  (pending admitted inputs; empty once consumed: `{"data":[]}`).
* `GET /api/session/{sessionID}` → `{"data": Session.Info}` (`outcome` is one of
  `succeeded | failed | interrupted` once a turn has resolved).
* `GET /api/session` → `{"data":[Session.Info…],"cursor":{"previous":…,"next":…}}`.

## Error envelope

Single-object union tagged by `_tag`:

```jsonc
{"_tag":"InvalidRequestError","message":"Missing key\n  at [\"text\"]","kind":"Payload","field":null}   // 400
{"_tag":"UnauthorizedError","message":"Authentication required"}                                        // 401
{"_tag":"SessionNotFoundError","sessionID":"ses_x","message":"Session not found: ses_x"}                // 404
{"_tag":"ConflictError","message":"…","resource":null}                                                  // 409
```

## Minimal flow (language-neutral)

1. Read descriptor → `url`, `passwordFile`.
2. `POST /api/session {location:{directory:<workspace>}}` → `sid`.
3. `POST /api/session/{sid}/prompt {id:"msg_<uuid>", text:"…"}` → receipt.
4. Poll `GET /api/session/{sid}/message` (or subscribe to `/api/event`) until an
   assistant message or `Session.Info.outcome` resolves.

The harness itself never performs step 4's turn trigger and never supplies
credentials, so these snippets are provided for the prototypes to exercise on
their own authorized budget.
