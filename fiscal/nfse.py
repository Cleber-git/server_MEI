"""NFS-e nacional (F8): DPS, assinatura, empacotamento e leitura da NFS-e autorizada.

Funções puras (sem rede nem banco), usadas pelo ``SefinNacionalProvider`` (``fiscal.sefin``).

Leiaute: XSD oficial ``1.01`` versionado em ``fiscal/xsd/nfse`` (ver ``SOURCE.md``), mais as
regras do Anexo I (v1.01, 09/02/2026) que valem para o MEI emitente:

- ``tpEmit=1`` (o próprio prestador emite), ``opSimpNac=2`` (MEI) e ``regEspTrib=0``.
- Prestador sem ``xNome`` e sem ``end`` (E0121/E0128): a SEFIN usa o cadastro.
- ``IM`` só se o cadastro tiver. A SEFIN cobra ou recusa conforme o CNC do município (E0116/E0120).
- MEI não tem retenção de ISSQN: ``tpRetISSQN=1`` sempre. Um pedido com retenção é recusado aqui.
- Série ``00001``–``49999`` = "aplicativo próprio". A série ``1`` do motor (F7) está nessa faixa.
- ``totTrib`` com ``indTotTrib=0`` (Decreto 8.264/2014: valor aproximado não informado).

Nada aqui loga XML, CPF/CNPJ ou texto do serviço. As exceções carregam só um código estável.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from lxml import etree

NS = "http://www.sped.fazenda.gov.br/nfse"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"
LAYOUT_VERSION = "1.01"
READ_VERSIONS = ("1.00", "1.01")
XSD_DIR = Path(__file__).parent / "xsd" / "nfse"
VER_APLIC = "CaltechMEI-1.0"
CANCEL_EVENT = "101101"
CANCEL_REASON_OTHER = "9"  # 1 erro na emissão, 2 serviço não prestado, 9 outros
SUBSTITUTION_REASON_OTHER = "99"
# Brasil sem horário de verão desde 2019: -03:00 representa o instante certo em qualquer UF.
BR_OFFSET = timezone(timedelta(hours=-3))
MAX_UNPACKED_BYTES = 5 * 1024 * 1024  # defesa contra bomba de descompressão

ENVIRONMENT_TPAMB = {"PRODUCTION": "1", "RESTRICTED_PRODUCTION": "2"}

_DIGITS = re.compile(r"^\d+$")
_MONEY = re.compile(r"^(0|[1-9]\d{0,14})(\.\d{2})?$")


class NfseError(Exception):
    """Falha local, antes de qualquer envio. ``code`` é estável e sem PII."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Issuer:
    """Prestador MEI (do cadastro fiscal do tenant)."""

    cnpj: str
    city_code: str
    municipal_registration: str | None = None

    def __repr__(self) -> str:  # sem CNPJ
        return f"Issuer(city={self.city_code})"


def issuer_from_establishment(est: dict) -> Issuer:
    cnpj = re.sub(r"[^0-9A-Za-z]", "", est.get("cnpj") or "").upper()
    if not re.fullmatch(r"\d{14}", cnpj):
        # O leiaute em produção (1.01) ainda só aceita CNPJ numérico (NT 009 não vigente).
        raise NfseError("ISSUER_CNPJ_UNSUPPORTED")
    city = (est.get("cityCode") or "").strip()
    if not re.fullmatch(r"\d{7}", city):
        raise NfseError("ESTABLISHMENT_INCOMPLETE")
    im = re.sub(r"[^0-9A-Za-z]", "", est.get("municipalRegistration") or "")
    return Issuer(cnpj, city, im[:15] or None)


# --- Texto --------------------------------------------------------------------------


def clean_text(value: str | None, max_len: int, *, multiline: bool = False) -> str:
    """Texto aceito pelo ``TSString`` do XSD: só Latin-1 imprimível, sem bordas em branco.

    Caracteres fora do Latin-1 viram a forma sem acento (NFKD) ou somem. ``multiline``
    mantém as quebras de linha (``TSStringComQuebraDeLinha``).
    """
    out = []
    for char in unicodedata.normalize("NFC", value or ""):
        if char in "\r\n" and multiline:
            out.append("\n" if char == "\n" else "")
            continue
        code = ord(char)
        if code < 0x20 or 0x7F <= code < 0xA0:
            out.append(" ")
        elif code <= 0xFF:
            out.append(char)
        else:
            base = unicodedata.normalize("NFKD", char)
            out.append("".join(c for c in base if 0x20 <= ord(c) <= 0x7E))
    text = "".join(out)
    if multiline:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
        text = "\n".join(lines).strip()
    else:
        text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len].rstrip()


