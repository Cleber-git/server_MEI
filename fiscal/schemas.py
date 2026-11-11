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


class FiscalConfigUpdate(StrictModel):
    """PUT /config. ``productionEnabled`` e ``lastValidatedAt`` são somente leitura."""

    nfseEnvironment: Environment
    nfeEnvironment: Environment
    productionEnabled: bool = False
    provider: Literal["SEFIN_NACIONAL", "SEFAZ_NFCE", "SEFAZ_NFE"] | None = None
    lastValidatedAt: datetime | None = None


class FiscalCapabilitiesDto(BaseModel):
    uf: str
    version: str
    nfseAutomatic: bool = False
    nfce: bool = False
    nfe: bool = False
    nffOfficialChannel: bool = False
    requiresCertificate: bool = False
    reason: str | None = None
    # Sempre enviados (podem ser null): ausência nunca autoriza nada no app.
    nfseUnidentifiedRecipientAllowed: bool | None = None
    stateOfficialChannelUrl: str | None = None


# --- Estabelecimento (contrato seção 5) ----------------------------------------

Text200 = Annotated[str, Field(min_length=1, max_length=200)]


class EstablishmentAddress(StrictModel):
    postalCode: str | None = Field(default=None, pattern=r"^\d{8}$")
    street: str | None = Field(default=None, min_length=1, max_length=120)
    number: str | None = Field(default=None, min_length=1, max_length=20)
    complement: str | None = Field(default=None, max_length=60)
    district: str | None = Field(default=None, min_length=1, max_length=60)
    city: str | None = Field(default=None, min_length=1, max_length=60)


class EstablishmentMissing(BaseModel):
    documentType: DocumentType
    fields: list[str]


class FiscalEstablishmentDto(BaseModel):
    cnpj: str | None
    legalName: str | None
    tradeName: str | None
    stateRegistration: str | None
    municipalRegistration: str | None
    responsibleCpf: str | None
    cityCode: str | None
    uf: str | None
    address: EstablishmentAddress
    taxRegime: str
    simei: bool
    cnaes: list[str]
    fiscalEnabled: bool
    missingFields: list[EstablishmentMissing]


class FiscalEstablishmentUpdate(StrictModel):
    """PUT /establishment. CNPJ vem do cadastro da empresa e não muda por aqui:
    se vier, precisa ser o mesmo. ``fiscalEnabled``/``missingFields`` são ignorados
    (somente leitura), para o app poder devolver o que recebeu no GET."""

    cnpj: str | None = Field(default=None, max_length=18)
    legalName: Text200
    tradeName: str | None = Field(default=None, max_length=200)
    stateRegistration: str | None = Field(default=None, pattern=r"^\d{2,14}$")
    municipalRegistration: str | None = Field(
        default=None, pattern=r"^[0-9A-Za-z]{1,20}$"
    )
    responsibleCpf: str | None = Field(default=None, pattern=r"^\d{11}$")
    cityCode: str | None = Field(default=None, pattern=r"^\d{7}$")
    uf: str = Field(pattern=r"^[A-Z]{2}$")
    address: EstablishmentAddress = Field(default_factory=EstablishmentAddress)
    taxRegime: Literal["MEI"] = "MEI"
    simei: bool = True
    cnaes: list[Annotated[str, Field(pattern=r"^\d{7}$")]] = Field(
        default_factory=list, max_length=20
    )
    fiscalEnabled: bool | None = None
    missingFields: list[EstablishmentMissing] | None = None


# --- /capabilities/evaluate (contrato 4.2) --------------------------------------


class EvaluationAddress(StrictModel):
    countryCode: str = Field(pattern=r"^[A-Z]{2}$")
    postalCode: str = Field(min_length=1, max_length=20)
    street: str = Field(min_length=1, max_length=120)
    number: str = Field(min_length=1, max_length=20)
    complement: str | None = Field(default=None, max_length=60)
    district: str | None = Field(default=None, max_length=60)
    city: str = Field(min_length=1, max_length=60)
    cityCode: str | None = Field(default=None, pattern=r"^\d{7}$")
    uf: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")


class EvaluationOperation(StrictModel):
    purpose: Literal["SALE", "RETURN", "SHIPMENT", "TRANSFER", "OTHER"]
    establishmentUf: str = Field(pattern=r"^[A-Z]{2}$")
    destination: Literal["INTERNAL", "INTERSTATE"]
    recipientType: Literal["INDIVIDUAL", "LEGAL_ENTITY", "FOREIGN"]
    recipientIcmsStatus: Literal["TAXPAYER", "EXEMPT", "NON_TAXPAYER", "UNKNOWN"]
    consumerFinal: bool
    salesChannel: Literal["IN_PERSON", "DELIVERY", "ECOMMERCE"]


