from pathlib import Path

import pytest
from pydantic import ValidationError

from fornecedores.api import _primary_credentials, passwords, router
from fornecedores.schemas import SetupIn, SupplierIn


def test_fornecedores_is_an_independent_api():
    paths = {route.path for route in router.routes}
    assert "/api/fornecedores" in paths
    assert "/api/fornecedores/cadastros/{supplier_id}" in paths
    assert "/api/fornecedores/auth/login" in paths
    assert "/api/fornecedores/auditoria/registros" in paths
    assert all("cpt_recursos" not in path for path in paths)


def test_supplier_requires_name_and_validates_email():
    with pytest.raises(ValidationError):
        SupplierIn(nome="", email="invalido")
    value = SupplierIn(nome="  Artes da Ana  ", email="ANA@EXEMPLO.COM")
    assert value.nome == "Artes da Ana"
    assert value.email == "ana@exemplo.com"


def test_independent_password_is_hashed():
    SetupIn(nome="Administrador", login="admin", senha="senha-segura")
    hashed = passwords.hash("senha-segura")
    assert passwords.verify("senha-segura", hashed)
    assert "senha-segura" not in hashed


def test_supplier_tables_do_not_reference_cpt_tables():
    sql = Path("fornecedores/migration.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS frn_fornecedor" in sql
    assert "CREATE TABLE IF NOT EXISTS frn_usuario" in sql
    assert "cpt_" not in sql


def test_main_exposes_separate_supplier_page():
    source = Path("main.py").read_text(encoding="utf-8")
    assert '@app.get("/fornecedores"' in source
    assert 'app.include_router(fornecedores_router)' in source


def test_supplier_app_uses_the_same_primary_login(monkeypatch):
    monkeypatch.delenv("PRIMARY_ADMIN_NAME", raising=False)
    monkeypatch.delenv("PRIMARY_ADMIN_LOGIN", raising=False)
    monkeypatch.delenv("PRIMARY_ADMIN_PASSWORD", raising=False)
    assert _primary_credentials() == ("Cleber Dev", "Cleber Dev", "02032002")
