"""Capabilities fiscais por UF × operação × ambiente (F5).

Fonte: tabela versionada ``fiscal_capabilities`` (migration 006). Regras:

- Sem linha, linha sem fonte, linha vencida ou URL fora de ``*.gov.br`` ⇒ bloqueado.
- Emissão automática (NFS-e, NFC-e, NF-e) só com a linha disponível **e** credenciada
  **e** homologação aprovada do tenant em ``fiscal_homologation_checks``, **e** o fiscal
  ligado no servidor.
- NFF é canal oficial externo: o servidor só sinaliza a rota, nunca emite.

``evaluate_*`` recebe fatos e decide como o ``ProductDocumentRoutingPolicy`` do app,
sempre falhando para o lado bloqueado.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .config import settings
from .domain import DocumentType

log = logging.getLogger("fiscal.capabilities")

AUTOMATIC = ("NFSE", "NFCE_65", "NFE_55")
OPERATIONS = AUTOMATIC + ("OFFICIAL_NFF", "STATE_NFAE")
# Uma decisão positiva vale por pouco tempo: o app reavalia antes de emitir.
DECISION_TTL = timedelta(hours=1)
NO_DATA_REASON = "Emissão automática ainda não homologada para esta UF."


def is_safe_gov_url(raw: str | None) -> bool:
    """Mesma régua do app (``FiscalOfficialLinks.isSafeGovernmentUrl``): https, sem
    credenciais, host ``gov.br`` ou ``*.gov.br``, sem porta."""
    if not raw or raw != raw.strip() or any(c.isspace() for c in raw):
        return False
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and not parts.username
        and not parts.password
        and "@" not in parts.netloc
        and port is None
        and (host == "gov.br" or host.endswith(".gov.br"))
    )


@dataclass(frozen=True)
class Capability:
    id: str
    uf: str
    operation: str
    environment: str
    ruleset_version: str
    layout_version: str | None
    valid_until: datetime | None
    available: bool
    accredited: bool
    requires_certificate: bool
    requires_csc: bool
    requires_state_registration: bool
    transport_contract_required: bool
    unidentified_recipient_allowed: bool | None
    official_channel_url: str | None
    source_url: str | None
    verified_at: object
    reason: str | None

    def usable(self, now: datetime) -> bool:
        """Disponível, credenciada, com fonte e dentro da validade."""
        return (
            self.available
            and self.accredited
            and bool(self.source_url and self.verified_at)
            and is_safe_gov_url(self.source_url)
            and (self.valid_until is None or now < self.valid_until)
        )


_SELECT = """SELECT DISTINCT ON (operation) id,uf,operation,environment,ruleset_version,
  layout_version,valid_until,available,accredited,requires_certificate,requires_csc,
  requires_state_registration,transport_contract_required,unidentified_recipient_allowed,
  official_channel_url,source_url,verified_at,reason
  FROM fiscal_capabilities WHERE uf=%s AND environment=%s AND effective_from<=%s
  ORDER BY operation, effective_from DESC"""


def current(cur, uf: str, environment: str, now: datetime) -> dict[str, Capability]:
    cur.execute(_SELECT, (uf, environment, now))
    result = {}
    for row in cur.fetchall():
        cap = Capability(*row)
        if cap.official_channel_url and not is_safe_gov_url(cap.official_channel_url):
            # O CHECK do banco já barra; isto cobre linha antiga/forjada.
            log.warning("capability_url_rejected", extra={"capabilityId": cap.id})
            cap = Capability(**{**cap.__dict__, "official_channel_url": None})
        result[cap.operation] = cap
    return result


def homologated(cur, tenant_id: str, establishment_id, uf: str) -> set[str]:
    """Modelos com homologação aprovada para o tenant nesta UF."""
    cur.execute(
        """SELECT DISTINCT model FROM fiscal_homologation_checks
           WHERE tenant_id=%s AND establishment_id=%s AND uf=%s AND result='APPROVED'""",
        (tenant_id, establishment_id, uf),
    )
    return {row[0] for row in cur.fetchall()}


def automatic_enabled(
    caps: dict[str, Capability], approved: set[str], operation: str, now: datetime
) -> bool:
    cap = caps.get(operation)
    return bool(
        settings.fiscal_enabled
        and cap is not None
        and cap.usable(now)
        and operation in approved
    )


def capabilities_dto(
    uf: str,
    caps: dict[str, Capability],
    approved: set[str],
    now: datetime,
    *,
    blocked_reason: str | None = None,
) -> dict:
    version = max((c.ruleset_version for c in caps.values()), default="none")
    if blocked_reason is not None:
        return {
            "uf": uf,
            "version": version,
            "nfseAutomatic": False,
            "nfce": False,
            "nfe": False,
            "nffOfficialChannel": False,
            "requiresCertificate": True,
            "reason": blocked_reason,
            "nfseUnidentifiedRecipientAllowed": None,
            "stateOfficialChannelUrl": None,
        }
    nfse = automatic_enabled(caps, approved, "NFSE", now)
    nfce = automatic_enabled(caps, approved, "NFCE_65", now)
    nfe = automatic_enabled(caps, approved, "NFE_55", now)
    nff = caps.get("OFFICIAL_NFF")
    nff_ok = bool(nff and nff.usable(now))
    state = caps.get("STATE_NFAE")
    state_url = state.official_channel_url if state and state.source_url else None
    nfse_cap = caps.get("NFSE")
    unidentified = (
        nfse_cap.unidentified_recipient_allowed if nfse and nfse_cap else None
    )
    enabled = [op for op, on in zip(AUTOMATIC, (nfse, nfce, nfe)) if on]
    # Sem canal automático ligado, exige certificado: nunca libera EMIT_IN_APP por ausência.
    requires_certificate = (
        any(caps[op].requires_certificate for op in enabled) if enabled else True
    )
    if nfse or nfce or nfe:
        reason = None
    elif nff_ok:
        reason = nff.reason
    else:
        reason = NO_DATA_REASON
    return {
        "uf": uf,
        "version": version,
        "nfseAutomatic": nfse,
        "nfce": nfce,
        "nfe": nfe,
        "nffOfficialChannel": nff_ok,
        "requiresCertificate": requires_certificate,
        "reason": reason,
        "nfseUnidentifiedRecipientAllowed": unidentified,
        "stateOfficialChannelUrl": state_url,
    }


# --- /capabilities/evaluate ------------------------------------------------------

MESSAGES = {
    "NFSE_AUTOMATIC": "Emissão automática de NFS-e disponível.",
    "NFCE_65": "Emissão automática de NFC-e disponível.",
    "NFE_55": "Emissão automática de NF-e disponível.",
    "OFFICIAL_NFF": "Use o aplicativo oficial Nota Fiscal Fácil (NFF).",
    "STATE_NFAE": "Use o emissor oficial da Secretaria da Fazenda do seu estado.",
    "CERTIFICATE_SETUP": "Configure e valide o certificado digital A1.",
    "OFFICIAL_PORTAL": "Emita pelo Emissor Nacional da NFS-e.",
    "UNAVAILABLE": "Não há canal de emissão disponível para esta operação.",
    "REQUIRES_FISCAL_REVIEW": "Esta operação precisa de revisão fiscal antes da emissão.",
}


@dataclass(frozen=True)
class Context:
    tenant_id: str
    establishment_uf: str | None
    missing: dict  # DocumentType -> list[str]
    environment: str
    caps: dict[str, Capability]
    approved: set[str]
    certificate_valid: bool
    csc_configured: bool
    production_effective: bool
    now: datetime


def _result(ctx: Context, decision: str, reasons: list[str], cap=None, **extra) -> dict:
    automatic = decision in ("NFSE_AUTOMATIC", "NFCE_65", "NFE_55")
    valid_until = None
    if cap is not None and decision not in ("UNAVAILABLE", "REQUIRES_FISCAL_REVIEW"):
        valid_until = ctx.now + DECISION_TTL
        if cap.valid_until is not None:
            valid_until = min(valid_until, cap.valid_until)
    allowed = {
        "NFSE_AUTOMATIC": ["NFSE"],
        "NFCE_65": ["NFCE_65"],
        "NFE_55": ["NFE_55"],
        "OFFICIAL_NFF": ["OFFICIAL_NFF"],
    }.get(decision, [])
    return {
        "decision": decision,
        "capabilityId": cap.id if cap is not None else None,
        "capabilityVersion": cap.ruleset_version if cap is not None else None,
        "validUntil": valid_until,
        "allowedDocumentTypes": allowed,
        "requiresCertificate": cap.requires_certificate if cap is not None else True,
        "requiresCsc": cap.requires_csc if cap is not None else False,
        "requiresStateRegistration": (
            cap.requires_state_registration if cap is not None else False
        ),
        "environment": ctx.environment,
        "layoutVersion": cap.layout_version if cap is not None else None,
        "rulesetVersion": (
            cap.ruleset_version
            if cap is not None
            else max((c.ruleset_version for c in ctx.caps.values()), default=None)
        ),
        "reasons": reasons,
        "message": MESSAGES[decision],
        "productionEligible": bool(
            automatic and ctx.production_effective and ctx.environment == "PRODUCTION"
        ),
        **extra,
    }


def _automatic_gate(ctx: Context, operation: str, decision: str, missing_kind):
    """Checagens comuns de um canal automático já escolhido. ``None`` = liberado."""
    cap = ctx.caps[operation]
    if ctx.missing[missing_kind]:
        return _result(ctx, "UNAVAILABLE", ["ESTABLISHMENT_INCOMPLETE"], cap)
    if cap.requires_certificate and not ctx.certificate_valid:
        return _result(ctx, "CERTIFICATE_SETUP", ["CERTIFICATE_REQUIRED"], cap)
    if cap.requires_csc and not ctx.csc_configured:
        return _result(ctx, "UNAVAILABLE", ["CSC_REQUIRED"], cap)
    return _result(ctx, decision, ["CAPABILITY_ACCEPTED"], cap)


def evaluate_service(ctx: Context, request) -> dict:
    if request.service.emissionMode == "BLOCKED_REVIEW":
        return _result(ctx, "REQUIRES_FISCAL_REVIEW", ["SERVICE_CLASSIFICATION_REVIEW"])
    if not ctx.establishment_uf:
        return _result(ctx, "UNAVAILABLE", ["ESTABLISHMENT_INCOMPLETE"])
    if not automatic_enabled(ctx.caps, ctx.approved, "NFSE", ctx.now):
        return _result(
            ctx, "OFFICIAL_PORTAL", ["NFSE_AUTOMATIC_UNAVAILABLE"], ctx.caps.get("NFSE")
        )
    return _automatic_gate(ctx, "NFSE", "NFSE_AUTOMATIC", DocumentType.NFSE)


def _nfce_compatible(request) -> bool:
    op = request.operation
    return (
        op.destination == "INTERNAL"
        and op.consumerFinal
        and op.salesChannel == "IN_PERSON"
        and request.logistics.mode != "DELIVERY"
        and op.recipientIcmsStatus == "NON_TAXPAYER"
        and op.recipientType == "INDIVIDUAL"
    )


def evaluate_product(ctx: Context, request) -> dict:
    op, recipient, logistics = request.operation, request.recipient, request.logistics
    if op.purpose != "SALE":
        return _result(ctx, "REQUIRES_FISCAL_REVIEW", ["OPERATION_REQUIRES_REVIEW"])
    if (
        op.recipientType == "FOREIGN"
        or recipient.type != op.recipientType
        or op.recipientIcmsStatus == "UNKNOWN"
        or (
            recipient.icmsStatus is not None
            and recipient.icmsStatus != op.recipientIcmsStatus
        )
    ):
        return _result(ctx, "REQUIRES_FISCAL_REVIEW", ["OPERATION_REQUIRES_REVIEW"])
    if not ctx.establishment_uf:
        return _result(ctx, "UNAVAILABLE", ["ESTABLISHMENT_INCOMPLETE"])
    if op.establishmentUf != ctx.establishment_uf:
        # A UF vale a do cadastro do servidor, nunca a do cliente.
        return _result(ctx, "REQUIRES_FISCAL_REVIEW", ["CAPABILITY_UF_MISMATCH"])
    if logistics.mode == "DELIVERY" and logistics.transportMode == "NONE":
        return _result(ctx, "REQUIRES_FISCAL_REVIEW", ["DELIVERY_CONTRACT_INCOMPLETE"])
    if op.salesChannel == "DELIVERY" and logistics.mode != "DELIVERY":
        return _result(ctx, "REQUIRES_FISCAL_REVIEW", ["DELIVERY_CONTRADICTS_CHANNEL"])

    nff = ctx.caps.get("OFFICIAL_NFF")
    if nff is not None and nff.usable(ctx.now):
        return _result(ctx, "OFFICIAL_NFF", ["OFFICIAL_CHANNEL"], nff)
    if _nfce_compatible(request) and automatic_enabled(
        ctx.caps, ctx.approved, "NFCE_65", ctx.now
    ):
        return _automatic_gate(ctx, "NFCE_65", "NFCE_65", DocumentType.NFCE_65)
    if automatic_enabled(ctx.caps, ctx.approved, "NFE_55", ctx.now):
        if (
            not recipient.completeAddressProvided
            or recipient.address is None
            or (op.recipientType == "LEGAL_ENTITY" and not recipient.taxId)
        ):
            return _result(
                ctx, "REQUIRES_FISCAL_REVIEW", ["RECIPIENT_ADDRESS_REQUIRED"]
            )
        return _automatic_gate(ctx, "NFE_55", "NFE_55", DocumentType.NFE_55)
    state = ctx.caps.get("STATE_NFAE")
    if state is not None and state.usable(ctx.now):
        return _result(ctx, "STATE_NFAE", ["OFFICIAL_CHANNEL"], state)
    return _result(ctx, "UNAVAILABLE", ["CHANNEL_UNAVAILABLE"])


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
