"""Cadastro fiscal do estabelecimento (contrato seção 5, F5).

O CNPJ vem sempre do cadastro da empresa (``empresa.cnpj`` do tenant do token):
o cliente não troca o CNPJ conferido contra o titular do certificado. A completude
é calculada por tipo de documento e ``fiscal_enabled`` liga sozinho quando a
operação mínima (NFS-e) está completa; nunca por valor vindo do cliente.
"""

from __future__ import annotations

import hashlib
import re
from uuid import UUID

from psycopg2.extras import Json

from .domain import DocumentType
from .errors import ApiProblem

# Prefixo IBGE (2 dígitos) de cada UF: o código do município precisa bater com a UF.
UF_IBGE_PREFIX = {
    "RO": "11", "AC": "12", "AM": "13", "RR": "14", "PA": "15", "AP": "16", "TO": "17",
    "MA": "21", "PI": "22", "CE": "23", "RN": "24", "PB": "25", "PE": "26", "AL": "27",
    "SE": "28", "BA": "29", "MG": "31", "ES": "32", "RJ": "33", "SP": "35", "PR": "41",
    "SC": "42", "RS": "43", "MS": "50", "MT": "51", "GO": "52", "DF": "53",
}  # fmt: skip
UFS = frozenset(UF_IBGE_PREFIX)

_CNPJ_RE = re.compile(r"^[0-9A-Z]{12}[0-9]{2}$")

_BASE_FIELDS = (
    "cnpj",
    "legalName",
    "uf",
    "cityCode",
    "address.postalCode",
    "address.street",
    "address.number",
    "address.district",
    "address.city",
)
# NF-e/NFC-e exigem a IE do emitente (ISENTO não vale para quem emite).
_REQUIRED = {
    DocumentType.NFSE: _BASE_FIELDS,
    DocumentType.NFCE_65: _BASE_FIELDS + ("stateRegistration",),
    DocumentType.NFE_55: _BASE_FIELDS + ("stateRegistration",),
}


def normalize_cnpj(value: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (value or "").upper())


def valid_cnpj(value: str | None) -> bool:
    """CNPJ numérico ou alfanumérico (12 posições [0-9A-Z] + 2 DV numéricos)."""
    if not value or not _CNPJ_RE.match(value) or len(set(value)) == 1:
        return False
    digits = [ord(c) - 48 for c in value]
    for size in (12, 13):
        weights = list(range(size - 7, 1, -1)) + list(range(9, 1, -1))
        total = sum(d * w for d, w in zip(digits[:size], weights))
        rest = total % 11
        if digits[size] != (0 if rest < 2 else 11 - rest):
            return False
    return True


def valid_cpf(value: str | None) -> bool:
    if not value or not re.fullmatch(r"\d{11}", value) or len(set(value)) == 1:
        return False
    digits = [int(c) for c in value]
    for size in (9, 10):
        total = sum(d * w for d, w in zip(digits[:size], range(size + 1, 1, -1)))
        dv = (total * 10) % 11 % 10
        if digits[size] != dv:
            return False
    return True


_COLUMNS = """fe.id,fe.cnpj,fe.legal_name,fe.trade_name,fe.state_registration,
  fe.municipal_registration,fe.responsible_cpf,fe.ibge_city_code,fe.uf,fe.address_zip_code,
  fe.address_street,fe.address_number,fe.address_complement,fe.address_district,
  fe.address_city,fe.tax_regime,fe.simei,fe.cnaes,fe.fiscal_enabled"""


def _row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "cnpj": (row[1] or "").strip() or None,
        "legalName": row[2],
        "tradeName": row[3],
        "stateRegistration": row[4],
        "municipalRegistration": row[5],
        "responsibleCpf": row[6],
        "cityCode": (row[7] or "").strip() or None,
        "uf": (row[8] or "").strip() or None,
        "address": {
            "postalCode": row[9],
            "street": row[10],
            "number": row[11],
            "complement": row[12],
            "district": row[13],
            "city": row[14],
        },
        "taxRegime": row[15] or "MEI",
        "simei": bool(row[16]),
        "cnaes": list(row[17] or []),
        "fiscalEnabled": bool(row[18]),
    }


def missing_fields(data: dict) -> dict[DocumentType, list[str]]:
    """O que falta para cada tipo. Valor presente mas inválido conta como faltando."""

    def value(path: str):
        current = data
        for part in path.split("."):
            current = (current or {}).get(part)
        return current

    def ok(path: str) -> bool:
        current = value(path)
        if path == "cnpj":
            return valid_cnpj(current)
        if path == "cityCode":
            uf = data.get("uf")
            return bool(
                current
                and uf in UFS
                and re.fullmatch(r"\d{7}", current)
                and current.startswith(UF_IBGE_PREFIX[uf])
            )
        if path == "uf":
            return current in UFS
        if path == "address.postalCode":
            return bool(current and re.fullmatch(r"\d{8}", current))
        if path == "stateRegistration":
            return bool(current and re.fullmatch(r"\d{2,14}", current))
        return bool(current and str(current).strip())

    return {
        kind: [path for path in fields if not ok(path)]
        for kind, fields in _REQUIRED.items()
    }


def to_dto(data: dict) -> dict:
    missing = missing_fields(data)
    body = {k: v for k, v in data.items() if k != "id"}
    body["missingFields"] = [
        {"documentType": kind.value, "fields": fields}
        for kind, fields in missing.items()
    ]
    return body


