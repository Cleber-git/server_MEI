"""Secure fiscal backend. External providers are disabled until homologated."""

from uuid import UUID as _UUID

from psycopg2.extensions import adapt as _adapt, register_adapter as _register_adapter

# psycopg2 não adapta uuid.UUID sozinho ("can't adapt type 'UUID'"); o código
# fiscal passa UUID em parâmetros. Só o adaptador de ida (como literal de texto,
# que o Postgres converte para a coluna): a leitura continua devolvendo str,
# então nada do legado muda de tipo.
_register_adapter(_UUID, lambda value: _adapt(str(value)))
