#!/usr/bin/env python3
"""Offline documentation validator for the Step 01 contract proposal bundle.

Scope: this is a *documentation* checker for the three proposed Step 01 contracts
and the bundle map. It is stdlib-only and offline (no network, no paid calls). It
makes **no** scientific-correctness claim and is **not** an executable production
schema — it checks that the proposal documents are self-consistent and that their
declared valid/invalid examples behave as the documents claim.

What it checks
--------------
- Local markdown link targets exist (relative to the repo root). Fragments
  (`path#anchor`) are also checked with a GitHub-style slug; see DISCLOSURE below.
- Every fenced ```json block in the three contract documents parses.
- C01: the inline `§7.0` validator is **extracted from the document markers**
  (`C7.0-VALIDATOR-BEGIN/END`) and executed against `docs/contracts/01-identities.md`
  (required keys, `stated` shapes, hash format, enum values, and pinned
  `RightsRecordRef` resolution incl. cross-project rejection).
- C02: the complete §11.0 JSON examples (human `Approval`, predicate decision,
  score decision) are type/range/enum/digest checked, and `CostRecord` semantics.
- C03: the eight decision/rights JSON examples are checked with **distinguished
  expectations** (valid vs. deliberately invalid); rights records are checked
  against the canonical identity `{project_id, rights_id, record_revision}`, the
  subject discriminated union, per-operation typed evidence, and the
  `authorization` binding of both subject and rights revision.
- **Ratification evidence** (added 2026-10-27): the finalized 2026-10-27 interview
  dispositions are checked as *document evidence*, distinct from the historical
  synthetic `proposed`/invalid fixtures. The check requires the ADR-002 acceptance
  record to state the accepted behavioral scope against the reviewed revision
  `ee7f7eafca80baa6c1aaca278361e56a663493c3`, requires the register to carry the
  reviewed revision, the per-record `Interview amendment` evidence, an
  ACCEPTED/RATIFIED banner, and an `accepted` status-summary row for every
  policy/technical record id. It is a **stable-id / section-level** check: it does
  not compare headings or long prose strings.
- **Cost/concurrency default agreement**: the normative `$25 / $10 / 2` defaults
  must agree between the authoritative register (`03-policy-decisions.md`, the
  structured caps object and the status-summary row) and C02
  (`02-gates-and-runtime.md`, §5.6 / §12). The check reads only the normative text
  and status table, **never** the synthetic fixtures, so an invalid fixture cannot
  satisfy it.
- **No invented per-project cutoff**: the pilot/coverage/tracing (and the
  categorical P1–P6) records keep `numeric_value = UNSET`; a numeric pilot/tracing
  threshold appearing as a *disposition* is rejected.
- **Link scope** (added 2026-10-27): local link targets are also checked in the
  acceptance record, the baseline, the step document, the kickoff, the Step 01
  handoff, and the root `README.md`, in addition to the three contracts and the
  bundle map.
- `--self-test`: applies negative mutations to the real examples (bad hash,
  missing required/state value, cross-project ref, allowed rights without basis,
  unknown cost recorded as zero, wrong typed answer) and asserts the validators
  reject each one. It also mutates the **ratification/default evidence** (a removed
  reviewed revision, a drifted cap, a numeric pilot cutoff stood up as a
  disposition, and an `accepted` row reverted to `proposed`) and asserts each is
  rejected.

DISCLOSURE / limits
-------------------
- Fragments are checked with a best-effort slug; anchors that a renderer slugs
  differently are reported, not silently accepted.
- C01 `§7.1`–`§7.7` invalid/missing examples are illustrative pseudo-JSON in prose
  (not fenced JSON), so they are **not** machine-parsed; only their valid anchors
  (§7.0) and the C02/C03 examples are machine-checked.
- The ratification/default checks read **specific stable anchors**: record ids, the
  status-summary table, the structured caps object, the reviewed-revision string,
  the interview-amendment count (a bounded range, not an exact prose match), and
  short normative phrases in C02 §5.6/§12. They deliberately do **not** parse or
  compare full prose, headings, or field-level schema spellings; a prose rewrite
  that keeps the ids/anchors and the normative values still passes, and a
  wholesale removal of the anchors is reported rather than silently accepted.
- No scientific numeric value is asserted or checked; no production schema is
  generated.
"""

import argparse
import contextlib
import io
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = {
    "c01": "docs/contracts/01-identities.md",
    "c02": "docs/contracts/02-gates-and-runtime.md",
    "c03": "docs/contracts/03-policy-decisions.md",
}
README = "docs/contracts/README.md"
HANDOFF = "docs/implementation/handoffs/01-contracts.md"
ADR002 = "docs/decisions/002-step01-contracts-and-policies.md"
BASELINE = "docs/specification.md"
STEP = "docs/implementation/01-contracts.md"
KICKOFF = "docs/implementation/kickoffs/01-contracts.md"
ROOT_README = "README.md"

# Finalized 2026-10-27 interview decisions bind to this reviewed revision.
REVIEWED_REVISION = "ee7f7eafca80baa6c1aaca278361e56a663493c3"

