"""One reader for an amount a person typed in an email ("1,500", "1.500,00", "Rp 1.500.000", "600.00").

A comma or a point can be the thousands mark or the decimal mark, depending on who wrote it. The rule, the same for
every email that asks for money:
  - both marks present: the one that comes last is the decimal mark;
  - one mark, more than once: thousands ("1.500.000");
  - one mark, once, followed by exactly three digits: thousands ("1,500", "2.500");
  - otherwise it is the decimal mark ("600.00", "1,5").
"""
from __future__ import annotations

import re


def parse_amount(v) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.,]", "", str(v)).strip(".,")
    if not s or not any(ch.isdigit() for ch in s):
        return None
    marks = [ch for ch in s if ch in ".,"]
    if len(set(marks)) == 2:
        dec = marks[-1]
        s = s.replace("," if dec == "." else ".", "").replace(dec, ".")
        if s.count(".") > 1:  # "1,500.00.5": not an amount
            return None
    elif marks:
        mark = marks[0]
        head, _, tail = s.rpartition(mark)
        thousands = len(marks) > 1 or (len(tail) == 3 and 1 <= len(head) <= 3)
        s = s.replace(mark, "" if thousands else ".")
    try:
        return float(s)
    except ValueError:
        return None
