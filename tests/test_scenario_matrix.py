"""The scenario matrix runs in CI: every variant must satisfy every invariant."""
import asyncio

import pytest

from venthora.testing.matrix import build_variants, run_variant

VARIANTS = build_variants()


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "matrix")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "matrix")


def test_matrix_has_about_a_hundred_variants_from_extreme_to_normal():
    assert len(VARIANTS) >= 100
    assert {v.severity for v in VARIANTS} == {"extreme", "high", "moderate", "normal"}
    assert len({v.id for v in VARIANTS}) == len(VARIANTS)


@pytest.mark.parametrize("variant", [v for v in VARIANTS if v.family != "load"], ids=lambda v: v.id)
def test_variant_satisfies_every_invariant(variant, tmp_path):
    rec = asyncio.run(run_variant(variant, tmp_path))
    assert rec["ok"], (rec["invariants"], rec["errors"], rec.get("traceback", "")[-600:])


@pytest.mark.parametrize("variant", [v for v in VARIANTS if v.family == "load"], ids=lambda v: v.id)
def test_load_variants_are_stable(variant, tmp_path):
    rec = asyncio.run(run_variant(variant, tmp_path))
    assert rec["ok"], rec["errors"]
    if rec.get("load"):
        assert rec["load"].get("stable", True), rec["load"]
