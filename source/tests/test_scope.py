"""Tests for geographic source scope."""

from source.scope import SourceScope, parse_scope


def test_parse_scope_from_zip_string():
    scope = parse_scope(state="WI", zip_codes="53202, 53203")
    assert scope.state == "WI"
    assert scope.zip_codes == ["53202", "53203"]


def test_scope_applies_to_zip():
    scope = SourceScope(state="WI", zip_codes=["53202"])
    assert scope.applies_to_zip("53202")
    assert not scope.applies_to_zip("53203")


def test_milwaukee_adapter_supports_wi_scope():
    from source.adapters.milwaukee import MilwaukeeSource

    scope = SourceScope(country="US", state="WI", city="Milwaukee")
    assert MilwaukeeSource().supports_scope(scope)
    assert not MilwaukeeSource().supports_scope(SourceScope(state="IL"))