def load(cur, tenant_id: str) -> dict | None:
    cur.execute(
        "SELECT "
        + _COLUMNS
        + """ FROM fiscal_establishments fe
           WHERE fe.tenant_id=%s ORDER BY fe.created_at LIMIT 1""",
        (tenant_id,),
    )
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def company(cur, tenant_id: str) -> dict:
    """Dados da empresa do tenant (fonte do CNPJ e dos nomes padrão)."""
    cur.execute(
        "SELECT cnpj,razaosocial,nomefantasia,uf FROM empresa WHERE uuid=%s",
        (tenant_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ApiProblem(404, "NOT_FOUND", "Recurso não encontrado.")
    return {
        "cnpj": normalize_cnpj(row[0]) or None,
        "legalName": row[1] or row[2],
        "tradeName": row[2],
        "uf": (row[3] or "").strip().upper() or None,
    }


def from_company(info: dict) -> dict:
    """Estabelecimento ainda não criado: devolve o que se sabe da empresa."""
    return {
        "id": None,
        "cnpj": info["cnpj"],
        "legalName": info["legalName"],
        "tradeName": info["tradeName"],
        "stateRegistration": None,
        "municipalRegistration": None,
        "responsibleCpf": None,
        "cityCode": None,
        "uf": info["uf"] if info["uf"] in UFS else None,
        "address": {
            "postalCode": None,
            "street": None,
            "number": None,
            "complement": None,
            "district": None,
            "city": None,
        },
        "taxRegime": "MEI",
        "simei": True,
        "cnaes": [],
        "fiscalEnabled": False,
    }


def validate_update(update, company_cnpj: str | None) -> None:
    """Regras que o schema sozinho não cobre. Erro nunca ecoa o valor recebido."""
    errors = []
    if update.cnpj is not None and normalize_cnpj(update.cnpj) != (company_cnpj or ""):
        errors.append({"path": "cnpj", "code": "READ_ONLY"})
    if update.uf not in UFS:
        errors.append({"path": "uf", "code": "INVALID"})
    elif update.cityCode and not update.cityCode.startswith(UF_IBGE_PREFIX[update.uf]):
        errors.append({"path": "cityCode", "code": "UF_MISMATCH"})
    if update.responsibleCpf and not valid_cpf(update.responsibleCpf):
        errors.append({"path": "responsibleCpf", "code": "INVALID"})
    if errors:
        raise ApiProblem(
            422,
            "VALIDATION_FAILED",
            "Confira os dados do cadastro fiscal.",
            field_errors=errors,
        )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def save(cur, tenant_id: str, update, company_cnpj: str | None) -> dict:
    """Grava (cria se não existir) e recalcula ``fiscal_enabled``. Linha travada."""
    address = update.address
    data = {
        "cnpj": company_cnpj,
        "legalName": update.legalName.strip(),
        "tradeName": _clean(update.tradeName),
        "stateRegistration": update.stateRegistration,
        "municipalRegistration": update.municipalRegistration,
        "responsibleCpf": update.responsibleCpf,
        "cityCode": update.cityCode,
        "uf": update.uf,
        "address": {
            "postalCode": address.postalCode,
            "street": _clean(address.street),
            "number": _clean(address.number),
            "complement": _clean(address.complement),
            "district": _clean(address.district),
            "city": _clean(address.city),
        },
        "taxRegime": update.taxRegime,
        "simei": update.simei,
        "cnaes": list(dict.fromkeys(update.cnaes)),
    }
    enabled = not missing_fields(data)[DocumentType.NFSE]
    cur.execute(
        "SELECT id FROM fiscal_establishments WHERE tenant_id=%s ORDER BY created_at LIMIT 1 FOR UPDATE",
        (tenant_id,),
    )
    row = cur.fetchone()
    values = (
        data["legalName"],
        data["tradeName"],
        data["cnpj"] or "",
        data["stateRegistration"],
        data["municipalRegistration"],
        data["responsibleCpf"],
        data["taxRegime"],
        data["taxRegime"] == "MEI",
        data["simei"],
        data["address"]["street"],
        data["address"]["number"],
        data["address"]["complement"],
        data["address"]["district"],
        data["address"]["postalCode"],
        data["address"]["city"],
        data["cityCode"],
        data["uf"],
        Json(data["cnaes"]),
        enabled,
    )
    if row:
        establishment_id = row[0]
        cur.execute(
            """UPDATE fiscal_establishments SET legal_name=%s,trade_name=%s,cnpj=%s,
                 state_registration=%s,municipal_registration=%s,responsible_cpf=%s,
                 tax_regime=%s,is_mei=%s,simei=%s,address_street=%s,address_number=%s,
                 address_complement=%s,address_district=%s,address_zip_code=%s,
                 address_city=%s,ibge_city_code=%s,uf=%s,cnaes=%s,fiscal_enabled=%s,
                 updated_at=NOW()
               WHERE id=%s AND tenant_id=%s""",
            values + (establishment_id, tenant_id),
        )
    else:
        # Mesmo id determinístico da 001 (md5(uuid || ':fiscal-establishment')).
        establishment_id = UUID(
            hashlib.md5(
                (tenant_id + ":fiscal-establishment").encode("utf-8")
            ).hexdigest()
        )
        cur.execute(
            """INSERT INTO fiscal_establishments (legal_name,trade_name,cnpj,
                 state_registration,municipal_registration,responsible_cpf,tax_regime,
                 is_mei,simei,address_street,address_number,address_complement,
                 address_district,address_zip_code,address_city,ibge_city_code,uf,cnaes,
                 fiscal_enabled,id,tenant_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            values + (establishment_id, tenant_id),
        )
        cur.execute(
            "INSERT INTO fiscal_configurations(establishment_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (establishment_id,),
        )
    data["id"] = establishment_id
    data["fiscalEnabled"] = enabled
    return data
