from pathlib import Path

from fiscal.schemas import FiscalCapabilitiesDto, FiscalConfigDto


def test_android_config_contract_has_exact_required_fields():
    value = FiscalConfigDto(
        nfseEnvironment="RESTRICTED_PRODUCTION",
        nfeEnvironment="RESTRICTED_PRODUCTION",
        productionEnabled=False,
        provider=None,
        lastValidatedAt=None,
    ).model_dump(mode="json")
    assert set(value) == {
        "nfseEnvironment",
        "nfeEnvironment",
        "productionEnabled",
        "provider",
        "lastValidatedAt",
    }


def test_capabilities_fail_closed():
    value = FiscalCapabilitiesDto(uf="SP", version="test", reason="not homologated")
    assert value.nfseAutomatic is value.nfce is value.nfe is False


def test_migration_enforces_tenant_and_authorized_completeness():
    sql = Path("migrations/001_fiscal_foundation.sql").read_text(encoding="utf-8")
    assert "UNIQUE (tenant_id, establishment_id, idempotency_key)" in sql
    assert "fiscal_documents_authorized_complete" in sql
    assert "xml_artifact_id IS NOT NULL" in sql


def test_fiscal_middleware_never_uses_validation_uuid():
    source = Path("main.py").read_text(encoding="utf-8")
    assert source.index('path.startswith("/api/v1/fiscal")') < source.index(
        'empresa_uuid = request.headers.get("validation-uuid")'
    )