# The policy/technical record ids the register must carry as `accepted`.
ACCEPTED_POLICY_IDS = ["POL-P1", "POL-P2", "POL-P3", "POL-P4", "POL-P5", "POL-P6"]
ACCEPTED_OPS_IDS = ["POL-OPS-PILOT", "POL-OPS-SEVERITY", "POL-OPS-COVERAGE",
                    "POL-OPS-COST", "POL-OPS-TRACE", "POL-OPS-SECONDARY"]
ACCEPTED_ARCH_IDS = ["ARCH-STORE", "ARCH-ASSETS", "ARCH-IPC", "ARCH-PKG",
                     "ARCH-MIGRATE", "ARCH-ACTIVE-ENF", "ARCH-RECOVERY"]
ACCEPTED_RIGHTS_IDS = ["RIGHTS-v1"]
ACCEPTED_IDS = (ACCEPTED_POLICY_IDS + ACCEPTED_OPS_IDS + ACCEPTED_ARCH_IDS
                + ACCEPTED_RIGHTS_IDS)
# Records whose representative numeric value stays UNSET (no invented cutoff):
# every policy/operational record except the accepted cost/concurrency default.
UNSET_NUMERIC_IDS = ACCEPTED_POLICY_IDS + [i for i in ACCEPTED_OPS_IDS
                                           if i != "POL-OPS-COST"]

OPS = {"allowed", "not_allowed", "unknown", "not_applicable"}
BASIS_KINDS = {"license", "public_domain", "statutory", "publisher_permission",
               "user_authorization", "unknown"}
STATE = {"known", "missing", "unknown", "not_applicable"}
DEC_STATUS = {"proposed", "accepted", "modified", "deferred"}
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
DECIMAL = re.compile(r"^\d+\.\d+$")

# Declared question definitions the documents state (only what the docs fix).
QUESTION_LEVELS = {"confidence_level": ["low", "moderate", "high"]}
WIRE = {"predicate": "noul", "choice": "choice", "score": "score"}

# Stable expectation table for the C03 decision examples (by the document's own
# ids; the validator must agree with what the document claims about each).
EXPECT_DECISIONS = {
    ("POL-OPS-COST", "proposed"): "valid-full",
    ("POL-OPS-COST", "accepted"): "patch",
    ("POL-P1", "accepted"): "reject",
    ("POL-OPS-PILOT", "proposed"): "reject",
}
DECISION_MANDATORY = ["id", "title", "status", "baseline_excerpt", "baseline_ref",
                      "recommendation", "options", "owner", "approval_required",
                      "user_evidence", "decided_against_revision", "affected_stages",
                      "blocking_dependencies", "fail_closed_behavior",
                      "numeric_value", "notes"]

failures = []
notes = []


def fail(msg):
    failures.append(msg)


def note(msg):
    notes.append(msg)


def is_str(x):
    return isinstance(x, str) and x != ""


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return fh.read()


def fenced_blocks(text, lang="json"):
    """Line-based fenced-block extractor.

    A line-based scanner, not a regex, so a ` ```json ` occurrence *inside*
    another fenced block (e.g. the C01 inline validator's regex source) is not
    mistaken for a fence opener.
    """
    out = []
    buf = []
    inblock = False
    opener = "```" + lang
    for line in text.splitlines():
        s = line.strip()
        if not inblock:
            if s.startswith(opener) and (len(s) == len(opener) or not s[len(opener)].isalnum()):
                inblock = True
                buf = []
            continue
        if s == "```":
            out.append("\n".join(buf))
            inblock = False
        else:
            buf.append(line)
    return out


# --------------------------------------------------------------------------- #
# markdown link / fragment checking
# --------------------------------------------------------------------------- #
def slug(heading):
    s = heading.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = s.replace(" ", "-")
    return s


def headings(text):
    return {slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.*?)\s*$", text, re.M)}


def check_links(path):
    text = read(path)
    problems = []
    for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text):
        tgt = m.group(1).strip()
        if re.match(r"^[a-z][a-z0-9+.-]*://", tgt) or tgt.startswith("mailto:"):
            continue
        filepart, _, frag = tgt.partition("#")
        if filepart == "":
            target = path
        else:
            target = os.path.normpath(os.path.join(os.path.dirname(path), filepart))
        if not os.path.exists(os.path.join(ROOT, target)):
            problems.append(f"{path}: missing local link target {tgt!r}")
            continue
        if frag:
            if frag not in headings(read(target)):
                problems.append(f"{path}: fragment #{frag} not found in {target}")
    return problems


# --------------------------------------------------------------------------- #
# C01 inline validator extraction + execution
# --------------------------------------------------------------------------- #
C01_VALIDATOR_RE = re.compile(
    r"<!--\s*C7\.0-VALIDATOR-BEGIN\s*-->\s*```python\n(.*?)```\s*<!--\s*C7\.0-VALIDATOR-END\s*-->",
    re.S)


def c01_validator_src(doc_text):
    m = C01_VALIDATOR_RE.search(doc_text)
    return m.group(1) if m else None


