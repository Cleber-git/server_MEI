from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from cryptography.fernet import Fernet
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
from .domain import DocumentType, Environment, canonical_hash, canonical_json
from .errors import ApiProblem
from .interfaces import UnavailableProvider
from .queue import PostgresFiscalJobQueue
from . import capabilities, establishment, vault
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
    FiscalEstablishmentDto,
    FiscalEstablishmentUpdate,
)

router = APIRouter(prefix="/api/v1/fiscal", tags=["fiscal"])
provider = UnavailableProvider()
queue = PostgresFiscalJobQueue()


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


def _document(row):
    return FiscalDocumentDto(
        id=row[0],
        documentType=row[1],
        environment=row[2],
        status=row[3],
        series=row[4],
        number=row[5],
        officialKey=row[6],
        protocol=row[7],
        authorizedAt=row[8],
        createdAt=row[9],
        updatedAt=row[10],
        fiscalErrorCode=row[11],
        fiscalErrorMessage=row[12],
        xmlSha256=row[13],
        xmlArtifactId=row[14],
        auxiliaryArtifactId=row[15],
        correlationId=row[16],
        attempt=row[17],
    )


DOC_SELECT = """SELECT d.id,d.document_type,d.environment,d.status,d.series,d.number,d.official_key,d.protocol,d.authorized_at,
 d.created_at,d.updated_at,d.fiscal_error_code,d.fiscal_error_message,a.sha256,d.xml_artifact_id,d.auxiliary_artifact_id,d.correlation_id,d.attempt
 FROM fiscal_documents d LEFT JOIN fiscal_artifacts a ON a.id=d.xml_artifact_id"""


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


@router.post("/capabilities/evaluate", response_model=CapabilityEvaluationDto)
def evaluate_capability(
    data: EvaluateRequest,
    principal: Principal = Depends(require_permission("FISCAL_CONFIG_READ")),
):
    is_service = isinstance(data, EvaluateServiceRequest)
    conn = get_conn()
    try:
        cur = conn.cursor()
        now = capabilities.now_utc()
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
        uf = info["uf"] if info else None
        caps, approved, cert_ok, csc_ok = {}, set(), False, False
        if est and uf:
            caps = capabilities.current(cur, uf, configured, now)
            approved = capabilities.homologated(cur, principal.tenant_id, est[0], uf)
            cur.execute(
                """SELECT EXISTS(SELECT 1 FROM fiscal_certificates WHERE tenant_id=%s
                   AND establishment_id=%s AND status='ACTIVE' AND valid_from<=%s AND expires_at>%s)""",
                (principal.tenant_id, est[0], now, now),
            )
            cert_ok = cur.fetchone()[0]
            cur.execute(
                """SELECT EXISTS(SELECT 1 FROM fiscal_credentials WHERE tenant_id=%s
                   AND establishment_id=%s AND credential_type=%s AND status='ACTIVE')""",
                (principal.tenant_id, est[0], vault.CSC_CREDENTIAL),
            )
            csc_ok = cur.fetchone()[0]
        conn.rollback()
        ctx = capabilities.Context(
            tenant_id=principal.tenant_id,
            establishment_uf=uf,
            missing=establishment.missing_fields(info)
            if info
            else {
                kind: ["establishment"]
                for kind in (
                    DocumentType.NFSE,
                    DocumentType.NFCE_65,
                    DocumentType.NFE_55,
                )
            },
            environment=configured,
            caps=caps,
            approved=approved,
            certificate_valid=bool(cert_ok),
            csc_configured=bool(csc_ok),
            production_effective=bool(est) and _production_effective(est),
            now=now,
        )
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


