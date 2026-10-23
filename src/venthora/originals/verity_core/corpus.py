"""Load the frozen evidence corpus, embargoed.

The embargo is the whole trust proposition of Track 4. A claim cited to a
document dated after the task's cutoff is not weak evidence, it is a look at the
answer, and the faithfulness gate treats it as such. So documents past the
cutoff are dropped at load time rather than filtered later - code that never
sees them cannot cite them.

Spans are byte-ish offsets into `text`. A citation whose span does not resolve
to the text it claims is unfaithful even when the underlying belief is right, so
the loader keeps the raw text and every span is checked against it before the
answer is written.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Document:
    doc_id: str
    doc_date: dt.date | None
    ticker: str | None
    title: str
    text: str
    #: Entity labels from the unit manifest (scorer 5.2.x): a claim may cite this document only for
    #: an entity listed here, or when it is `shared`. None = the manifest labels nothing (admit all).
    entity_ids: tuple[str, ...] | None = None
    shared: bool = False

    def span(self, start: int, end: int) -> str:
        return self.text[start:end]

    def admits(self, entity_id: str) -> bool:
        """Whether a claim about `entity_id` may cite this document (scorer 5.2.x: a claim citing
        a document not labelled for its entity is false - 1 Oct, up to 41 of 48 claims a unit)."""
        if self.entity_ids is None or self.shared:
            return True
        return str(entity_id) in self.entity_ids


def _manifest_labels(corpus_dir: pathlib.Path) -> dict[str, tuple[tuple[str, ...] | None, bool]]:
    """file stem -> (entity_ids or None, shared) from the unit manifest(s) next to the corpus."""
    labels: dict[str, tuple[tuple[str, ...] | None, bool]] = {}
    for manifest in (corpus_dir / "manifest.json", corpus_dir.parent / "manifest.json"):
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in raw.get("files", []) if isinstance(raw, dict) else []:
            if not isinstance(entry, dict) or not entry.get("path"):
                continue
            ids = entry.get("entity_ids")
            shared = entry.get("shared") is True
            if ids is None and not shared:
                continue
            labels[pathlib.PurePosixPath(str(entry["path"])).stem] = (
                tuple(str(x) for x in ids) if isinstance(ids, list) else (), shared)
    return labels


def _parse_date(value) -> dt.date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def load(corpus_dir: pathlib.Path, cutoff: str) -> tuple[list[Document], list[str]]:
    """Returns (admissible documents, embargoed doc_ids)."""
    limit = _parse_date(cutoff)
    documents: list[Document] = []
    embargoed: list[str] = []
    labels = _manifest_labels(corpus_dir)

    for path in sorted(corpus_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(raw, dict) or "text" not in raw:
            continue
        # the citable doc_id is the manifest-declared filename stem (AGENTS.md), not the inner field
        doc_id = path.stem
        date = _parse_date(raw.get("doc_date") or raw.get("filed_date"))
        if limit is not None and date is not None and date > limit:
            embargoed.append(doc_id)
            continue
        documents.append(Document(
            doc_id=doc_id,
            doc_date=date,
            ticker=raw.get("ticker"),
            title=str(raw.get("title") or ""),
            text=str(raw["text"]),
            # A labelled manifest leaves an unlisted document admissible for nobody: () not None.
            entity_ids=(labels.get(path.stem, ((), False))[0] if labels else None),
            shared=(labels.get(path.stem, ((), False))[1] if labels else False),
        ))
    return documents, embargoed


SENTENCE = re.compile(r"[^.!?]+[.!?]")


def sentences(document: Document, min_chars: int = 60) -> list[tuple[int, int, str]]:
    """(start, end, text) for each sentence, with offsets into the raw text.

    Offsets come from the match, never from re-finding the string afterwards:
    a sentence that occurs twice would otherwise be cited at the wrong place and
    fail span verification for a reason that looks like a retrieval bug.
    """
    out = []
    for match in SENTENCE.finditer(document.text):
        raw_match = match.group()
        body = raw_match.strip()
        if len(body) >= min_chars:
            lead = len(raw_match) - len(raw_match.lstrip())
            s = match.start() + lead
            e = s + len(body)
            out.append((s, e, body))
    if not out and document.text.strip():
        raw_text = document.text
        stripped = raw_text.strip()
        lead = len(raw_text) - len(raw_text.lstrip())
        s = lead
        body = stripped[:600].strip()
        e = s + len(body)
        out.append((s, e, body))
    return out