def money(value) -> str:
    """Decimal com 2 casas no formato ``TSDec15V2``."""
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise NfseError("SERVICE_AMOUNT_INVALID") from None
    if amount <= 0:
        raise NfseError("SERVICE_AMOUNT_INVALID")
    text = f"{amount:.2f}"
    if not _MONEY.match(text):
        raise NfseError("SERVICE_AMOUNT_INVALID")
    return text


# --- Identificadores ------------------------------------------------------------------


def dps_id(issuer: Issuer, series: str, number: int) -> str:
    """``DPS`` + município (7) + tipo de inscrição (1; 2 = CNPJ) + CNPJ (14) + série (5) + nº (15)."""
    if not (_DIGITS.match(series or "") and len(series) <= 5):
        raise NfseError("DPS_SERIES_INVALID")
    if not (isinstance(number, int) and 0 < number < 10**15):
        raise NfseError("DPS_NUMBER_INVALID")
    return f"DPS{issuer.city_code}2{issuer.cnpj}{series.zfill(5)}{number:015d}"


def official_key_from_nfse_id(value: str | None) -> str | None:
    match = re.fullmatch(r"NFS(\d{50})", value or "")
    return match.group(1) if match else None


def _valid_cpf(value: str) -> bool:
    if not re.fullmatch(r"\d{11}", value) or len(set(value)) == 1:
        return False
    for size in (9, 10):
        total = sum(int(value[i]) * (size + 1 - i) for i in range(size))
        if (total * 10 % 11) % 10 != int(value[size]):
            return False
    return True


def _valid_cnpj(value: str) -> bool:
    if not re.fullmatch(r"\d{14}", value) or len(set(value)) == 1:
        return False
    for size, weights in ((12, "543298765432"), (13, "6543298765432")):
        total = sum(int(value[i]) * int(weights[i]) for i in range(size))
        digit = 0 if total % 11 < 2 else 11 - total % 11
        if digit != int(value[size]):
            return False
    return True


# --- DPS ----------------------------------------------------------------------------


def _sub(parent, tag: str, text: str | None = None):
    element = etree.SubElement(parent, f"{{{NS}}}{tag}")
    if text is not None:
        element.text = text
    return element


def _recipient(inf, recipient: dict) -> None:
    tax_id = re.sub(r"\D", "", recipient.get("taxId") or "")
    raw = (recipient.get("taxId") or "").strip()
    if not raw:
        return  # tomador não identificado: sem grupo ``toma``
    if re.search(r"[A-Za-z]", raw) or recipient.get("foreignTaxId"):
        raise NfseError("RECIPIENT_UNSUPPORTED")  # CNPJ alfanumérico/exterior: leiaute 1.01 não
    if len(tax_id) == 11 and _valid_cpf(tax_id):
        tag = "CPF"
    elif len(tax_id) == 14 and _valid_cnpj(tax_id):
        tag = "CNPJ"
    else:
        raise NfseError("RECIPIENT_TAX_ID_INVALID")
    name = clean_text(recipient.get("name"), 300)
    if not name:
        raise NfseError("RECIPIENT_NAME_REQUIRED")
    toma = _sub(inf, "toma")
    _sub(toma, tag, tax_id)
    _sub(toma, "xNome", name)
    email = clean_text(recipient.get("email"), 80)
    if email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        _sub(toma, "email", email)


