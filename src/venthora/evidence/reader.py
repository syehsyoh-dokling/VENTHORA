"""Read the inbox, propose receivable updates, and let Verity decide what is believed.

The language model only *reads*: for each expected receipt it says whether an email reports a
delay, how many days, and quotes the sentence. It never sets an amount, a limit or a decision.
Code then:

1. finds the quoted sentence in the original document (exact, then whitespace/case-normalised),
2. runs the original Verity validator (`originals/verity_core/validators.check_citations`): the
   document must exist, predate the as-of embargo, and the cited span must carry the claim,
3. requires the delay figure itself to appear in the cited span ("5", "five", "a week", ...).

Only a verified delay updates the forecast. Unverified *bad* news is not ignored: it drops the
receipt below the reliance threshold and is flagged for review, because over-trusting a receipt
is the costly mistake. A model's output can never make a receipt earlier or more certain.
If no model is configured or it fails, a deterministic lexicon reader takes over (the same
fallback pattern as Horizon's news reader).
"""
from __future__ import annotations

import datetime as dt
import re
from time import perf_counter
from typing import Literal

from pydantic import Field

from ..domain.models import Evidence, StageResult, StrictModel, TreasuryState
from ..originals.verity_core import corpus as vcorpus
from ..originals.verity_core import retriever as vretriever
from ..originals.verity_core import validators as vvalidators
from .llm import ChatModel, LLMUnavailable, first_json_object

DELAYED_CONFIDENCE_BP = 4_000  # a verified delay: the receipt is no longer relied upon
UNVERIFIED_SIGNAL_BP = 6_000  # bad news Verity could not verify: below threshold, flagged for review
MAX_DELAY_DAYS = 90

NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())} | {"thirty": 30, "a": 1, "an": 1}
DELAY_TERMS = re.compile(
    r"\b(delay\w*|postpon\w*|push\w* back|pushed|moved|slip\w*|late|later|resched\w*|defer\w*)\b", re.I)
AMOUNT_UNIT = re.compile(
    r"\b(\d{1,3}|" + "|".join(NUMBER_WORDS) + r")[\s-]+(business\s+|working\s+|calendar\s+)?(day|week)s?\b", re.I)


class InboxMessage(StrictModel):
    doc_id: str = Field(min_length=1, max_length=120)
    date: str = Field(description="ISO date the message was received")
    sender: str = Field(min_length=1, max_length=200)
    subject: str = Field(default="", max_length=300)
    text: str = Field(min_length=1, max_length=20_000)


class ReceivableUpdate(StrictModel):
    receivable_id: str
    payer: str
    verified: bool
    delay_days: int | None
    previous_expected_in_hours: int
    new_expected_in_hours: int
    previous_confidence_bp: int
    new_confidence_bp: int
    doc_id: str
    span_start: int | None
    span_end: int | None
    quote: str
    reader: Literal["llm", "lexicon"]
    problems: list[str] = Field(default_factory=list)


class EvidenceReading(StrictModel):
    state: TreasuryState
    updates: list[ReceivableUpdate]
    stage: StageResult


def _days_in(text: str) -> list[int]:
    out = []
    for m in AMOUNT_UNIT.finditer(text):
        raw = m.group(1).lower()
        n = int(raw) if raw.isdigit() else NUMBER_WORDS[raw]
        out.append(n * (7 if m.group(3).lower() == "week" else 1))
    return out


def _locate(quote: str, doc: vcorpus.Document) -> tuple[int, int] | None:
    quote = quote.strip().strip('"').strip()
    if not quote:
        return None
    at = doc.text.find(quote)
    if at >= 0:
        return at, at + len(quote)
    # tolerate whitespace and case differences, never different words
    pattern = r"\s+".join(re.escape(w) for w in quote.split())
    m = re.search(pattern, doc.text, re.I)
    return (m.start(), m.end()) if m else None


def _lexicon(passages) -> dict | None:
    for doc, _start, _end, body, _score in passages:
        if DELAY_TERMS.search(body):
            days = _days_in(body)
            return {"delayed": True, "delay_days": max(days) if days else None, "doc_id": doc.doc_id,
                    "quote": body.strip(), "claim": body.strip()}
    return None


async def _ask(model: ChatModel, receivable, passages) -> dict | None:
    lines = "\n".join(f'[{d.doc_id}] ({d.doc_date}) "{body.strip()}"' for d, _s, _e, body, _sc in passages)
    prompt = (
        f"A customer, {receivable.payer}, owes us money that we expect in {receivable.expected_in_hours} hours.\n"
        f"Email passages:\n{lines}\n\n"
        "Does a passage say this payment will arrive later than expected? Reply with JSON only:\n"
        '{"delayed": true or false, "delay_days": integer or null, "doc_id": "id in brackets", '
        '"quote": "the exact sentence copied from the passage", "claim": "one sentence restating the quote"}')
    reply = await model.complete([{"role": "system", "content": "You extract facts from emails. Reply with JSON only."},
                                  {"role": "user", "content": prompt}], max_tokens=200)
    data = first_json_object(reply)
    if data.get("delayed") not in (True, "true", "yes"):
        return None
    # Tolerate format slips of small models; the span check below still decides what is true.
    data["doc_id"] = str(data.get("doc_id", "")).strip().strip("[]\"' ")
    days = data.get("delay_days")
    if isinstance(days, str):
        found = _days_in(days) or ([int(days)] if days.strip().isdigit() else [])
        days = found[0] if found else None
    if days is None:
        span_days = _days_in(str(data.get("quote", "")))  # the model quoted a figure but did not extract it
        days = span_days[0] if len(span_days) == 1 else None
    data["delay_days"] = days
    return data


