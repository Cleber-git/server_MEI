"""SEFIN Nacional falsa para os testes da F8 (``httpx.MockTransport``, sem rede).

Recebe a DPS de verdade (GZip+Base64), confere XSD e assinatura com o certificado esperado e
devolve uma NFS-e **sintética** no leiaute 1.01 (assinada por uma chave de "município" de
teste). Os corpos JSON seguem os nomes usados pela SEFIN (``dpsXmlGZipB64``,
``nfseXmlGZipB64``, ``chaveAcesso``, ``erros[].Codigo/Descricao/Complemento``), que não
puderam ser conferidos no Swagger oficial (exige certificado; ver ``FISCAL_BACKEND.md``).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree

from fiscal import nfse

FIXTURES = Path(__file__).parent / "fixtures" / "nfse"
N = {"n": nfse.NS}


def municipality_credential():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MUNICIPIO TESTE")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    return SimpleNamespace(private_key=key, certificate=cert)


def _sub(parent, tag, text=None):
    element = etree.SubElement(parent, f"{{{nfse.NS}}}{tag}")
    if text is not None:
        element.text = text
    return element


def build_nfse(dps_xml: bytes, signer, *, nfse_number: int = 1, processed=None) -> bytes:
    """NFS-e 1.01 sintética que embute a DPS recebida (como a SEFIN faz)."""
    dps = nfse.parse_xml(dps_xml)
    inf_dps = dps.find("n:infDPS", N)
    city = inf_dps.findtext("n:cLocEmi", namespaces=N)
    cnpj = inf_dps.findtext("n:prest/n:CNPJ", namespaces=N)
    processed = processed or datetime.now(timezone(timedelta(hours=-3))).replace(
        microsecond=0
    )
    key = (
        f"{city}2" + "2" + cnpj + f"{nfse_number:013d}" + processed.strftime("%y%m")
        + "123456789" + "7"
    )
    assert len(key) == 50
    root = etree.Element(f"{{{nfse.NS}}}NFSe", nsmap={None: nfse.NS}, versao="1.01")
    inf = _sub(root, "infNFSe")
    inf.set("Id", "NFS" + key)
    _sub(inf, "xLocEmi", "São Paulo")
    _sub(inf, "xLocPrestacao", "São Paulo")
    _sub(inf, "nNFSe", str(nfse_number))
    _sub(inf, "cLocIncid", city)
    _sub(inf, "xLocIncid", "São Paulo")
    _sub(inf, "xTribNac", "Análise e desenvolvimento de sistemas.")
    _sub(inf, "verAplic", "SefinNac_Pre_1.0")
    _sub(inf, "ambGer", "2")
    _sub(inf, "tpEmis", "1")
    _sub(inf, "procEmi", "1")
    _sub(inf, "cStat", "107")
    _sub(inf, "dhProc", processed.isoformat())
    _sub(inf, "nDFSe", str(nfse_number))
    emit = _sub(inf, "emit")
    _sub(emit, "CNPJ", cnpj)
    _sub(emit, "xNome", "EMPRESA TESTE MEI")
    ender = _sub(emit, "enderNac")
    _sub(ender, "xLgr", "Praça da Sé")
    _sub(ender, "nro", "100")
    _sub(ender, "xBairro", "Sé")
    _sub(ender, "cMun", city)
    _sub(ender, "UF", "SP")
    _sub(ender, "CEP", "01001000")
    valores = _sub(inf, "valores")
    _sub(valores, "vLiq", inf_dps.findtext("n:valores/n:vServPrest/n:vServ", namespaces=N))
    inf.append(dps)
    signed = nfse.sign(root, signer)
    errors = nfse.schema_errors(signed, "NFSe")
    assert not errors, errors
    return nfse.to_bytes(signed)


def fixture_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass
class FakeSefin:
    """Roteiro por operação: comportamentos consumidos em ordem.

    submit: ``authorize`` (padrão), ``reject``, ``duplicate``, ``timeout_after_receipt``,
    ``connect_error``, ``http_503``, ``http_403``, ``bad_nfse``.
    cancel: ``cancel`` (padrão), ``cancel_reject``, ``already_cancelled``.
    """

    expected_certificate: object = None
    script: dict[str, list[str]] = field(default_factory=dict)
    received: dict[str, bytes] = field(default_factory=dict)  # dpsId -> DPS
    nfse_by_key: dict[str, bytes] = field(default_factory=dict)
    key_by_dps: dict[str, str] = field(default_factory=dict)
    events: dict[str, bytes] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    signer: object = field(default_factory=municipality_credential)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def count(self, method: str, prefix: str = "") -> int:
        return sum(1 for m, p in self.requests if m == method and p.startswith(prefix))

    def _next(self, operation, default):
        queue = self.script.get(operation) or []
        return queue.pop(0) if queue else default

    def _path(self, request: httpx.Request) -> str:
        return request.url.path.removeprefix("/API/SefinNacional")

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            path = self._path(request)
            self.requests.append((request.method, path))
            if path.startswith("/parametros_municipais/"):
                return httpx.Response(200, json={"codigoMunicipio": path.split("/")[2]})
            if request.method == "POST" and path == "/nfse":
                return self._submit(request)
            if request.method == "GET" and path.startswith("/dps/"):
                key = self.key_by_dps.get(path.removeprefix("/dps/"))
                if key is None:
                    return httpx.Response(404, json={"erro": "DPS não encontrada"})
                return httpx.Response(200, json={"idDps": path[5:], "chaveAcesso": key})
            if request.method == "POST" and path.endswith("/eventos"):
                return self._cancel(request, path.split("/")[2])
            if request.method == "GET" and "/eventos/" in path:
                key = path.split("/")[2]
                if key not in self.events:
                    return httpx.Response(404, json={})
                return httpx.Response(
                    200, json={"eventoXmlGZipB64": nfse.pack(self.events[key])}
                )
            if request.method == "GET" and path.startswith("/nfse/"):
                key = path.removeprefix("/nfse/")
                if key not in self.nfse_by_key:
                    return httpx.Response(404, json={})
                return httpx.Response(
                    200,
                    json={"chaveAcesso": key, "nfseXmlGZipB64": nfse.pack(self.nfse_by_key[key])},
                )
            return httpx.Response(404, json={})

    def _accept(self, dps_xml: bytes) -> str:
        dps_id = nfse.parse_xml(dps_xml).find("n:infDPS", N).get("Id")
        if self.expected_certificate is not None:
            assert nfse.verify_signature(dps_xml, self.expected_certificate)
        assert not nfse.schema_errors(nfse.parse_xml(dps_xml), "DPS")
        xml = build_nfse(dps_xml, self.signer, nfse_number=len(self.received) + 1)
        key = nfse.official_key_from_nfse_id(
            nfse.parse_xml(xml).find("n:infNFSe", N).get("Id")
        )
        self.received[dps_id] = dps_xml
        self.nfse_by_key[key] = xml
        self.key_by_dps[dps_id] = key
        return dps_id

    def _submit(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        dps_xml = nfse.unpack(body["dpsXmlGZipB64"])
        dps_id = nfse.parse_xml(dps_xml).find("n:infDPS", N).get("Id")
        behavior = self._next("submit", "authorize")
        if behavior == "connect_error":
            raise httpx.ConnectError("conexão recusada", request=request)
        if behavior == "http_403":
            return httpx.Response(403, json={})
        if behavior == "reject":
            return httpx.Response(400, json=fixture_json("post_nfse_rejeicao.json"))
        if dps_id in self.received or behavior == "duplicate":
            if dps_id not in self.received:
                self._accept(dps_xml)
            return httpx.Response(400, json=fixture_json("post_nfse_duplicidade.json"))
        self._accept(dps_xml)
        if behavior == "timeout_after_receipt":
            raise httpx.ReadTimeout("sem resposta", request=request)
        if behavior == "http_503":
            return httpx.Response(503, headers={"Retry-After": "7"})
        xml = self.nfse_by_key[self.key_by_dps[dps_id]]
        if behavior == "bad_nfse":
            xml = xml.replace(dps_id.encode(), b"DPS" + b"9" * 42)
        return httpx.Response(
            201,
            json={
                "tipoAmbiente": 2,
                "versaoAplicativo": "SefinNac_Pre_1.0",
                "dataHoraProcessamento": "2026-11-12T10:00:00-03:00",
                "idDps": dps_id,
                "chaveAcesso": self.key_by_dps[dps_id],
                "nfseXmlGZipB64": nfse.pack(xml),
                "alertas": [],
            },
        )

    def _cancel(self, request: httpx.Request, key: str) -> httpx.Response:
        body = json.loads(request.content)
        event = nfse.unpack(body["pedidoRegistroEventoXmlGZipB64"])
        assert not nfse.schema_errors(nfse.parse_xml(event), "pedRegEvento")
        if self.expected_certificate is not None:
            assert nfse.verify_signature(event, self.expected_certificate)
        behavior = self._next("cancel", "cancel")
        if behavior == "already_cancelled":
            self.events[key] = self._event_xml(key)
            return httpx.Response(400, json=fixture_json("post_evento_rejeicao.json"))
        if behavior == "cancel_reject" or key not in self.nfse_by_key:
            return httpx.Response(400, json=fixture_json("post_evento_rejeicao.json"))
        self.events[key] = self._event_xml(key)
        return httpx.Response(201, json={"eventoXmlGZipB64": nfse.pack(self.events[key])})

    @staticmethod
    def _event_xml(key: str) -> bytes:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<evento xmlns="{nfse.NS}" versao="1.01"><infEvento Id="EVT{key}1011010001">'
            f"<chNFSe>{key}</chNFSe><tpEvento>101101</tpEvento></infEvento></evento>"
        ).encode()
