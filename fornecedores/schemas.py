import re

from pydantic import BaseModel, Field, field_validator


class SetupIn(BaseModel):
    nome: str = Field(min_length=2, max_length=120)
    login: str = Field(min_length=3, max_length=60, pattern=r"^[a-zA-Z0-9._-]+$")
    senha: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    login: str
    senha: str


class SupplierIn(BaseModel):
    nome: str = Field(min_length=2, max_length=160)
    documento: str = Field(default="", max_length=24)
    contato: str = Field(default="", max_length=140)
    telefone: str = Field(default="", max_length=30)
    email: str = Field(default="", max_length=180)
    chave_pix: str = Field(default="", max_length=180)
    endereco: str = Field(default="", max_length=1000)
    observacoes: str = Field(default="", max_length=2000)

    @field_validator("nome", "documento", "contato", "telefone", "email", "chave_pix", "endereco", "observacoes")
    @classmethod
    def trim(cls, value: str) -> str:
        return value.strip()

    @field_validator("email")
    @classmethod
    def email_format(cls, value: str) -> str:
        if value and not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", value):
            raise ValueError("Informe um e-mail válido")
        return value.lower()