def run_c01_validator(src, target_path):
    """Exec the extracted validator and call main(target_path). Returns exit code."""
    ns = {"__name__": "c01_inline_validator"}
    exec(compile(src, "<c01-inline-validator>", "exec"), ns)
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            ns["main"](target_path)
        except SystemExit as exc:
            code = exc.code or 0
    return code, buf.getvalue()


def run_c01_on_data(src, data):
    fd, tmp = tempfile.mkstemp(suffix=".md")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("<!-- C7.0-JSON-BEGIN -->\n```json\n"
                     + json.dumps(data) + "\n```\n<!-- C7.0-JSON-END -->\n")
        code, _ = run_c01_validator(src, tmp)
        return code != 0
    finally:
        os.unlink(tmp)


# --------------------------------------------------------------------------- #
# rights record validation (canonical identity + rules)
# --------------------------------------------------------------------------- #
def rights_violations(obj, label):
    v = []
    if not isinstance(obj, dict):
        return [f"{label}: malformed record is not a JSON object"]

    def need(field, ok):
        if field not in obj:
            v.append(f"{label}: missing mandatory field {field!r}")
        elif not ok(obj[field]):
            v.append(f"{label}: field {field!r} has the wrong type")

    need("rights_id", is_str)
    need("project_id", is_str)
    need("record_revision", lambda x: isinstance(x, int) and not isinstance(x, bool))
    need("subject", lambda x: isinstance(x, dict))
    need("operations", lambda x: isinstance(x, dict))
    need("basis", lambda x: isinstance(x, dict))
    need("restrictions", lambda x: isinstance(x, list))
    need("privacy", lambda x: isinstance(x, dict))
    need("recorded_by", is_str)
    need("recorded_at", is_str)
    if "authorization" not in obj:
        v.append(f"{label}: missing mandatory 'authorization' key (present or null)")

    subj = obj.get("subject")
    if isinstance(subj, dict):
        kind = subj.get("kind")
        if kind == "source_revision":
            for k in ("project_id", "artifact_id", "revision_id"):
                if not is_str(subj.get(k)):
                    v.append(f"{label}: source_revision subject missing {k}")
        elif kind == "subject_artifact_ref":
            for k in ("project_id", "artifact_id"):
                if not is_str(subj.get(k)):
                    v.append(f"{label}: subject_artifact_ref subject missing {k}")
            if "revision_id" in subj:
                v.append(f"{label}: subject_artifact_ref must not carry a revision_id")
        else:
            v.append(f"{label}: subject.kind {kind!r} is not the canonical union")
        if is_str(obj.get("project_id")) and subj.get("project_id") != obj["project_id"]:
            v.append(f"{label}: subject cross-project {subj.get('project_id')!r}")

    ops = obj.get("operations")
    basis = obj.get("basis")
    if isinstance(ops, dict):
        for op in ("acquisition", "storage", "redistribution", "hosted_transfer"):
            if op not in ops:
                v.append(f"{label}: operations missing {op}")
            elif ops[op] not in OPS:
                v.append(f"{label}: operations.{op} bad enum {ops[op]!r}")
        if isinstance(basis, dict):
            for op in ("acquisition", "storage", "redistribution", "hosted_transfer"):
                if ops.get(op) != "allowed":
                    continue
                b = basis.get(op)
                if not isinstance(b, dict):
                    v.append(f"{label}: allowed {op} needs a typed basis")
                    continue
                if b.get("kind") not in BASIS_KINDS:
                    v.append(f"{label}: {op} basis kind {b.get('kind')!r} invalid")
                if b.get("kind") == "unknown":
                    v.append(f"{label}: allowed {op} cannot have an 'unknown' basis")
                ev = b.get("evidence")
                if not (isinstance(ev, dict) and is_str(ev.get("ref"))
                        and is_str(ev.get("provenance"))):
                    v.append(f"{label}: allowed {op} needs non-empty evidence ref+provenance")
                if b.get("kind") == "user_authorization" and op in ("redistribution", "hosted_transfer"):
                    v.append(f"{label}: user_authorization cannot confer {op}")

    priv = obj.get("privacy")
    if isinstance(priv, dict):
        if not isinstance(priv.get("personal_data"), bool):
            v.append(f"{label}: privacy.personal_data must be bool")
        if priv.get("confidentiality") not in {"public", "confidential", "unknown"}:
            v.append(f"{label}: privacy.confidentiality bad enum")
        if not isinstance(priv.get("denial"), bool):
            v.append(f"{label}: privacy.denial must be bool")

    auth = obj.get("authorization")
    if isinstance(auth, dict):
        if auth.get("method") != "out_of_band_authenticated":
            v.append(f"{label}: authorization.method must be out_of_band_authenticated")
        if not is_str(auth.get("actor")):
            v.append(f"{label}: authorization.actor must be a non-empty string")
        if auth.get("against_subject") != subj:
            v.append(f"{label}: authorization must bind the exact source subject")
        if auth.get("against_rights_revision") != obj.get("record_revision"):
            v.append(f"{label}: authorization must bind the exact rights record_revision")
        ab = auth.get("basis")
        if not isinstance(ab, dict) or ab.get("kind") not in BASIS_KINDS:
            v.append(f"{label}: authorization.basis must be typed")

    dstat = obj.get("derived_status")
    if dstat is not None and dstat not in {"incomplete", "valid", "inconsistent"}:
        v.append(f"{label}: derived_status {dstat!r} invalid")
    return v


