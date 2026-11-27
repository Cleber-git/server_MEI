from __future__ import annotations

import base64
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
    Response,
    Request,
    UploadFile,
)

from db import get_conn, put_conn
from .auth import Principal, require_permission
from .config import settings
from .domain import Environment, canonical_hash, canonical_json
from .errors import ApiProblem
from .interfaces import UnavailableProvider
from .queue import PostgresFiscalJobQueue
from .schemas import (
    CancelFiscalDocumentRequest,
    CreateFiscalDocumentRequest,
    CertificateStatusDto,
    FiscalCapabilitiesDto,
    FiscalConfigDto,
    FiscalDocumentDto,
)
from .vault import LocalEncryptedCertificateVault

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
            "FISCAL_CONFIGURATION_INCOMPLETE",
            "Configuração fiscal incompleta",
            "Cadastre um estabelecimento fiscal válido",
        )
    return row


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
            metadata or {},
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
        # There is no homologated provider in this delivery. Database/client
        # flags can never bypass this operational gate.
        effective = False
        return FiscalConfigDto(
            nfseEnvironment=est[5],
            nfeEnvironment=est[6],
            productionEnabled=effective,
            provider=est[7],
            lastValidatedAt=est[8],
        )
    finally:
        put_conn(conn)


@router.put("/config", response_model=FiscalConfigDto)
def put_config(
    data: FiscalConfigDto,
    request: Request,
    principal: Principal = Depends(require_permission("FISCAL_CONFIG_WRITE")),
):
    if (
        data.productionEnabled
        and "FISCAL_PRODUCTION_ENABLE" not in principal.permissions
    ):
        raise ApiProblem(
            403,
            "ACCESS_DENIED",
            "Acesso negado",
            "Permissão para solicitar produção ausente",
        )
    conn = get_conn()
    try:
        cur = conn.cursor()
        est = _establishment(cur, principal.tenant_id)
        if data.productionEnabled:
            raise ApiProblem(
                409,
                "HOMOLOGATION_REQUIRED",
                "Homologação obrigatória",
                "A ativação exige checklist, homologação e aprovação operacional no servidor",
            )
        cur.execute(
            """UPDATE fiscal_configurations SET nfse_environment=%s,nfe_environment=%s,nfse_provider=%s,
          production_requested=FALSE,production_enabled=FALSE,version=version+1 WHERE establishment_id=%s""",
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
            productionEnabled=False,
            provider=data.provider,
            lastValidatedAt=est[8],
        )
    except ApiProblem:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


@router.get("/capabilities", response_model=FiscalCapabilitiesDto)
def capabilities(
    uf: str = Query(pattern=r"^[A-Z]{2}$"),
    principal: Principal = Depends(require_permission("FISCAL_CONFIG_READ")),
):
    conn = get_conn()
    try:
        cur = conn.cursor()
        est = _establishment(cur, principal.tenant_id)
        reasons = []
        if uf != est[1]:
            reasons.append("UF solicitada não corresponde ao estabelecimento")
        if not est[2]:
            reasons.append("código IBGE do município não configurado")
        if not settings.fiscal_enabled:
            reasons.append("feature fiscal global desabilitada")
        reasons.append(provider.reason)
        return FiscalCapabilitiesDto(
            uf=uf,
            version="server-mei-fiscal-capabilities-v1",
            reason="; ".join(reasons),
            requiresCertificate=True,
        )
    finally:
        put_conn(conn)


