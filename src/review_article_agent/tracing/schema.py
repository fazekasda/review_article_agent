"""Pure, deterministic citation-origin tracing rules for the Step 13 stage (MVP).

A small, dependency-light MVP: like :mod:`review_article_agent.review.schema` it is
plain ``dict``-in/``dict``-out with **no** storage, network, or model calls.

Authority: ``docs/implementation/13-origin-tracing.md``; ``docs/specification.md``
"Citation-origin policy"; ``docs/contracts/03-policy-decisions.md`` POL-OPS-TRACE.

Obligations encoded:

* depth 0 is the cited (read) source; trace follows **claim-relevant in-text
  citations** toward **direct** support for an atomic claim -- never earliest
  discovery, never a whole bibliography;
* attributions: ``own_result``/``conceptual``/``secondary``/``mixed``/``unclear``;
  missing markers prove neither originality nor attribution;
* per-project limits (``max_hops``, ``max_sources_per_claim``,
  ``max_sources_per_project``, ``max_resolution_retries``) are **required**: unset
  -> ``configuration_required`` with no acquisition and no "limit reached" inferred
  (no universal default);
* acquiring a primary does not establish support -- a contradicting/qualifying
  upstream yields a correction *proposal* (never applied);
* at a limit/unavailable origin, a depth-0 source that itself supports the claim
  (Step 12 verdict ``supported``) -> ``secondary_warning`` (visible warning,
  ``requires_final_human_approval=True``, ``cite_source_key`` = the depth-0 source
  actually read); otherwise ``unsupported``.  ``primary_verified`` is never emitted;
* upstream candidates use the existing acquisition/disposition pipeline via an
  injected port; tracing never admits sources to the corpus or exceeds budgets.

Disclosed limitation: attribution cues and support assessment are **lexical
heuristics** (case-insensitive cue regex lists over synthetic fixtures, plus
content-word overlap) -- deterministic and fixturable, not semantics of arbitrary
prose; no exactly-correct historical-origin claim is permitted.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque

from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
#: The five attribution classes (spec "Citation-origin policy").
ATTRIBUTIONS = ("own_result", "conceptual", "secondary", "mixed", "unclear")

#: The four required per-project limiting fields (no universal default; POL-OPS-TRACE).
POLICY_FIELDS = (
    "max_hops",
    "max_sources_per_claim",
    "max_sources_per_project",
    "max_resolution_retries",
)

#: Case-insensitive cue phrases.  A cue is present when it appears (as a substring)
#: in the *claim-relevant* passage text.
_OWN_CUES = (
    "we found",
    "we observed",
    "our results",
    "in this study",
    "we measured",
    "we reanaly",
    "our analysis",
)
_CONCEPTUAL_CUES = ("we propose", "we introduce", "we define", "our framework")
_SECONDARY_CUES = ("reported", "has been shown", "previously", "showed that", "according to")
_METHOD_CUES = ("using the method", "as described", "protocol of", "method of")
_REUSE_CUES = ("data from", "reanaly", "dataset")

#: Negation markers that make an upstream passage *contradict* an unnegated atom.
_NEGATION_CUES = ("did not", "no significant", "not ", "failed to")

#: Qualifier/hedge markers; copied (small) from review/schema.py so a qualifying
#: upstream passage is not mistaken for a plain supporting one.
_QUALIFIER_PATTERNS = (
    r"\bmay\b",
    r"\bmight\b",
    r"\bcould\b",
    r"\bsuggest",
    r"\bpossibly\b",
    r"\blikely\b",
    r"\bpreliminary\b",
    r"\bassociated\b",
    r"\bcorrelat",
    r"\bin mice\b",
    r"\bmouse model\b",
)

#: Species/population term groups; copied (small) from review/schema.py.
_SPECIES_GROUPS = (
    ("mouse", r"\bmouse\b|\bmice\b"),
    ("rat", r"\brats?\b"),
    ("human", r"\bhumans?\b|\bpatients?\b|\badults?\b|\bmen\b|\bwomen\b"),
    ("children", r"\bchildren\b|\bpediatric\b"),
    ("cells", r"\bcells?\b|\bin vitro\b"),
    ("animal", r"\bin vivo\b"),
)

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Visible warning text for a warned secondary attribution; never "verified".
_SECONDARY_WARNING = (
    "The origin of this claim could not be traced within the configured limits; the "
    "depth-0 source that was actually read supports the claim only as a secondary "
    "account. This is not direct verification and requires final human approval."
)


class TracingError(ReviewAgentError):
    """A tracing payload/policy is malformed.

    Messages name the structural defect only; they never embed source text.
    """


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TracingError(message)


def _digest(payload) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _dedupe(items) -> list:
    seen: set = set()
    ordered: list = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _content_words(text: str) -> set:
    """Content words of >= 4 characters, lowercased."""
    return {token for token in _WORD_RE.findall((text or "").lower()) if len(token) >= 4}


def _matches(text: str, patterns) -> list:
    return [pattern for pattern in patterns if re.search(pattern, text or "", flags=re.IGNORECASE)]


def _detect_cues(text: str) -> dict:
    return {
        "own": _matches(text, _OWN_CUES),
        "conceptual": _matches(text, _CONCEPTUAL_CUES),
        "secondary": _matches(text, _SECONDARY_CUES),
        "method": _matches(text, _METHOD_CUES),
        "reuse": _matches(text, _REUSE_CUES),
    }


def _has_cue(cues: dict, group: str) -> bool:
    return bool(cues.get(group))


def _source_key(source: dict) -> str | None:
    return source.get("source_key")


def _source_sha(source: dict) -> str | None:
    inner = source.get("source")
    return inner.get("sha256") if isinstance(inner, dict) else None


def _find_ref(source: dict, ref_id: str):
    for ref in source.get("references") or []:
        if isinstance(ref, dict) and ref.get("ref_id") == ref_id:
            return ref
    return None


def _marker_for_ref(source: dict, passage_ids, ref_id: str):
    for link in source.get("citation_links") or []:
        if not isinstance(link, dict):
            continue
        if link.get("passage_id") not in passage_ids:
            continue
        if ref_id in (link.get("resolved_ref_ids") or []):
            return link.get("passage_id"), link.get("marker")
    return None, None


# --------------------------------------------------------------------------- #
# validate_policy
# --------------------------------------------------------------------------- #
def validate_policy(policy) -> dict:
    """Return ``{"status", "missing"}`` for a per-project tracing policy.

    ``configured`` requires all four limits as non-negative integers (booleans are
    rejected).  A missing field is reported in ``missing`` (``configuration_required``);
    a present-but-malformed field (wrong type, bool, or negative) raises
    :class:`TracingError`.  ``None`` is treated as an entirely unset policy, never as
    "limits reached".
    """
    if policy is None:
        return {"status": "configuration_required", "missing": list(POLICY_FIELDS)}
    _require(isinstance(policy, dict), "policy must be a JSON object")
    missing = []
    for field in POLICY_FIELDS:
        if field not in policy:
            missing.append(field)
            continue
        value = policy[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise TracingError(f"policy.{field} must be a non-negative integer")
        if value < 0:
            raise TracingError(f"policy.{field} must be a non-negative integer")
    if missing:
        return {"status": "configuration_required", "missing": missing}
    return {"status": "configured", "missing": []}


# --------------------------------------------------------------------------- #
# classify_attribution
# --------------------------------------------------------------------------- #
def classify_attribution(atom, source) -> dict:
    """Classify how *source* attributes the atomic claim *atom*.

    Only markers whose ``passage_id`` is in ``atom.passage_ids`` count; a citation
    in another passage is not attribution.  Returns ``{"attribution", "evidence",
    "candidate_ref_ids", "method_ref_ids", "unresolved_markers", "notes"}``.
    """
    _require(isinstance(atom, dict), "atom must be a JSON object")
    _require(isinstance(source, dict), "source must be a JSON object")
    claim_pids = list(atom.get("passage_ids") or [])
    passages = {
        passage.get("passage_id"): passage
        for passage in source.get("passages") or []
        if isinstance(passage, dict)
    }

    cues_by_pid: dict = {}
    for pid in claim_pids:
        cues_by_pid[pid] = _detect_cues((passages.get(pid) or {}).get("text", ""))

    has_own = any(_has_cue(cues, "own") for cues in cues_by_pid.values())
    has_conceptual = any(_has_cue(cues, "conceptual") for cues in cues_by_pid.values())
    has_secondary = any(_has_cue(cues, "secondary") for cues in cues_by_pid.values())
    has_reuse = any(_has_cue(cues, "reuse") for cues in cues_by_pid.values())

    resolved_ref_ids: list = []
    unresolved_markers: list = []
    for link in source.get("citation_links") or []:
        if not isinstance(link, dict) or link.get("passage_id") not in claim_pids:
            continue
        if link.get("state") == "resolved":
            resolved_ref_ids.extend(link.get("resolved_ref_ids") or [])
        else:
            unresolved_markers.append(
                {
                    "passage_id": link.get("passage_id"),
                    "marker": link.get("marker"),
                    "state": link.get("state"),
                }
            )
    resolved_ref_ids = _dedupe(resolved_ref_ids)

    # Simple method-ref rule: a passage with a method cue and no secondary cue has
    # all of its resolved citations treated as method refs.
    method_ref_ids: list = []
    for pid in claim_pids:
        cues = cues_by_pid[pid]
        if _has_cue(cues, "method") and not _has_cue(cues, "secondary"):
            for link in source.get("citation_links") or []:
                if (
                    isinstance(link, dict)
                    and link.get("passage_id") == pid
                    and link.get("state") == "resolved"
                ):
                    method_ref_ids.extend(link.get("resolved_ref_ids") or [])
    method_ref_ids = _dedupe(method_ref_ids)

    evidence = []
    for pid, cues in cues_by_pid.items():
        for group in ("own", "conceptual", "secondary", "method", "reuse"):
            for cue in cues[group]:
                evidence.append({"passage_id": pid, "cue": cue})

    notes: list = []
    candidates: list = []
    if has_own and has_secondary:
        attribution = "mixed"
        candidates = [ref for ref in resolved_ref_ids if ref not in set(method_ref_ids)]
    elif has_own:
        attribution = "own_result"
        method_ref_ids = _dedupe(method_ref_ids + resolved_ref_ids)
        if has_reuse:
            notes.append("reused_data")
    elif has_conceptual:
        attribution = "conceptual"
    elif has_secondary and resolved_ref_ids:
        attribution = "secondary"
        candidates = [ref for ref in resolved_ref_ids if ref not in set(method_ref_ids)]
    else:
        attribution = "unclear"
        if not resolved_ref_ids and not unresolved_markers:
            notes.append("no_markers_not_proof_of_originality")

    return {
        "attribution": attribution,
        "evidence": evidence,
        "candidate_ref_ids": sorted(candidates),
        "method_ref_ids": sorted(method_ref_ids),
        "unresolved_markers": unresolved_markers,
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# assess_support
# --------------------------------------------------------------------------- #
def assess_support(atom_text, source) -> dict:
    """Read *source* and judge whether it supports *atom_text*.

    Picks the passage with the highest content-word overlap (words >= 4 chars,
    at least 0.5 of the atom's words); no such passage yields ``not_found``.  A
    negation absent from the atom yields ``contradicts``; an additional
    qualifier/species term absent from the atom yields ``qualifies``; otherwise
    ``supports``.
    """
    _require(isinstance(source, dict), "source must be a JSON object")
    atom_words = _content_words(atom_text if isinstance(atom_text, str) else "")
    best = None
    best_ratio = 0.0
    if atom_words:
        for passage in source.get("passages") or []:
            if not isinstance(passage, dict):
                continue
            words = _content_words(passage.get("text", ""))
            ratio = len(atom_words & words) / len(atom_words)
            if ratio > best_ratio:
                best_ratio = ratio
                best = passage
    if best is None or best_ratio < 0.5:
        return {"verdict": "not_found", "passage_id": None, "quote": None}

    text = best.get("text", "")
    atom_lower = (atom_text or "").lower()
    passage_lower = (text or "").lower()

    passage_negation = any(cue in passage_lower for cue in _NEGATION_CUES)
    if passage_negation and not any(cue in atom_lower for cue in _NEGATION_CUES):
        verdict = "contradicts"
    else:
        passage_terms = set(_matches(text, _QUALIFIER_PATTERNS))
        passage_terms |= {name for name, pattern in _SPECIES_GROUPS if re.search(pattern, text or "", re.IGNORECASE)}
        atom_terms = set(_matches(atom_text if isinstance(atom_text, str) else "", _QUALIFIER_PATTERNS))
        atom_terms |= {
            name
            for name, pattern in _SPECIES_GROUPS
            if re.search(pattern, atom_text if isinstance(atom_text, str) else "", re.IGNORECASE)
        }
        verdict = "qualifies" if (passage_terms - atom_terms) else "supports"

    return {"verdict": verdict, "passage_id": best.get("passage_id"), "quote": text}


# --------------------------------------------------------------------------- #
# trace_atom
# --------------------------------------------------------------------------- #
def _node(source_key, sha256, depth, attribution, corpus_admission=None) -> dict:
    node = {
        "source_key": source_key,
        "sha256": sha256,
        "depth": depth,
        "attribution": attribution,
    }
    if corpus_admission is not None:
        node["corpus_admission"] = corpus_admission
    return node


def _correction(verdict: str, upstream_key, support: dict) -> dict:
    return {
        "kind": verdict,
        "upstream_source_key": upstream_key,
        "passage_id": support.get("passage_id"),
        "quote": support.get("quote"),
        "proposed_action": (
            "revise the claim to reflect the primary source; route to step 11 "
            "drafting / step 12 review"
        ),
        "applied": False,
    }


def _blank_trace(atom) -> dict:
    return {
        "atom_id": atom.get("atom_id"),
        "outcome": None,
        "graph": {"nodes": [], "edges": []},
        "stopping_reasons": [],
        "corrections": [],
        "requests": [],
        "sources_used": 0,
        "cite_source_key": None,
        "warning": None,
        "requires_final_human_approval": False,
    }


def _edge(from_key, from_sha, to, ref_id, marker, passage_id, status, stop_reason) -> dict:
    return {
        "from": from_key,
        "to": to,
        "ref_id": ref_id,
        "marker": marker,
        "passage_id": passage_id,
        "status": status,
        "stop_reason": stop_reason,
        "provenance": {"from_sha256": from_sha, "passage_id": passage_id},
    }


def trace_atom(
    atom,
    *,
    root_source,
    policy,
    project_sources_used,
    acquire,
    depth0_verdict,
    claim_sources_used=0,
) -> dict:
    """Trace the origin of one atomic claim across cited sources (BFS).

    Never mutates its inputs and never admits upstream sources to the corpus.  An
    unconfigured policy returns ``configuration_required`` without calling
    ``acquire`` at all.  ``acquire(request)`` returns one of ``{"status":
    "acquired", "source": source}``, ``{"status": "pending_disposition"}``, or
    ``{"status": "unavailable"}``.

    ``claim_sources_used`` is the number of sources already consumed by earlier
    atoms of the *same* claim; it is added to the per-atom count for the
    ``max_sources_per_claim`` check so the whole claim shares one budget.
    """
    _require(isinstance(atom, dict), "atom must be a JSON object")
    _require(isinstance(root_source, dict), "root_source must be a JSON object")

    result = _blank_trace(atom)
    root_key, root_sha = _source_key(root_source), _source_sha(root_source)
    result["graph"]["nodes"].append(_node(root_key, root_sha, 0, None))

    check = validate_policy(policy)
    if check["status"] != "configured":
        result["outcome"] = "configuration_required"
        return result

    max_hops = policy["max_hops"]
    max_claim = policy["max_sources_per_claim"]
    max_project = policy["max_sources_per_project"]
    max_retries = policy["max_resolution_retries"]

    edges = result["graph"]["edges"]
    stopping = result["stopping_reasons"]
    corrections = result["corrections"]
    requests = result["requests"]
    nodes_by_key = {root_key: result["graph"]["nodes"][0]}
    visited = {root_key, root_sha}
    sources_used, outcome, cite_source_key, warning, requires = 0, None, None, None, False
    queue = deque([(root_source, 0, atom, root_key, root_sha)])

    while queue:
        source, depth, cur_atom, skey, ssha = queue.popleft()
        classification = classify_attribution(cur_atom, source)
        attribution = classification["attribution"]
        nodes_by_key[skey]["attribution"] = attribution
        for marker in classification["unresolved_markers"]:
            edges.append(
                _edge(
                    skey, ssha, None, None, marker.get("marker"),
                    marker.get("passage_id"), "stopped", "unresolved_reference",
                )
            )
            stopping.append("unresolved_reference")
        if attribution in ("own_result", "conceptual") and depth == 0:
            outcome, cite_source_key = "origin_traced", skey
            break
        if attribution in ("own_result", "conceptual"):
            support = assess_support(cur_atom.get("text", ""), source)
            if support["verdict"] == "supports":
                outcome, cite_source_key = "origin_traced", skey
                break
            if support["verdict"] in ("contradicts", "qualifies"):
                corrections.append(_correction(support["verdict"], skey, support))
                outcome = "correction_proposed"
                break
            stopping.append("not_supported_upstream")
            continue
        if not classification["candidate_ref_ids"]:
            continue

        correction_found = False
        for ref_id in classification["candidate_ref_ids"]:
            ref = _find_ref(source, ref_id)
            pid, marker = _marker_for_ref(source, cur_atom.get("passage_ids") or [], ref_id)

            def stop(reason, to=None):
                edges.append(_edge(skey, ssha, to, ref_id, marker, pid, "stopped", reason))
                stopping.append(reason)

            if ref is None:
                stop("unresolved_reference")
                continue
            if depth + 1 > max_hops:
                stop("max_hops")
                continue
            if claim_sources_used + sources_used >= max_claim:
                stop("claim_source_budget")
                continue
            if project_sources_used + sources_used >= max_project:
                stop("project_source_budget")
                continue
            identifiers = ref.get("identifiers") or {}
            if not (identifiers.get("doi") or identifiers.get("pmid")):
                stop("missing_identifier")
                continue

            acquired_source, last_reason, attempt = None, None, 0
            while acquired_source is None and attempt < 1 + max_retries:
                attempt += 1
                request = {
                    "route": "step06_acquisition", "requires_disposition": True,
                    "ref_text": ref.get("text"), "identifiers": identifiers,
                    "from_source_key": skey, "ref_id": ref_id, "attempt": attempt,
                }
                requests.append(request)
                response = acquire(request)
                status = response.get("status") if isinstance(response, dict) else None
                if status == "acquired":
                    acquired_source = response.get("source")
                elif status == "pending_disposition":
                    last_reason = "awaiting_disposition"
                    break
                else:
                    last_reason = "full_text_unavailable"
            if acquired_source is None:
                stop(last_reason)
                continue

            upstream_key, upstream_sha = _source_key(acquired_source), _source_sha(acquired_source)
            if upstream_key in visited or upstream_sha in visited:
                stop("cycle", to=upstream_key)
                continue
            visited |= {upstream_key, upstream_sha}
            sources_used += 1
            edge = _edge(skey, ssha, upstream_key, ref_id, marker, pid, "acquired", None)
            edges.append(edge)
            node = _node(upstream_key, upstream_sha, depth + 1, None, corpus_admission="not_admitted")
            nodes_by_key[upstream_key] = node
            result["graph"]["nodes"].append(node)

            support = assess_support(cur_atom.get("text", ""), acquired_source)
            if support["verdict"] in ("contradicts", "qualifies"):
                corrections.append(_correction(support["verdict"], upstream_key, support))
                outcome, correction_found = "correction_proposed", True
                break
            if support["verdict"] == "not_found":
                edge["status"], edge["stop_reason"] = "stopped", "not_supported_upstream"
                stopping.append("not_supported_upstream")
                node["attribution"] = "unclear"
                continue
            next_atom = dict(cur_atom)
            next_atom["passage_ids"] = [support["passage_id"]]
            queue.append((acquired_source, depth + 1, next_atom, upstream_key, upstream_sha))

        if correction_found:
            break

    if outcome is None:
        if depth0_verdict == "supported":
            outcome, cite_source_key, warning, requires = "secondary_warning", root_key, _SECONDARY_WARNING, True
        else:
            outcome = "unsupported"

    # Depth-0 support is adjudicated by Step 12 (the depth0_verdict): a claim
    # whose cited, actually-read source is *not* supported cannot be reported as
    # traced or as a warned secondary -- it is simply unsupported.  Traversal is
    # unchanged, so any correction an upstream primary suggests is still recorded.
    if depth0_verdict != "supported" and outcome in ("origin_traced", "secondary_warning"):
        outcome, cite_source_key, warning, requires = "unsupported", None, None, False

    result.update(
        outcome=outcome,
        stopping_reasons=_dedupe(stopping),
        sources_used=sources_used,
        cite_source_key=cite_source_key,
        warning=warning,
        requires_final_human_approval=requires,
    )
    return result


# --------------------------------------------------------------------------- #
# trace_claim
# --------------------------------------------------------------------------- #
def trace_claim(claim, *, root_source, policy, project_sources_used, acquire, depth0_verdict) -> dict:
    """Trace every atom of a compound claim; aggregate the outcome.

    The claim outcome is the single atom outcome when all atoms agree, else
    ``mixed_outcomes``.
    """
    _require(isinstance(claim, dict), "claim must be a JSON object")
    atoms = claim.get("atoms")
    _require(isinstance(atoms, list) and atoms, "claim.atoms must be a non-empty JSON array")
    traces = []
    sources_so_far = 0
    for atom in atoms:
        trace = trace_atom(
            atom,
            root_source=root_source,
            policy=policy,
            project_sources_used=project_sources_used + sources_so_far,
            acquire=acquire,
            depth0_verdict=depth0_verdict,
            claim_sources_used=sources_so_far,
        )
        traces.append(trace)
        sources_so_far += trace["sources_used"]
    outcomes = {trace["outcome"] for trace in traces}
    outcome = traces[0]["outcome"] if len(outcomes) == 1 else "mixed_outcomes"
    return {
        "claim_id": claim.get("claim_id"),
        "atoms": traces,
        "outcome": outcome,
        "sources_used": sources_so_far,
    }


# --------------------------------------------------------------------------- #
# trace_fingerprint
# --------------------------------------------------------------------------- #
def trace_fingerprint(atom, root_source, policy, upstream_digests: dict) -> str:
    """Return a SHA-256 hex fingerprint of the trace inputs.

    A change to the claim atom, the depth-0 source identity, the policy, or any
    upstream digest changes the fingerprint (dependency-valid caching).
    """
    _require(isinstance(root_source, dict), "root_source must be a JSON object")
    _require(isinstance(upstream_digests, dict), "upstream_digests must be a JSON object")
    payload = {
        "atom": atom,
        "root_source_key": _source_key(root_source),
        "root_source_sha256": _source_sha(root_source),
        "policy": policy,
        "upstream_digests": upstream_digests,
    }
    return _digest(payload)


__all__ = [
    "ATTRIBUTIONS",
    "POLICY_FIELDS",
    "TracingError",
    "assess_support",
    "classify_attribution",
    "trace_atom",
    "trace_claim",
    "trace_fingerprint",
    "validate_policy",
]