# --------------------------------------------------------------------------- #
# C02 record validation
# --------------------------------------------------------------------------- #
def version_violations(vers, label):
    v = []
    if vers.get("context_kind") not in {"model_derived", "human_authored", "mixed"}:
        v.append(f"{label}: versions.context_kind invalid")
    if not DIGEST.match(str(vers.get("input_digest", ""))):
        v.append(f"{label}: versions.input_digest must be 64-hex")
    for slot in ("model", "prompt", "rubric", "parser", "adapter"):
        s = vers.get(slot)
        if not isinstance(s, dict):
            v.append(f"{label}: versions.{slot} missing")
            continue
        st = s.get("state")
        if st not in {"known", "unknown", "not_applicable"}:
            v.append(f"{label}: versions.{slot}.state invalid")
        if st == "known":
            ref = s.get("ref")
            if not (isinstance(ref, dict) and is_str(ref.get("version"))
                    and DIGEST.match(str(ref.get("digest", "")))):
                v.append(f"{label}: versions.{slot} known requires version+digest")
        elif "ref" in s:
            v.append(f"{label}: versions.{slot} {st} must not carry a ref")
    if vers.get("route") not in {"opencode_session", "direct_call", "not_applicable"}:
        v.append(f"{label}: versions.route invalid")
    return v


def cost_violations(c, label):
    v = []
    if not isinstance(c, dict):
        return [f"{label}: malformed cost record is not a JSON object"]
    st = c.get("state")
    if st not in {"known", "estimated", "unknown", "not_incurred"}:
        v.append(f"{label}: cost.state invalid")
    if st in {"known", "estimated"}:
        amt = c.get("amount_usd")
        if not (isinstance(amt, str) and DECIMAL.match(amt)):
            v.append(f"{label}: cost.amount_usd must be an exact decimal string")
    elif "amount_usd" in c:
        v.append(f"{label}: cost {st} must not carry amount_usd (unknown cost is not zero)")
    if c.get("currency") != "USD":
        v.append(f"{label}: cost.currency must be USD")
    route = c.get("route")
    if route not in {"opencode_session", "direct_call"}:
        v.append(f"{label}: cost.route invalid")
    expected_bucket = "direct_call" if route == "direct_call" else "session_statistics"
    if c.get("accounting_bucket") != expected_bucket:
        v.append(f"{label}: cost.accounting_bucket inconsistent with route")
    if st in {"unknown", "estimated"} and not is_str(c.get("reserve_usd")):
        v.append(f"{label}: cost reserve must be retained for {st}")
    return v


def decision_c02_violations(obj, label):
    v = []
    if not isinstance(obj, dict):
        return [f"{label}: malformed decision record is not a JSON object"]
    for f in ("decision_id", "question_id", "question_def_ref", "target",
              "outcome", "versions", "route"):
        if f not in obj:
            v.append(f"{label}: missing {f}")
    if obj.get("outcome") not in {"answered", "partial_error", "uncertain", "omitted",
                                  "refusal", "malformed", "unsupported"}:
        v.append(f"{label}: outcome invalid")
    if obj.get("route") not in {"opencode_session", "direct_call"}:
        v.append(f"{label}: route invalid")

    ans = obj.get("answer")
    if obj.get("outcome") in {"answered", "partial_error"} and ans is None:
        v.append(f"{label}: outcome {obj.get('outcome')} requires an answer")
    if isinstance(ans, dict):
        ak = ans.get("answer_kind")
        if ak not in {"predicate", "choice", "score"}:
            v.append(f"{label}: answer_kind {ak!r} invalid")
        if ans.get("wire_type") != WIRE.get(ak):
            v.append(f"{label}: wire_type inconsistent with answer_kind")
        if ak == "predicate":
            p = ans.get("probability")
            if not (isinstance(p, (int, float)) and not isinstance(p, bool) and 0.0 <= p <= 1.0):
                v.append(f"{label}: predicate probability must be a number in [0,1]")
            if "choice" in ans or "score" in ans:
                v.append(f"{label}: predicate answer carries foreign fields")
        elif ak == "choice":
            if not is_str(ans.get("choice")):
                v.append(f"{label}: choice answer needs a non-empty choice")
        elif ak == "score":
            sc = ans.get("score")
            levels = QUESTION_LEVELS.get(obj.get("question_id"))
            if not (isinstance(sc, (int, float)) and not isinstance(sc, bool)):
                v.append(f"{label}: score must be a number")
            elif levels is not None and not (0 <= sc <= len(levels) - 1):
                v.append(f"{label}: score {sc} outside declared levels 0..{len(levels)-1}")
            legend = ans.get("legend")
            if isinstance(legend, dict) and levels is not None:
                for k in legend:
                    if not (is_str(k) and k.isdigit() and 0 <= int(k) <= len(levels) - 1):
                        v.append(f"{label}: legend key {k!r} is not a declared level index")

    vers = obj.get("versions")
    if isinstance(vers, dict):
        v += version_violations(vers, label)
        if vers.get("adapter", {}).get("state") != "known":
            v.append(f"{label}: adapter version slot must be known for Jev")
    if obj.get("route") == "direct_call":
        if not isinstance(obj.get("cost"), dict):
            v.append(f"{label}: direct_call requires a cost record")
        else:
            v += cost_violations(obj["cost"], label)
    return v


