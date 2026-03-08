from pydantic import BaseModel
from typing import Optional


class ClienteIn(BaseModel):
    id: str
    empresaUuid: str
    nome: str
    telefone: Optional[str] = None
    email: Optional[str] = None
    endereco: Optional[str] = None
    totalDebitos: float = 0.0


class VendaIn(BaseModel):
    id: str
    empresaUuid: str
    formaPagamento: str
    valor: float
    data: str


class ItemVendaIn(BaseModel):
    id: str
    empresaUuid: str
    vendaId: str
    tipo: str
    nome: str
    valor: float
    quantidade: int


class ServicoIn(BaseModel):
    id: str
    empresaUuid: str
    nome: str
    preco: float
    precoAnterior: float
    dataCriacao: Optional[str] = None
    tipo: Optional[str] = None
    pendenteSync: bool
    atualizadoEm: int
    deletado: bool


class PerfilIn(BaseModel):
    id: str
    empresaUuid: str
    url: str


class PdfVendaIn(BaseModel):
    id: str
    empresaUuid: str
    vendaId: str
    caminhoPdf: str
    data: Optional[str] = None
    dataGeracao: Optional[str] = None
    horaGeracao: Optional[str] = None


class PagamentoIn(BaseModel):
    id: str
    empresaUuid: str
    data: str
    valor: float
    motivo: Optional[str] = None


class DebitoClienteIn(BaseModel):
    id: str
    empresaUuid: str
    codigoCliente: str
    periodo: str
    valor: float
    situacao: str

class Empresa(BaseModel):
    id: str
    uuid: Optional[str] = None
    cnpj: Optional[str] = None
    razaoSocial: Optional[str] = None
    nomeFantasia: Optional[str] = None
    municipio: Optional[str] = None
    uf: Optional[str] = None
    cnae: Optional[str] = None
    ativo: Optional[bool] = None
    bloqueado: Optional[bool] = None
    motivoBloqueio: Optional[str] = None
    plano: Optional[str] = None
    statusAssinatura: Optional[str] = None
    dataInicioAssinatura: Optional[str] = None
    dataFimAssinatura: Optional[str] = None
    origemAssinatura: Optional[str] = None
    dataCadastro: Optional[str] = None
    dataAtualizacao: Optional[str] = None

class getEmpresa(BaseModel):
    nomeFantasia : str
    cnpj : str
    
class receiverEmail(BaseModel):
    email: str
    
class responseEmail(BaseModel):
    sucesso: bool
    mensagem: str
