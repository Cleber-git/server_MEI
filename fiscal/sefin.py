"""``SefinNacionalProvider`` (F8): NFS-e do MEI pela SEFIN Nacional, só em produção restrita.

Os 9 passos de ``docs/fiscal-backend-contract.md`` (app), nesta ordem:

1. Dados do prestador: ``DocumentContext.issuer`` (cadastro fiscal) e a credencial do cofre, já
   conferida pelo worker contra o titular do cadastro.
2. ``GET /parametros_municipais/{cMun}/convenio`` com cache curto. Para o MEI, a emissão pela
   SEFIN vale mesmo sem convênio (Anexo I, regra da data de competência), então o resultado só
   é registrado e **nunca bloqueia** a emissão.
3. Número da DPS: reservado pelo motor na criação (F7), nunca muda.
4. DPS montada (``fiscal.nfse``) e validada no XSD oficial 1.01.
5. XMLDSig.
6. GZip + Base64.
7. ``POST /nfse`` por mTLS.
8. Resultado ambíguo (timeout, 5xx, duplicidade E0014): ``GET /dps/{id}`` antes de reenviar.
9. A persistência (XML, DANFSe, chave e número) fica no worker.

Também faz o cancelamento (evento 101101 em ``POST /nfse/{chave}/eventos``) e a substituição
(grupo ``subst`` da DPS: a SEFIN cancela a original sozinha). O DANFSe é gerado aqui a partir
do XML autorizado (``fiscal.danfse``): a API de DANFSe do ADN foi suspensa em 03/08/2026
(NT 008 v1.02).

O ambiente fica **travado em produção restrita**: não há URL de produção neste módulo.

Nenhum log traz XML, CPF/CNPJ, chave completa ou corpo de resposta.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import httpx

from . import nfse
from .logging import get_logger
from .providers import DocumentContext, Outcome, OutcomeKind

log = get_logger("fiscal.sefin")

RESTRICTED_BASE_URL = "https://sefin.producaorestrita.nfse.gov.br/API/SefinNacional"
DUPLICATE_DPS_CODES = frozenset({"E0014"})  # série/número/município/CNPJ já usados
MUNICIPAL_PARAMS_TTL_SECONDS = 600
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _field(body, *names):
    """Campo do JSON da SEFIN sem depender de maiúsculas (``chaveAcesso``/``ChaveAcesso``)."""
    if not isinstance(body, dict):
        return None
    lowered = {str(k).lower(): v for k, v in body.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _json(response: httpx.Response):
    if len(response.content) > MAX_RESPONSE_BYTES:
        return None
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None


def _first_error(body) -> tuple[str | None, str | None]:
    """``(código, descrição)`` do primeiro erro. O ``Complemento`` pode ter dado do contribuinte."""
    errors = _field(body, "erros", "erro", "errors")
    if isinstance(errors, dict):
        errors = [errors]
    if not isinstance(errors, list) or not errors:
        return None, None
    first = errors[0] if isinstance(errors[0], dict) else {}
    code = _field(first, "codigo", "code")
    text = _field(first, "descricao", "mensagem", "message")
    return (str(code) if code else None), (str(text) if text else None)


def _error_codes(body) -> set[str]:
    errors = _field(body, "erros", "erro", "errors")
    if isinstance(errors, dict):
        errors = [errors]
    if not isinstance(errors, list):
        return set()
    return {str(_field(e, "codigo", "code")) for e in errors if isinstance(e, dict)}


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After", "")
    return int(value) if value.isdigit() else None


class SefinNacionalProvider:
    name = "sefin_nacional"
    supports_substitution = True
    needs_issuer = True  # o worker entrega cadastro + credencial no DocumentContext

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 30.0,
        cafile: str | None = None,
        clock=None,
        danfse_renderer=None,
    ):
        self._transport = transport
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds))
        self._cafile = cafile or None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._danfse = danfse_renderer
        self._params: dict[str, tuple[float, int]] = {}
        self._params_lock = threading.Lock()

    def __repr__(self) -> str:
        return "SefinNacionalProvider(RESTRICTED_PRODUCTION)"

    # --- infraestrutura ---------------------------------------------------------------

    def _client(self, doc: DocumentContext) -> httpx.Client:
        if self._transport is not None:
            return httpx.Client(
                base_url=RESTRICTED_BASE_URL,
                transport=self._transport,
                timeout=self._timeout,
            )
        from . import vault

        return httpx.Client(
            base_url=RESTRICTED_BASE_URL,
            verify=vault.mtls_context(doc.credential, cafile=self._cafile),
            timeout=self._timeout,
            follow_redirects=False,
        )

    def _blocked(self, doc: DocumentContext) -> Outcome | None:
        """Motivos para não falar com a SEFIN (nada é enviado)."""
        if doc.environment != "RESTRICTED_PRODUCTION":
            return Outcome(OutcomeKind.NOT_SENT, official_code="PRODUCTION_LOCKED")
        if doc.document_type != "NFSE":
            return Outcome(OutcomeKind.NOT_SENT, official_code="PROVIDER_UNAVAILABLE")
        if doc.credential is None:
            return Outcome(
                OutcomeKind.NOT_SENT,
                official_code=doc.credential_error or "CERTIFICATE_INVALID",
            )
        if doc.issuer is None:
            return Outcome(OutcomeKind.NOT_SENT, official_code="ESTABLISHMENT_INCOMPLETE")
        return None

    def _issuer(self, doc: DocumentContext) -> nfse.Issuer:
        return nfse.issuer_from_establishment(doc.issuer)

    def _request(self, client: httpx.Client, method: str, path: str, **kwargs):
        """``(response, None)`` ou ``(None, Outcome)`` para falha de rede.

        Falha na conexão (antes de o pedido sair) é ``TEMPORARY``; qualquer outra falha de
        transporte é ``TIMEOUT``, porque o pedido pode ter chegado.
        """
        try:
            return client.request(method, path, **kwargs), None
        except (httpx.ConnectError, httpx.ConnectTimeout):
            log.warning("sefin_connect_failed", extra={"operation": method})
            return None, Outcome(OutcomeKind.TEMPORARY, retry_after=30)
        except httpx.TransportError:
            log.warning("sefin_transport_failed", extra={"operation": method})
            return None, Outcome(OutcomeKind.TIMEOUT)

    def municipal_parameters(self, client: httpx.Client, city_code: str) -> int | None:
        """Passo 2: status HTTP do convênio do município, em cache por 10 min. Nunca bloqueia."""
        now = time.monotonic()
        with self._params_lock:
            hit = self._params.get(city_code)
            if hit and now - hit[0] < MUNICIPAL_PARAMS_TTL_SECONDS:
                return hit[1]
        response, failure = self._request(
            client, "GET", f"/parametros_municipais/{city_code}/convenio"
        )
        if failure is not None:
            return None  # sem cache: tenta de novo na próxima emissão
        status = response.status_code
        with self._params_lock:
            self._params[city_code] = (now, status)
        log.info("sefin_municipal_parameters", extra={"httpStatus": status})
        return status

    def _authorized(self, doc, issuer, dps_id: str, xml: bytes) -> Outcome:
        try:
            found = nfse.read_authorized(
                xml, expected_dps_id=dps_id, issuer=issuer, environment=doc.environment
            )
        except nfse.NfseError as error:
            # Sem evidência confiável não autoriza: o worker consulta de novo.
            log.warning("sefin_nfse_unreadable", extra={"reason": error.code})
            return Outcome(OutcomeKind.PROCESSING, retry_after=30)
        pdf = None
        renderer = self._danfse
        if renderer is None:
            from .danfse import render as renderer
        try:
            pdf = renderer(found.xml)
        except Exception:
            log.exception("danfse_render_failed")
        return Outcome(
            OutcomeKind.AUTHORIZED,
            official_key=found.official_key,
            protocol=found.nfse_number,
            authorized_at=found.authorized_at,
            xml=found.xml,
            auxiliary_pdf=pdf,
        )

    def _fetch_nfse(self, client, doc, issuer, dps_id: str, key: str) -> Outcome:
        response, failure = self._request(client, "GET", f"/nfse/{key}")
        if failure is not None:
            return failure
        if response.status_code == 200:
            try:
                xml = nfse.unpack(_field(_json(response), "nfseXmlGZipB64"))
            except nfse.NfseError:
                return Outcome(OutcomeKind.PROCESSING, retry_after=30)
            return self._authorized(doc, issuer, dps_id, xml)
        return Outcome(OutcomeKind.TIMEOUT, retry_after=_retry_after(response))

    def _query_dps(self, client, doc, issuer) -> Outcome:
        dps_id = nfse.dps_id(issuer, doc.series, doc.number)
        response, failure = self._request(client, "GET", f"/dps/{dps_id}")
        if failure is not None:
            return failure
        if response.status_code == 404:
            return Outcome(OutcomeKind.NOT_FOUND)
        if response.status_code == 200:
            key = _field(_json(response), "chaveAcesso")
            if isinstance(key, str) and len(key) == 50 and key.isdigit():
                return self._fetch_nfse(client, doc, issuer, dps_id, key)
            return Outcome(OutcomeKind.PROCESSING, retry_after=30)
        return Outcome(OutcomeKind.TIMEOUT, retry_after=_retry_after(response))

    # --- protocolo do worker ----------------------------------------------------------

    def submit(self, doc: DocumentContext) -> Outcome:
        blocked = self._blocked(doc)
        if blocked is not None:
            return blocked
        now = self._clock()
        try:
            issuer = self._issuer(doc)
            dps = nfse.build_dps(
                payload=doc.payload,
                issuer=issuer,
                environment=doc.environment,
                series=doc.series,
                number=doc.number,
                created_at=doc.created_at or now,
                now=now,
                substitutes_official_key=doc.substitutes_official_key,
            )
            nfse.validate(dps, "DPS")
            signed = nfse.sign(dps, doc.credential)
            nfse.validate(signed, "DPS")
            body = {"dpsXmlGZipB64": nfse.pack(nfse.to_bytes(signed))}
        except nfse.NfseError as error:
            log.warning("sefin_dps_not_built", extra={"reason": error.code})
            return Outcome(OutcomeKind.NOT_SENT, official_code=error.code)
        dps_id = signed[0].get("Id")
        with self._client(doc) as client:
            self.municipal_parameters(client, issuer.city_code)
            response, failure = self._request(client, "POST", "/nfse", json=body)
            if failure is not None:
                return failure
            status = response.status_code
            payload = _json(response)
            log.info("sefin_submit_response", extra={"httpStatus": status})
            if status in (200, 201):
                try:
                    xml = nfse.unpack(_field(payload, "nfseXmlGZipB64"))
                except nfse.NfseError:
                    return self._query_dps(client, doc, issuer)
                return self._authorized(doc, issuer, dps_id, xml)
            if status in (400, 422):
                if _error_codes(payload) & DUPLICATE_DPS_CODES:
                    # A SEFIN já tem esta DPS (envio anterior sem resposta): busca a NFS-e.
                    return self._query_dps(client, doc, issuer)
                code, message = _first_error(payload)
                if code is None:
                    return Outcome(OutcomeKind.TIMEOUT)
                return Outcome(
                    OutcomeKind.REJECTED, official_code=code, official_message=message
                )
            if status in (401, 403):
                # Recusado no mTLS/autorização: a DPS não foi processada.
                return Outcome(OutcomeKind.NOT_SENT, official_code="UPSTREAM_AUTH_REJECTED")
            # 429/5xx e qualquer outro: pode ter sido processado; o worker consulta.
            return Outcome(OutcomeKind.TIMEOUT, retry_after=_retry_after(response))

    def query(self, doc: DocumentContext) -> Outcome:
        blocked = self._blocked(doc)
        if blocked is not None:
            # Sem credencial não dá para consultar; nunca dizer "não chegou" sem saber.
            return Outcome(OutcomeKind.TIMEOUT)
        try:
            issuer = self._issuer(doc)
        except nfse.NfseError:
            return Outcome(OutcomeKind.TIMEOUT)
        with self._client(doc) as client:
            if doc.official_key:
                return self._query_cancel(client, doc)
            return self._query_dps(client, doc, issuer)

    def _query_cancel(self, client, doc) -> Outcome:
        """Documento já autorizado: cancelado se existe o evento 101101."""
        response, failure = self._request(
            client, "GET", f"/nfse/{doc.official_key}/eventos/{nfse.CANCEL_EVENT}"
        )
        if failure is not None:
            return failure
        if response.status_code == 404:
            return Outcome(OutcomeKind.AUTHORIZED, official_key=doc.official_key)
        if response.status_code == 200:
            return self._cancelled(_json(response), doc.official_key)
        return Outcome(OutcomeKind.TIMEOUT, retry_after=_retry_after(response))

    def _cancelled(self, payload, key: str) -> Outcome:
        event = _field(payload, "eventoXmlGZipB64")
        if event is None:
            items = _field(payload, "eventos", "itens", "items")
            if isinstance(items, list) and items:
                event = _field(items[0], "eventoXmlGZipB64", "arquivoXml")
        try:
            xml = nfse.read_cancel_event(nfse.unpack(event), key)
        except nfse.NfseError:
            return Outcome(OutcomeKind.TIMEOUT)
        return Outcome(OutcomeKind.CANCELLED, event_xml=xml)

    def cancel(self, doc: DocumentContext, reason: str) -> Outcome:
        blocked = self._blocked(doc)
        if blocked is not None:
            return blocked
        try:
            issuer = self._issuer(doc)
            event = nfse.build_cancel_event(
                official_key=doc.official_key or "",
                issuer=issuer,
                environment=doc.environment,
                reason=reason,
                now=self._clock(),
            )
            nfse.validate(event, "pedRegEvento")
            signed = nfse.sign(event, doc.credential)
            nfse.validate(signed, "pedRegEvento")
            body = {"pedidoRegistroEventoXmlGZipB64": nfse.pack(nfse.to_bytes(signed))}
        except nfse.NfseError as error:
            log.warning("sefin_event_not_built", extra={"reason": error.code})
            return Outcome(OutcomeKind.NOT_SENT, official_code=error.code)
        with self._client(doc) as client:
            response, failure = self._request(
                client, "POST", f"/nfse/{doc.official_key}/eventos", json=body
            )
            if failure is not None:
                return failure
            status = response.status_code
            payload = _json(response)
            log.info("sefin_cancel_response", extra={"httpStatus": status})
            if status in (200, 201):
                outcome = self._cancelled(payload, doc.official_key)
                if outcome.kind == OutcomeKind.CANCELLED:
                    return outcome
                return self._query_cancel(client, doc)
            if status in (400, 422):
                # Pode ser "evento já registrado": confirma antes de dizer que foi recusado.
                confirmed = self._query_cancel(client, doc)
                if confirmed.kind == OutcomeKind.CANCELLED:
                    return confirmed
                code, message = _first_error(payload)
                return Outcome(
                    OutcomeKind.CANCEL_REJECTED,
                    official_code=code or "CANCEL_REJECTED",
                    official_message=message,
                )
            if status in (401, 403):
                return Outcome(OutcomeKind.NOT_SENT, official_code="UPSTREAM_AUTH_REJECTED")
            return Outcome(OutcomeKind.TIMEOUT, retry_after=_retry_after(response))