class EvaluationRecipient(StrictModel):
    type: Literal["INDIVIDUAL", "LEGAL_ENTITY", "FOREIGN"]
    taxId: str | None = Field(default=None, max_length=32)
    icmsStatus: Literal["TAXPAYER", "EXEMPT", "NON_TAXPAYER", "UNKNOWN"] | None = None
    stateRegistration: str | None = Field(default=None, max_length=20)
    countryCode: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    completeAddressProvided: bool
    address: EvaluationAddress | None = None


class EvaluationLogistics(StrictModel):
    mode: Literal["NO_TRANSPORT", "CUSTOMER_PICKUP", "DELIVERY"]
    transportMode: Literal[
        "NONE",
        "SELLER_CONTRACTED",
        "RECIPIENT_CONTRACTED",
        "SELLER_OWN",
        "RECIPIENT_OWN",
    ]


class EvaluateProductRequest(StrictModel):
    environment: Environment = Environment.RESTRICTED_PRODUCTION
    operation: EvaluationOperation
    recipient: EvaluationRecipient
    logistics: EvaluationLogistics


class EvaluationService(StrictModel):
    nationalTaxCode: str = Field(pattern=r"^\d{6}$")
    nbs: str | None = Field(default=None, pattern=r"^\d{9}$")
    emissionMode: Literal["SIMPLIFIED", "COMPLETE", "OFFICIAL_PORTAL", "BLOCKED_REVIEW"]
    description: str | None = Field(default=None, max_length=2000)
    amount: DecimalText | None = None
    withholdingResponsibility: Literal["RECIPIENT", "INTERMEDIARY"] | None = None
    incidenceMunicipalityCode: str | None = Field(default=None, pattern=r"^\d{7}$")


class EvaluateServiceRequest(StrictModel):
    documentType: Literal["NFSE"]
    environment: Environment = Environment.RESTRICTED_PRODUCTION
    service: EvaluationService


EvaluateRequest = Union[EvaluateServiceRequest, EvaluateProductRequest]


class CapabilityEvaluationDto(BaseModel):
    decision: Literal[
        "NFSE_AUTOMATIC",
        "NFCE_65",
        "NFE_55",
        "OFFICIAL_NFF",
        "STATE_NFAE",
        "CERTIFICATE_SETUP",
        "OFFICIAL_PORTAL",
        "UNAVAILABLE",
        "REQUIRES_FISCAL_REVIEW",
    ]
    capabilityId: str | None = None
    capabilityVersion: str | None = None
    validUntil: datetime | None = None
    allowedDocumentTypes: list[str] = Field(default_factory=list)
    requiresCertificate: bool = True
    requiresCsc: bool = False
    requiresStateRegistration: bool = False
    environment: Environment
    layoutVersion: str | None = None
    rulesetVersion: str | None = None
    reasons: list[str] = Field(default_factory=list)
    message: str
    productionEligible: bool = False


class CatalogMetadataDto(BaseModel):
    type: str
    source: str
    version: str
    publishedAt: datetime
    validUntil: datetime | None = None
    sha256: str


class CertificateStatusDto(BaseModel):
    configured: bool
    valid: bool
    certificateId: UUID | None = None
    holder: str | None = None
    expiresAt: datetime | None = None
    type: str | None = None
    lastValidatedAt: datetime | None = None
    holderTaxId: str | None = None


class CscRequest(StrictModel):
    """PUT /csc: vai só para o cofre; nunca é devolvido nem logado."""

    cscId: str = Field(pattern=r"^0*[1-9][0-9]{0,5}$", max_length=6)
    cscToken: str = Field(pattern=r"^[A-Za-z0-9]{16,36}$")


class CancelFiscalDocumentRequest(StrictModel):
    reason: str = Field(min_length=15, max_length=255)


class LoginRequest(StrictModel):
    """Contrato 3.1. ``login`` aceita e-mail ou CNPJ, como no ``/login`` legado."""

    login: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)
    deviceId: str | None = Field(default=None, min_length=1, max_length=128)


class RefreshRequest(StrictModel):
    refreshToken: str = Field(min_length=43, max_length=512)


class LogoutRequest(StrictModel):
    refreshToken: str | None = Field(default=None, min_length=43, max_length=512)
