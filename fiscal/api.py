from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
)

from psycopg2.extras import Json

from db import get_conn, put_conn
from .auth import Principal, require_permission
from .config import settings
from .domain import DocumentType, Environment, FiscalStatus, canonical_hash
from .errors import ApiProblem
from . import capabilities, documents, establishment, providers, vault
from .schemas import (
    CancelFiscalDocumentRequest,
    CapabilityEvaluationDto,
    CatalogMetadataDto,
    CreateFiscalDocumentRequest,
    CertificateStatusDto,
    CscRequest,
    EvaluateRequest,
    EvaluateServiceRequest,
    FiscalCapabilitiesDto,
    FiscalConfigDto,
    FiscalConfigUpdate,
    FiscalDocumentDto,
    FiscalDocumentPageDto,
    FiscalEstablishmentDto,
    FiscalEstablishmentUpdate,
    FiscalEventPageDto,
    SubstituteFiscalDocumentRequest,
)

router = APIRouter(prefix="/api/v1/fiscal", tags=["fiscal"])


def _establishment(cur, tenant_id):
    cur.execute(
        """SELECT fe.id,fe.uf,fe.ibge_city_code,fe.fiscal_enabled,fe.production_enabled,
      fc.nfse_environment,fc.nfe_environment,fc.nfse_provider,fc.last_validated_at,fc.production_requested,
      fc.production_enabled,fc.homologation_approved_at,fc.monitoring_active,fc.operational_checklist_approved_at
      FROM fiscal_establishments fe JOIN fiscal_configurations fc ON fc.establishment_id=fe.id
      WHERE fe.tenant_id=%s ORDER BY fe.created_at LIMIT 1""",
        (tenant_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ApiProblem(
            422,
            "ESTABLISHMENT_INCOMPLETE",
            "Complete o cadastro fiscal da empresa.",
        )
    return row


def _production_effective(est) -> bool:
    """Produção só com todos os gates do servidor e do operador (nunca pelo cliente)."""
    return bool(
        settings.production_enabled
        and est[4]
        and est[10]
        and est[11] is not None
        and est[12]
        and est[13] is not None
    )


def _audit(
    cur,
    principal,
    action,
    resource_type,
    resource_id,
    correlation_id,
    result,
    metadata=None,
):
    cur.execute(
        """INSERT INTO fiscal_audit_log(id,tenant_id,actor_id,action,resource_type,resource_id,correlation_id,result,sanitized_metadata)
      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            uuid4(),
            principal.tenant_id,
            principal.user_id,
            action,
            resource_type,
            str(resource_id) if resource_id else None,
            correlation_id,
            result,
            # dict cru em JSONB quebra no psycopg2 (can't adapt type 'dict').
            Json(metadata or {}),
        ),
    )


@router.get("/config", response_model=FiscalConfigDto)
def get_config(
    principal: Principal = Depends(require_permission("FISCAL_CONFIG_READ")),
):
    conn = get_conn()
    try:
        cur = conn.cursor()
        est = _establishment(cur, principal.tenant_id)
        conn.rollback()
        return FiscalConfigDto(
            nfseEnvironment=est[5],
            nfeEnvironment=est[6],
            productionEnabled=_production_effective(est),
            provider=est[7],
            lastValidatedAt=est[8],
        )
    finally:
        put_conn(conn)


@router.put("/config", response_model=FiscalConfigDto)
def put_config(
    data: FiscalConfigUpdate,
    request: Request,
    principal: Principal = Depends(require_permission("FISCAL_CONFIG_WRITE")),
):
    """Troca de ambiente dentro do que o servidor permite (contrato 4.1)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        est = _establishment(cur, principal.tenant_id)
        effective = _production_effective(est)
        if data.productionEnabled != effective:
            # Somente leitura: só o operador liga a produção, nunca o app.
            raise ApiProblem(
                422,
                "VALIDATION_FAILED",
                "A produção é ligada somente pelo operador do servidor.",
                field_errors=[{"path": "productionEnabled", "code": "READ_ONLY"}],
            )
        wants_production = Environment.PRODUCTION in (
            data.nfseEnvironment,
            data.nfeEnvironment,
        )
        if wants_production and not effective:
            raise ApiProblem(
                403,
                "PRODUCTION_NOT_ENABLED",
                "A produção ainda não foi liberada para esta empresa.",
            )
        cur.execute(
            """UPDATE fiscal_configurations SET nfse_environment=%s,nfe_environment=%s,nfse_provider=%s,
          version=version+1 WHERE establishment_id=%s""",
            (
                data.nfseEnvironment.value,
                data.nfeEnvironment.value,
                data.provider,
                est[0],
            ),
        )
        _audit(
            cur,
            principal,
            "FISCAL_CONFIG_UPDATE",
            "establishment",
            est[0],
            request.state.correlation_id,
            "SUCCESS",
        )
        conn.commit()
        return FiscalConfigDto(
            nfseEnvironment=data.nfseEnvironment,
            nfeEnvironment=data.nfeEnvironment,
            productionEnabled=effective,
            provider=data.provider,
            lastValidatedAt=est[8],
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


@router.get("/establishment", response_model=FiscalEstablishmentDto)
def get_establishment(
    principal: Principal = Depends(require_permission("FISCAL_CONFIG_READ")),
):
    conn = get_conn()
    try:
        cur = conn.cursor()
        data = establishment.load(cur, principal.tenant_id)
        if data is None:
            data = establishment.from_company(
                establishment.company(cur, principal.tenant_id)
            )
        conn.rollback()
        return establishment.to_dto(data)
    finally:
        conn.rollback()
        put_conn(conn)


@router.put("/establishment", response_model=FiscalEstablishmentDto)
def put_establishment(
    data: FiscalEstablishmentUpdate,
    request: Request,
    principal: Principal = Depends(require_permission("FISCAL_CONFIG_WRITE")),
):
    conn = get_conn()
    try:
        cur = conn.cursor()
        company = establishment.company(cur, principal.tenant_id)
        establishment.validate_update(data, company["cnpj"])
        saved = establishment.save(cur, principal.tenant_id, data, company["cnpj"])
        dto = establishment.to_dto(saved)
        _audit(
            cur,
            principal,
            "ESTABLISHMENT_UPDATE",
            "establishment",
            saved["id"],
            request.state.correlation_id,
            "SUCCESS",
            {
                "fiscalEnabled": saved["fiscalEnabled"],
                "missing": {
                    m["documentType"]: len(m["fields"]) for m in dto["missingFields"]
                },
            },
        )
        conn.commit()
        return dto
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def _environment_for(est, operation: str) -> str:
    return est[5] if operation == "NFSE" else est[6]


@router.get("/capabilities", response_model=FiscalCapabilitiesDto)
def get_capabilities(
    uf: str = Query(pattern=r"^[A-Z]{2}$"),
    principal: Principal = Depends(require_permission("FISCAL_CONFIG_READ")),
):
    conn = get_conn()
    try:
        cur = conn.cursor()
        now = capabilities.now_utc()
        data = establishment.load(cur, principal.tenant_id)
        if data is None:
            conn.rollback()
            return capabilities.capabilities_dto(
                uf,
                {},
                set(),
                now,
                blocked_reason="Complete o cadastro fiscal da empresa.",
            )
        est = _establishment(cur, principal.tenant_id)
        # NFS-e e produto podem estar em ambientes diferentes: lê cada um no seu.
        caps = capabilities.current(cur, uf, est[5], now)
        if est[6] != est[5]:
            product = capabilities.current(cur, uf, est[6], now)
            caps = {
                op: (caps if op == "NFSE" else product).get(op)
                for op in capabilities.OPERATIONS
            }
            caps = {op: cap for op, cap in caps.items() if cap is not None}
        approved = capabilities.homologated(cur, principal.tenant_id, est[0], uf)
        conn.rollback()
        blocked = None
        if uf != data["uf"]:
            blocked = "A UF consultada não é a do cadastro fiscal da empresa."
        elif establishment.missing_fields(data)[DocumentType.NFSE]:
            blocked = "Complete o cadastro fiscal da empresa."
        return capabilities.capabilities_dto(
            uf, caps, approved, now, blocked_reason=blocked
        )
    finally:
        conn.rollback()
        put_conn(conn)


def _certificate_ready(cur, tenant_id, establishment_id, now) -> bool:
    """Certificado ativo, na validade e do titular do cadastro (CNPJ ou CPF do MEI).

    Um certificado de outro titular (por exemplo, depois de trocar o CNPJ da empresa)
    não conta: a emissão fica bloqueada até um novo envio.
    """
    cur.execute(
        """SELECT EXISTS(SELECT 1 FROM fiscal_certificates c
             JOIN fiscal_establishments fe ON fe.id=c.establishment_id AND fe.tenant_id=c.tenant_id
             WHERE c.tenant_id=%s AND c.establishment_id=%s AND c.status='ACTIVE'
               AND c.valid_from<=%s AND c.expires_at>%s AND c.holder_tax_id IS NOT NULL
               AND (c.holder_tax_id=regexp_replace(upper(fe.cnpj),'[^0-9A-Z]','','g')
                    OR c.holder_tax_id=fe.responsible_cpf))""",
        (tenant_id, establishment_id, now, now),
    )
    return bool(cur.fetchone()[0])


def _evaluation_context(cur, tenant_id, info, est, configured) -> capabilities.Context:
    now = capabilities.now_utc()
    uf = info["uf"] if info else None
    caps, approved, cert_ok, csc_ok = {}, set(), False, False
    if est and uf:
        caps = capabilities.current(cur, uf, configured, now)
        approved = capabilities.homologated(cur, tenant_id, est[0], uf)
        cert_ok = _certificate_ready(cur, tenant_id, est[0], now)
        cur.execute(
            """SELECT EXISTS(SELECT 1 FROM fiscal_credentials WHERE tenant_id=%s
               AND establishment_id=%s AND credential_type=%s AND status='ACTIVE')""",
            (tenant_id, est[0], vault.CSC_CREDENTIAL),
        )
        csc_ok = cur.fetchone()[0]
    return capabilities.Context(
        tenant_id=tenant_id,
        establishment_uf=uf,
        missing=establishment.missing_fields(info)
        if info
        else {
            kind: ["establishment"]
            for kind in (DocumentType.NFSE, DocumentType.NFCE_65, DocumentType.NFE_55)
        },
        environment=configured,
        caps=caps,
        approved=approved,
        certificate_valid=bool(cert_ok),
        csc_configured=bool(csc_ok),
        production_effective=bool(est) and _production_effective(est),
        now=now,
    )


@router.post("/capabilities/evaluate", response_model=CapabilityEvaluationDto)
def evaluate_capability(
    data: EvaluateRequest,
    principal: Principal = Depends(require_permission("FISCAL_CONFIG_READ")),
):
    is_service = isinstance(data, EvaluateServiceRequest)
    conn = get_conn()
    try:
        cur = conn.cursor()
        info = establishment.load(cur, principal.tenant_id)
        est = _establishment(cur, principal.tenant_id) if info else None
        configured = (
            _environment_for(est, "NFSE" if is_service else "NFE_55")
            if est
            else Environment.RESTRICTED_PRODUCTION.value
        )
        if data.environment.value != configured:
            if data.environment == Environment.PRODUCTION:
                raise ApiProblem(
                    403,
                    "PRODUCTION_NOT_ENABLED",
                    "A produção ainda não foi liberada para esta empresa.",
                )
            raise ApiProblem(
                422,
                "VALIDATION_FAILED",
                "O ambiente não é o configurado para esta empresa.",
                field_errors=[{"path": "environment", "code": "ENVIRONMENT_MISMATCH"}],
            )
        if data.environment == Environment.PRODUCTION and not _production_effective(
            est
        ):
            raise ApiProblem(
                403,
                "PRODUCTION_NOT_ENABLED",
                "A produção ainda não foi liberada para esta empresa.",
            )
        ctx = _evaluation_context(cur, principal.tenant_id, info, est, configured)
        conn.rollback()
        if is_service:
            return capabilities.evaluate_service(ctx, data)
        return capabilities.evaluate_product(ctx, data)
    finally:
        conn.rollback()
        put_conn(conn)


@router.get("/catalogs/{catalog_type}/metadata", response_model=CatalogMetadataDto)
def catalog_metadata(
    catalog_type: str,
    principal: Principal = Depends(require_permission("FISCAL_CONFIG_READ")),
):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT type,source_url,version,published_at,valid_until,sha256
               FROM fiscal_catalog_metadata WHERE type=%s
               ORDER BY published_at DESC, loaded_at DESC LIMIT 1""",
            (catalog_type,),
        )
        row = cur.fetchone()
        conn.rollback()
        # Tipo desconhecido e catálogo sem fonte oficial carregada respondem igual.
        if not row or not capabilities.is_safe_gov_url(row[1]):
            raise ApiProblem(404, "NOT_FOUND", "Recurso não encontrado.")
        return CatalogMetadataDto(
            type=row[0],
            source=row[1],
            version=row[2],
            publishedAt=row[3],
            validUntil=row[4],
            sha256=row[5].strip(),
        )
    finally:
        conn.rollback()
        put_conn(conn)


_PFX_CONTENT_TYPES = {
    "application/x-pkcs12",
    "application/pkcs12",
    "application/octet-stream",
}


def _locked_establishment(cur, tenant_id):
    """``(id, cnpj, is_mei, responsible_cpf)`` com a linha travada (serializa uploads)."""
    est = _establishment(cur, tenant_id)
    cur.execute(
        """SELECT id,cnpj,is_mei,responsible_cpf FROM fiscal_establishments
           WHERE id=%s AND tenant_id=%s FOR UPDATE""",
        (est[0], tenant_id),
    )
    return cur.fetchone()


@router.post("/certificates", response_model=CertificateStatusDto)
async def import_certificate(
    request: Request,
    certificate: UploadFile = File(...),
    password: str = Form(...),
    principal: Principal = Depends(require_permission("FISCAL_CERTIFICATE_MANAGE")),
):
    secret = bytearray(password.encode("utf-8"))
    del password
    content = b""
    try:
        ring = vault.keyring()
        if certificate.content_type not in _PFX_CONTENT_TYPES:
            raise ApiProblem(
                415, "CERTIFICATE_INVALID", "Envie o arquivo .pfx do certificado A1."
            )
        content = await certificate.read(vault.PFX_MAX_BYTES + 1)
        if len(content) > vault.PFX_MAX_BYTES:
            raise ApiProblem(413, "PAYLOAD_TOO_LARGE", "Arquivo maior que o permitido.")
        parsed = vault.parse_pkcs12(content, secret)
    finally:
        secret[:] = bytes(len(secret))
        content = b""
        await certificate.close()
    conn = get_conn()
    try:
        cur = conn.cursor()
        est_id, cnpj, is_mei, responsible_cpf = _locked_establishment(
            cur, principal.tenant_id
        )
        vault.check_holder(
            parsed, cnpj=cnpj, is_mei=is_mei, responsible_cpf=responsible_cpf
        )
        now = datetime.now(timezone.utc)
        stored = vault.store_certificate(
            cur, principal.tenant_id, est_id, parsed, ring, now
        )
        _audit(
            cur,
            principal,
            "CERTIFICATE_IMPORT",
            "certificate",
            stored.certificate_id,
            request.state.correlation_id,
            "SUCCESS",
            {
                "reactivated": stored.reactivated,
                "replaced": len(stored.replaced_fingerprints),
                "holderKind": parsed.holder.kind if parsed.holder else None,
            },
        )
        conn.commit()
        vault.forget_mtls(stored.replaced_fingerprints)
        holder = parsed.holder
        return CertificateStatusDto(
            configured=True,
            valid=True,
            certificateId=stored.certificate_id,
            holder=vault.mask_tax_id(holder.tax_id if holder else None),
            expiresAt=parsed.expires_at,
            type="A1",
            lastValidatedAt=now,
            holderTaxId=holder.tax_id if holder else None,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


@router.get("/certificates/status", response_model=CertificateStatusDto)
def certificate_status(
    principal: Principal = Depends(require_permission("FISCAL_CONFIG_READ")),
):
    conn = get_conn()
    try:
        cur = conn.cursor()
        est = _establishment(cur, principal.tenant_id)
        cur.execute(
            """SELECT id,holder_masked,expires_at,type,last_validated_at,
          (status='ACTIVE' AND valid_from<=NOW() AND expires_at>NOW()),holder_tax_id FROM fiscal_certificates
          WHERE tenant_id=%s AND establishment_id=%s AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1""",
            (principal.tenant_id, est[0]),
        )
        row = cur.fetchone()
        conn.rollback()
        if not row:
            return CertificateStatusDto(configured=False, valid=False)
        return CertificateStatusDto(
            configured=True,
            valid=row[5],
            certificateId=row[0],
            holder=row[1],
            expiresAt=row[2],
            type=row[3],
            lastValidatedAt=row[4],
            holderTaxId=row[6],
        )
    finally:
        put_conn(conn)


@router.delete("/certificates/{certificate_id}", status_code=204)
def revoke_certificate(
    certificate_id: UUID,
    request: Request,
    confirmation: str | None = Header(default=None, alias="X-Confirm-Revocation"),
    principal: Principal = Depends(require_permission("FISCAL_CERTIFICATE_MANAGE")),
):
    if confirmation != "REVOKE":
        raise ApiProblem(
            400,
            "REVOCATION_CONFIRMATION_REQUIRED",
            "Confirme a remoção do certificado.",
        )
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT vault_reference,fingerprint FROM fiscal_certificates
               WHERE id=%s AND tenant_id=%s AND status='ACTIVE' FOR UPDATE""",
            (certificate_id, principal.tenant_id),
        )
        row = cur.fetchone()
        if not row:
            raise ApiProblem(404, "NOT_FOUND", "Recurso não encontrado.")
        vault.destroy(cur, principal.tenant_id, row[0])
        cur.execute(
            "UPDATE fiscal_certificates SET status='REVOKED',revoked_at=NOW() WHERE id=%s AND tenant_id=%s",
            (certificate_id, principal.tenant_id),
        )
        _audit(
            cur,
            principal,
            "CERTIFICATE_REVOKE",
            "certificate",
            certificate_id,
            request.state.correlation_id,
            "SUCCESS",
        )
        conn.commit()
        vault.forget_mtls([row[1].strip()])
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


@router.put("/csc", status_code=204)
def put_csc(
    data: CscRequest,
    request: Request,
    principal: Principal = Depends(require_permission("FISCAL_CONFIG_WRITE")),
):
    ring = vault.keyring()
    conn = get_conn()
    try:
        cur = conn.cursor()
        est_id = _locked_establishment(cur, principal.tenant_id)[0]
        replaced = vault.store_csc(
            cur, principal.tenant_id, est_id, data.cscId, data.cscToken, ring
        )
        _audit(
            cur,
            principal,
            "CSC_UPDATE",
            "establishment",
            est_id,
            request.state.correlation_id,
            "SUCCESS",
            {"replaced": replaced},
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)
    return Response(status_code=204)


# --- Documentos (contrato 4.4, F7) -------------------------------------------------

S = FiscalStatus
_PRODUCTION_DENIED = (
    403,
    "PRODUCTION_NOT_ENABLED",
    "A produção ainda não foi liberada para esta empresa.",
)


def _unavailable(reasons, message="A emissão automática não está disponível."):
    return ApiProblem(422, "CAPABILITY_UNAVAILABLE", message, reasons=reasons)


def _require_fiscal_enabled():
    if not settings.fiscal_enabled:
        raise ApiProblem(
            503, "FISCAL_DISABLED", "A emissão fiscal está desligada no servidor."
        )


def _gate_document(cur, principal, data: CreateFiscalDocumentRequest, est) -> None:
    """Recusa tudo que não pode virar documento. Nada é gravado antes daqui."""
    if data.documentType == "OFFICIAL_NFF":
        raise _unavailable(
            ["OFFICIAL_CHANNEL"],
            "A NFF é emitida no aplicativo oficial, não por este servidor.",
        )
    _require_fiscal_enabled()
    is_service = data.documentType == "NFSE"
    configured = _environment_for(est, "NFSE" if is_service else "NFE_55")
    if data.environment == Environment.PRODUCTION and not _production_effective(est):
        raise ApiProblem(*_PRODUCTION_DENIED)
    if data.environment.value != configured:
        raise ApiProblem(
            422,
            "VALIDATION_FAILED",
            "O ambiente não é o configurado para esta empresa.",
            field_errors=[{"path": "environment", "code": "ENVIRONMENT_MISMATCH"}],
        )
    if data.schemaVersion == "1.1":
        raise ApiProblem(
            422,
            "FISCAL_OPERATION_INCOMPLETE",
            "Atualize o aplicativo para emitir notas de produto.",
            reasons=["PRODUCT_INTENT_V2_REQUIRED"],
        )
    if is_service:
        documents.check_service_totals(data)
    else:
        intent = data.product
        missing = []
        if any(i.productSource is None for i in intent.items):
            missing.append("ITEM_SOURCE_REQUIRED")
        if any(i.classification is None or i.gtin is None for i in intent.items):
            missing.append("ITEM_CLASSIFICATION_REQUIRED")
        if intent.operation.purpose == "SALE" and intent.paymentExpectation != "PAID":
            missing.append("SALE_REQUIRES_PAYMENT")
        if missing:
            raise ApiProblem(
                422,
                "FISCAL_OPERATION_INCOMPLETE",
                "Faltam dados fiscais para emitir esta nota.",
                reasons=missing,
            )
        documents.check_product_totals(intent)
    info = establishment.load(cur, principal.tenant_id)
    ctx = _evaluation_context(cur, principal.tenant_id, info, est, configured)
    if is_service:
        result = capabilities.evaluate_service(ctx, data)
        expected = "NFSE_AUTOMATIC"
    else:
        result = capabilities.evaluate_product(ctx, data.product)
        expected = data.documentType
    decision, reasons = result["decision"], result["reasons"]
    if decision == expected and (
        is_service or result["capabilityId"] == data.product.route.capabilityId
    ):
        return
    if decision == "REQUIRES_FISCAL_REVIEW":
        raise ApiProblem(
            422,
            "FISCAL_OPERATION_INCOMPLETE",
            "Esta operação precisa de revisão fiscal antes da emissão.",
            reasons=reasons,
        )
    if "ESTABLISHMENT_INCOMPLETE" in reasons:
        raise ApiProblem(
            422,
            "ESTABLISHMENT_INCOMPLETE",
            "Complete o cadastro fiscal da empresa.",
            reasons=reasons,
        )
    if decision == expected:
        raise _unavailable(["CAPABILITY_ROUTE_MISMATCH"])
    raise _unavailable(reasons)


def _replayed(cur, tenant_id, existing, digest):
    resource_id = documents.replay(existing, digest)
    doc = documents.load(cur, tenant_id, resource_id) if resource_id else None
    if doc is None:
        raise documents.not_found()
    return doc


def _conflict_after_insert(cur, tenant_id, est_id, key, data, digest, substitutes):
    """O INSERT do documento bateu num índice único: diz qual regra foi violada."""
    cur.execute(
        """SELECT 1 FROM fiscal_documents WHERE tenant_id=%s AND establishment_id=%s
           AND idempotency_key=%s""",
        (tenant_id, est_id, key),
    )
    if cur.fetchone():
        raise ApiProblem(
            409,
            "IDEMPOTENCY_KEY_REUSED",
            "Esta Idempotency-Key já foi usada com outro conteúdo.",
        )
    if substitutes:
        raise ApiProblem(
            409,
            "INVALID_STATE_TRANSITION",
            "Já existe uma substituição em andamento para este documento.",
        )
    existing = _duplicate_intent(cur, tenant_id, data, digest)
    raise _duplicate_problem(existing)


def _duplicate_intent(cur, tenant_id, data, digest):
    if not data.saleId:
        return None
    cur.execute(
        """SELECT id FROM fiscal_documents WHERE tenant_id=%s AND sale_id=%s
           AND document_type=%s AND canonical_request_hash=%s
           AND status NOT IN ('REJECTED','ERROR_FINAL') LIMIT 1""",
        (tenant_id, data.saleId, data.documentType, digest),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


def _duplicate_problem(existing_id):
    return ApiProblem(
        409,
        "DUPLICATE_FISCAL_INTENT",
        "Esta venda já tem um documento fiscal com o mesmo conteúdo.",
        reasons=[existing_id] if existing_id else [],
    )


def _create(
    cur,
    principal,
    data: CreateFiscalDocumentRequest,
    key: str,
    correlation_id: str,
    *,
    operation=documents.OP_CREATE,
    command_digest: str | None = None,
    substitutes: documents.Doc | None = None,
    extra_payload: dict | None = None,
):
    """Valida, reserva número e grava documento + idempotência + job numa transação.

    Devolve ``(doc, created)``; ``created=False`` é a repetição idempotente."""
    tenant = principal.tenant_id
    payload = data.model_dump(mode="json", exclude_none=True)
    digest = canonical_hash(payload)
    command_digest = command_digest or digest
    est = _establishment(cur, tenant)
    existing = documents.find_idempotency(cur, tenant, est[0], operation, key)
    if existing:
        return _replayed(cur, tenant, existing, command_digest), False
    _gate_document(cur, principal, data, est)
    duplicate = _duplicate_intent(cur, tenant, data, digest)
    if duplicate:
        raise _duplicate_problem(duplicate)
    ring = vault.keyring()
    document_id = str(uuid4())
    raced = documents.claim_idempotency(
        cur, tenant, est[0], operation, key, command_digest, document_id
    )
    if raced is not None:
        return _replayed(cur, tenant, raced, command_digest), False
    series, number = documents.reserve_number(
        cur, tenant, est[0], data.documentType, data.environment.value
    )
    blob = documents.seal_json(cur, tenant, {**payload, **(extra_payload or {})}, ring)
    cur.execute(
        """INSERT INTO fiscal_documents(id,tenant_id,establishment_id,idempotency_key,
             canonical_request_hash,schema_version,sale_id,document_type,environment,status,
             series,number,correlation_id,payload_blob_id,substitutes_document_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'VALIDATING',%s,%s,%s,%s,%s)
           ON CONFLICT DO NOTHING RETURNING id""",
        (
            document_id,
            tenant,
            est[0],
            key,
            digest,
            data.schemaVersion,
            data.saleId,
            data.documentType,
            data.environment.value,
            series,
            number,
            correlation_id,
            blob,
            substitutes.id if substitutes else None,
        ),
    )
    if cur.fetchone() is None:
        _conflict_after_insert(cur, tenant, est[0], key, data, digest, substitutes)
    doc = documents.load(cur, tenant, document_id, for_update=True)
    documents.record_event(cur, doc, "CREATED", None, S.VALIDATING, correlation_id)
    doc = documents.transition(
        cur, doc, S.QUEUED, event="QUEUED", correlation_id=correlation_id
    )
    documents.enqueue(
        cur, tenant, document_id, "SUBMIT", correlation_id, settings.job_max_attempts
    )
    _audit(
        cur,
        principal,
        "FISCAL_DOCUMENT_CREATE",
        "document",
        document_id,
        correlation_id,
        "ACCEPTED",
        {"documentType": data.documentType, "substitution": bool(substitutes)},
    )
    return documents.load(cur, tenant, document_id), True


@router.post("/documents", response_model=FiscalDocumentDto, status_code=202)
def create_document(
    request: Request,
    data: CreateFiscalDocumentRequest = Body(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_CREATE")),
):
    key = documents.check_idempotency_key(idempotency_key)
    conn = get_conn()
    try:
        cur = conn.cursor()
        doc, _ = _create(cur, principal, data, key, request.state.correlation_id)
        conn.commit()
        return documents.to_dto(doc)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


@router.get("/documents/{document_id}", response_model=FiscalDocumentDto)
def get_document(
    document_id: UUID,
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_READ")),
):
    conn = get_conn()
    try:
        doc = documents.load(conn.cursor(), principal.tenant_id, document_id)
        if doc is None:
            raise documents.not_found()
        return documents.to_dto(doc)
    finally:
        conn.rollback()
        put_conn(conn)


@router.get("/documents", response_model=FiscalDocumentPageDto)
def list_documents(
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    documentType: Literal["NFSE", "NFCE_65", "NFE_55"] | None = None,
    status: FiscalStatus | None = None,
    date_from: datetime | None = Query(None, alias="from"),
    date_to: datetime | None = Query(None, alias="to"),
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_READ")),
):
    conn = get_conn()
    try:
        docs, next_cursor = documents.list_page(
            conn.cursor(),
            principal.tenant_id,
            limit=limit,
            cursor=cursor,
            document_type=documentType,
            status=status.value if status else None,
            date_from=date_from,
            date_to=date_to,
        )
        now = datetime.now(timezone.utc)
        return {
            "items": [documents.to_dto(d, now) for d in docs],
            "nextCursor": next_cursor,
        }
    finally:
        conn.rollback()
        put_conn(conn)


def _command_document(cur, principal, document_id):
    """Documento travado para um comando; outro tenant responde como inexistente."""
    doc = documents.load(cur, principal.tenant_id, document_id, for_update=True)
    if doc is None:
        raise documents.not_found()
    return doc


def _active_substitution(cur, doc) -> bool:
    cur.execute(
        """SELECT 1 FROM fiscal_documents WHERE tenant_id=%s AND substitutes_document_id=%s
           AND status NOT IN ('REJECTED','ERROR_FINAL','AUTHORIZED','CANCELLED')""",
        (doc.tenant_id, doc.id),
    )
    return cur.fetchone() is not None


def _invalid_state(message="Esta ação não é permitida no estado atual do documento."):
    return ApiProblem(409, "INVALID_STATE_TRANSITION", message)


@router.post(
    "/documents/{document_id}/cancel", response_model=FiscalDocumentDto, status_code=202
)
def cancel_document(
    document_id: UUID,
    data: CancelFiscalDocumentRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_CANCEL")),
):
    key = documents.check_idempotency_key(idempotency_key)
    digest = canonical_hash({"documentId": str(document_id), "reason": data.reason})
    corr = request.state.correlation_id
    conn = get_conn()
    try:
        cur = conn.cursor()
        doc = _command_document(cur, principal, document_id)
        _require_fiscal_enabled()
        existing = documents.find_idempotency(
            cur, doc.tenant_id, doc.establishment_id, documents.OP_CANCEL, key
        )
        if existing:
            replayed = _replayed(cur, doc.tenant_id, existing, digest)
            conn.rollback()
            return documents.to_dto(replayed)
        if doc.status != S.AUTHORIZED or _active_substitution(cur, doc):
            raise _invalid_state()
        if documents.claim_idempotency(
            cur,
            doc.tenant_id,
            doc.establishment_id,
            documents.OP_CANCEL,
            key,
            digest,
            doc.id,
        ):
            raise _invalid_state()  # outra requisição com a mesma chave chegou antes
        blob = documents.seal_json(
            cur, doc.tenant_id, {"reason": data.reason}, vault.keyring()
        )
        doc = documents.transition(
            cur,
            doc,
            S.CANCEL_PENDING,
            event="CANCEL_REQUESTED",
            correlation_id=corr,
            cancel_requested_at=datetime.now(timezone.utc),
            cancel_reason_blob_id=blob,
            fiscal_error_code=None,
            fiscal_error_message=None,
        )
        documents.enqueue(
            cur, doc.tenant_id, doc.id, "CANCEL", corr, settings.job_max_attempts
        )
        _audit(
            cur,
            principal,
            "FISCAL_DOCUMENT_CANCEL",
            "document",
            doc.id,
            corr,
            "ACCEPTED",
        )
        conn.commit()
        return documents.to_dto(documents.load(cur, doc.tenant_id, doc.id))
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


@router.post(
    "/documents/{document_id}/retry", response_model=FiscalDocumentDto, status_code=202
)
def retry_document(
    document_id: UUID,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_CREATE")),
):
    key = documents.check_idempotency_key(idempotency_key)
    digest = canonical_hash({"documentId": str(document_id), "command": "retry"})
    corr = request.state.correlation_id
    conn = get_conn()
    try:
        cur = conn.cursor()
        doc = _command_document(cur, principal, document_id)
        _require_fiscal_enabled()
        existing = documents.find_idempotency(
            cur, doc.tenant_id, doc.establishment_id, documents.OP_RETRY, key
        )
        if existing:
            replayed = _replayed(cur, doc.tenant_id, existing, digest)
            conn.rollback()
            return documents.to_dto(replayed)
        if doc.status != S.ERROR_RETRYABLE:
            raise _invalid_state()
        if documents.claim_idempotency(
            cur,
            doc.tenant_id,
            doc.establishment_id,
            documents.OP_RETRY,
            key,
            digest,
            doc.id,
        ):
            raise _invalid_state()
        # Mesmo número e mesma intenção; se o último envio ficou incerto, o worker consulta antes.
        doc = documents.transition(
            cur,
            doc,
            S.QUEUED,
            event="RETRY_REQUESTED",
            correlation_id=corr,
            fiscal_error_code=None,
            fiscal_error_message=None,
        )
        documents.enqueue(
            cur, doc.tenant_id, doc.id, "SUBMIT", corr, settings.job_max_attempts
        )
        _audit(
            cur,
            principal,
            "FISCAL_DOCUMENT_RETRY",
            "document",
            doc.id,
            corr,
            "ACCEPTED",
        )
        conn.commit()
        return documents.to_dto(documents.load(cur, doc.tenant_id, doc.id))
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


@router.post(
    "/documents/{document_id}/substitute",
    response_model=FiscalDocumentDto,
    status_code=202,
)
def substitute_document(
    document_id: UUID,
    data: SubstituteFiscalDocumentRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_CANCEL")),
):
    """Substituição (só NFS-e, e só com provider que a suporte). Devolve o substituto."""
    key = documents.check_idempotency_key(idempotency_key)
    digest = canonical_hash(
        {
            "documentId": str(document_id),
            **data.model_dump(mode="json", exclude_none=True),
        }
    )
    corr = request.state.correlation_id
    conn = get_conn()
    try:
        cur = conn.cursor()
        original = _command_document(cur, principal, document_id)
        _require_fiscal_enabled()
        existing = documents.find_idempotency(
            cur,
            original.tenant_id,
            original.establishment_id,
            documents.OP_SUBSTITUTE,
            key,
        )
        if existing:
            replayed = _replayed(cur, original.tenant_id, existing, digest)
            conn.rollback()
            return documents.to_dto(replayed)
        if original.status != S.AUTHORIZED or _active_substitution(cur, original):
            raise _invalid_state()
        replacement = data.replacement
        if (
            original.document_type != "NFSE"
            or replacement.documentType != original.document_type
            or not providers.provider_for(original.document_type).supports_substitution
        ):
            raise _unavailable(
                ["SUBSTITUTION_UNAVAILABLE"],
                "A substituição não está disponível para este documento.",
            )
        doc, _ = _create(
            cur,
            principal,
            replacement,
            key,
            corr,
            operation=documents.OP_SUBSTITUTE,
            command_digest=digest,
            substitutes=original,
            extra_payload={"substitution": {"reason": data.reason}},
        )
        documents.record_event(
            cur, original, "SUBSTITUTION_REQUESTED", None, None, corr
        )
        conn.commit()
        return documents.to_dto(doc)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


@router.get("/documents/{document_id}/events", response_model=FiscalEventPageDto)
def document_events(
    document_id: UUID,
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_READ")),
):
    conn = get_conn()
    try:
        cur = conn.cursor()
        doc = documents.load(cur, principal.tenant_id, document_id)
        if doc is None:
            raise documents.not_found()
        return {"items": documents.events(cur, doc.tenant_id, doc.id)}
    finally:
        conn.rollback()
        put_conn(conn)


def _download(document_id, principal, request, column: str, media_type: str, ext: str):
    """Download com autorização a cada pedido e SHA-256 conferido. Auditado."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        doc = documents.load(cur, principal.tenant_id, document_id)
        artifact_id = getattr(doc, column) if doc else None
        if (
            doc is None
            or doc.status not in documents.EVIDENCE_STATUSES
            or not artifact_id
        ):
            raise documents.not_found()
        content, _ = documents.read_artifact(
            cur, doc.tenant_id, artifact_id, vault.keyring()
        )
        _audit(
            cur,
            principal,
            "FISCAL_ARTIFACT_DOWNLOAD",
            "document",
            doc.id,
            request.state.correlation_id,
            "SUCCESS",
            {"artifact": ext},
        )
        conn.commit()
        name = (doc.official_key or doc.id) + "." + ext
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


@router.get("/documents/{document_id}/xml")
def get_xml(
    document_id: UUID,
    request: Request,
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_READ")),
):
    return _download(
        document_id, principal, request, "xml_artifact_id", "application/xml", "xml"
    )


@router.get("/documents/{document_id}/auxiliary-document")
def get_auxiliary_document(
    document_id: UUID,
    request: Request,
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_READ")),
):
    return _download(
        document_id,
        principal,
        request,
        "auxiliary_artifact_id",
        "application/pdf",
        "pdf",
    )


@router.get("/internal/readiness", include_in_schema=False)
def readiness(principal: Principal = Depends(require_permission("FISCAL_CONFIG_READ"))):
    return {
        "ready": False,
        "fiscalFeature": settings.fiscal_enabled,
        "production": False,
        "provider": settings.fiscal_provider,
        "vault": settings.vault_provider,
    }
