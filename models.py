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

class ServicoIn(BaseModel):
    id: str
    nome: str
    preco: float
    precoAnterior: float
    dataCriacao: Optional[str] = None
    tipo: Optional[str] = None

class PerfilIn(BaseModel):
    id: str
    url: str

class PdfVendaIn(BaseModel):
    id: str
    vendaId: str
    caminhoPdf: str
    data: Optional[str] = None
    dataGeracao: Optional[str] = None
    horaGeracao: Optional[str] = None


class PagamentoIn(BaseModel):
    id: str
    data: str
    valor: float
    motivo: Optional[str] = None

class DebitoClienteIn(BaseModel):
    id: str
    codigoCliente: str
    periodo: str
    valor: float
    situacao: str

