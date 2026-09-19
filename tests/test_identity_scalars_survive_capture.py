"""Public API contract; production-derived narrative omitted."""
import json

import pytest

from exitmgr.trade_capture import _as_dict


@pytest.mark.parametrize("value", [
    "deepseek-v4-flash-0731",
    "gpt-oss-120b",
    "",
])
def test_a_string_identity_survives_verbatim(value):
    assert _as_dict(value) == value, (
        "a config-sourced identity is a plain string; wrapping it in {'repr': ...} mangles the "
        "one field that records which model made the decision")


@pytest.mark.parametrize("value", [7, 0, -3, 3.5, 0.0, True, False])
def test_other_scalars_survive_verbatim(value):
    got = _as_dict(value)
    assert got == value and type(got) is type(value), (
        "%r came back as %r -- a scalar is already JSON-safe and must not be repr'd" % (value, got))


def test_none_is_still_none():
    assert _as_dict(None) is None


def test_a_dict_is_unchanged():
    d = {"name": "m", "source": "config"}
    assert _as_dict(d) == d


def test_a_dataclass_still_becomes_a_dict():
    from dataclasses import dataclass

    @dataclass
    class Ident:
        name: str
        source: str

    assert _as_dict(Ident("m", "meta")) == {"name": "m", "source": "meta"}


def test_a_genuinely_unrepresentable_object_still_falls_back():
    """Public API contract; production-derived narrative omitted."""
    class Opaque:
        __slots__ = ()

    out = _as_dict(Opaque())
    assert isinstance(out, dict) and "repr" in out


def test_the_captured_row_is_still_json_serialisable():
    for v in ("m", 7, True, None, {"a": 1}):
        json.dumps({"model_identity": _as_dict(v)})


def test_a_quoted_repr_never_appears_for_a_name():
    """Public API contract; production-derived narrative omitted."""
    out = _as_dict("deepseek-v4-flash-0731")
    assert not (isinstance(out, dict) and str(out.get("repr", "")).startswith("'")), (
        "identity captured as a quoted repr -- this is the corpus corruption")
