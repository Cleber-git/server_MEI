"""Senha de ``usuariomei``: bcrypt (novo) ou texto puro (legado), migrada no sucesso.

Vale para o ``POST /login`` legado e para o ``POST /api/v1/auth/login``. O banco
de produção ainda tem senhas em texto puro (``/usuarios`` e ``/redefinirSenha``
gravam o que recebem), então os dois formatos precisam ser aceitos para ninguém
ficar trancado fora. No primeiro login certo, a senha vira bcrypt.

O app não usa o ``senhaHash`` devolvido pelo servidor: o login offline compara
com um hash calculado no próprio aparelho (``Login.kt``).
"""

from __future__ import annotations

import hmac

from passlib.context import CryptContext

_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
# bcrypt só considera os primeiros 72 bytes. Uma senha legada maior continua em
# texto puro, para o hash não passar a aceitar qualquer sufixo.
BCRYPT_MAX_BYTES = 72
_dummy_hash: str | None = None


def hash_password(raw: str) -> str:
    return _ctx.hash(raw)


def is_bcrypt(stored: str | None) -> bool:
    return bool(stored) and _ctx.identify(stored, required=False) == "bcrypt"


def verify_password(raw: str, stored: str | None) -> tuple[bool, str | None]:
    """``(válida, novo_hash)``. ``novo_hash`` vem preenchido quando a senha deve migrar."""
    if is_bcrypt(stored):
        try:
            return _ctx.verify(raw, stored), None
        except ValueError:  # hash bcrypt corrompido
            return False, None
    if stored is None or not hmac.compare_digest(
        raw.encode("utf-8"), stored.encode("utf-8")
    ):
        return False, None
    if len(raw.encode("utf-8")) > BCRYPT_MAX_BYTES:
        return True, None
    return True, hash_password(raw)


def burn_verification(raw: str) -> None:
    """Gasta o mesmo tempo de um bcrypt quando o usuário não existe (sem oráculo de tempo)."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("caltech-mei-usuario-inexistente")
    _ctx.verify(raw, _dummy_hash)


def check_and_upgrade(cur, user_id: str, raw: str, stored: str | None) -> bool:
    """Confere a senha e, se ela for texto puro, grava o bcrypt no mesmo cursor.

    O ``UPDATE`` só vale se a senha guardada ainda for a mesma (CAS), para não
    sobrescrever uma redefinição concorrente. Quem chama faz o commit.
    """
    valid, new_hash = verify_password(raw, stored)
    if valid and new_hash:
        cur.execute(
            "UPDATE usuariomei SET senhahash=%s WHERE uuid=%s AND senhahash=%s",
            (new_hash, user_id, stored),
        )
    return valid
