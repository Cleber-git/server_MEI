from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class SetupIn(BaseModel):
    nome: str = Field(min_length=2, max_length=120)
    login: str = Field(min_length=3, max_length=60, pattern=r"^[a-zA-Z0-9._-]+$")
    senha: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    login: str
    senha: str


class CampaignIn(BaseModel):
    nome: str = Field(min_length=3, max_length=140)
    descricao: str = Field(default="", max_length=1000)
    chave_pix: str = Field(default="", max_length=180)
    ativa: bool = True


class ProductIn(BaseModel):
    nome: str = Field(min_length=2, max_length=140)
    descricao: str = Field(default="", max_length=500)
    preco: Decimal = Field(gt=0, decimal_places=2)
    ativo: bool = True


class OrderItemIn(BaseModel):
    produto_id: int
    quantidade: int = Field(ge=1, le=999)


class OrderIn(BaseModel):
    campanha_id: int
    cliente_nome: str = Field(min_length=2, max_length=140)
    data_coleta: date = Field(default_factory=date.today)
    observacoes: str = Field(default="", max_length=1000)
    itens: list[OrderItemIn] = Field(min_length=1, max_length=50)

    @field_validator("cliente_nome")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2:
            raise ValueError("Informe o nome do cliente")
        return value

    @field_validator("observacoes")
    @classmethod
    def clean_notes(cls, value: str) -> str:
        return value.strip()


class PaymentIn(BaseModel):
    status: Literal["pago", "pendente"]


class TeamMemberIn(BaseModel):
    nome: str = Field(min_length=2, max_length=140)
    tipo: Literal["oficial", "temporario"] = "oficial"
    inicio_em: date = Field(default_factory=date.today)
    fim_em: Optional[date] = None

    @model_validator(mode="after")
    def temporary_has_end(self):
        if self.tipo == "temporario" and self.fim_em is None:
            raise ValueError("Integrante temporário precisa de uma data final")
        return self


class ResponsibleIn(BaseModel):
    nome: str = Field(min_length=2, max_length=140)
    chave_pix: str = Field(default="", max_length=180)
