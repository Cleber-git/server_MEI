from decimal import Decimal

import pytest

from fiscal.domain import (
    FiscalStatus,
    assert_transition,
    canonical_hash,
    fiscal_decimal,
    is_complete_authorization,
)
from fiscal.errors import ApiProblem


def test_canonicalization_is_order_independent():
    assert canonical_hash({"b": 2, "a": "1.00"}) == canonical_hash(
        {"a": "1.00", "b": 2}
    )


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "1e2", "01.00", "-1.00", 1.0, "1.00000"]
)
def test_decimal_rejects_unsafe_formats(value):
    with pytest.raises(ValueError):
        fiscal_decimal(value)


def test_decimal_keeps_exact_value():
    assert fiscal_decimal("10.00") == Decimal("10.00")


def test_state_machine_accepts_only_declared_transition():
    assert_transition(FiscalStatus.DRAFT, FiscalStatus.VALIDATING)
    with pytest.raises(ApiProblem):
        assert_transition(FiscalStatus.DRAFT, FiscalStatus.AUTHORIZED)


def test_authorized_requires_official_xml_evidence():
    base = {
        "environment": "RESTRICTED_PRODUCTION",
        "documentType": "NFSE",
        "series": "1",
        "number": 1,
        "officialKey": "key",
        "protocol": "protocol",
        "authorizedAt": "2026-01-01T00:00:00Z",
        "xmlArtifactId": "id",
    }
    assert not is_complete_authorization(base)
    base["xmlSha256"] = "a" * 64
    assert is_complete_authorization(base)


def test_transitions_mirror_the_app_shared_state_machine():
    """Espelho de FiscalRules.transitions (shared): ERROR_FINAL só de VALIDATING/ERROR_RETRYABLE."""
    from fiscal.domain import TRANSITIONS

    finals = {src for src, targets in TRANSITIONS.items() if FiscalStatus.ERROR_FINAL in targets}
    assert finals == {FiscalStatus.VALIDATING, FiscalStatus.ERROR_RETRYABLE}
    assert TRANSITIONS[FiscalStatus.QUEUED] == {
        FiscalStatus.SUBMITTED,
        FiscalStatus.ERROR_RETRYABLE,
    }
    assert TRANSITIONS[FiscalStatus.AUTHORIZED] == {FiscalStatus.CANCEL_PENDING}
    assert FiscalStatus.REJECTED not in TRANSITIONS
    assert FiscalStatus.CANCELLED not in TRANSITIONS
