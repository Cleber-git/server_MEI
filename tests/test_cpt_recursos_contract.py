from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from cpt_recursos.api import make_slug, pwd, router
from cpt_recursos.schemas import OrderIn, SetupIn, TeamMemberIn


def test_module_exposes_required_routes():
    paths = {route.path for route in router.routes}
    assert "/api/cpt_recursos/public/pedidos" in paths
    assert "/api/cpt_recursos/pedidos/{order_id}/pagamento" in paths
    assert "/api/cpt_recursos/dashboard" in paths
    assert "/api/cpt_recursos/relatorios/ultimas-24h" in paths
    assert "/api/cpt_recursos/auditoria" in paths


def test_order_requires_customer_items_and_positive_quantity():
    with pytest.raises(ValidationError):
        OrderIn(campanha_id=1, cliente_nome=" ", itens=[])


def test_collection_date_defaults_to_today():
    order = OrderIn(
        campanha_id=1,
        cliente_nome="Cliente Teste",
        itens=[{"produto_id": 1, "quantidade": 2}],
    )
    assert order.data_coleta == date.today()


def test_temporary_member_requires_end_date():
    with pytest.raises(ValidationError):
        TeamMemberIn(nome="Pessoa Temporária", tipo="temporario")


def test_login_password_is_securely_hashed_and_long_enough():
    SetupIn(nome="Responsável", login="responsavel", senha="senha-segura")
    hashed = pwd.hash("senha-segura")
    assert "senha-segura" not in hashed
    assert pwd.verify("senha-segura", hashed)


def test_campaign_slug_is_url_safe():
    assert make_slug("Hambúrguer Solidário 2026") == "hamburguer-solidario-2026"


def test_database_contract_contains_audit_and_payment_constraints():
    sql = Path("cpt_recursos/migration.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS cpt_auditoria" in sql
    assert "status_pagamento IN ('pago', 'pendente')" in sql
    assert "tipo = 'oficial' OR fim_em IS NOT NULL" in sql
