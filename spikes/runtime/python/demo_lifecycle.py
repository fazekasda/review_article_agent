"""End-to-end demo of the lifecycle against the disposable mock service.

Demonstrates, with no live service and no paid calls:

1. Client 1 creates a session, submits with a client admission ID, and receives
   a *durable admission receipt* (not a completion).
2. Client 1 closes. The server-owned task keeps running and completes.
3. A brand-new Client 2 (missing every event) retrieves the persisted inbox,
   persists the completion, and reconciles the admission ID.
4. Retrying the same admission ID yields one accepted input.
5. A fresh critic session is created (never a fork).
6. Typed Jev predicate/choice/score decisions, including a per-question failure.

Run:  python3 demo_lifecycle.py
"""

from __future__ import annotations

import threading
import time

from mock_server import MockServer
from opencode_v2 import (
    OpenCodeApiError,
    OpenCodeV2Client,
    first_decision_error,
    summarize_decisions,
    validate_decision_result,
)

WRITER = {"providerID": "openrouter", "id": "some/writer-model"}
CRITIC = {"providerID": "openrouter", "id": "typesafe/jev-1.14"}


def main() -> None:
    with MockServer(token="demo-token") as server:
        print(f"# disposable mock service: {server.base_url}")
        print("# api version reported:", "mock-0.0.1 (see README: live version unverified)")

        # --- client 1: admit durable work, then close ---------------------
        client1 = OpenCodeV2Client(server.base_url, token="demo-token", directory="/tmp/spike")
        writer = client1.create_session(agent="writer", model=WRITER, title="demo writer")
        admission = client1.admit(writer["id"], "Write the introduction.", "msg_demo0001")
        print(f"1. admitted {admission.admission_id}: durable_receipt={admission.has_durable_receipt} "
              f"(admission, not completion)")
        print(f"   receipt type={admission.receipt['type']} delivery={admission.receipt['delivery']}")

        # server-owned task completes after the client is gone
        def server_owned_task() -> None:
            time.sleep(0.2)
            server.state.add_assistant(writer["id"], "Introduction drafted.")
            server.state.complete(writer["id"], outcome="succeeded")

        threading.Thread(target=server_owned_task, daemon=True).start()
        del client1  # "CLI closed"; server-owned task is unaffected
        print("2. client 1 closed; server-owned task continues")

        time.sleep(0.4)

        # --- client 2: miss every event, recover from durable state -------
        client2 = OpenCodeV2Client(server.base_url, token="demo-token")
        inbox = client2.list_inbox(writer["id"])
        found = client2.reconcile(writer["id"], "msg_demo0001")
        print(f"3. client 2 recovered: state={found.state} complete={found.is_complete} "
              f"outcome={found.completion['outcome'] if found.completion else None}")
        print(f"   inbox before delivery consumed: {[i['id'] for i in inbox]}")

        # --- idempotent retry --------------------------------------------
        retry = client2.admit(writer["id"], "Write the introduction.", "msg_demo0001")
        print(f"4. retry same admission id: duplicate={retry.duplicate} -> one input "
              f"(inbox={[i['id'] for i in client2.list_inbox(writer['id'])]})")

        # --- fresh critic, not a fork ------------------------------------
        critic = client2.start_critic(agent="critic", model=CRITIC, writer_model=WRITER)
        print(f"5. critic {critic['id']} role={critic['agent']} model="
              f"{critic['model']['providerID']}/{critic['model']['id']} "
              f"fork={'fork' in critic}")

        # --- typed decisions with a per-question failure ------------------
        server.state.decision_answers = [
            {"type": "predicate", "name": "is_review", "probability": 0.73},
            {"type": "error", "name": "study_design", "reason": "refusal"},
            {
                "type": "score",
                "name": "quality",
                "score": 1.6,
                "probabilities": [
                    {"value": 1, "label": "low", "probability": 0.4},
                    {"value": 2, "label": "medium", "probability": 0.6},
                ],
                "confidence": 0.55,
            },
        ]
        result = client2.generate_decisions(
            model=CRITIC,
            input_parts=[{"type": "text", "text": "Abstract of a candidate paper."}],
            questions=[
                {"type": "predicate", "name": "is_review", "instructions": "Is it a review?"},
                {"type": "choice", "name": "study_design", "instructions": "Design?",
                 "choices": [{"value": "rct"}, {"value": "cohort"}]},
                {"type": "score", "name": "quality", "instructions": "Quality?",
                 "levels": [{"label": "low"}, {"label": "medium"}]},
            ],
        )
        validate_decision_result(result)
        print("6. typed decisions:", summarize_decisions(result))
        error = first_decision_error(result)
        print(f"   per-question failure: {error.question_name!r} reason={error.reason!r} "
              f"(other answers still present, not an exclusion)")

        # --- repair evidence: per-turn reconciliation and endpoint safety --
        server.state.enqueue(writer["id"], "msg_demo0002", "second turn")
        queued = client2.reconcile(writer["id"], "msg_demo0002")
        print(f"7. queued second turn: state={queued.state} complete={queued.is_complete} "
              f"(a prior idle does not complete it)")
        try:
            OpenCodeV2Client("http://example.com", token="t")
        except ValueError as exc:
            print(f"8. non-loopback endpoint refused: {exc}")
        try:
            client2._request("GET", "/api/redirect")
        except OpenCodeApiError as exc:
            print(f"9. redirect blocked: HTTP {exc.status} ({exc.message})")


if __name__ == "__main__":
    main()