def _verify(proposal: dict, by_id: dict, cutoff: dt.date) -> tuple[list[str], tuple[int, int] | None, int | None]:
    problems: list[str] = []
    doc = by_id.get(str(proposal.get("doc_id", "")))
    if doc is None:
        return [f"cites unknown document {proposal.get('doc_id')!r}"], None, None
    span = _locate(str(proposal.get("quote", "")), doc)
    if span is None:
        return ["quoted sentence does not occur in the document"], None, None
    # Verity checks the citation (document exists, predates the embargo, span resolves) on the
    # located sentence. Verity's term-overlap test is not punctuation- or stem-aware ("delay" vs
    # "delayed."), so the extracted *fact* is checked literally below instead of via the model's
    # paraphrase.
    citation = {"entity_id": "receivable", "claims": [{"claim": doc.span(*span), "doc_id": doc.doc_id,
                                                       "span_start": span[0], "span_end": span[1]}]}
    problems += [str(p) for p in vvalidators.check_citations(citation, by_id, cutoff)]
    days = proposal.get("delay_days")
    if not isinstance(days, int) or not 0 < days <= MAX_DELAY_DAYS:
        problems.append(f"delay_days {days!r} is not a whole number of days in 1..{MAX_DELAY_DAYS}")
        days = None
    elif days not in _days_in(doc.span(*span)):
        problems.append(f"the cited span does not state a {days}-day delay")
    if not DELAY_TERMS.search(doc.span(*span)):
        problems.append("the cited span does not describe a delay")
    return problems, span, days


async def read_inbox(state: TreasuryState, messages: list[InboxMessage], *, as_of: dt.date,
                     model: ChatModel | None = None) -> EvidenceReading:
    started = perf_counter()
    model = model if model is not None else ChatModel()
    docs = [vcorpus.Document(doc_id=m.doc_id, doc_date=dt.date.fromisoformat(m.date[:10]), ticker=None,
                             title=f"{m.subject} from {m.sender}", text=m.text) for m in messages]
    admissible = [d for d in docs if d.doc_date is None or d.doc_date <= as_of]
    by_id = {d.doc_id: d for d in docs}
    index = vretriever.Index(admissible) if admissible else None
    receivables = {r.receivable_id: r for r in state.receivables}
    evidence = list(state.evidence)
    updates: list[ReceivableUpdate] = []
    notes: list[str] = []

    for r in state.receivables:
        if index is None:
            break
        terms = tuple(w for w in re.findall(r"[A-Za-z]{3,}", r.payer)[:2])
        passages = index.search(f"{r.payer} payment transfer remittance delay date", entity_terms=terms, top_k=3)
        if not passages:
            continue
        reader: Literal["llm", "lexicon"] = "llm"
        proposal = None
        if model.configured:
            try:
                proposal = await _ask(model, r, passages)
            except (LLMUnavailable, ValueError) as exc:
                notes.append(f"model unavailable for {r.receivable_id} ({type(exc).__name__}); lexicon fallback")
                reader, proposal = "lexicon", _lexicon(passages)
        else:
            reader, proposal = "lexicon", _lexicon(passages)
        if proposal is None:
            continue

        problems, span, days = _verify(proposal, by_id, as_of)
        verified = not problems
        if verified:
            new_hours = min(r.expected_in_hours + days * 24, 24 * 365)
            new_conf = min(r.confidence_bp, DELAYED_CONFIDENCE_BP)
        else:  # unverified bad news: stop relying on the receipt, never move it earlier
            new_hours, new_conf = r.expected_in_hours, min(r.confidence_bp, UNVERIFIED_SIGNAL_BP)
        quote = by_id[proposal["doc_id"]].span(*span) if span else str(proposal.get("quote", ""))[:300]
        updates.append(ReceivableUpdate(
            receivable_id=r.receivable_id, payer=r.payer, verified=verified, delay_days=days,
            previous_expected_in_hours=r.expected_in_hours, new_expected_in_hours=new_hours,
            previous_confidence_bp=r.confidence_bp, new_confidence_bp=new_conf, doc_id=str(proposal.get("doc_id")),
            span_start=span[0] if span else None, span_end=span[1] if span else None, quote=quote,
            reader=reader, problems=problems))
        ref = f"inbox:{proposal.get('doc_id')}"
        if verified and ref not in {e.ref for e in evidence}:
            sender = next((m.sender for m in messages if m.doc_id == proposal["doc_id"]), "unknown")
            evidence.append(Evidence(ref=ref, subject_id=r.receivable_id, source=f"email:{sender}", status="verified",
                                     summary=quote[:300]))
        receivables[r.receivable_id] = r.model_copy(update={
            "expected_in_hours": new_hours, "confidence_bp": new_conf,
            "evidence_ref": ref if verified else r.evidence_ref})

    new_state = TreasuryState.model_validate(state.model_dump() | {
        "receivables": [receivables[r.receivable_id].model_dump() for r in state.receivables],
        "evidence": [e.model_dump() for e in evidence]})
    status = "WARN" if any(not u.verified for u in updates) else "PASS"
    summary = (f"{sum(u.verified for u in updates)} verified, {sum(not u.verified for u in updates)} unverified "
               f"receivable updates from {len(admissible)}/{len(docs)} admissible messages")
    return EvidenceReading(state=new_state, updates=updates, stage=StageResult(
        engine="Verity · inbox", status=status, summary=summary,
        details={"model": model.model if model.configured else None, "embargoed": [d.doc_id for d in docs
                 if d not in admissible], "notes": notes, "updates": [u.model_dump() for u in updates]},
        duration_ms=(perf_counter() - started) * 1000))
