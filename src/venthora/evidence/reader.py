"""Read the inbox, propose updates, and let Verity decide what is believed.

The language model only *reads* (Kit guidance: "read unstructured text, compare evidence"). For
each expected receipt it says whether an email reports a delay, how many days, and quotes the
sentence; for each payee it says whether an email announces new bank details. It never sets an
amount, a limit or a decision. Code then, for every claim:

1. finds the quoted sentence in the original document (exact, then whitespace/case-normalised;
   SeismoForge's intake rule: a value the model cannot quote from the source is rejected),
2. runs the original Verity validator (`originals/verity_core/validators.check_citations`): the
   document must exist, predate the as-of embargo, and the cited span must resolve,
3. checks the fact literally in the cited span (the delay figure; bank + change terms).

Forecast calibration (Verity: "calibrates interval width to evidence strength, widens when thin"):
* confidence = Laplace/Beta posterior of the payer keeping promises, counting this delay as a
  miss: (kept + 1) / (kept + missed + 1 + 2);
* a 90% arrival window [new date, new date + spread], spread = 24h x days x (n + 3) / (n + 1),
  widest when the payer has little history;
* unverified bad news halves the calibrated confidence and keeps the old date: a model can never
  make a receipt earlier or more certain.
A payee bank-detail change is a policy exception whether or not it verifies: an unverified signal
is still a fraud signal, so it always goes to a person.
If no model is configured or it fails, a deterministic lexicon reader takes over (Horizon's
fallback pattern).
"""
from __future__ import annotations

import datetime as dt
import re
from time import perf_counter
from typing import Literal

from pydantic import Field

from ..domain.models import Evidence, PayerHistory, StageResult, StrictModel, TreasuryState
from ..originals.verity_core import corpus as vcorpus
from ..originals.verity_core import retriever as vretriever
from ..originals.verity_core import validators as vvalidators
from .llm import ChatModel, LLMUnavailable, first_json_object

MAX_DELAY_DAYS = 90
BANK_CHANGE_FLAG = "payee_bank_details_changed"

NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())} | {"thirty": 30, "a": 1, "an": 1}
DELAY_TERMS = re.compile(
    r"\b(delay\w*|postpon\w*|push\w* back|pushed|moved|slip\w*|late|later|resched\w*|defer\w*)\b", re.I)
AMOUNT_UNIT = re.compile(
    r"\b(\d{1,3}|" + "|".join(NUMBER_WORDS) + r")[\s-]+(business\s+|working\s+|calendar\s+)?(day|week)s?\b", re.I)
BANK_TERMS = re.compile(r"\b(bank\w*|account\w*|iban|sort code|routing|payment details|remit\w* to)\b", re.I)
CHANGE_TERMS = re.compile(r"\b(chang\w*|new|updat\w*|mov\w*|switch\w*|replac\w*|different)\b", re.I)


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
    arrival_window_hours: list[int] | None
    calibration: str
    doc_id: str
    span_start: int | None
    span_end: int | None
    quote: str
    reader: Literal["llm", "lexicon"]
    problems: list[str] = Field(default_factory=list)


class PolicyFlagUpdate(StrictModel):
    obligation_id: str
    payee: str
    flag: str
    verified: bool
    doc_id: str
    span_start: int | None
    span_end: int | None
    quote: str
    reader: Literal["llm", "lexicon"]
    problems: list[str] = Field(default_factory=list)


class EvidenceReading(StrictModel):
    state: TreasuryState
    updates: list[ReceivableUpdate]
    flags: list[PolicyFlagUpdate] = Field(default_factory=list)
    stage: StageResult


# -- calibration ---------------------------------------------------------------------------