def approval_violations(obj, label):
    v = []
    if not isinstance(obj, dict):
        return [f"{label}: malformed approval record is not a JSON object"]
    for f in ("approval_id", "granted_by", "project_id", "target", "decision",
              "gate_kind", "gate_id", "dependency_snapshot", "granted_at", "status",
              "rationale"):
        if f not in obj:
            v.append(f"{label}: missing {f}")
    gb = obj.get("granted_by")
    if not isinstance(gb, dict):
        v.append(f"{label}: granted_by must be an object")
    else:
        if gb.get("actor_kind") != "human":
            v.append(f"{label}: granted_by.actor_kind must be human")
        if gb.get("authority") != "owner_authenticated":
            v.append(f"{label}: granted_by.authority must be owner_authenticated")
        if not is_str(gb.get("owner_auth_ref")):
            v.append(f"{label}: owner_auth_ref required for owner_authenticated")
    if obj.get("decision") not in {"approved", "rejected"}:
        v.append(f"{label}: decision invalid")
    if obj.get("gate_kind") not in {"protocol", "pilot", "outline", "framework",
                                    "exclusion_set", "artifact_revision", "batch",
                                    "final_release"}:
        v.append(f"{label}: gate_kind invalid")
    if obj.get("status") not in {"active", "stale", "invalidated", "superseded"}:
        v.append(f"{label}: status invalid")
    if not is_str(obj.get("rationale")):
        v.append(f"{label}: rationale must be non-empty")
    pid = obj.get("project_id")
    tgt = obj.get("target")
    if not isinstance(tgt, dict) or tgt.get("project_id") != pid:
        v.append(f"{label}: target project mismatch")
    snap = obj.get("dependency_snapshot")
    if not isinstance(snap, dict):
        v.append(f"{label}: dependency_snapshot missing")
    else:
        if snap.get("project_id") != pid:
            v.append(f"{label}: snapshot project mismatch")
        entries = snap.get("entries")
        if not isinstance(entries, list) or not entries:
            v.append(f"{label}: snapshot entries must be non-empty")
        else:
            for i, e in enumerate(entries):
                if e.get("project_id") != pid:
                    v.append(f"{label}: snapshot entry {i} cross-project")
                if not DIGEST.match(str(e.get("digest", ""))):
                    v.append(f"{label}: snapshot entry {i} digest must be 64-hex")
                if e.get("role") not in {"source_document", "summary", "finding", "rubric",
                                         "framework", "outline", "parser_output",
                                         "model_output", "question_def", "other"}:
                    v.append(f"{label}: snapshot entry {i} role invalid")
        vs = snap.get("versions")
        if isinstance(vs, dict):
            v += version_violations(vs, label)
    return v


def decision_c03_violations(obj, label):
    v = []
    st = obj.get("status")
    if st not in DEC_STATUS:
        v.append(f"{label}: status {st!r} invalid")
    full_markers = {"owner", "approval_required", "options"}
    if full_markers <= set(obj):
        miss = [f for f in DECISION_MANDATORY if f not in obj]
        if miss:
            v.append(f"{label}: full record missing {miss}")
    if st == "proposed" and obj.get("numeric_value") != "UNSET":
        v.append(f"{label}: proposed record carries numeric_value "
                 f"{obj.get('numeric_value')!r} (only the user may set a number)")
    if st in {"accepted", "modified", "deferred"}:
        if not is_str(obj.get("user_evidence")):
            v.append(f"{label}: {st} without user_evidence")
        if not is_str(obj.get("decided_against_revision")):
            v.append(f"{label}: {st} without decided_against_revision")
    return v


# --------------------------------------------------------------------------- #
# finalized-interview ratification evidence (distinct from synthetic fixtures)
# --------------------------------------------------------------------------- #
SUMMARY_ID_RE = re.compile(r"^(POL-[\w-]+|ARCH-[\w-]+|RIGHTS-v1)$")


def summary_rows(text):
    """Parse the register's status-summary table into {id: [cells]}.

    Keyed by the stable record id in the first cell; tolerant of status markup
    changes (bold, dates) elsewhere in the row.
    """
    rows = {}
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and SUMMARY_ID_RE.match(cells[0]):
            rows[cells[0]] = cells
    return rows


