from decimal import Decimal

import pytest

from fiscal.domain import (
    Environment,
    FiscalStatus,
    assert_transition,
    canonical_hash,
    fiscal_decimal,
    is_complete_authorization,
)
from fiscal.errors import ApiProblem
from fiscal.schemas import NfseRequest


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


def test_nfse_recomputes_totals_and_forbids_unknown_fields():
    payload = {
        "schemaVersion": "1",
        "documentType": "NFSE",
        "environment": Environment.RESTRICTED_PRODUCTION,
        "recipient": {
            "name": "Maria",
            "taxId": "52998224725",
            "ibgeCityCode": "3550308",
            "uf": "SP",
        },
        "items": [
            {
                "description": "Serviço",
                "nationalServiceCode": "1.01",
                "municipalTaxCode": "0101",
                "incidenceCityCode": "3550308",
                "serviceCityCode": "3550308",
                "amount": "10.00",
            }
        ],
        "totals": {"items": "10.00", "total": "10.00"},
    }
    assert NfseRequest.model_validate(payload).totals.total == "10.00"
    payload["totals"]["total"] = "9.99"
    with pytest.raises(ValueError):
        NfseRequest.model_validate(payload)
    payload["totals"]["total"] = "10.00"
    payload["surprise"] = True
    with pytest.raises(ValueError):
        NfseRequest.model_validate(payload)
