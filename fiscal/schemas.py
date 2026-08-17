from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import DocumentType, Environment, FiscalStatus, fiscal_decimal


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


DecimalText = Annotated[str, Field(pattern=r"^(0|[1-9]\d{0,12})(\.\d{1,4})?$")]


class Recipient(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    taxId: str = Field(min_length=11, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    ibgeCityCode: str | None = Field(default=None, pattern=r"^\d{7}$")
    uf: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    stateRegistrationIndicator: str | None = None


class NfseItem(StrictModel):
    description: str = Field(min_length=1, max_length=2000)
    nationalServiceCode: str = Field(min_length=1, max_length=20)
    municipalTaxCode: str = Field(min_length=1, max_length=30)
    nbs: str | None = Field(default=None, max_length=20)
    incidenceCityCode: str = Field(pattern=r"^\d{7}$")
    serviceCityCode: str = Field(pattern=r"^\d{7}$")
    amount: DecimalText
    discount: DecimalText = "0.00"
    deductions: DecimalText = "0.00"
    issWithheld: bool = False
    additionalInformation: str | None = Field(default=None, max_length=2000)


class ProductItem(StrictModel):
    description: str = Field(min_length=1, max_length=120)
    ncm: str = Field(pattern=r"^\d{8}$")
    cest: str | None = Field(default=None, pattern=r"^\d{7}$")
    cfop: str = Field(pattern=r"^[1256]\d{3}$")
    origin: int = Field(ge=0, le=8)
    csosn: str | None = Field(default=None, pattern=r"^\d{3}$")
    cst: str | None = Field(default=None, pattern=r"^\d{2}$")
    commercialUnit: str = Field(min_length=1, max_length=6)
    taxableUnit: str = Field(min_length=1, max_length=6)
    gtin: str = Field(pattern=r"^(SEM GTIN|\d{8}|\d{12,14})$")
    quantity: DecimalText
    unitPrice: DecimalText
    total: DecimalText

    @model_validator(mode="after")
    def tax_code(self):
        if bool(self.csosn) == bool(self.cst):
            raise ValueError("informe exatamente um entre csosn e cst")
        return self


class Totals(StrictModel):
    items: DecimalText
    discount: DecimalText = "0.00"
    freight: DecimalText = "0.00"
    insurance: DecimalText = "0.00"
    other: DecimalText = "0.00"
    total: DecimalText

    @field_validator("items", "discount", "freight", "insurance", "other", "total")
    @classmethod
    def decimal_scale(cls, value: str):
        fiscal_decimal(value, 2)
        return value


class Payment(StrictModel):
    method: str = Field(min_length=2, max_length=2)
    amount: DecimalText


class NfseRequest(StrictModel):
    schemaVersion: Literal["1"]
    documentType: Literal[DocumentType.NFSE]
    environment: Environment
    saleId: str | None = Field(default=None, max_length=100)
    recipient: Recipient
    items: list[NfseItem] = Field(min_length=1, max_length=100)
    totals: Totals

    @model_validator(mode="after")
    def validate_totals(self):
        item_total = sum(
            (fiscal_decimal(item.amount, 2) for item in self.items),
            start=fiscal_decimal("0.00", 2),
        )
        expected = (
            item_total
            - fiscal_decimal(self.totals.discount, 2)
            + fiscal_decimal(self.totals.freight, 2)
            + fiscal_decimal(self.totals.insurance, 2)
            + fiscal_decimal(self.totals.other, 2)
        )
        if (
            fiscal_decimal(self.totals.items, 2) != item_total
            or fiscal_decimal(self.totals.total, 2) != expected
        ):
            raise ValueError("totais não conferem com os itens")
        return self


class ProductRequest(StrictModel):
    schemaVersion: Literal["1"]
    documentType: Literal[DocumentType.NFCE_65, DocumentType.NFE_55]
    environment: Environment
    saleId: str | None = Field(default=None, max_length=100)
    operationNature: str = Field(min_length=1, max_length=60)
    purpose: int = Field(ge=1, le=4)
    presenceIndicator: int = Field(ge=0, le=9)
    finalConsumer: bool
    recipient: Recipient
    items: list[ProductItem] = Field(min_length=1, max_length=990)
    totals: Totals
    payments: list[Payment] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_totals(self):
        item_total = sum(
            (fiscal_decimal(item.total, 2) for item in self.items),
            start=fiscal_decimal("0.00", 2),
        )
        expected = (
            item_total
            - fiscal_decimal(self.totals.discount, 2)
            + fiscal_decimal(self.totals.freight, 2)
            + fiscal_decimal(self.totals.insurance, 2)
            + fiscal_decimal(self.totals.other, 2)
        )
        paid = sum(
            (fiscal_decimal(p.amount, 2) for p in self.payments),
            start=fiscal_decimal("0.00", 2),
        )
        if (
            fiscal_decimal(self.totals.items, 2) != item_total
            or fiscal_decimal(self.totals.total, 2) != expected
            or paid < expected
        ):
            raise ValueError("totais ou pagamentos não conferem")
        return self


CreateFiscalDocumentRequest = Annotated[
    Union[NfseRequest, ProductRequest], Field(discriminator="documentType")
]


class FiscalDocumentDto(BaseModel):
    id: UUID
    documentType: DocumentType
    environment: Environment
    status: FiscalStatus
    series: str | None = None
    number: int | None = None
    officialKey: str | None = None
    protocol: str | None = None
    authorizedAt: datetime | None = None
    createdAt: datetime
    updatedAt: datetime
    fiscalErrorCode: str | None = None
    fiscalErrorMessage: str | None = None
    xmlSha256: str | None = None
    xmlArtifactId: UUID | None = None
    auxiliaryArtifactId: UUID | None = None
    correlationId: UUID
    attempt: int


class FiscalConfigDto(BaseModel):
    nfseEnvironment: Environment
    nfeEnvironment: Environment
    productionEnabled: bool
    provider: str | None
    lastValidatedAt: datetime | None


class FiscalCapabilitiesDto(BaseModel):
    uf: str
    version: str
    nfseAutomatic: bool = False
    nfce: bool = False
    nfe: bool = False
    nffOfficialChannel: bool = False
    requiresCertificate: bool = False
    reason: str | None = None


class CertificateStatusDto(BaseModel):
    configured: bool
    valid: bool
    certificateId: UUID | None = None
    holder: str | None = None
    expiresAt: datetime | None = None
    type: str | None = None
    lastValidatedAt: datetime | None = None


class CancelFiscalDocumentRequest(StrictModel):
    reason: str = Field(min_length=15, max_length=255)


class RefreshRequest(StrictModel):
    refreshToken: str = Field(min_length=43, max_length=512)


class LogoutRequest(StrictModel):
    refreshToken: str | None = Field(default=None, min_length=43, max_length=512)
