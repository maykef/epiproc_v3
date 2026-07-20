"""slug_supplier — the supplier-key derivation ported from v1.

The slug is the join key between a freshly-extracted seller name and the existing
data, so its normalisation (lower-casing, non-alnum → underscore, and the stacked
legal-form suffix stripping) must stay stable: a drift here silently splits one
supplier into two dashboard rows or merges two into one.
"""
from __future__ import annotations

from epiproc.ingest.pipeline import slug_supplier


def test_basic_slugify():
    assert slug_supplier("W. Tuning Bloemenexport") == "w_tuning_bloemenexport"


def test_legal_suffix_is_stripped_so_variants_collapse():
    # "…B.V." and the bare name must map to ONE key, not two suppliers.
    assert slug_supplier("MM Flowers Europe B.V.") == "mm_flowers_europe"
    assert slug_supplier("MM Flowers Europe") == "mm_flowers_europe"


def test_stacked_suffixes_are_stripped():
    # The while-loop peels one suffix per pass: "_ltd" then "_co".
    assert slug_supplier("Acme Trading Co Ltd") == "acme_trading"


def test_single_suffix_forms():
    assert slug_supplier("Nikon GmbH") == "nikon"
    assert slug_supplier("Contoso LLC") == "contoso"


def test_a_name_that_is_only_a_legal_form_is_not_over_stripped():
    # No token precedes the suffix (no leading underscore), so it is kept — we must
    # not slug a supplier literally named "Ltd" down to nothing.
    assert slug_supplier("Ltd") == "ltd"


def test_empty_and_none_yield_none():
    assert slug_supplier(None) is None
    assert slug_supplier("") is None
    assert slug_supplier("   ") is None