def build_dps(
    *,
    payload: dict,
    issuer: Issuer,
    environment: str,
    series: str,
    number: int,
    created_at: datetime,
    now: datetime,
    substitutes_official_key: str | None = None,
) -> etree._Element:
    """Monta a DPS (sem assinatura) a partir da intenção ``1.2`` já validada pelo motor."""
    tp_amb = ENVIRONMENT_TPAMB.get(environment)
    if tp_amb is None:
        raise NfseError("ENVIRONMENT_INVALID")
    service = payload.get("service") or {}
    if service.get("withholdingResponsibility"):
        raise NfseError("MEI_WITHHOLDING_NOT_ALLOWED")  # Anexo I: MEI ⇒ tpRetISSQN=1
    tax_code = service.get("nationalTaxCode") or ""
    if not re.fullmatch(r"\d{6}", tax_code):
        raise NfseError("SERVICE_CODE_INVALID")
    description = clean_text(service.get("description"), 2000, multiline=True)
    if not description:
        raise NfseError("SERVICE_DESCRIPTION_REQUIRED")
    amount = money(service.get("amount"))
    place = service.get("incidenceMunicipalityCode") or issuer.city_code
    if not re.fullmatch(r"\d{7}", place):
        raise NfseError("SERVICE_PLACE_INVALID")

    emitted = now.astimezone(BR_OFFSET).replace(microsecond=0)
    competence = min(created_at.astimezone(BR_OFFSET), emitted).date()

    root = etree.Element(f"{{{NS}}}DPS", nsmap={None: NS}, versao=LAYOUT_VERSION)
    inf = _sub(root, "infDPS")
    inf.set("Id", dps_id(issuer, series, number))
    _sub(inf, "tpAmb", tp_amb)
    _sub(inf, "dhEmi", emitted.isoformat())
    _sub(inf, "verAplic", VER_APLIC)
    _sub(inf, "serie", series)
    _sub(inf, "nDPS", str(number))
    _sub(inf, "dCompet", competence.isoformat())
    _sub(inf, "tpEmit", "1")
    _sub(inf, "cLocEmi", issuer.city_code)
    substitution = payload.get("substitution")
    if substitution is not None:
        if not substitutes_official_key:
            raise NfseError("SUBSTITUTED_KEY_MISSING")
        reason = clean_text(substitution.get("reason"), 255)
        if len(reason) < 15:
            raise NfseError("SUBSTITUTION_REASON_INVALID")
        subst = _sub(inf, "subst")
        _sub(subst, "chSubstda", substitutes_official_key)
        _sub(subst, "cMotivo", SUBSTITUTION_REASON_OTHER)
        _sub(subst, "xMotivo", reason)
    prest = _sub(inf, "prest")
    _sub(prest, "CNPJ", issuer.cnpj)
    if issuer.municipal_registration:
        _sub(prest, "IM", issuer.municipal_registration)
    reg = _sub(prest, "regTrib")
    _sub(reg, "opSimpNac", "2")
    _sub(reg, "regEspTrib", "0")
    _recipient(inf, payload.get("recipient") or {})
    serv = _sub(inf, "serv")
    _sub(_sub(serv, "locPrest"), "cLocPrestacao", place)
    cserv = _sub(serv, "cServ")
    _sub(cserv, "cTribNac", tax_code)
    _sub(cserv, "xDescServ", description)
    nbs = service.get("nbs")
    if nbs:
        if not re.fullmatch(r"\d{9}", nbs):
            raise NfseError("SERVICE_NBS_INVALID")
        _sub(cserv, "cNBS", nbs)
    valores = _sub(inf, "valores")
    _sub(_sub(valores, "vServPrest"), "vServ", amount)
    trib = _sub(valores, "trib")
    trib_mun = _sub(trib, "tribMun")
    _sub(trib_mun, "tribISSQN", "1")
    _sub(trib_mun, "tpRetISSQN", "1")
    _sub(_sub(trib, "totTrib"), "indTotTrib", "0")
    return root


def build_cancel_event(
    *,
    official_key: str,
    issuer: Issuer,
    environment: str,
    reason: str,
    now: datetime,
) -> etree._Element:
    """Pedido de registro do evento 101101 (cancelamento), sem assinatura."""
    tp_amb = ENVIRONMENT_TPAMB.get(environment)
    if tp_amb is None:
        raise NfseError("ENVIRONMENT_INVALID")
    if not re.fullmatch(r"\d{50}", official_key or ""):
        raise NfseError("OFFICIAL_KEY_INVALID")
    text = clean_text(reason, 255)
    if len(text) < 15:
        raise NfseError("CANCEL_REASON_INVALID")
    root = etree.Element(
        f"{{{NS}}}pedRegEvento", nsmap={None: NS}, versao=LAYOUT_VERSION
    )
    inf = _sub(root, "infPedReg")
    inf.set("Id", f"PRE{official_key}{CANCEL_EVENT}")
    _sub(inf, "tpAmb", tp_amb)
    _sub(inf, "verAplic", VER_APLIC)
    _sub(inf, "dhEvento", now.astimezone(BR_OFFSET).replace(microsecond=0).isoformat())
    _sub(inf, "CNPJAutor", issuer.cnpj)
    _sub(inf, "chNFSe", official_key)
    event = _sub(inf, "e" + CANCEL_EVENT)
    _sub(event, "xDesc", "Cancelamento de NFS-e")
    _sub(event, "cMotivo", CANCEL_REASON_OTHER)
    _sub(event, "xMotivo", text)
    return root