def mutate_summary_cell(text, rid, index, value):
    """Return text with the summary row `rid`'s cell `index` replaced by `value`.

    Test helper for the self-test drift mutations; tolerant of surrounding markup.
    """
    out = []
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\n")
        if bare.strip().startswith("|"):
            cells = [c.strip() for c in bare.strip().strip("|").split("|")]
            if cells and cells[0] == rid and index < len(cells):
                cells[index] = value
                bare = "| " + " | ".join(cells) + " |"
                line = bare + ("\n" if line.endswith("\n") else "")
        out.append(line)
    return "".join(out)


def ratification_violations(adr_text=None, reg_text=None):
    """Evidence that the finalized 2026-10-27 interview was ratified.

    Checks stable anchors (accepted-scope phrase, reviewed revision, per-record
    status rows, an amendment count range), not headings or long prose.
    """
    adr = read(ADR002) if adr_text is None else adr_text
    reg = read(DOCS["c03"]) if reg_text is None else reg_text
    v = []

    if not re.search(r"Accepted by the user", adr):
        v.append("ADR002: does not declare 'Accepted by the user'")
    if "2026-10-27" not in adr:
        v.append("ADR002: missing acceptance date 2026-10-27")
    if not re.search(r"described behavior", adr):
        v.append("ADR002: does not state the accepted behavioral scope "
                 "('described behavior')")
    if REVIEWED_REVISION not in adr:
        v.append(f"ADR002: reviewed revision {REVIEWED_REVISION} missing")
    if REVIEWED_REVISION not in reg:
        v.append(f"register: reviewed revision {REVIEWED_REVISION} missing")

    # A bounded range, not an exact prose match: the register states one
    # interview amendment per accepted policy/technical record.
    n_amend = len(re.findall(r"Interview amendment", reg))
    if n_amend < 10:
        v.append(f"register: too few 'Interview amendment' records ({n_amend} < 10)")

    if not re.search(r"\b(ACCEPTED|RATIFIED)\b", reg):
        v.append("register: no ACCEPTED/RATIFIED status banner")

    rows = summary_rows(reg)
    for rid in ACCEPTED_IDS:
        row = rows.get(rid)
        if row is None:
            v.append(f"register status summary: record {rid} missing")
        elif "accepted" not in " ".join(row).lower():
            v.append(f"register status summary: {rid} not marked accepted")
    return v


def defaults_violations(reg_text=None, c02_text=None):
    """The normative $25 / $10 / 2 cost/concurrency defaults must agree.

    Reads the register's structured caps object and status-summary row and C02's
    normative §5.6/§12 text; the synthetic fixtures never satisfy this.
    """
    reg = read(DOCS["c03"]) if reg_text is None else reg_text
    c02 = read(DOCS["c02"]) if c02_text is None else c02_text
    v = []

    if not re.search(r"project_cap_usd:\s*25\b", reg):
        v.append("register: project_cap_usd 25 missing from the structured caps")
    if not re.search(r"stage_cap_usd:\s*10\b", reg):
        v.append("register: stage_cap_usd 10 missing from the structured caps")
    if not re.search(r"max_concurrent_external_model_calls:\s*2\b", reg):
        v.append("register: max_concurrent_external_model_calls 2 missing "
                 "from the structured caps")

    cost_row = summary_rows(reg).get("POL-OPS-COST")
    if cost_row is None:
        v.append("register status summary: POL-OPS-COST row missing")
    elif not re.search(r"25\s*/\s*10\s*/\s*2", " ".join(cost_row)):
        v.append("register status summary: POL-OPS-COST numeric column is not "
                 "'25 / 10 / 2'")

    if not re.search(r"USD\s*25\b", c02):
        v.append("C02: USD 25 cumulative project cap missing")
    if not re.search(r"USD\s*10\b", c02):
        v.append("C02: USD 10 cumulative stage cap missing")
    if not re.search(r"at most\s*2\b", c02):
        v.append("C02: 'at most 2' concurrent external model calls missing")
    return v


def no_invented_cutoff_violations(reg_text=None):
    """Per-project/categorical records must keep numeric_value = UNSET.

    Detects a numeric pilot/coverage/tracing threshold (or a numeric P1–P6
    value) recorded as a disposition in the register's status summary. It does
    not scan the intentional invalid fixtures, which stay historical examples.
    """
    reg = read(DOCS["c03"]) if reg_text is None else reg_text
    v = []
    rows = summary_rows(reg)
    for rid in UNSET_NUMERIC_IDS:
        row = rows.get(rid)
        if row is None:
            v.append(f"register status summary: record {rid} missing")
            continue
        numeric = row[4].strip("* ").strip() if len(row) > 4 else ""
        if numeric != "UNSET":
            v.append(f"register status summary: {rid} numeric_value is "
                     f"{numeric!r}, expected UNSET (no invented cutoff)")
    return v


