import io
import json
import logging

import pytest

from fiscal.logging import (
    JsonFormatter,
    RedactingFilter,
    configure_logging,
    correlation_id_var,
    get_logger,
    redact,
)

CNPJ_NUM = "11222333000181"
CNPJ_ALFA = "12ABC34501DE35"


@pytest.mark.parametrize(
    "raw,secret",
    [
        (f"empresa {CNPJ_NUM} falhou", CNPJ_NUM),
        ("empresa 11.222.333/0001-81 falhou", "11.222.333/0001-81"),
        (f"empresa {CNPJ_ALFA} falhou", CNPJ_ALFA),
        ("empresa 12.ABC.345/01DE-35 falhou", "12.ABC.345/01DE-35"),
        ("cpf 123.456.789-09 ok", "123.456.789-09"),
        ("cpf 12345678909 ok", "12345678909"),
        ("contato mei@example.com.br", "mei@example.com.br"),
        ("<?xml version='1.0'?><NFe><CNPJ>1</CNPJ></NFe>", "<NFe>"),
        ("resposta <retEnviNFe><xMotivo>ok</xMotivo></retEnviNFe> fim", "xMotivo"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl", "eyJhbGci"),
        ("senha=minhaSenha123 e mais", "minhaSenha123"),
        ('{"password": "p@ss w0rd"}', "p@ss w0rd"),
        ("csc_token: ABCDEF123456", "ABCDEF123456"),
        ("refresh " + "aB3" * 15, "aB3" * 15),
        ("pfx " + "QUJD" * 40, "QUJD" * 40),
        ("-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----", "MIIE"),
    ],
)
def test_redact_removes_sensitive_values(raw, secret):
    assert secret not in redact(raw)


def test_redact_keeps_operational_identifiers():
    corr = "8f0c1d2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f"
    sha = "a" * 64
    text = f"documento {corr} sha {sha} status AUTHORIZED tentativa 3"
    assert redact(text) == text


def _record(msg, *args, exc_info=None, **extra):
    record = logging.LogRecord(
        "fiscal.t", logging.ERROR, __file__, 1, msg, args, exc_info
    )
    record.__dict__.update(extra)
    return record


def test_filter_redacts_args_extras_and_traceback():
    try:
        raise ValueError(f"duplicate key (cnpj)=({CNPJ_NUM})")
    except ValueError:
        import sys

        record = _record(
            "falha para %s", CNPJ_NUM, exc_info=sys.exc_info(), titular=CNPJ_ALFA
        )
    RedactingFilter().filter(record)
    output = JsonFormatter().format(record)
    assert CNPJ_NUM not in output and CNPJ_ALFA not in output
    body = json.loads(output)
    assert body["message"] == "falha para [CNPJ]"
    assert body["titular"] == "[CNPJ]"
    assert "ValueError" in body["exception"]


def test_json_formatter_carries_correlation_id():
    token = correlation_id_var.set("8f0c1d2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f")
    try:
        record = _record("ok")
        RedactingFilter().filter(record)
        body = json.loads(JsonFormatter().format(record))
    finally:
        correlation_id_var.reset(token)
    assert body["correlationId"] == "8f0c1d2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f"
    assert body["level"] == "ERROR" and body["ts"].endswith("Z")


def test_configured_pipeline_never_emits_full_cnpj():
    configure_logging()
    handler = next(
        h for h in logging.getLogger("fiscal").handlers if h.get_name() == "fiscal-json"
    )
    stream = io.StringIO()
    previous = handler.setStream(stream)
    try:
        log = get_logger("fiscal.teste")
        log.warning("emitindo para %s e %s", CNPJ_NUM, "11.222.333/0001-81")
        log.info("titular", extra={"cnpj": CNPJ_ALFA, "email": "a@b.com"})
    finally:
        handler.setStream(previous)
    output = stream.getvalue()
    assert output.count("\n") == 2
    for secret in (CNPJ_NUM, "11.222.333/0001-81", CNPJ_ALFA, "a@b.com"):
        assert secret not in output


def test_uvicorn_loggers_get_redaction_filter():
    configure_logging()
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(self.format(record))

    target = logging.getLogger("uvicorn.error")
    handler = Capture()
    target.addHandler(handler)
    try:
        target.error("Exception in ASGI application para %s", CNPJ_NUM)
    finally:
        target.removeHandler(handler)
    assert captured and CNPJ_NUM not in captured[0]


def test_get_logger_always_lives_under_fiscal():
    assert get_logger("sefaz").name == "fiscal.sefaz"
    assert get_logger("fiscal.api").name == "fiscal.api"


def test_no_print_left_in_fiscal_package():
    from pathlib import Path

    offenders = [
        f"{path}:{number}"
        for path in Path("fiscal").glob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if line.lstrip().startswith("print(")
    ]
    assert offenders == []