# --- XSD ------------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _schema(version: str, name: str) -> etree.XMLSchema:
    if version not in READ_VERSIONS:
        raise NfseError("LAYOUT_VERSION_UNSUPPORTED")
    folder = XSD_DIR / version
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    parser.resolvers.add(_LocalXsdResolver(folder))
    path = folder / f"{name}_v{version}.xsd"
    doc = etree.parse(
        io.BytesIO(_fixed_xsd(path.read_bytes())), parser, base_url=path.as_uri()
    )
    return etree.XMLSchema(doc)


# O XSD oficial usa ``^…$`` em alguns padrões (ex.: ``TSSerieDPS``). Em regex de XML Schema,
# ``^`` e ``$`` são caracteres literais; o libxml2 segue a norma e recusaria toda série. O
# validador da SEFIN (.NET) trata como âncoras. A correção é feita só em memória, sem mudar
# os arquivos versionados (conferidos por SHA-256 em ``SHA256SUMS``).
_ANCHORED_PATTERN = re.compile(rb'(<xs:pattern\s+value=")\^([^"]*)\$(")')


def _fixed_xsd(data: bytes) -> bytes:
    return _ANCHORED_PATTERN.sub(rb"\1\2\3", data)


class _LocalXsdResolver(etree.Resolver):
    """``include``/``import`` só da pasta da versão (nunca da rede), com a correção acima."""

    def __init__(self, folder: Path):
        super().__init__()
        self._folder = folder.resolve()

    def resolve(self, url, pubid, context):
        name = Path(str(url).removeprefix("file://")).name
        path = (self._folder / name).resolve()
        if path.parent != self._folder or not path.is_file():
            raise NfseError("XSD_RESOLUTION_REFUSED")
        return self.resolve_string(
            _fixed_xsd(path.read_bytes()), context, base_url=path.as_uri()
        )


def schema_errors(element, name: str, version: str = LAYOUT_VERSION) -> list[str]:
    """Erros do XSD, só com linha e tipo (a mensagem do lxml pode citar o conteúdo)."""
    schema = _schema(version, name)
    if schema.validate(element):
        return []
    return [f"line {e.line}: {e.type_name}" for e in schema.error_log]


def validate(element, name: str, version: str = LAYOUT_VERSION) -> None:
    if schema_errors(element, name, version):
        raise NfseError(f"{name.upper()}_SCHEMA_INVALID")


# --- Assinatura -------------------------------------------------------------------------


def sign(element, credential) -> etree._Element:
    """XMLDSig envelopada (RSA-SHA256, C14N 1.0) sobre o primeiro filho (``infDPS``/``infPedReg``).

    A ``Signature`` fica como último filho da raiz, no namespace **padrão** do xmldsig: o Anexo I
    recusa prefixo de namespace na área de dados ("Uso de prefixo de namespace não permitido").
    Assina com o ``xmlsec`` (libxmlsec1, o mesmo do ``sefaz_engine``) sobre o XML **reparseado**:
    o C14N de uma árvore montada em memória pode sair diferente do que o autorizador calcula
    sobre o arquivo recebido. O ``signxml`` não gera assinatura válida sem prefixo.
    Devolve a árvore assinada (nova); a original não muda.
    """
    import xmlsec
    from cryptography.hazmat.primitives import serialization

    root = parse_xml(to_bytes(element))
    reference = root[0].get("Id") if len(root) else None
    if not reference or root.find(f"{{{DS_NS}}}Signature") is not None:
        raise NfseError("SIGNATURE_TARGET_INVALID")
    signature = xmlsec.template.create(
        root, xmlsec.Transform.C14N, xmlsec.Transform.RSA_SHA256, ns=None
    )
    root.append(signature)
    ref = xmlsec.template.add_reference(
        signature, xmlsec.Transform.SHA256, uri="#" + reference
    )
    xmlsec.template.add_transform(ref, xmlsec.Transform.ENVELOPED)
    xmlsec.template.add_transform(ref, xmlsec.Transform.C14N)
    x509_data = xmlsec.template.add_x509_data(
        xmlsec.template.ensure_key_info(signature)
    )
    xmlsec.template.x509_data_add_certificate(x509_data)
    key_pem = credential.private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    try:
        key = xmlsec.Key.from_memory(key_pem, xmlsec.KeyFormat.PEM, None)
    finally:
        del key_pem
    key.load_cert_from_memory(
        credential.certificate.public_bytes(serialization.Encoding.PEM),
        xmlsec.KeyFormat.PEM,
    )
    xmlsec.tree.add_ids(root, ["Id"])
    context = xmlsec.SignatureContext()
    context.key = key
    try:
        context.sign(signature)
    except xmlsec.Error:
        raise NfseError("SIGNATURE_FAILED") from None
    return root


