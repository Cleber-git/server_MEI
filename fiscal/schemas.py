from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain import DocumentType, Environment, FiscalStatus


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


DecimalText = Annotated[str, Field(pattern=r"^(0|[1-9]\d{0,12})(\.\d{1,4})?$")]

# --- POST /documents (contrato 4.4), espelho de app/.../Fiscal/FiscalApi.kt ----------
# O Gson do app omite campos nulos: todo campo anulável precisa de default None.

Money = Annotated[str, Field(pattern=r"^(0|[1-9]\d{0,12})\.\d{2}$")]
Quantity = Annotated[str, Field(pattern=r"^(0|[1-9]\d{0,8})(\.\d{1,4})?$")]
UnitPrice = Annotated[str, Field(pattern=r"^(0|[1-9]\d{0,12})(\.\d{1,10})?$")]
PaymentMethod = Literal[
    "CASH",
    "CREDIT_CARD",
    "DEBIT_CARD",
    "PIX",
    "STORE_CREDIT",
    "FOOD_VOUCHER",
    "MEAL_VOUCHER",
    "OTHER",
]
RecipientType = Literal["INDIVIDUAL", "LEGAL_ENTITY", "FOREIGN"]
RequestDocumentType = Literal["NFSE", "NFCE_65", "NFE_55", "OFFICIAL_NFF"]


class FiscalTotals(StrictModel):
    products: Money
    discount: Money
    freight: Money
    other: Money
    invoice: Money
    paid: Money
    change: Money


class FiscalPayment(StrictModel):
    method: PaymentMethod
    amount: Money


class ProductClassification(StrictModel):
    source: Literal[
        "PURCHASE_XML", "OFFICIAL_SEARCH", "USER_CONFIRMED", "PROFESSIONAL_REVIEW"
    ]
    tableVersion: str = Field(min_length=1, max_length=40)
    tablePublishedAt: int = Field(ge=0)
    confirmedAt: int = Field(ge=0)
    validUntil: int | None = Field(default=None, ge=0)


class FiscalItem(StrictModel):
    productId: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=120)
    gtin: str | None = Field(default=None, pattern=r"^(SEM GTIN|\d{8}|\d{12,14})$")
    ncm: str = Field(pattern=r"^\d{8}$")
    classification: ProductClassification | None = None
    productSource: Literal["OWN_PRODUCTION", "RESALE"] | None = None
    cest: str | None = Field(default=None, pattern=r"^\d{7}$")
    cfop: str = Field(pattern=r"^\d{4}$")
    unit: Literal["UN", "KG", "L", "M", "PCT", "CX"]
    quantity: Quantity
    unitPrice: UnitPrice
    grossAmount: Money
    discountAmount: Money
    netAmount: Money
    origin: str = Field(pattern=r"^[0-8]$")
    csosn: str = Field(pattern=r"^\d{3}$")


class FiscalRecipient(StrictModel):
    taxId: str | None = Field(default=None, pattern=r"^[0-9A-Za-z]{1,32}$")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    stateRegistration: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=254)
    type: RecipientType | None = None
    foreignTaxId: str | None = Field(default=None, max_length=40)
    countryCode: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")


class FiscalService(StrictModel):
    nationalTaxCode: str = Field(pattern=r"^\d{6}$")
    nbs: str | None = Field(default=None, pattern=r"^\d{9}$")
    emissionMode: Literal["SIMPLIFIED", "COMPLETE", "OFFICIAL_PORTAL", "BLOCKED_REVIEW"]
    description: str = Field(min_length=1, max_length=2000)
    amount: Money
    withholdingResponsibility: Literal["RECIPIENT", "INTERMEDIARY"] | None = None
    incidenceMunicipalityCode: str | None = Field(default=None, pattern=r"^\d{7}$")


class ProductLogistics(StrictModel):
    mode: Literal["NO_TRANSPORT", "CUSTOMER_PICKUP", "DELIVERY"]
    transportMode: Literal[
        "NONE",
        "SELLER_CONTRACTED",
        "RECIPIENT_CONTRACTED",
        "SELLER_OWN",
        "RECIPIENT_OWN",
    ]


class LegacyProductOperation(StrictModel):
    """``ProductOperationDto`` (schema 1.1): o servidor só aceita parsear."""

    consumerFinal: bool
    presencial: bool
    interstate: bool
    recipientIsIcmsTaxpayer: bool
    delivery: bool
    foreignTrade: bool
    logistics: ProductLogistics | None = None


