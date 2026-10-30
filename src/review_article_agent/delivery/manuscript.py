"""Pure renderer: reviewed section drafts -> a delivery manuscript (MVP).

The Step 14 delivery export needs an **exportable** manuscript whose citations
the Pandoc-citation reader (:func:`review_article_agent.delivery.export.cited_keys`)
recognises.  The drafting *assembly* Markdown is deliberately provisional and
labels its citations with bare ``cit_*`` markers (never ``[@...]``), so it cannot
be the export manuscript.  This module closes that seam by rendering the
manuscript from the **post-review** section drafts instead: the final draft of
each section's durable review state
(:meth:`review_article_agent.review.workflow.SectionReviewRepository.read`),
which already carries each sentence's exact reviewed text and its
verified-corpus ``citation_keys``.

Each sentence's citation keys are rendered as Pandoc citations ``[@<key>]`` so
:func:`~review_article_agent.delivery.export.cited_keys` (and the consistency
check) see exactly the keys the export bibliography is built from.  The returned
paragraph mapping matches the delivery paragraph model
(``{paragraph_id: {"text", "checks"}}``) that
:meth:`~review_article_agent.delivery.workflow.DeliveryRepository.revalidate`
and the notes planner consume.

Pure: plain ``dict``-in/``dict``-out with no storage, network, or model call.
"""

from __future__ import annotations

#: The generic document-envelope type of a durable delivery-manuscript revision.
MANUSCRIPT_DOCUMENT_TYPE = "delivery_manuscript"

#: The initial per-paragraph check state: the section review already passed, so
#: each reviewed paragraph starts ``valid`` (a later notes edit marks it
#: ``stale`` until revalidated).
_INITIAL_CHECKS = "valid"


def _render_sentence(sentence) -> str:
    """Render one reviewed sentence with its Pandoc citations.

    The sentence's exact reviewed text is kept; each verified-corpus citation key
    is appended as ``[@<key>]`` (multiple keys as ``[@a; @b]``), the form the
    export Pandoc-citation reader recognises.
    """
    text = sentence.get("text", "")
    if not isinstance(text, str):
        text = ""
    keys = [key for key in (sentence.get("citation_keys") or []) if key]
    if keys:
        text = text + " [" + "; ".join("@" + str(key) for key in keys) + "]"
    return text


def _render_paragraph(paragraph) -> str:
    """Join one paragraph's rendered sentences with a single space."""
    return " ".join(
        rendered
        for rendered in (
            _render_sentence(sentence)
            for sentence in (paragraph.get("sentences") or [])
        )
        if rendered
    )


def build_manuscript(sections) -> dict:
    """Render reviewed section drafts into a delivery manuscript.

    ``sections`` is an ordered list of ``{"section_id", "title"?, "review_state"}``
    entries (outline section order, supplied by the caller).  Each
    ``review_state`` is the payload of a durable section-review revision whose
    final ``draft`` carries the post-review paragraphs/sentences.  Returns::

        {"text": <Markdown>,
         "paragraphs": {<paragraph_id>: {"text", "checks"}},
         "sections": [{"section_id", "paragraphs": [{"paragraph_id", "text"}]}]}

    Paragraph ids are ``"<section_id>:<draft paragraph_id>"`` so ids stay unique
    across sections while remaining stable for the same reviewed draft.
    """
    lines: list = []
    paragraphs: dict = {}
    rendered_sections: list = []
    for section in sections or []:
        section_id = section.get("section_id")
        title = section.get("title") or section_id or ""
        review_state = section.get("review_state") or {}
        draft = review_state.get("draft") or {}
        if section_id:
            lines.append(f"## {title}")
            lines.append("")
        section_paragraphs: list = []
        for index, paragraph in enumerate(draft.get("paragraphs") or []):
            draft_id = paragraph.get("paragraph_id")
            paragraph_id = f"{section_id}:{draft_id}"
            text = _render_paragraph(paragraph)
            paragraphs[paragraph_id] = {"text": text, "checks": _INITIAL_CHECKS}
            lines.append(text)
            lines.append("")
            section_paragraphs.append(
                {"paragraph_id": paragraph_id, "text": text}
            )
        rendered_sections.append(
            {"section_id": section_id, "paragraphs": section_paragraphs}
        )
    text = "\n".join(lines).rstrip("\n") + "\n"
    return {"text": text, "paragraphs": paragraphs, "sections": rendered_sections}


__all__ = [
    "MANUSCRIPT_DOCUMENT_TYPE",
    "build_manuscript",
]