def verify_signature(xml: bytes, certificate) -> bool:
    """Confere a assinatura com o certificado dado (testes e conferência local)."""
    import xmlsec
    from cryptography.hazmat.primitives import serialization

    root = parse_xml(xml)
    node = root.find(f"{{{DS_NS}}}Signature")
    if node is None:
        return False
    xmlsec.tree.add_ids(root, ["Id"])
    context = xmlsec.SignatureContext()
    context.key = xmlsec.Key.from_memory(
        certificate.public_bytes(serialization.Encoding.PEM),
        xmlsec.KeyFormat.CERT_PEM,
        None,
    )
    try:
        context.verify(node)
    except xmlsec.Error:
        return False
    return True


def to_bytes(element) -> bytes:
    return etree.tostring(element, xml_declaration=True, encoding="UTF-8")


# --- Empacotamento (GZip + Base64) ------------------------------------------------------


def pack(xml: bytes) -> str:
    return base64.b64encode(gzip.compress(xml, mtime=0)).decode("ascii")


def unpack(value: str | None) -> bytes:
    """Base64 de GZip vindo da SEFIN. Limite de tamanho: corpo maior é recusado."""
    if not value or not isinstance(value, str):
        raise NfseError("RESPONSE_XML_MISSING")
    try:
        compressed = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise NfseError("RESPONSE_XML_INVALID") from None
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as handle:
            data = handle.read(MAX_UNPACKED_BYTES + 1)
    except (OSError, EOFError):
        raise NfseError("RESPONSE_XML_INVALID") from None
    if len(data) > MAX_UNPACKED_BYTES:
        raise NfseError("RESPONSE_XML_TOO_LARGE")
    return data


def parse_xml(data: bytes) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
    )
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError:
        raise NfseError("RESPONSE_XML_INVALID") from None
    if root.getroottree().docinfo.doctype:
        raise NfseError("RESPONSE_XML_INVALID")  # sem DTD/entidades vindas de fora
    return root


# --- Leitura da NFS-e autorizada ----------------------------------------------------------


@dataclass(frozen=True)
class AuthorizedNfse:
    official_key: str
    nfse_number: str
    authorized_at: datetime
    xml: bytes

    def __repr__(self) -> str:
        return f"AuthorizedNfse(number={self.nfse_number})"


def _text(root, path: str) -> str | None:
    found = root.find(path, {"n": NS})
    return found.text.strip() if found is not None and found.text else None


def read_authorized(
    xml: bytes, *, expected_dps_id: str, issuer: Issuer, environment: str
) -> AuthorizedNfse:
    """Confere a NFS-e devolvida (XSD da versão dela, DPS, prestador e ambiente)."""
    root = parse_xml(xml)
    if root.tag != f"{{{NS}}}NFSe":
        raise NfseError("NFSE_XML_UNEXPECTED")
    version = root.get("versao")
    if version not in READ_VERSIONS or schema_errors(root, "NFSe", version):
        raise NfseError("NFSE_SCHEMA_INVALID")
    inf = root.find("n:infNFSe", {"n": NS})
    key = official_key_from_nfse_id(inf.get("Id"))
    dps = inf.find("n:DPS/n:infDPS", {"n": NS})
    if key is None or dps is None or dps.get("Id") != expected_dps_id:
        raise NfseError("NFSE_DPS_MISMATCH")
    if _text(dps, "n:prest/n:CNPJ") != issuer.cnpj:
        raise NfseError("NFSE_ISSUER_MISMATCH")
    if _text(dps, "n:tpAmb") != ENVIRONMENT_TPAMB.get(environment):
        raise NfseError("NFSE_ENVIRONMENT_MISMATCH")
    number = _text(inf, "n:nNFSe")
    processed = _text(inf, "n:dhProc")
    try:
        when = datetime.fromisoformat(processed).astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise NfseError("NFSE_SCHEMA_INVALID") from None
    return AuthorizedNfse(key, number, when, xml)


def read_cancel_event(xml: bytes, official_key: str) -> bytes:
    """Evento devolvido pela SEFIN: XML legível e vinculado à mesma chave."""
    root = parse_xml(xml)
    keys = {
        (el.text or "").strip() for el in root.iter(f"{{{NS}}}chNFSe")
    }
    if official_key not in keys:
        raise NfseError("EVENT_KEY_MISMATCH")
    return xml
