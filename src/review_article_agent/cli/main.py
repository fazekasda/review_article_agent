"""The ``review-article-agent`` command line: argument tree, handlers, output.

Design (``docs/implementation/02-project-foundation.md`` AC1/AC2/AC3/AC7 and
``docs/decisions/002-step01-contracts-and-policies.md``):

* **Stdlib only.** ``argparse`` + ``json``; no third-party runtime dependency.
* **Stable JSON by default.** Every command prints one JSON document to stdout
  (``sort_keys`` and a fixed indent, so output is deterministic and
  machine-readable). Raw artifact content is available only through the
  explicit ``artifact content`` command, never in a status/log line.
* **Stable non-success exit categories** (see :data:`EXIT_*`), with a JSON
  ``{"error": {"category", "message"}}`` document on **stderr**. Never a raw
  traceback, raw exception payload, file contents, SQL text, or secret.
* **Verified project access.** ``Registry.get`` validates only the ID shape, so
  every command that touches a project's data opens the store through
  :meth:`review_article_agent.storage.store.ProjectStore.open_at` with
  ``expected_project_id`` — a project ID/path mismatch or traversal is refused.
* **Untrusted imports.** A JSON document imported from a file or stdin is data,
  never authority: unknown envelope fields are rejected, and no field
  (``approved``, ``actor``, ``decision``, ...) can self-grant an approval.

The command handlers are intentionally small and uniform (``cmd_*`` functions
taking ``(args, context)`` and returning a JSON-ready object) so later steps can
add subcommands without reworking dispatch. No ``approve``/``run``/``worker``/
``model`` command exists here: those belong to later steps once their storage
and approval layers are integrated.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence, TextIO

from .. import __version__
from ..domain.errors import (
    InvalidProjectId,
    NoActiveProject,
    ProjectConflict,
    ProjectNotFound,
    ProjectPathError,
    ReviewAgentError,
    StoreError,
    UnsupportedSchemaVersion,
)
from ..domain.records import ARTIFACT_KINDS, RecordValidationError
from ..storage import (
    ArtifactConflictError,
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from ..storage.artifacts import Revision
from .config import (
    CONFIG_SCHEMA_VERSION,
    ConfigError,
    load_config_file,
    resolve_data_dir,
)

PROG = "review-article-agent"

#: Success.
EXIT_SUCCESS = 0
#: Unexpected internal error (never a traceback in output).
EXIT_INTERNAL = 1
#: CLI usage and configuration problems (``argparse`` also uses 2).
EXIT_USAGE = 2
#: Input data validation failures (identifiers, malformed imports, unknown ids).
EXIT_VALIDATION = 2
#: Divergent duplicate / immutable-state conflict.
EXIT_CONFLICT = 3
#: Store/registry/schema/path/OS/permission errors.
EXIT_STORAGE = 4


class CliUsageError(Exception):
    """A command-line usage problem, surfaced as a JSON error (exit 2)."""


class _ArgumentParser(argparse.ArgumentParser):
    """An ``argparse`` parser that raises instead of printing and exiting.

    ``--help``/``--version`` still print and exit ``0`` as usual; every *error*
    (unknown argument, missing required argument, bad choice) is raised as a
    :class:`CliUsageError` so :func:`main` can emit a consistent JSON error.
    """

    def error(self, message: str) -> "None":  # type: ignore[override]
        raise CliUsageError(message)


@dataclass
class Context:
    """Per-invocation dependencies, so handlers stay free of global state."""

    registry: Registry
    data_dir: str
    config: dict[str, object]
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))


@dataclass
class RawBytes:
    """Output that must be written verbatim to stdout (artifact content)."""

    data: bytes


# --------------------------------------------------------------------------- #
# Argument tree
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Build the full argument tree (kept separate so it is testable)."""
    parser = _ArgumentParser(
        prog=PROG,
        description=(
            "Durable local scientific-review project core: projects, registry, "
            "and immutable artifact revisions. Stdlib-only; no agent execution."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"{PROG} {__version__}"
    )
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        help="application data directory (overrides config and the XDG default).",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="optional versioned JSON configuration file (schema_version, data_dir).",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p_create = sub.add_parser("create", help="create and register a project at ROOT")
    p_create.add_argument("root", metavar="ROOT")
    p_create.add_argument("--name", help="human-readable project name")

    sub.add_parser("list", help="list registered projects")

    p_select = sub.add_parser("select", help="select the active project")
    p_select.add_argument("project_id", metavar="ID")

    p_status = sub.add_parser(
        "status", help="global status, or one project's status with --project"
    )
    p_status.add_argument("--project", metavar="ID")

    p_relocate = sub.add_parser(
        "relocate", help="explicitly re-register a project at a new ROOT"
    )
    p_relocate.add_argument("project_id", metavar="ID")
    p_relocate.add_argument("root", metavar="ROOT")
    p_relocate.add_argument(
        "--move",
        action="store_true",
        help="move the app-owned store too (same filesystem only)",
    )

    p_register = sub.add_parser(
        "register",
        help="explicitly adopt an already-moved/restored app-owned store at ROOT",
    )
    p_register.add_argument("root", metavar="ROOT")
    p_register.add_argument("--project", dest="project_id", metavar="ID")
    p_register.add_argument("--name")

    _build_artifact_parser(sub)

    return parser


def _build_artifact_parser(sub: "argparse._SubParsersAction") -> None:
    art = sub.add_parser("artifact", help="manage immutable artifact revisions")
    art_sub = art.add_subparsers(
        dest="artifact_command", required=True, metavar="ARTIFACT_COMMAND"
    )

    p_create = art_sub.add_parser("create", help="create a logical artifact")
    p_create.add_argument("--artifact-id", required=True, metavar="ID")
    p_create.add_argument("--kind", required=True, choices=list(ARTIFACT_KINDS))
    p_create.add_argument("--label")
    p_create.add_argument("--project", metavar="ID")

    p_list = art_sub.add_parser("list", help="list artifacts in a project")
    p_list.add_argument("--project", metavar="ID")

    for name, help_text in (
        ("import", "import a new immutable revision from a file or stdin"),
        ("edit", "alias of 'import': add a new revision to an existing artifact"),
    ):
        p_imp = art_sub.add_parser(name, help=help_text)
        p_imp.add_argument("--artifact-id", required=True, metavar="ID")
        p_imp.add_argument("--revision-id", required=True, metavar="ID")
        p_imp.add_argument(
            "--file",
            required=True,
            metavar="PATH",
            help="content path, or '-' for stdin (JSON envelope, or raw with --binary)",
        )
        p_imp.add_argument(
            "--binary",
            action="store_true",
            help="treat the content as raw bytes instead of a JSON envelope",
        )
        p_imp.add_argument("--rights-file", required=True, metavar="PATH")
        p_imp.add_argument("--parent", metavar="CURRENT_REVISION_ID")
        p_imp.add_argument("--deps-file", metavar="PATH")
        p_imp.add_argument("--request-id", metavar="ID")
        p_imp.add_argument("--origin", metavar="LOCATOR")
        p_imp.add_argument("--project", metavar="ID")

    p_get = art_sub.add_parser(
        "get", aliases=["show"], help="show safe metadata for an artifact (no content)"
    )
    p_get.add_argument("artifact_id", metavar="ID")
    p_get.add_argument("--revision", metavar="ID")
    p_get.add_argument("--project", metavar="ID")

    p_revs = art_sub.add_parser("revisions", help="list an artifact's revision metadata")
    p_revs.add_argument("artifact_id", metavar="ID")
    p_revs.add_argument("--project", metavar="ID")

    p_content = art_sub.add_parser(
        "content", help="explicitly output one revision's raw content bytes"
    )
    p_content.add_argument("artifact_id", metavar="ID")
    p_content.add_argument("--revision", required=True, metavar="ID")
    p_content.add_argument("--output", metavar="PATH", help="write bytes to PATH")
    p_content.add_argument("--project", metavar="ID")


# --------------------------------------------------------------------------- #
# Shared projections / helpers
# --------------------------------------------------------------------------- #
def _project_info_dict(info: Any, *, selected: bool | None = None) -> dict[str, object]:
    data = info.as_dict()
    if selected is not None:
        data["selected"] = selected
    return data


def _artifact_dict(artifact: Any) -> dict[str, object]:
    return {
        "project_id": artifact.project_id,
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind,
        "created_at_utc": artifact.created_at_utc,
        "created_by": artifact.created_by,
        "label": artifact.label,
        "status": artifact.status,
    }


def _revision_dict(revision: Revision) -> dict[str, object]:
    """A safe revision projection: no content bytes, no rights strings, no actor."""
    return {
        "project_id": revision.project_id,
        "artifact_id": revision.artifact_id,
        "revision_id": revision.revision_id,
        "parent_revision_id": revision.parent_revision_id,
        "content_sha256": revision.content_sha256,
        "byte_size": revision.byte_size,
        "media_type": revision.media_type,
        "origin_locator": revision.origin_locator,
        "acquired_at_utc": revision.acquired_at_utc,
        "acquired_by": revision.acquired_by,
        "provenance_method": revision.provenance_method,
        "rights_ref": revision.rights_ref.as_dict(),
        "dependencies": [dep.as_dict() for dep in revision.dependencies],
    }


def _resolve_project_id(registry: Registry, explicit: str | None) -> str:
    """Return the explicit project ID or the active project's ID."""
    if explicit is not None:
        return explicit
    return registry.active_project().project_id


def _open_project(
    registry: Registry, project_id: str
) -> tuple[Any, ProjectStore, ArtifactRepository]:
    """Resolve and *verify* a project before touching its data.

    ``Registry.get`` validates only the ID shape (it deliberately does not read
    the store), so the app-owned store is always opened with
    ``expected_project_id`` — a mismatch between the registry location and the
    store's recorded identity is refused before any read or write.
    """
    info = registry.get(project_id)
    store = ProjectStore.open_at(
        paths.app_dir_for(info.root_path), expected_project_id=info.project_id
    )
    return info, store, ArtifactRepository(store)


# --------------------------------------------------------------------------- #
# Project commands
# --------------------------------------------------------------------------- #
def cmd_create(args: argparse.Namespace, ctx: Context) -> dict[str, object]:
    info = ctx.registry.create_project(args.root, name=args.name)
    return {"project": _project_info_dict(info)}


def cmd_list(args: argparse.Namespace, ctx: Context) -> dict[str, object]:
    active = ctx.registry.active()
    active_id = None if active is None else active.project_id
    projects = [
        _project_info_dict(info, selected=(info.project_id == active_id))
        for info in ctx.registry.list_projects()
    ]
    return {"active_project_id": active_id, "projects": projects}


def cmd_select(args: argparse.Namespace, ctx: Context) -> dict[str, object]:
    info = ctx.registry.select(args.project_id)
    return {"selected": _project_info_dict(info, selected=True)}


def cmd_status(args: argparse.Namespace, ctx: Context) -> dict[str, object]:
    if args.project is not None:
        info, store, repo = _open_project(ctx.registry, args.project)
        try:
            active = ctx.registry.active()
            return {
                "scope": "project",
                "project_id": info.project_id,
                "name": info.name,
                "root_path": info.root_path,
                "selected": active is not None and active.project_id == info.project_id,
                "store_schema_version": store.schema_version,
                "artifact_count": len(repo.list_artifacts()),
                "running": False,
                "scientific_validation": False,
            }
        finally:
            store.close()
    data = ctx.registry.status()
    data["scope"] = "global"
    data["scientific_validation"] = False
    return data


def cmd_relocate(args: argparse.Namespace, ctx: Context) -> dict[str, object]:
    info = ctx.registry.relocate(args.project_id, args.root, move=args.move)
    return {"project": _project_info_dict(info)}


def cmd_register(args: argparse.Namespace, ctx: Context) -> dict[str, object]:
    info = ctx.registry.register_restored(
        args.root, project_id=args.project_id, name=args.name
    )
    return {"project": _project_info_dict(info)}


# --------------------------------------------------------------------------- #
# Artifact commands
# --------------------------------------------------------------------------- #
def _load_json_document(path: str, *, what: str, stdin: TextIO | None = None) -> Any:
    """Parse an untrusted JSON file/stdin; errors never echo file contents."""
    try:
        if path == "-":
            text = (stdin or sys.stdin).read()
        else:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        raise RecordValidationError(f"{what}: could not be read ({reason}).") from exc
    except UnicodeDecodeError as exc:
        raise RecordValidationError(f"{what}: is not valid UTF-8 text.") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecordValidationError(f"{what}: is not valid JSON.") from exc


def _load_content(args: argparse.Namespace, stdin: TextIO | None = None) -> object:
    """Load import content: raw bytes (``--binary``) or a JSON envelope object."""
    if args.binary:
        try:
            if args.file == "-":
                stream = stdin if stdin is not None else sys.stdin
                data = getattr(stream, "buffer", stream).read()
            else:
                with open(args.file, "rb") as handle:
                    data = handle.read()
        except OSError as exc:
            reason = exc.strerror or exc.__class__.__name__
            raise RecordValidationError(f"content: could not be read ({reason}).") from exc
        return data
    return _load_json_document(args.file, what="content", stdin=stdin)


def cmd_artifact_create(
    args: argparse.Namespace, ctx: Context
) -> dict[str, object]:
    project_id = _resolve_project_id(ctx.registry, args.project)
    _info, store, repo = _open_project(ctx.registry, project_id)
    try:
        artifact = repo.create_artifact(
            args.artifact_id, kind=args.kind, label=args.label
        )
        return {"artifact": _artifact_dict(artifact)}
    finally:
        store.close()


def cmd_artifact_list(args: argparse.Namespace, ctx: Context) -> dict[str, object]:
    project_id = _resolve_project_id(ctx.registry, args.project)
    _info, store, repo = _open_project(ctx.registry, project_id)
    try:
        return {"artifacts": [_artifact_dict(a) for a in repo.list_artifacts()]}
    finally:
        store.close()


def cmd_artifact_import(args: argparse.Namespace, ctx: Context) -> dict[str, object]:
    project_id = _resolve_project_id(ctx.registry, args.project)
    content = _load_content(args)
    rights = _load_json_document(args.rights_file, what="rights record")
    dependencies: object = ()
    if args.deps_file is not None:
        dependencies = _load_json_document(args.deps_file, what="dependencies")
    _info, store, repo = _open_project(ctx.registry, project_id)
    try:
        revision = repo.import_revision(
            artifact_id=args.artifact_id,
            content=content,
            origin_locator=args.origin or "import:cli",
            rights=rights,
            revision_id=args.revision_id,
            parent_revision_id=args.parent,
            dependencies=dependencies,
            request_id=args.request_id,
        )
        return {"revision": _revision_dict(revision)}
    finally:
        store.close()


def cmd_artifact_get(args: argparse.Namespace, ctx: Context) -> dict[str, object]:
    project_id = _resolve_project_id(ctx.registry, args.project)
    _info, store, repo = _open_project(ctx.registry, project_id)
    try:
        artifact = repo.get_artifact(args.artifact_id)
        if artifact is None:
            raise RecordValidationError(
                f"artifact_id: no artifact {args.artifact_id!r} in this project."
            )
        if args.revision is not None:
            revision = repo.get_revision(args.artifact_id, args.revision)
            if revision is None:
                raise RecordValidationError(
                    f"revision_id: no revision {args.revision!r} for artifact "
                    f"{args.artifact_id!r}."
                )
        else:
            revision = repo.current_revision(args.artifact_id)
        return {
            "artifact": _artifact_dict(artifact),
            "current_revision": None if revision is None else _revision_dict(revision),
        }
    finally:
        store.close()


def cmd_artifact_revisions(
    args: argparse.Namespace, ctx: Context
) -> dict[str, object]:
    project_id = _resolve_project_id(ctx.registry, args.project)
    _info, store, repo = _open_project(ctx.registry, project_id)
    try:
        return {
            "revisions": [
                _revision_dict(r) for r in repo.list_revisions(args.artifact_id)
            ]
        }
    finally:
        store.close()


def cmd_artifact_content(
    args: argparse.Namespace, ctx: Context
) -> dict[str, object] | RawBytes:
    project_id = _resolve_project_id(ctx.registry, args.project)
    _info, store, repo = _open_project(ctx.registry, project_id)
    try:
        data = repo.read_content(args.artifact_id, args.revision)
    finally:
        store.close()
    if args.output is not None:
        try:
            with open(args.output, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            reason = exc.strerror or exc.__class__.__name__
            raise StoreError(
                f"could not write the revision content ({reason})."
            ) from exc
        return {
            "artifact_id": args.artifact_id,
            "revision_id": args.revision,
            "byte_size": len(data),
            "written": args.output,
        }
    return RawBytes(data)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
_COMMANDS = {
    "create": cmd_create,
    "list": cmd_list,
    "select": cmd_select,
    "status": cmd_status,
    "relocate": cmd_relocate,
    "register": cmd_register,
}

_ARTIFACT_COMMANDS = {
    "create": cmd_artifact_create,
    "list": cmd_artifact_list,
    "import": cmd_artifact_import,
    "edit": cmd_artifact_import,
    "get": cmd_artifact_get,
    "show": cmd_artifact_get,
    "revisions": cmd_artifact_revisions,
    "content": cmd_artifact_content,
}


def dispatch(args: argparse.Namespace, ctx: Context) -> object:
    """Route a parsed invocation to its handler (extension hook for new commands)."""
    if args.command == "artifact":
        try:
            handler = _ARTIFACT_COMMANDS[args.artifact_command]
        except KeyError as exc:  # pragma: no cover - argparse enforces choices
            raise CliUsageError(f"unknown artifact command {args.artifact_command!r}") from exc
        return handler(args, ctx)
    try:
        handler = _COMMANDS[args.command]
    except KeyError as exc:  # pragma: no cover - argparse enforces choices
        raise CliUsageError(f"unknown command {args.command!r}") from exc
    return handler(args, ctx)


# --------------------------------------------------------------------------- #
# Output and error handling
# --------------------------------------------------------------------------- #
def _emit_result(result: object, stdout: TextIO | None = None) -> None:
    out = stdout if stdout is not None else sys.stdout
    if isinstance(result, RawBytes):
        buffer = getattr(out, "buffer", None)
        if buffer is None:  # pragma: no cover - text-only stream in tests
            out.write(result.data.decode("utf-8", errors="replace"))
        else:
            buffer.write(result.data)
        out.flush()
        return
    out.write(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    out.flush()


def classify_error(exc: BaseException) -> tuple[str, int]:
    """Map an exception to a stable ``(category, exit_code)`` pair."""
    if isinstance(exc, (CliUsageError, ConfigError)):
        return "usage", EXIT_USAGE
    if isinstance(exc, (ProjectConflict, ArtifactConflictError)):
        return "conflict", EXIT_CONFLICT
    if isinstance(exc, (StoreError, ProjectPathError, UnsupportedSchemaVersion)):
        return "storage", EXIT_STORAGE
    if isinstance(exc, InvalidProjectId):
        return "validation", EXIT_VALIDATION
    if isinstance(exc, ProjectNotFound):
        return "not_found", EXIT_VALIDATION
    if isinstance(exc, NoActiveProject):
        return "validation", EXIT_VALIDATION
    if isinstance(exc, RecordValidationError):
        return "validation", EXIT_VALIDATION
    if isinstance(exc, (json.JSONDecodeError, ValueError, KeyError, TypeError)):
        return "validation", EXIT_VALIDATION
    if isinstance(exc, OSError):
        return "storage", EXIT_STORAGE
    return "internal", EXIT_INTERNAL


def _error_message(exc: BaseException, category: str) -> str:
    """A safe, actionable message: never a traceback, payload, file, or secret."""
    if category == "internal":
        return (
            "an unexpected internal error occurred; the command did not complete. "
            "No traceback is shown. Re-run with a supported command, or report this."
        )
    if category == "storage" and isinstance(exc, OSError) and not isinstance(
        exc, ReviewAgentError
    ):
        reason = exc.strerror or exc.__class__.__name__
        return f"a filesystem/storage error occurred: {reason}."
    return str(exc)


def _emit_error(
    exc: BaseException, stderr: TextIO | None = None
) -> int:
    category, code = classify_error(exc)
    payload = {"error": {"category": category, "message": _error_message(exc, category)}}
    err = stderr if stderr is not None else sys.stderr
    err.write(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    err.flush()
    return code


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the CLI; return a process exit code. Never raises for user errors."""
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except CliUsageError as exc:
        return _emit_error(exc, stderr)

    try:
        if args.config:
            config = load_config_file(args.config)
        else:
            config = {"schema_version": CONFIG_SCHEMA_VERSION, "data_dir": None}
        data_dir = resolve_data_dir(args.data_dir, config, env)
        ctx = Context(
            registry=Registry(data_dir),
            data_dir=data_dir,
            config=config,
            env=dict(os.environ if env is None else env),
        )
        result = dispatch(args, ctx)
    except Exception as exc:  # noqa: BLE001 - convert every failure to JSON
        return _emit_error(exc, stderr)

    _emit_result(result, stdout)
    return EXIT_SUCCESS


__all__ = [
    "EXIT_CONFLICT",
    "EXIT_INTERNAL",
    "EXIT_STORAGE",
    "EXIT_SUCCESS",
    "EXIT_USAGE",
    "EXIT_VALIDATION",
    "CliUsageError",
    "Context",
    "RawBytes",
    "build_parser",
    "classify_error",
    "dispatch",
    "main",
]
