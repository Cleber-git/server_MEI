from pydantic import BaseModel
from typing import Optional


class ClienteIn(BaseModel):
    id: str
    nome: str
    telefone: Optional[str] = None
    email: Optional[str] = None
    endereco: Optional[str] = None
    totalDebitos: float = 0.0


class VendaIn(BaseModel):
    id: str
    formaPagamento: str
    valor: float
    data: str


class ItemVendaIn(BaseModel):
    id: str
    vendaId: str
    tipo: str
    nome: str
    valor: float
    quantidade: int
