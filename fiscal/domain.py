from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from enum import Enum

from .errors import ApiProblem


class DocumentType(str, Enum):
    NFSE = "NFSE"
    NFCE_65 = "NFCE_65"
    NFE_55 = "NFE_55"


class Environment(str, Enum):
    RESTRICTED_PRODUCTION = "RESTRICTED_PRODUCTION"
    PRODUCTION = "PRODUCTION"


class FiscalStatus(str, Enum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    SUBMITTED = "SUBMITTED"
    PROCESSING = "PROCESSING"
    AUTHORIZED = "AUTHORIZED"
    REJECTED = "REJECTED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    CONTINGENCY_PENDING = "CONTINGENCY_PENDING"
    ERROR_RETRYABLE = "ERROR_RETRYABLE"
    ERROR_FINAL = "ERROR_FINAL"


TRANSITIONS = {
    FiscalStatus.DRAFT: {FiscalStatus.VALIDATING},
    FiscalStatus.VALIDATING: {FiscalStatus.QUEUED, FiscalStatus.ERROR_FINAL},
    FiscalStatus.QUEUED: {
        FiscalStatus.SUBMITTED,
        FiscalStatus.ERROR_RETRYABLE,
        FiscalStatus.ERROR_FINAL,
    },
    FiscalStatus.SUBMITTED: {
        FiscalStatus.PROCESSING,
        FiscalStatus.AUTHORIZED,
        FiscalStatus.REJECTED,
        FiscalStatus.ERROR_RETRYABLE,
    },
    FiscalStatus.PROCESSING: {
        FiscalStatus.AUTHORIZED,
        FiscalStatus.REJECTED,
        FiscalStatus.CONTINGENCY_PENDING,
        FiscalStatus.ERROR_RETRYABLE,
        FiscalStatus.ERROR_FINAL,
    },
    FiscalStatus.AUTHORIZED: {FiscalStatus.CANCEL_PENDING},
    FiscalStatus.CANCEL_PENDING: {
        FiscalStatus.CANCELLED,
        FiscalStatus.AUTHORIZED,
        FiscalStatus.ERROR_RETRYABLE,
        FiscalStatus.ERROR_FINAL,
    },
    FiscalStatus.CONTINGENCY_PENDING: {
        FiscalStatus.SUBMITTED,
        FiscalStatus.PROCESSING,
        FiscalStatus.ERROR_RETRYABLE,
        FiscalStatus.ERROR_FINAL,
    },
    FiscalStatus.ERROR_RETRYABLE: {
        FiscalStatus.QUEUED,
        FiscalStatus.PROCESSING,
        FiscalStatus.ERROR_FINAL,
    },
}


def assert_transition(current: FiscalStatus, target: FiscalStatus) -> None:
    if target not in TRANSITIONS.get(current, set()):
        raise ApiProblem(
            409,
            "INVALID_STATE_TRANSITION",
            "Transição fiscal inválida",
            f"Transição {current.value} para {target.value} não permitida",
        )


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


DECIMAL_PATTERN = re.compile(r"^(0|[1-9]\d{0,12})(\.\d{1,4})?$")


def fiscal_decimal(value: str, scale: int = 2) -> Decimal:
    if not isinstance(value, str) or not DECIMAL_PATTERN.fullmatch(value):
        raise ValueError(
            "decimal deve ser string positiva, sem notação científica, com até 4 casas"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("decimal inválido") from exc
    if (
        len(parsed.as_tuple().digits) > 17
        or max(0, -parsed.as_tuple().exponent) > scale
    ):
        raise ValueError(f"decimal excede precisão ou escala {scale}")
    return parsed


def is_complete_authorization(data: dict) -> bool:
    required = (
        "environment",
        "documentType",
        "series",
        "number",
        "officialKey",
        "protocol",
        "authorizedAt",
        "xmlArtifactId",
    )
    return all(data.get(field) is not None for field in required) and bool(
        re.fullmatch(r"[0-9a-f]{64}", data.get("xmlSha256", ""))
    )