def calibrate_delay(expected_hours: int, confidence_bp: int, history: PayerHistory | None,
                    delay_days: int | None, verified: bool, ceiling_bp: int = 7_000) -> tuple[int, int, list[int], str]:
    h = history or PayerHistory()
    kept, missed = h.kept, h.missed + 1  # this delay is itself a missed promise
    posterior = (kept + 1) * 10_000 // (kept + missed + 2)
    n = kept + missed
    if verified and delay_days:
        hours = min(expected_hours + delay_days * 24, 24 * 365)
        spread = 24 * max(1, delay_days) * (n + 3) // (n + 1)
        # Kit 01: a contradicting customer email must lower the autonomous limit, whatever the
        # payer's record. A contradicted forecast is capped below the fully-supported band.
        conf = min(confidence_bp, posterior, ceiling_bp)
        why = (f"payer kept {kept} of {kept + missed} promises incl. this delay -> posterior {posterior / 100:.0f}%, "
               f"capped at {ceiling_bp / 100:.0f}% because the payer contradicted the forecast; "
               f"90% window widens to {spread}h with {n} past promises")
        return hours, conf, [hours, hours + spread], why
    conf = min(confidence_bp, posterior // 2)
    spread = 24 * 14 * (n + 3) // (n + 1)
    why = (f"unverified delay signal: half of the {posterior / 100:.0f}% posterior; date not moved earlier, "
           f"window widened by {spread}h")
    return expected_hours, conf, [expected_hours, expected_hours + spread], why


# -- reading helpers -----------------------------------------------------------------------

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


def _lexicon_delay(passages) -> dict | None:
    for doc, _start, _end, body, _score in passages:
        if DELAY_TERMS.search(body):
            days = _days_in(body)
            return {"delayed": True, "delay_days": max(days) if days else None, "doc_id": doc.doc_id,
                    "quote": body.strip()}
    return None


def _lexicon_bank(passages) -> dict | None:
    for doc, _start, _end, body, _score in passages:
        if BANK_TERMS.search(body) and CHANGE_TERMS.search(body):
            return {"changed": True, "doc_id": doc.doc_id, "quote": body.strip()}
    return None


def _clean(data: dict) -> dict:
    data["doc_id"] = str(data.get("doc_id", "")).strip().strip("[]\"' ")
    return data


async def _ask_delay(model: ChatModel, receivable, passages) -> dict | None:
    lines = "\n".join(f'[{d.doc_id}] ({d.doc_date}) "{body.strip()}"' for d, _s, _e, body, _sc in passages)
    prompt = (
        f"A customer, {receivable.payer}, owes us money that we expect in {receivable.expected_in_hours} hours.\n"
        f"Email passages:\n{lines}\n\n"
        "Does a passage say this payment will arrive later than expected? Reply with JSON only:\n"
        '{"delayed": true or false, "delay_days": integer or null, "doc_id": "id in brackets", '
        '"quote": "the exact sentence copied from the passage"}')
    reply = await model.complete([{"role": "system", "content": "You extract facts from emails. Reply with JSON only."},
                                  {"role": "user", "content": prompt}], max_tokens=200)
    data = first_json_object(reply)
    if data.get("delayed") not in (True, "true", "yes"):
        return None
    _clean(data)
    days = data.get("delay_days")
    if isinstance(days, str):  # tolerate format slips of small models; the span check still decides
        found = _days_in(days) or ([int(days)] if days.strip().isdigit() else [])
        days = found[0] if found else None
    if days is None:
        span_days = _days_in(str(data.get("quote", "")))
        days = span_days[0] if len(span_days) == 1 else None
    data["delay_days"] = days
    return data


async def _ask_bank(model: ChatModel, payee: str, passages) -> dict | None:
    lines = "\n".join(f'[{d.doc_id}] ({d.doc_date}) "{body.strip()}"' for d, _s, _e, body, _sc in passages)
    prompt = (f"We are about to pay {payee}.\nEmail passages:\n{lines}\n\n"
              "Does a passage say this payee changed its bank account or payment details? Reply with JSON only:\n"
              '{"changed": true or false, "doc_id": "id in brackets", "quote": "the exact sentence copied"}')
    reply = await model.complete([{"role": "system", "content": "You extract facts from emails. Reply with JSON only."},
                                  {"role": "user", "content": prompt}], max_tokens=150)
    data = first_json_object(reply)
    return _clean(data) if data.get("changed") in (True, "true", "yes") else None


def _cite(proposal: dict, by_id: dict, cutoff: dt.date) -> tuple[list[str], tuple[int, int] | None, vcorpus.Document | None]:
    """Locate the quote and run the original Verity citation check on it."""
    doc = by_id.get(str(proposal.get("doc_id", "")))
    if doc is None:
        return [f"cites unknown document {proposal.get('doc_id')!r}"], None, None
    span = _locate(str(proposal.get("quote", "")), doc)
    if span is None:
        return ["quoted sentence does not occur in the document"], None, doc
    # Verity's term-overlap test is not punctuation- or stem-aware, so it runs on the located
    # sentence (existence, embargo, span); the extracted fact is checked literally by the caller.
    citation = {"entity_id": "claim", "claims": [{"claim": doc.span(*span), "doc_id": doc.doc_id,
                                                  "span_start": span[0], "span_end": span[1]}]}
    return [str(p) for p in vvalidators.check_citations(citation, by_id, cutoff)], span, doc


def _verify_delay(proposal: dict, by_id: dict, cutoff: dt.date):
    problems, span, doc = _cite(proposal, by_id, cutoff)
    days = proposal.get("delay_days")
    if span is None:
        return problems, None, None
    if not isinstance(days, int) or not 0 < days <= MAX_DELAY_DAYS:
        problems.append(f"delay_days {days!r} is not a whole number of days in 1..{MAX_DELAY_DAYS}")
        days = None
    elif days not in _days_in(doc.span(*span)):
        problems.append(f"the cited span does not state a {days}-day delay")
    if not DELAY_TERMS.search(doc.span(*span)):
        problems.append("the cited span does not describe a delay")
    return problems, span, days


def _verify_bank(proposal: dict, by_id: dict, cutoff: dt.date):
    problems, span, doc = _cite(proposal, by_id, cutoff)
    if span is not None:
        text = doc.span(*span)
        if not BANK_TERMS.search(text) or not CHANGE_TERMS.search(text):
            problems.append("the cited span does not announce changed bank details")
    return problems, span


async def _model_claim(model, ask, notes, label):
    if not model.configured:
        return None
    try:
        return await ask()
    except (LLMUnavailable, ValueError) as exc:
        notes.append(f"model unavailable for {label} ({type(exc).__name__}); lexicon fallback")
        return None


# -- main entry ----------------------------------------------------------------------------

async def read_inbox(state: TreasuryState, messages: list[InboxMessage], *, as_of: dt.date,
                     model: ChatModel | None = None) -> EvidenceReading:
    started = perf_counter()
    model = model if model is not None else ChatModel()
    docs = [vcorpus.Document(doc_id=m.doc_id, doc_date=dt.date.fromisoformat(m.date[:10]), ticker=None,
                             title=f"{m.subject} from {m.sender}", text=m.text) for m in messages]
    admissible = [d for d in docs if d.doc_date is None or d.doc_date <= as_of]
    by_id = {d.doc_id: d for d in docs}
    sender = {m.doc_id: m.sender for m in messages}
    index = vretriever.Index(admissible) if admissible else None
    data = state.model_dump()
    evidence = data["evidence"]
    updates: list[ReceivableUpdate] = []
    flags: list[PolicyFlagUpdate] = []
    notes: list[str] = []
    rejected_claims: list[dict] = []

    def terms_for(name: str) -> tuple[str, ...]:
        return tuple(w for w in re.findall(r"[A-Za-z]{3,}", name)[:2])

    def about(passages, name: str):
        # Verity's retriever falls back to other entities' passages when none match; a claim about
        # a payee or payer must come from a passage that names it.
        terms = [t.lower() for t in terms_for(name)]
        return [p for p in passages if any(t in (p[3] + " " + p[0].title).lower() for t in terms)]

    def add_evidence(ref: str, subject: str, doc_id: str, quote: str) -> None:
        if ref not in {e["ref"] for e in evidence}:
            evidence.append(Evidence(ref=ref, subject_id=subject, source=f"email:{sender.get(doc_id, 'unknown')}",
                                     status="verified", summary=quote[:300]).model_dump())

    for i, r in enumerate(state.receivables):
        if index is None:
            break
        passages = about(index.search(f"{r.payer} payment transfer remittance delay date",
                                      entity_terms=terms_for(r.payer), top_k=3), r.payer)
        if not passages:
            continue
        # 1) the model proposes; 2) if its claim does not verify, the deterministic reader reads the
        # same passages; 3) a claim no reader can ground in the email has no effect at all.
        attempts = []
        llm = await _model_claim(model, lambda r=r, p=passages: _ask_delay(model, r, p), notes, r.receivable_id)
        if llm:
            attempts.append(("llm", llm, *_verify_delay(llm, by_id, as_of)))
        if not attempts or attempts[-1][2]:
            lex = _lexicon_delay(passages)
            if lex:
                attempts.append(("lexicon", lex, *_verify_delay(lex, by_id, as_of)))
        good = next((a for a in attempts if not a[2]), None)
        # Unverified bad news counts only when grounded in real text: a located sentence that says
        # "delay" but whose figure could not be confirmed. A hallucinated claim is discarded.
        grounded = next((a for a in attempts if a[3] is not None
                         and DELAY_TERMS.search(by_id[a[1]["doc_id"]].span(*a[3]))), None)
        chosen = good or grounded
        rejected_claims.extend({"subject": r.receivable_id, "reader": a[0], "doc_id": a[1].get("doc_id"),
                                "quote": str(a[1].get("quote", ""))[:200], "problems": a[2]}
                               for a in attempts if a is not chosen and a[2])
        if chosen is None:
            continue
        reader, proposal, problems, span, days = chosen
        verified = not problems
        hours, conf, window, why = calibrate_delay(r.expected_in_hours, r.confidence_bp, r.payer_history,
                                                   days if verified else None, verified,
                                                   ceiling_bp=state.policy.contradicted_confidence_ceiling_bp)
        quote = by_id[proposal["doc_id"]].span(*span)
        updates.append(ReceivableUpdate(
            receivable_id=r.receivable_id, payer=r.payer, verified=verified, delay_days=days,
            previous_expected_in_hours=r.expected_in_hours, new_expected_in_hours=hours,
            previous_confidence_bp=r.confidence_bp, new_confidence_bp=conf, arrival_window_hours=window,
            calibration=why, doc_id=proposal["doc_id"], span_start=span[0], span_end=span[1], quote=quote,
            reader=reader, problems=problems))
        ref = f"inbox:{proposal['doc_id']}"
        if verified:
            add_evidence(ref, r.receivable_id, proposal["doc_id"], quote)
        data["receivables"][i].update(expected_in_hours=hours, confidence_bp=conf, arrival_window_hours=window,
                                      evidence_ref=ref if verified else r.evidence_ref)

    for i, o in enumerate(state.obligations):
        if index is None or not o.payee_name or o.record_kind != "payable":
            continue
        passages = about(index.search(f"{o.payee_name} bank account payment details changed new",
                                      entity_terms=terms_for(o.payee_name), top_k=3), o.payee_name)
        if not passages:
            continue
        attempts = []
        llm = await _model_claim(model, lambda o=o, p=passages: _ask_bank(model, o.payee_name, p), notes, o.obligation_id)
        if llm:
            attempts.append(("llm", llm, *_verify_bank(llm, by_id, as_of)))
        if not attempts or attempts[-1][2]:
            lex = _lexicon_bank(passages)
            if lex:
                attempts.append(("lexicon", lex, *_verify_bank(lex, by_id, as_of)))
        good = next((a for a in attempts if not a[2]), None)
        rejected_claims.extend({"subject": o.obligation_id, "reader": a[0], "doc_id": a[1].get("doc_id"),
                                "quote": str(a[1].get("quote", ""))[:200], "problems": a[2]}
                               for a in attempts if a is not good and a[2])
        if good is None:
            continue  # only a bank-detail change the email actually states is a policy exception
        reader, proposal, _problems, span = good
        quote = by_id[proposal["doc_id"]].span(*span)
        flags.append(PolicyFlagUpdate(obligation_id=o.obligation_id, payee=o.payee_name, flag=BANK_CHANGE_FLAG,
                                      verified=True, doc_id=proposal["doc_id"], span_start=span[0], span_end=span[1],
                                      quote=quote, reader=reader))
        add_evidence(f"inbox:{proposal['doc_id']}:{o.obligation_id}", o.obligation_id, proposal["doc_id"], quote)
        if BANK_CHANGE_FLAG not in data["obligations"][i]["policy_flags"]:
            data["obligations"][i]["policy_flags"].append(BANK_CHANGE_FLAG)

    new_state = TreasuryState.model_validate(data)
    unverified = sum(not u.verified for u in updates) + sum(not f.verified for f in flags)
    summary = (f"{sum(u.verified for u in updates)} verified / {sum(not u.verified for u in updates)} unverified receipt "
               f"updates, {len(flags)} policy exception(s), {len(rejected_claims)} ungrounded claim(s) rejected, "
               f"from {len(admissible)}/{len(docs)} admissible messages")
    return EvidenceReading(state=new_state, updates=updates, flags=flags, stage=StageResult(
        engine="Verity · inbox", status="WARN" if unverified else "PASS", summary=summary,
        details={"model": model.model if model.configured else None,
                 "embargoed": [d.doc_id for d in docs if d not in admissible], "notes": notes,
                 "updates": [u.model_dump() for u in updates], "flags": [f.model_dump() for f in flags],
                 "rejected_claims": rejected_claims},
        duration_ms=(perf_counter() - started) * 1000))
