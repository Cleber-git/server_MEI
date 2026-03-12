from pydantic import BaseModel
from typing import Optional


class ClienteIn(BaseModel):
    id: str
    empresaUuid: str
    nome: str
    telefone: Optional[str] = None
    email: Optional[str] = None
    endereco: Optional[str] = None
    totalDebitos: str
    atualizadoEm : int
    pendenteSync : bool
    deletado: bool

class VendaIn(BaseModel):
    id: str
    empresaUuid: str
    formaPagamento: str
    valor: float
    data: str
    sincronizado : bool
    dataCadastro : int
    atualizadoEm : int
    deletado : bool
    
class ItemVendaIn(BaseModel):
    id: str
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
    dataCriacao: str = None
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
    vendaId: str
    empresaUuid: str
    caminhoPdf: str
    dataGeracao: int
    horaGeracao: Optional[str] = None

class PagamentoIn(BaseModel):
    id: str
    empresaUuid: str
    data: str
    valor: float
    motivo: Optional[str] = None
    atualizadoEm : int
    pendenteSync: bool
    deletado : bool

class DebitoClienteIn(BaseModel):
    id: str
    empresaUuid: str
    codigoCliente: str
    periodo: str
    valor: str
    situacao: str
    atualizadoEm: int
    pendenteSync: bool
    deletado: bool

class Empresa(BaseModel):
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
    dataInicioAssinatura: Optional[int] = None
    dataFimAssinatura: Optional[int] = None
    origemAssinatura: Optional[str] = None
    dataCadastro: Optional[int] = None
    dataAtualizacao: Optional[int] = None
    sincronizado: bool
    

class getEmpresa(BaseModel):
    nomeFantasia : str
    cnpj : str
    
class receiverEmail(BaseModel):
    email: str
    
class responseEmail(BaseModel):
    sucesso: bool
    mensagem: str

class loginIn(BaseModel):
    login :str
    senha: str

    
class Usuario(BaseModel):
    uuid : str
    email: str
    senhaHash: str
    nome: str
    empresaUuid: str
    ativo: bool
    dataCadastro: int
    ultimoLogin: Optional[int] = None
    
class loginResponse(BaseModel):
    sucesso:str
    mensagem:str
    usuario: Usuario
    empresa : Empresa
    
    
class ValidarEmailIn(BaseModel):
    email: str
    codigo: str
    
class ValidarSenha(BaseModel):
    email:str
    novaSenha: str
    
class VendaCompletaIn(BaseModel):
    venda: VendaIn
    itens: list[ItemVendaIn]
