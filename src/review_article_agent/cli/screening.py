"""Minimal local JSON proposal/read interface for screening."""

from ..domain.errors import ReviewAgentError
from ..screening import ScreeningRepository
from .parsing import ParsingControlError, _read_object, _resolve_project_id, _verified_store


def build_screening_parser(sub):
    group = sub.add_parser("screening", help="local screening proposals; no paid dispatch")
    actions = group.add_subparsers(dest="screening_command", required=True)
    for action in ("propose", "show", "exclusions"):
        parser = actions.add_parser(action)
        parser.add_argument("--project")
        parser.add_argument("--artifact-id", required=True)
        parser.add_argument("--revision-id", required=True)
        if action != "exclusions":
            parser.add_argument("--kind", required=True, choices=("config", "pilot", "batch", "audit"))
        if action == "propose":
            parser.add_argument("--input-file", required=True)
            parser.add_argument("--rights-file", required=True)
            parser.add_argument("--parent-revision-id")
        elif action == "show":
            parser.add_argument("--content", action="store_true")
            # Explicit, read-only inspection of a superseded/stale revision.
            # Never available on propose/exclusions: stale evidence cannot
            # authorize work.
            parser.add_argument("--historical", action="store_true")


def dispatch_screening(args, ctx):
    pid = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, pid) as store:
        repo = ScreeningRepository(store)
        ref = {"project_id": pid, "artifact_id": args.artifact_id, "revision_id": args.revision_id}
        try:
            if args.screening_command == "propose":
                ref = repo.propose(args.kind, _read_object(args.input_file, what="screening"),
                                   artifact_id=args.artifact_id, revision_id=args.revision_id,
                                   rights=_read_object(args.rights_file, what="rights"),
                                   parent_revision_id=args.parent_revision_id)
                return {"ref": ref, "approved": False, "authorizes_extraction": False}
            if args.screening_command == "exclusions":
                return {"excluded_case_ids": repo.require_exclusions(ref), "authorizes_extraction": False}
            if args.screening_command == "show" and getattr(args, "historical", False):
                stored = repo.read(ref, args.kind, historical=True)
                payload = stored["payload"]
                result = {"ref": ref, "kind": args.kind, "historical": True,
                          "freshly_validated": False, "current_validation": False,
                          "authorizes_extraction": False, "notice": stored["notice"]}
            else:
                payload = repo.read(ref, args.kind)
                result = {"ref": ref, "kind": args.kind, "authorizes_extraction": False}
            if args.content:
                result["payload"] = payload
            else:
                result.update({"case_count": len(payload.get("cases", [])),
                               "paused": payload.get("paused", False),
                               "blocking_failure_count": len(payload.get("blocking_failures", []))})
            return result
        except (ReviewAgentError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
            raise ParsingControlError("validation", "screening request rejected; check inputs, current dependencies, and named approvals") from None