# --------------------------------------------------------------------------- #
# main checks
# --------------------------------------------------------------------------- #
def check_bundle():
    link_docs = (DOCS["c01"], DOCS["c02"], DOCS["c03"], README, ADR002,
                 BASELINE, STEP, HANDOFF, KICKOFF, ROOT_README)
    for path in link_docs:
        for problem in check_links(path):
            fail(problem)

    # every fenced json block parses
    parsed = {}
    for key, path in DOCS.items():
        blocks = fenced_blocks(read(path))
        parsed[key] = []
        for i, raw in enumerate(blocks):
            try:
                parsed[key].append(json.loads(raw))
            except Exception as exc:  # noqa: BLE001
                fail(f"{path}: fenced json block {i} does not parse: {exc}")

    # C01: run the document's own inline validator against the document
    c01_text = read(DOCS["c01"])
    src = c01_validator_src(c01_text)
    if src is None:
        fail("C01: inline validator markers not found")
    else:
        code, out = run_c01_validator(src, os.path.join(ROOT, DOCS["c01"]))
        if code != 0 or not out.startswith("OK:"):
            fail(f"C01: inline validator did not pass: {out.strip()}")
        else:
            note(f"C01 §7.0 inline validator: {out.strip()}")

    # C02: three complete examples
    c02 = parsed["c02"]
    if len(c02) != 3:
        fail(f"C02: expected 3 json blocks, found {len(c02)}")
    for obj in c02:
        if "approval_id" in obj:
            label = "C02 approval"
            v = approval_violations(obj, label)
        elif "decision_id" in obj:
            label = f"C02 decision {obj.get('decision_id')}"
            v = decision_c02_violations(obj, label)
        else:
            fail("C02: unrecognized json block")
            continue
        if v:
            for x in v:
                fail(x)
        else:
            note(f"{label}: valid")

    # C03: eight blocks, expectations distinguished
    c03 = parsed["c03"]
    if len(c03) != 8:
        fail(f"C03: expected 8 json blocks, found {len(c03)}")
    rights_seen = {"A": 0, "B": 0, "C": 0, "D": 0}
    dec_seen = {}
    for obj in c03:
        if "rights_id" in obj:
            rid = obj["rights_id"]
            suffix = None
            for s in ("A", "B", "C", "D"):
                if rid.endswith("-" + s):
                    suffix = s
            if suffix is None:
                fail(f"C03: unexpected rights example {rid!r}")
                continue
            rights_seen[suffix] += 1
            v = rights_violations(obj, f"C03 {rid}")
            if suffix in ("A", "C"):
                if v:
                    for x in v:
                        fail(f"{rid} (expected valid): {x}")
                else:
                    note(f"{rid}: valid")
            else:
                if not v:
                    fail(f"{rid}: expected rule violations, found none")
                else:
                    note(f"{rid}: rejected as documented ({len(v)} violation(s))")
            if suffix == "B" and obj.get("derived_status") != "inconsistent":
                fail(f"{rid}: expected derived_status inconsistent")
            if suffix == "C" and obj.get("subject", {}).get("kind") != "subject_artifact_ref":
                fail(f"{rid}: expected subject_artifact_ref")
            if suffix == "D":
                for req in ("basis", "authorization", "privacy"):
                    if req in obj:
                        fail(f"{rid}: raw-incomplete example should not carry {req}")
        elif "id" in obj and "status" in obj:
            key = (obj["id"], obj.get("status"))
            if key not in EXPECT_DECISIONS:
                fail(f"C03: unexpected decision block {key}")
                continue
            dec_seen[key] = dec_seen.get(key, 0) + 1
            expect = EXPECT_DECISIONS[key]
            v = decision_c03_violations(obj, f"C03 decision {key[0]}/{key[1]}")
            if expect == "reject":
                if not v:
                    fail(f"C03 {key}: expected rejection, found none")
                else:
                    note(f"C03 {key}: rejected as documented ({len(v)} violation(s))")
            elif expect == "valid-full":
                if v:
                    for x in v:
                        fail(f"C03 {key} (expected full valid): {x}")
                else:
                    note(f"C03 {key}: full proposed record valid")
            elif expect == "patch":
                for f in ("user_evidence", "decided_against_revision"):
                    if f not in obj:
                        fail(f"C03 {key}: illustrative patch must carry {f}")
                note(f"C03 {key}: illustrative patch accepted as partial by design")
        else:
            fail("C03: unrecognized json block")

    for s, n in rights_seen.items():
        if n != 1:
            fail(f"C03: expected exactly one rights example {s}, found {n}")
    for key in EXPECT_DECISIONS:
        if dec_seen.get(key, 0) != 1:
            fail(f"C03: expected exactly one decision block {key}, found {dec_seen.get(key, 0)}")

    # Finalized-interview ratification evidence and normative default agreement.
    for _group, violations, ok_note in (
        ("ratification", ratification_violations(),
         "ratification: ADR002 accepted behavioral scope + register statuses "
         "anchored to the reviewed revision"),
        ("cost defaults", defaults_violations(),
         "cost defaults: register and C02 agree on USD 25 / USD 10 / 2"),
        ("per-project cutoffs", no_invented_cutoff_violations(),
         "per-project policies: pilot/coverage/tracing numeric values stay UNSET "
         "(no invented cutoff)"),
    ):
        if violations:
            for x in violations:
                fail(x)
        else:
            note(ok_note)