@router.post("/certificates", response_model=CertificateStatusDto)
async def import_certificate(
    request: Request,
    certificate: UploadFile = File(...),
    password: str = Form(...),
    principal: Principal = Depends(require_permission("FISCAL_CERTIFICATE_MANAGE")),
):
    if certificate.content_type not in {
        "application/x-pkcs12",
        "application/pkcs12",
        "application/octet-stream",
    }:
        raise ApiProblem(
            415,
            "CERTIFICATE_INVALID",
            "Certificado inválido",
            "Content-Type PKCS#12 obrigatório",
        )
    content = await certificate.read(min(settings.max_upload_bytes, 64 * 1024) + 1)
    if len(content) > min(settings.max_upload_bytes, 64 * 1024):
        raise ApiProblem(
            413,
            "CERTIFICATE_INVALID",
            "Certificado inválido",
            "Arquivo excede o limite",
        )
    conn = get_conn()
    try:
        cur = conn.cursor()
        est = _establishment(cur, principal.tenant_id)
        cur.execute(
            "SELECT cnpj FROM fiscal_establishments WHERE id=%s AND tenant_id=%s",
            (est[0], principal.tenant_id),
        )
        cnpj = cur.fetchone()[0]
        metadata = LocalEncryptedCertificateVault().import_pkcs12(
            principal.tenant_id, content, password
        )
        if (
            not metadata["holderTaxId"]
            or "".join(ch for ch in cnpj if ch.isalnum()).upper()
            != metadata["holderTaxId"].upper()
        ):
            LocalEncryptedCertificateVault().revoke(
                principal.tenant_id, metadata["reference"]
            )
            raise ApiProblem(
                422,
                "CERTIFICATE_HOLDER_MISMATCH",
                "Titular incompatível",
                "Certificado não pertence ao estabelecimento",
            )
        cert_id = uuid4()
        now = datetime.now(timezone.utc)
        cur.execute(
            """INSERT INTO fiscal_certificates(id,tenant_id,establishment_id,vault_reference,fingerprint,serial_number,holder_masked,issuer,
          valid_from,expires_at,type,status,last_validated_at,holder_tax_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'A1','PENDING_VALIDATION',%s,%s)""",
            (
                cert_id,
                principal.tenant_id,
                est[0],
                metadata["reference"],
                metadata["fingerprint"],
                metadata["serial"],
                metadata["holder"],
                metadata["issuer"],
                metadata["validFrom"],
                metadata["expiresAt"],
                now,
                metadata["holderTaxId"],
            ),
        )
        _audit(
            cur,
            principal,
            "CERTIFICATE_IMPORT",
            "certificate",
            cert_id,
            request.state.correlation_id,
            "SUCCESS",
        )
        conn.commit()
        return CertificateStatusDto(
            configured=True,
            # The local vault parses PKCS#12 but does not validate ICP-Brasil
            # trust chain and revocation. Never claim validity without those checks.
            valid=False,
            certificateId=cert_id,
            holder=metadata["holder"],
            expiresAt=metadata["expiresAt"],
            type="A1",
            lastValidatedAt=now,
            holderTaxId=metadata["holderTaxId"],
        )
    except ApiProblem:
        conn.rollback()
        raise
    finally:
        content = b""
        password = ""
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
            """SELECT c.id,c.holder_masked,c.expires_at,c.type,c.last_validated_at,
          (c.status='ACTIVE' AND c.valid_from<=NOW() AND c.expires_at>NOW()),c.holder_tax_id,c.status,e.cnpj
          FROM fiscal_certificates c JOIN fiscal_establishments e ON e.id=c.establishment_id AND e.tenant_id=c.tenant_id
          WHERE c.tenant_id=%s AND c.establishment_id=%s AND c.status IN ('ACTIVE','PENDING_VALIDATION') ORDER BY c.created_at DESC LIMIT 1""",
            (principal.tenant_id, est[0]),
        )
        row = cur.fetchone()
        if not row:
            return CertificateStatusDto(configured=False, valid=False)
        return CertificateStatusDto(
            configured=True,
            valid=(
                row[5]
                and row[6] is not None
                and row[7] == "ACTIVE"
                and "".join(char for char in row[6] if char.isalnum()).upper()
                == "".join(char for char in row[8] if char.isalnum()).upper()
            ),
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
            422,
            "VALIDATION_ERROR",
            "Confirmação obrigatória",
            "Envie X-Confirm-Revocation: REVOKE",
        )
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT vault_reference FROM fiscal_certificates WHERE id=%s AND tenant_id=%s AND status='ACTIVE' FOR UPDATE",
            (certificate_id, principal.tenant_id),
        )
        row = cur.fetchone()
        if not row:
            raise ApiProblem(
                404,
                "TENANT_RESOURCE_NOT_FOUND",
                "Certificado não encontrado",
                "Recurso não encontrado",
            )
        LocalEncryptedCertificateVault().revoke(principal.tenant_id, row[0])
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
    except ApiProblem:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