class ProductRoute(StrictModel):
    document: RequestDocumentType
    reason: str = Field(min_length=1, max_length=60)
    capabilityId: str = Field(min_length=1, max_length=100)
    capabilityVersion: str = Field(min_length=1, max_length=60)


class ProductFiscalIntent(StrictModel):
    contractVersion: Literal["2.3"]
    route: ProductRoute
    operation: "EvaluationOperation"
    recipient: "EvaluationRecipient"
    logistics: ProductLogistics
    paymentExpectation: Literal["PAID", "NO_PAYMENT"]
    items: list[FiscalItem] = Field(min_length=1, max_length=990)
    payments: list[FiscalPayment] = Field(default_factory=list, max_length=20)
    totals: FiscalTotals


class CreateFiscalDocumentRequest(StrictModel):
    """Contrato 4.4: 1.2 = NFS-e, 1.1 = produto legado (só parse), 2.3 = produto roteado."""

    schemaVersion: Literal["1.1", "1.2", "2.3"]
    documentType: RequestDocumentType
    environment: Environment
    saleId: str | None = Field(default=None, min_length=1, max_length=100)
    operation: LegacyProductOperation | None = None
    product: ProductFiscalIntent | None = None
    service: FiscalService | None = None
    recipient: FiscalRecipient | None = None
    items: list[FiscalItem] = Field(default_factory=list, max_length=990)
    payments: list[FiscalPayment] = Field(default_factory=list, max_length=20)
    totals: FiscalTotals

    @model_validator(mode="after")
    def shape(self):
        if self.documentType == "OFFICIAL_NFF":
            return self  # recusado pela API com CAPABILITY_UNAVAILABLE
        version, kind = self.schemaVersion, self.documentType
        if version == "1.2":
            ok = (
                kind == "NFSE"
                and self.service is not None
                and self.operation is None
                and self.product is None
                and not self.items
                and not self.payments
            )
        elif version == "1.1":
            ok = (
                kind in ("NFCE_65", "NFE_55")
                and self.operation is not None
                and self.product is None
                and self.service is None
                and bool(self.items)
            )
        else:
            ok = (
                kind in ("NFCE_65", "NFE_55")
                and self.product is not None
                and self.product.route.document == kind
                and self.operation is None
                and self.service is None
                and not self.items
                and not self.payments
                and self.totals == self.product.totals
            )
        if not ok:
            raise ValueError("corpo incompatível com schemaVersion e documentType")
        return self


class CancelFiscalDocumentRequest(StrictModel):
    reason: str = Field(min_length=15, max_length=255)


class SubstituteFiscalDocumentRequest(StrictModel):
    reason: str = Field(min_length=15, max_length=255)
    replacement: CreateFiscalDocumentRequest


class FiscalDocumentDto(BaseModel):
    id: UUID
    documentType: DocumentType
    environment: Environment
    status: FiscalStatus
    series: str | None = None
    # String no app (FiscalApi.kt): número sempre como texto.
    number: str | None = None
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
    # Aditivos opcionais (contrato 4.4).
    retryAfter: int | None = None
    etag: str | None = None


class FiscalDocumentPageDto(BaseModel):
    items: list[FiscalDocumentDto]
    nextCursor: str | None = None


class FiscalEventDto(BaseModel):
    id: UUID
    type: str
    fromStatus: FiscalStatus | None = None
    toStatus: FiscalStatus | None = None
    occurredAt: datetime
    correlationId: UUID | None = None
    officialCode: str | None = None
    message: str | None = None


class FiscalEventPageDto(BaseModel):
    items: list[FiscalEventDto]


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


class LoginRequest(StrictModel):
    """Contrato 3.1. ``login`` aceita e-mail ou CNPJ, como no ``/login`` legado."""

    login: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)
    deviceId: str | None = Field(default=None, min_length=1, max_length=128)


class RefreshRequest(StrictModel):
    refreshToken: str = Field(min_length=43, max_length=512)


class LogoutRequest(StrictModel):
    refreshToken: str | None = Field(default=None, min_length=43, max_length=512)


ProductFiscalIntent.model_rebuild()
CreateFiscalDocumentRequest.model_rebuild()
SubstituteFiscalDocumentRequest.model_rebuild()