@router.post("/documents", response_model=FiscalDocumentDto, status_code=202)
def create_document(
    request: Request,
    data: CreateFiscalDocumentRequest = Body(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_CREATE")),
):
    if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 200:
        raise ApiProblem(
            400,
            "IDEMPOTENCY_KEY_REQUIRED",
            "Informe Idempotency-Key imprevisível e não vazio",
        )
    payload = data.model_dump(mode="json")
    digest = canonical_hash(payload)
    conn = get_conn()
    try:
        cur = conn.cursor()
        est = _establishment(cur, principal.tenant_id)
        if data.environment == Environment.PRODUCTION:
            raise ApiProblem(
                409,
                "PRODUCTION_DISABLED",
                "Produção não foi habilitada pelos gates do servidor",
            )
        if not settings.fiscal_enabled or not est[3]:
            raise ApiProblem(409, "CAPABILITY_NOT_AVAILABLE", provider.reason)
        if not settings.local_vault_key or not settings.kms_key_reference:
            raise ApiProblem(
                409,
                "FISCAL_CONFIGURATION_INCOMPLETE",
                "Cofre/KMS para payload fiscal não configurado",
            )
        cur.execute(
            """SELECT canonical_payload_hash,resource_id FROM fiscal_idempotency
          WHERE tenant_id=%s AND establishment_id=%s AND operation='CREATE_DOCUMENT' AND idempotency_key=%s FOR UPDATE""",
            (principal.tenant_id, est[0], idempotency_key),
        )
        idem = cur.fetchone()
        if idem:
            if idem[0] != digest:
                raise ApiProblem(
                    409,
                    "IDEMPOTENCY_KEY_REUSED",
                    "A chave já foi usada com outro payload",
                )
            cur.execute(
                DOC_SELECT + " WHERE d.tenant_id=%s AND d.id=%s",
                (principal.tenant_id, idem[1]),
            )
            return _document(cur.fetchone())
        document_id, correlation_id, now = (
            uuid4(),
            UUID(request.state.correlation_id),
            datetime.now(timezone.utc),
        )
        cur.execute(
            """INSERT INTO fiscal_idempotency(tenant_id,establishment_id,operation,idempotency_key,canonical_payload_hash,resource_id,status,expires_at)
          VALUES(%s,%s,'CREATE_DOCUMENT',%s,%s,%s,'PROCESSING',NOW()+INTERVAL '90 days')""",
            (principal.tenant_id, est[0], idempotency_key, digest, document_id),
        )
        cur.execute(
            """INSERT INTO fiscal_documents(id,tenant_id,establishment_id,idempotency_key,canonical_request_hash,schema_version,sale_id,
          document_type,environment,status,correlation_id,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'VALIDATING',%s,%s,%s)""",
            (
                document_id,
                principal.tenant_id,
                est[0],
                idempotency_key,
                digest,
                data.schemaVersion,
                data.saleId,
                data.documentType.value,
                data.environment.value,
                correlation_id,
                now,
                now,
            ),
        )
        try:
            encrypted_payload = Fernet(
                settings.local_vault_key.encode("ascii")
            ).encrypt(canonical_json(payload))
        except ValueError as exc:
            raise ApiProblem(
                409, "FISCAL_CONFIGURATION_INCOMPLETE", "Chave do cofre fiscal inválida"
            ) from exc
        cur.execute(
            """INSERT INTO fiscal_document_payloads(document_id,tenant_id,canonical_request_ciphertext,
              encryption_key_reference,retention_until) VALUES(%s,%s,%s,%s,CURRENT_DATE+INTERVAL '5 years')""",
            (
                document_id,
                principal.tenant_id,
                encrypted_payload,
                settings.kms_key_reference,
            ),
        )
        _audit(
            cur,
            principal,
            "FISCAL_DOCUMENT_CREATE",
            "document",
            document_id,
            correlation_id,
            "ACCEPTED",
        )
        conn.commit()
        queue.enqueue_validation(
            principal.tenant_id, str(document_id), str(correlation_id)
        )
        cur = conn.cursor()
        cur.execute(
            DOC_SELECT + " WHERE d.tenant_id=%s AND d.id=%s",
            (principal.tenant_id, document_id),
        )
        return _document(cur.fetchone())
    except ApiProblem:
        conn.rollback()
        raise
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
        cur = conn.cursor()
        cur.execute(
            DOC_SELECT + " WHERE d.tenant_id=%s AND d.id=%s",
            (principal.tenant_id, document_id),
        )
        row = cur.fetchone()
        if not row:
            raise ApiProblem(404, "NOT_FOUND", "Recurso não encontrado.")
        return _document(row)
    finally:
        put_conn(conn)


@router.get("/documents")
def list_documents(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    documentType: str | None = None,
    status: str | None = None,
    environment: str | None = None,
    saleId: str | None = None,
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_READ")),
):
    conn = get_conn()
    try:
        cur = conn.cursor()
        clauses = ["d.tenant_id=%s"]
        params = [principal.tenant_id]
        for col, val in (
            ("d.document_type", documentType),
            ("d.status", status),
            ("d.environment", environment),
            ("d.sale_id", saleId),
        ):
            if val:
                clauses.append(col + "=%s")
                params.append(val)
        where = " AND ".join(clauses)
        cur.execute("SELECT count(*) FROM fiscal_documents d WHERE " + where, params)
        total = cur.fetchone()[0]
        cur.execute(
            DOC_SELECT
            + " WHERE "
            + where
            + " ORDER BY d.created_at DESC,d.id DESC LIMIT %s OFFSET %s",
            params + [size, (page - 1) * size],
        )
        return {
            "items": [_document(r) for r in cur.fetchall()],
            "page": page,
            "size": size,
            "total": total,
        }
    finally:
        put_conn(conn)


@router.post("/documents/{document_id}/cancel", status_code=202)
def cancel_document(
    document_id: UUID,
    data: CancelFiscalDocumentRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_CANCEL")),
):
    if not idempotency_key:
        raise ApiProblem(400, "IDEMPOTENCY_KEY_REQUIRED", "Informe Idempotency-Key")
    raise ApiProblem(409, "CAPABILITY_NOT_AVAILABLE", provider.reason)


@router.post("/documents/{document_id}/retry", status_code=202)
def retry_document(
    document_id: UUID,
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_CREATE")),
):
    raise ApiProblem(409, "CAPABILITY_NOT_AVAILABLE", provider.reason)


@router.get("/documents/{document_id}/xml")
@router.get("/documents/{document_id}/auxiliary-document")
def get_artifact(
    document_id: UUID,
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_READ")),
):
    # Deliberately uniform until private encrypted object storage is configured.
    raise ApiProblem(404, "NOT_FOUND", "Recurso não encontrado.")


@router.get("/internal/readiness", include_in_schema=False)
def readiness(principal: Principal = Depends(require_permission("FISCAL_CONFIG_READ"))):
    return {
        "ready": False,
        "fiscalFeature": settings.fiscal_enabled,
        "production": False,
        "provider": "unavailable",
        "vault": settings.vault_provider,
    }