@router.post("/documents", response_model=FiscalDocumentDto, status_code=202)
def create_document(
    request: Request,
    response: Response,
    data: CreateFiscalDocumentRequest = Body(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_CREATE")),
):
    if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 200:
        raise ApiProblem(
            400,
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotência obrigatória",
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
                422,
                "PRODUCTION_DISABLED",
                "Produção desabilitada",
                "Produção não foi habilitada pelos gates do servidor",
            )
        if not settings.fiscal_enabled or not est[3]:
            raise ApiProblem(
                409,
                "CAPABILITY_NOT_AVAILABLE",
                "Capacidade indisponível",
                provider.reason,
            )
        if not settings.local_vault_key or not settings.kms_key_reference:
            raise ApiProblem(
                409,
                "FISCAL_CONFIGURATION_INCOMPLETE",
                "Configuração fiscal incompleta",
                "Cofre/KMS para payload fiscal não configurado",
            )
        # Serialize the check-and-create path even when the idempotency row
        # does not exist yet (a row-level FOR UPDATE cannot lock a missing row).
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"{principal.tenant_id}:{est[0]}:CREATE_DOCUMENT:{idempotency_key}",),
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
                    "IDEMPOTENCY_CONFLICT",
                    "Chave de idempotência reutilizada",
                    "A chave já foi usada com outro payload",
                )
            cur.execute(
                DOC_SELECT + " WHERE d.tenant_id=%s AND d.id=%s",
                (principal.tenant_id, idem[1]),
            )
            response.status_code = 200
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
                409,
                "FISCAL_CONFIGURATION_INCOMPLETE",
                "Configuração fiscal incompleta",
                "Chave do cofre fiscal inválida",
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
            raise ApiProblem(
                404,
                "TENANT_RESOURCE_NOT_FOUND",
                "Documento não encontrado",
                "Recurso não encontrado",
            )
        return _document(row)
    finally:
        put_conn(conn)


@router.get("/documents")
def list_documents(
    document_type: str | None = Query(default=None, alias="type"),
    status: str | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_READ")),
):
    conn = get_conn()
    try:
        cur = conn.cursor()
        clauses = ["d.tenant_id=%s"]
        params = [principal.tenant_id]
        for col, val in (
            ("d.document_type", document_type),
            ("d.status", status),
        ):
            if val:
                clauses.append(col + "=%s")
                params.append(val)
        where = " AND ".join(clauses)
        if from_:
            clauses.append("d.created_at >= %s")
            params.append(from_)
        if to:
            clauses.append("d.created_at <= %s")
            params.append(to)
        if from_ and from_.utcoffset() is None or to and to.utcoffset() is None:
            raise ApiProblem(400, "BAD_REQUEST", "Requisição inválida", "Datas devem incluir fuso horário")
        if cursor:
            try:
                decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
                timestamp, document_id = decoded.split("|", 1)
                cursor_time = datetime.fromisoformat(timestamp)
                cursor_uuid = UUID(document_id)
                if cursor_time.utcoffset() is None:
                    raise ValueError("cursor sem fuso")
            except (ValueError, UnicodeDecodeError):
                raise ApiProblem(400, "BAD_REQUEST", "Cursor inválido", "Cursor de paginação inválido")
            clauses.append("(d.created_at,d.id) < (%s,%s)")
            params.extend([cursor_time, cursor_uuid])
        where = " AND ".join(clauses)
        cur.execute(
            DOC_SELECT
            + " WHERE "
            + where
            + " ORDER BY d.created_at DESC,d.id DESC LIMIT %s",
            params + [limit + 1],
        )
        rows = cur.fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            value = f"{last[9].isoformat()}|{last[0]}".encode("ascii")
            next_cursor = base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
        return {
            "items": [_document(r) for r in rows],
            "nextCursor": next_cursor,
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
        raise ApiProblem(
            400,
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotência obrigatória",
            "Informe Idempotency-Key",
        )
    raise ApiProblem(
        409, "CAPABILITY_NOT_AVAILABLE", "Cancelamento indisponível", provider.reason
    )


@router.post("/documents/{document_id}/retry", status_code=202)
def retry_document(
    document_id: UUID,
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_CREATE")),
):
    raise ApiProblem(
        409, "CAPABILITY_NOT_AVAILABLE", "Reconciliação indisponível", provider.reason
    )


@router.get("/documents/{document_id}/xml")
@router.get("/documents/{document_id}/auxiliary-document")
def get_artifact(
    document_id: UUID,
    principal: Principal = Depends(require_permission("FISCAL_DOCUMENT_READ")),
):
    # Deliberately uniform until private encrypted object storage is configured.
    raise ApiProblem(
        404,
        "ARTIFACT_NOT_AVAILABLE",
        "Artefato indisponível",
        "Artefato oficial não disponível",
    )


@router.get("/internal/readiness", include_in_schema=False)
def readiness(principal: Principal = Depends(require_permission("FISCAL_CONFIG_READ"))):
    return {
        "ready": False,
        "fiscalFeature": settings.fiscal_enabled,
        "production": False,
        "provider": "unavailable",
        "vault": settings.vault_provider,
    }