# --------------------------------------------------------------------------- #
# self-test: negative mutations must be rejected
# --------------------------------------------------------------------------- #
def self_test():
    c01_text = read(DOCS["c01"])
    src = c01_validator_src(c01_text)
    if src is None:
        fail("self-test: C01 validator markers not found")
        return

    m = re.search(r"<!--\s*C7\.0-JSON-BEGIN\s*-->(.*?)<!--\s*C7\.0-JSON-END\s*-->",
                  c01_text, re.S)
    c01_data = json.loads(re.search(r"```json\s*(.*?)```", m.group(1), re.S).group(1))

    c02 = [json.loads(b) for b in fenced_blocks(read(DOCS["c02"]))]
    approval = next(o for o in c02 if "approval_id" in o)
    dec_pred = next(o for o in c02 if o.get("decision_id") == "dec_0001")
    dec_score = next(o for o in c02 if o.get("decision_id") == "dec_0002")

    c03 = [json.loads(b) for b in fenced_blocks(read(DOCS["c03"]))]
    rights_a = next(o for o in c03 if o.get("rights_id", "").endswith("-A"))

    def expect_reject(desc, rejected):
        if rejected:
            note(f"self-test '{desc}': rejected as required")
        else:
            fail(f"self-test '{desc}': mutation was NOT rejected")

    # 1. bad hash
    d = json.loads(json.dumps(c01_data))
    d["revision_records"][0]["content_hash"]["value"] = "sha256:nothex"
    expect_reject("C01 bad hash", run_c01_on_data(src, d))

    d = json.loads(json.dumps(approval))
    d["dependency_snapshot"]["entries"][0]["digest"] = "xyz"
    expect_reject("C02 snapshot bad digest", bool(approval_violations(d, "st")))

    # 2. missing required / bad state value
    d = json.loads(json.dumps(c01_data))
    d["revision_records"][0]["content_hash"] = {"state": "known"}  # known without value
    expect_reject("C01 stated known without value", run_c01_on_data(src, d))

    d = json.loads(json.dumps(rights_a))
    del d["privacy"]
    expect_reject("C03 rights missing required privacy", bool(rights_violations(d, "st")))

    # 3. cross-project ref
    d = json.loads(json.dumps(rights_a))
    d["subject"]["project_id"] = "proj-other"
    expect_reject("C03 rights cross-project subject", bool(rights_violations(d, "st")))

    # 4. allowed rights without basis
    d = json.loads(json.dumps(rights_a))
    d["basis"]["acquisition"]["evidence"] = {"ref": "", "provenance": ""}
    expect_reject("C03 allowed operation without basis evidence",
                  bool(rights_violations(d, "st")))

    # 5. unknown cost recorded as zero
    d = json.loads(json.dumps(dec_pred))
    d["cost"] = {"state": "unknown", "amount_usd": "0", "currency": "USD",
                 "route": "direct_call", "accounting_bucket": "direct_call",
                 "reserve_usd": "0.25"}
    expect_reject("C02 unknown cost recorded as zero",
                  bool(decision_c02_violations(d, "st")))

    # 6. wrong typed answer
    d = json.loads(json.dumps(dec_pred))
    d["answer"]["probability"] = 1.5
    expect_reject("C02 predicate probability out of [0,1]",
                  bool(decision_c02_violations(d, "st")))

    d = json.loads(json.dumps(dec_score))
    d["answer"]["score"] = 9.0  # outside declared levels 0..2
    expect_reject("C02 score outside declared levels",
                  bool(decision_c02_violations(d, "st")))

    # 7. ratification / normative-default evidence drift
    adr = read(ADR002)
    reg = read(DOCS["c03"])
    c02_text = read(DOCS["c02"])

    expect_reject("ratification drift: ADR002 reviewed revision removed",
                  bool(ratification_violations(
                      adr_text=adr.replace(REVIEWED_REVISION, "0" * 40),
                      reg_text=reg)))
    expect_reject("ratification drift: accepted record reverted to proposed",
                  bool(ratification_violations(
                      adr_text=adr,
                      reg_text=mutate_summary_cell(reg, "POL-P1", 3, "proposed"))))
    expect_reject("default drift: register project cap changed",
                  bool(defaults_violations(
                      reg_text=reg.replace("project_cap_usd: 25",
                                           "project_cap_usd: 30"),
                      c02_text=c02_text)))
    expect_reject("default drift: C02 stage cap changed",
                  bool(defaults_violations(
                      reg_text=reg,
                      c02_text=c02_text.replace("USD 10", "USD 20"))))
    expect_reject("cutoff drift: pilot numeric cutoff stood up as a disposition",
                  bool(no_invented_cutoff_violations(
                      reg_text=mutate_summary_cell(reg, "POL-OPS-PILOT", 4,
                                                   "0.95"))))


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true",
                    help="run negative-mutation self-tests in addition to the checks")
    args = ap.parse_args()

    check_bundle()
    if args.self_test:
        self_test()

    for n in notes:
        print("  ok:", n)
    print(f"checks: {len(notes)} ok, {len(failures)} failed")
    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("OK: Step 01 contract docs are self-consistent"
          + (" (including self-test)" if args.self_test else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
