from lxml import etree
from signxml import XMLSigner, methods, XMLVerifier
from signxml.algorithms import CanonicalizationMethod
import xmlsec
from cryptography.hazmat.primitives.serialization import (
    pkcs12,
    Encoding,
    PrivateFormat,
    NoEncryption
)
from cryptography.hazmat.primitives.serialization.pkcs7 import load_der_pkcs7_certificates
import base64
import hashlib
import os
import re
import requests
import tempfile
import time
import warnings
from cryptography.hazmat.primitives import hashes, serialization

from cryptography import x509
from cryptography.x509.oid import ExtensionOID, AuthorityInformationAccessOID
from base64 import b64decode
from cryptography.hazmat.primitives.asymmetric import padding

from signxml.verifier import SignatureConfiguration
from signxml.algorithms import SignatureMethod, DigestAlgorithm
import certifi
from lxml import etree
import hashlib, base64
from copy import deepcopy




UF_CONFIG = {
    "SP": {
        "cUF": "35",
        "producao": {
            "autorizacao": "https://nfce.fazenda.sp.gov.br/ws/NFeAutorizacao4.asmx",
            "qrcode": "https://www.nfce.fazenda.sp.gov.br/qrcode",
            "consulta": "https://www.nfce.fazenda.sp.gov.br/consulta",
        },
        "homologacao": {
            "autorizacao": "https://homologacao.nfce.fazenda.sp.gov.br/ws/NFeAutorizacao4.asmx",
            "qrcode": "https://www.homologacao.nfce.fazenda.sp.gov.br/qrcode",
            "consulta": "https://www.homologacao.nfce.fazenda.sp.gov.br/consulta",
        },
    },
    "RS": {
        "cUF": "43",
        "producao": {
            "autorizacao": "https://nfce.svrs.rs.gov.br/ws/NfeAutorizacao/NFeAutorizacao4.asmx",
            "qrcode": "https://www.sefaz.rs.gov.br/NFCE/NFCE-COM.aspx",
            "consulta": "https://www.sefaz.rs.gov.br/NFCE/NFCE-COM.aspx",
        },
        "homologacao": {
            "autorizacao": "https://nfce-homologacao.svrs.rs.gov.br/ws/NfeAutorizacao/NFeAutorizacao4.asmx",
            "qrcode": "https://www.sefaz.rs.gov.br/NFCE/NFCE-COM.aspx",
            "consulta": "https://www.sefaz.rs.gov.br/NFCE/NFCE-COM.aspx",
        },
    },
    "BA": {
        "cUF": "29",
        "producao": {
            "autorizacao": "https://nfce.svrs.rs.gov.br/ws/NfeAutorizacao/NFeAutorizacao4.asmx",
            "qrcode": "http://nfe.sefaz.ba.gov.br/servicos/nfce/qrcode.aspx",
            "consulta": "http://internet.sefaz.ba.gov.br/nfce/consulta",
        },
        "homologacao": {
            "autorizacao": "https://nfce-homologacao.svrs.rs.gov.br/ws/NfeAutorizacao/NFeAutorizacao4.asmx",
            "qrcode": "http://hnfe.sefaz.ba.gov.br/servicos/nfce/qrcode.aspx",
            "consulta": "http://hinternet.sefaz.ba.gov.br/nfce/consulta",
        },
    },
}

# NF-e modelo 55 (autorização síncrona via SVRS para UFs que utilizam o
# Ambiente Nacional/SVRS, incluindo a Bahia).
UF_CONFIG_NFE = {
    "BA": {
        "cUF": "29",
        "producao": "https://nfe.sefaz.ba.gov.br/webservices/NFeAutorizacao4/NFeAutorizacao4.asmx",
        "homologacao": "https://hnfe.sefaz.ba.gov.br/webservices/NFeAutorizacao4/NFeAutorizacao4.asmx",
    },
    "RS": {
        "cUF": "43",
        "producao": "https://nfe.svrs.rs.gov.br/ws/NfeAutorizacao/NFeAutorizacao4.asmx",
        "homologacao": "https://nfe-homologacao.svrs.rs.gov.br/ws/NfeAutorizacao/NFeAutorizacao4.asmx",
    },
    "SP": {
        "cUF": "35",
        "producao": "https://nfe.fazenda.sp.gov.br/ws/NFeAutorizacao4.asmx",
        "homologacao": "https://homologacao.nfe.fazenda.sp.gov.br/ws/NFeAutorizacao4.asmx",
    },
}



NFE_NS = "http://www.portalfiscal.inf.br/nfe"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"

ROTA_SP_HOMOLOG = "https://homologacao.nfce.fazenda.sp.gov.br/ws/NFeAutorizacao4.asmx"
ROTA_RS_HOMOLOG = "https://nfce-homologacao.sefazrs.rs.gov.br/ws/NfeAutorizacao/NFeAutorizacao4.asmx"

# URL_AUTORIZACAO = "https://nfce.fazenda.sp.gov.br/ws/NFeAutorizacao4.asmx"
# URL_QRCODE = "https://www.nfce.fazenda.sp.gov.br/qrcode"
# URL_CONSULTA = "https://www.nfce.fazenda.sp.gov.br/consulta"

ROTA_SP = "https://nfce.fazenda.sp.gov.br/ws/NFeAutorizacao4.asmx"
QRCODE_SP = "https://www.nfce.fazenda.sp.gov.br/qrcode"
CONSULTA_URL_SP = "https://www.nfce.fazenda.sp.gov.br/consulta"

QRCODE_SP_HOMOLOG = "https://www.homologacao.nfce.fazenda.sp.gov.br/qrcode"
QRCODE_RS = "https://www.sefaz.rs.gov.br/NFCE/NFCE-COM.aspx"


CONSULTA_URL_SP_HOMOLOG = "https://www.homologacao.nfce.fazenda.sp.gov.br/consulta"
CONSULTA_URL_RS = "https://www.sefaz.rs.gov.br/NFCE/NFCE-COM.aspx"

URL_AUTORIZACAO_SP_HOMOLOGACAO = ROTA_SP
URL_QRCODE_SP_HOMOLOGACAO = QRCODE_SP

# url_producao = "https://nfe.fazenda.sp.gov.br/ws/nferetautorizacao4.asmx"
# URL_AUTORIZACAO_SP_HOMOLOGACAO = "https://homologacao.nfe.fazenda.sp.gov.br/ws/nferetautorizacao4.asmx"

URL_CONSULTA_SP_HOMOLOGACAO = CONSULTA_URL_SP

class NFeXMLSigner(XMLSigner):
    def check_deprecated_methods(self):
        # NF-e/NFC-e usa XMLDSig antigo com SHA1 no schema.
        pass

def assinar_nfe_xmlsec(root, id_nfe: str, cert_pem: bytes, key_pem: bytes):
    xmlsec.tree.add_ids(root, ["Id"])

    signature = xmlsec.template.create(
        root,
        xmlsec.Transform.C14N,
        xmlsec.Transform.RSA_SHA1,
        ns=None,
    )
    reference = xmlsec.template.add_reference(
        signature,
        xmlsec.Transform.SHA1,
        uri="#" + id_nfe,
    )
    xmlsec.template.add_transform(reference, xmlsec.Transform.ENVELOPED)
    xmlsec.template.add_transform(reference, xmlsec.Transform.C14N)

    key_info = xmlsec.template.ensure_key_info(signature)
    x509_data = xmlsec.template.add_x509_data(key_info)
    xmlsec.template.x509_data_add_certificate(x509_data)

    remover_espacos_assinatura(signature)
    root.append(signature)

    key = xmlsec.Key.from_memory(key_pem, xmlsec.KeyFormat.PEM, None)
    key.load_cert_from_memory(cert_pem, xmlsec.KeyFormat.PEM)

    context = xmlsec.SignatureContext()
    context.key = key
    context.sign(signature)

    return root

def remover_espacos_assinatura(elemento):
    if elemento.text is not None and not elemento.text.strip():
        elemento.text = None
    if elemento.tail is not None and not elemento.tail.strip():
        elemento.tail = None
    for filho in elemento:
        remover_espacos_assinatura(filho)

def validar_assinatura_xmlsec(root_assinado, cert_pem: bytes):
    xmlsec.tree.add_ids(root_assinado, ["Id"])
    signature = xmlsec.tree.find_node(root_assinado, xmlsec.Node.SIGNATURE)
    if signature is None:
        raise Exception("Signature nao encontrada para validar assinatura XMLSec")

    key = xmlsec.Key.from_memory(cert_pem, xmlsec.KeyFormat.CERT_PEM, None)
    context = xmlsec.SignatureContext()
    context.key = key
    context.verify(signature)
    print("OK -> xmlsec verificou assinatura e digest")

def somente_digitos(valor: str) -> str:
    return re.sub(r"\D", "", valor or "")

def _carregar_certificado_bytes(cert_bytes: bytes):
    cert_bytes = cert_bytes.strip()
    loaders = (
        x509.load_pem_x509_certificate,
        x509.load_der_x509_certificate,
    )
    for loader in loaders:
        try:
            return [loader(cert_bytes)]
        except Exception:
            pass

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="PKCS#7 certificates could not be parsed as DER.*",
                category=UserWarning,
            )
            return load_der_pkcs7_certificates(cert_bytes)
    except Exception:
        return []

def env_bool(nome: str, default: bool = False) -> bool:
    valor = os.getenv(nome)
    if valor is None:
        return default
    return valor.strip().lower() in {"1", "true", "t", "yes", "y", "sim", "s"}

def resolver_verify_tls_sefaz():
    if not env_bool("SEFAZ_TLS_VERIFY", True):
        print("ATENCAO -> verificacao TLS da SEFAZ desabilitada por SEFAZ_TLS_VERIFY=false")
        return False

    bundle = os.getenv("SEFAZ_CA_BUNDLE")
    if bundle:
        bundle = bundle.strip().strip('"').strip("'")
        if os.path.exists(bundle):
            return bundle
        raise Exception(f"SEFAZ_CA_BUNDLE configurado, mas arquivo nao encontrado: {bundle}")

    ca_pem = os.getenv("SEFAZ_CA_PEM")
    if ca_pem:
        ca_pem = ca_pem.strip()
        if os.path.exists(ca_pem):
            with open(ca_pem, "rb") as ca_file:
                ca_pem = ca_file.read().decode("utf-8")

        ca_path = os.path.join(tempfile.gettempdir(), "sefaz-ca-bundle.pem")
        with open(ca_path, "wb") as ca_file:
            with open(certifi.where(), "rb") as certifi_file:
                ca_file.write(certifi_file.read())
            ca_file.write(b"\n")
            ca_file.write(ca_pem.encode("utf-8"))
            ca_file.write(b"\n")
        return ca_path

    return certifi.where()

def _certificados_intermediarios_env():
    valor = os.getenv("CERTISIGN_INTERMEDIARIO")
    if not valor:
        return []

    valor = valor.strip().strip('"').strip("'")
    if not valor or valor.lower() == "algo":
        return []

    if os.path.exists(valor):
        with open(valor, "rb") as cert_file:
            return _carregar_certificado_bytes(cert_file.read())

    try:
        return _carregar_certificado_bytes(valor.encode())
    except Exception:
        return []

def _certificados_intermediarios_config(config):
    valor = (config or {}).get("cert_intermediario")
    if not valor:
        return []

    valor = str(valor).strip().strip('"').strip("'")
    if not valor:
        return []

    if os.path.exists(valor):
        with open(valor, "rb") as cert_file:
            return _carregar_certificado_bytes(cert_file.read())

    try:
        return _carregar_certificado_bytes(valor.encode())
    except Exception:
        return []

def _certificados_intermediarios_locais():
    certificados = []
    for nome in (
        "AC_Certisign_RFB_G5.p7c",
        "AC_Certisign_RFB_G5.crt",
        "AC_Certisign_RFB_G5.pem",
    ):
        if not os.path.exists(nome):
            continue
        try:
            with open(nome, "rb") as cert_file:
                certificados.extend(_carregar_certificado_bytes(cert_file.read()))
        except Exception as exc:
            print(f"ATENCAO -> nao foi possivel carregar intermediario local {nome}: {exc}")
    return certificados

def _baixar_intermediarios_aia(certificate):
    try:
        aia = certificate.extensions.get_extension_for_oid(
            ExtensionOID.AUTHORITY_INFORMATION_ACCESS
        ).value
    except Exception:
        return []

    certificados = []
    for acesso in aia:
        if acesso.access_method != AuthorityInformationAccessOID.CA_ISSUERS:
            continue

        url = acesso.access_location.value
        if not url.lower().startswith(("http://", "https://")):
            continue

        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            certificados.extend(_carregar_certificado_bytes(response.content))
        except Exception as exc:
            print(f"ATENCAO -> nao foi possivel baixar intermediario AIA {url}: {exc}")

    return certificados

def _deduplicar_certificados(certificados):
    unicos = []
    fingerprints = set()
    for cert in certificados:
        fingerprint = cert.fingerprint(hashes.SHA1())
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        unicos.append(cert)
    return unicos

def _ordenar_cadeia(certificate, certificados):
    restantes = list(certificados)
    ordenados = []
    issuer_atual = certificate.issuer

    while restantes:
        proximo = None
        for cert in restantes:
            if cert.subject == issuer_atual:
                proximo = cert
                break

        if proximo is None:
            ordenados.extend(restantes)
            break

        ordenados.append(proximo)
        restantes.remove(proximo)
        if proximo.subject == proximo.issuer:
            ordenados.extend(restantes)
            break
        issuer_atual = proximo.issuer

    return ordenados

def montar_cert_pem_tls(cert_pem: bytes, additional_certs):
    cadeia = b"".join(
        cert.public_bytes(Encoding.PEM)
        for cert in additional_certs
    )
    return cert_pem + cadeia

def load_cert_env():
    pfx_base64 = os.getenv("PFX_PATH")
    senha = os.getenv("PFX_PASSWORD")

    if not pfx_base64 or not senha:
        raise Exception("Certificado não configurado no .env")

    pfx_base64 = (
        pfx_base64
        .strip()
        .replace("\n", "")
        .replace("\r", "")
    )

    pfx_bytes = base64.b64decode(pfx_base64, validate=True)

    # private_key, certificate, _ = pkcs12.load_key_and_certificates(
    #     pfx_bytes,
    #     senha.encode()
    # )
    private_key, certificate, additional_certs = pkcs12.load_key_and_certificates(
    pfx_bytes,
    senha.encode()
    )

    if private_key is None or certificate is None:
        raise Exception("Certificado PFX inválido ou sem chave privada")

    key_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=NoEncryption()
    )

    # Certificado em PEM para usar no requests.post(cert=...)
    cert_pem = certificate.public_bytes(Encoding.PEM)

    # Certificado limpo para colocar dentro do XML Signature
    cert_der = certificate.public_bytes(Encoding.DER)
    cert_base64_limpo = base64.b64encode(cert_der).decode("ascii")

    public_from_cert = certificate.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo
    )

    public_from_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo
    )

    if public_from_cert != public_from_key:
        raise Exception("A chave privada não pertence ao certificado informado")

    print("Certificado e chave privada conferem:", public_from_cert == public_from_key)

    additional_certs = _ordenar_cadeia(certificate, _deduplicar_certificados(
        list(additional_certs or [])
        + _certificados_intermediarios_env()
        + _certificados_intermediarios_locais()
        + _baixar_intermediarios_aia(certificate)
    ))

    if additional_certs:
        print(f"Cadeia intermediaria carregada: {len(additional_certs)} certificado(s)")
    else:
        print("ATENCAO -> nenhum certificado intermediario carregado")

    return cert_pem, key_pem, certificate, additional_certs

def load_cert_config(config):
    pfx_base64 = (config or {}).get("pfx_base64")
    senha = (config or {}).get("pfx_password")

    if not pfx_base64 or not senha:
        raise Exception("Certificado fiscal nao cadastrado para a empresa")

    pfx_base64 = (
        str(pfx_base64)
        .strip()
        .replace("\n", "")
        .replace("\r", "")
    )

    pfx_bytes = base64.b64decode(pfx_base64, validate=True)
    private_key, certificate, additional_certs = pkcs12.load_key_and_certificates(
        pfx_bytes,
        str(senha).encode()
    )

    if private_key is None or certificate is None:
        raise Exception("Certificado PFX invalido ou sem chave privada")

    key_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=NoEncryption()
    )
    cert_pem = certificate.public_bytes(Encoding.PEM)

    public_from_cert = certificate.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo
    )
    public_from_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo
    )

    if public_from_cert != public_from_key:
        raise Exception("A chave privada nao pertence ao certificado informado")

    additional_certs = _ordenar_cadeia(certificate, _deduplicar_certificados(
        list(additional_certs or [])
        + _certificados_intermediarios_config(config)
        + _certificados_intermediarios_locais()
        + _baixar_intermediarios_aia(certificate)
    ))

    return cert_pem, key_pem, certificate, additional_certs

# def load_cert_env():
#     pfx_base64 = os.getenv("PFX_PATH")
#     senha = os.getenv("PFX_PASSWORD")

#     if not pfx_base64 or not senha:
#         raise Exception("Certificado não configurado no .env")

#     pfx_bytes = base64.b64decode(pfx_base64, validate=True)
#     private_key, certificate, additional_certs = pkcs12.load_key_and_certificates(
#         pfx_bytes,
#         senha.encode()
#     )

#     if private_key is None or certificate is None:
#         raise Exception("Certificado PFX inválido ou sem chave privada")

#     key_pem = private_key.private_bytes(
#         encoding=Encoding.PEM,
#         format=PrivateFormat.TraditionalOpenSSL,
#         encryption_algorithm=NoEncryption()
#     )
#     cert_pem = certificate.public_bytes(Encoding.PEM)


#     # Carregar intermediário da variável de ambiente
#     cert_intermediario_pem = os.getenv("CERTISIGN_INTERMEDIARIO").encode()
#     intermediario = x509.load_pem_x509_certificate(cert_intermediario_pem)

#     # Garantir que additional_certs não seja None
#     additional_certs = (additional_certs or []) + [intermediario]

#     return cert_pem, key_pem, certificate, additional_certs

# def load_cert_env():
#     pfx_base64 = os.getenv("PFX_PATH")
#     senha = os.getenv("PFX_PASSWORD")

#     if not pfx_base64 or not senha:
#         raise Exception("Certificado não configurado no .env")

#     pfx_bytes = base64.b64decode(pfx_base64, validate=True)
#     private_key, certificate, additional_certs = pkcs12.load_key_and_certificates(
#         pfx_bytes,
#         senha.encode()
#     )

#     if private_key is None or certificate is None:
#         raise Exception("Certificado PFX inválido ou sem chave privada")

#     key_pem = private_key.private_bytes(
#         encoding=Encoding.PEM,
#         format=PrivateFormat.TraditionalOpenSSL,
#         encryption_algorithm=NoEncryption()
#     )
#     cert_pem = certificate.public_bytes(Encoding.PEM)

#     # Carregar intermediário manualmente
#     cert_intermediario_pem = os.getenv("CERTISIGN_INTERMEDIARIO").encode()
#     intermediario = x509.load_pem_x509_certificate(cert_intermediario_pem)

#     # Evitar duplicação usando fingerprints
#     additional_certs = additional_certs or []
#     fingerprints = {c.fingerprint(hashes.SHA1()) for c in additional_certs}
#     if intermediario.fingerprint(hashes.SHA1()) not in fingerprints:
#         additional_certs.append(intermediario)

#     return cert_pem, key_pem, certificate, additional_certs



def extrair_cnpj_certificado(certificate) -> str:
    subject = certificate.subject.rfc4514_string()

    match = re.search(r":(\d{14})", subject)
    if match:
        return match.group(1)

    match = re.search(r"(\d{14})", subject)
    if match:
        return match.group(1)

    raise Exception("Não foi possível extrair CNPJ do certificado digital")

def validar_cnpj_base_emitente_certificado(root, certificate):
    ns = {"nfe": NFE_NS}

    cnpj_emit = root.findtext(".//nfe:emit/nfe:CNPJ", namespaces=ns)
    if not cnpj_emit:
        raise Exception("CNPJ do emitente não encontrado no XML")

    cnpj_emit = somente_digitos(cnpj_emit)
    cnpj_cert = extrair_cnpj_certificado(certificate)

    if cnpj_emit[:8] != cnpj_cert[:8]:
        raise Exception(
            f"CNPJ-base do emitente difere do certificado. "
            f"XML={cnpj_emit} CERTIFICADO={cnpj_cert}"
        )

def ajustar_homologacao(root):
    ns = {"nfe": NFE_NS}

    tp_amb = root.findtext(".//nfe:ide/nfe:tpAmb", namespaces=ns)
    mod = root.findtext(".//nfe:ide/nfe:mod", namespaces=ns)

    if tp_amb == "2" and mod == "65":
        xprod = root.find(".//nfe:det[@nItem='1']/nfe:prod/nfe:xProd", namespaces=ns)
        if xprod is not None:
            xprod.text = "NOTA FISCAL EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL"

def validar_uf_webservice(root, uf="SP"):
    ns = {"nfe": "http://www.portalfiscal.inf.br/nfe"}

    cuf = root.findtext(
        ".//nfe:ide/nfe:cUF",
        namespaces=ns
    )

    if cuf is None:
        raise Exception("Tag cUF não encontrada")

    uf = (uf or "").strip().upper()
    if uf not in UF_CONFIG:
        raise Exception(f"UF {uf or '(vazia)'} ainda nao possui configuracao de NFC-e")

    cuf_esperado = UF_CONFIG[uf]["cUF"]

    if cuf != cuf_esperado:
        raise Exception(
            f"UF inválida para este endpoint. "
            f"cUF={cuf}. "
            f"Este emissor está configurado para NFC-e {uf}, "
            f"então cUF precisa ser {cuf_esperado}."
        )

    print(
        f"OK -> cUF {cuf} compatível com endpoint {uf}"
    )

def urls_nfce(uf: str, tp_amb: str):
    uf = (uf or "").strip().upper()
    if uf not in UF_CONFIG:
        raise Exception(f"UF {uf or '(vazia)'} ainda nao possui configuracao de NFC-e")
    ambiente = "homologacao" if tp_amb == "2" else "producao"
    return UF_CONFIG[uf][ambiente]


def urls_nfce_sp(tp_amb: str):
    return urls_nfce("SP", tp_amb)

def url_autorizacao_nfce(root, config=None):
    ns = {"nfe": NFE_NS}
    tp_amb = root.findtext(".//nfe:ide/nfe:tpAmb", namespaces=ns)
    uf = (config or {}).get("uf") or "SP"
    mod = root.findtext(".//nfe:ide/nfe:mod", namespaces=ns)
    if mod == "55":
        if uf not in UF_CONFIG_NFE:
            raise Exception(f"UF {uf} sem endpoint de NF-e configurado")
        ambiente = "homologacao" if tp_amb == "2" else "producao"
        return UF_CONFIG_NFE[uf][ambiente]
    return urls_nfce(uf, tp_amb)["autorizacao"]

def montar_qrcode_nfce(root, config=None):
    ns = {"nfe": NFE_NS}

    inf_nfe = root.find(".//nfe:infNFe", namespaces=ns)
    ide = root.find(".//nfe:ide", namespaces=ns)

    if inf_nfe is None or ide is None:
        raise Exception("infNFe/ide não encontrados para montar QR-Code")

    mod = ide.findtext("nfe:mod", namespaces=ns)
    tp_amb = ide.findtext("nfe:tpAmb", namespaces=ns)

    if mod != "65":
        return

    id_nfe = inf_nfe.get("Id")
    if not id_nfe or not id_nfe.startswith("NFe") or len(id_nfe) != 47:
        raise Exception("Id da NFe inválido para montar QR-Code")

    chave = id_nfe[3:]

    csc_id = (config or {}).get("csc_id") or os.getenv("NFCE_CSC_ID")
    csc_token = (config or {}).get("csc_token") or os.getenv("NFCE_CSC_TOKEN")

    if not csc_id or not csc_token:
        raise Exception(
            "CSC da NFC-e não configurado. Defina NFCE_CSC_ID e NFCE_CSC_TOKEN no .env"
        )

    csc_id = str(int(csc_id))
    VERSAO_QRCODE = "2"

    parametros_sem_hash = f"{chave}|{VERSAO_QRCODE}|{tp_amb}|{csc_id}"

    hash_qrcode = hashlib.sha1(
        (parametros_sem_hash + csc_token).encode("utf-8")
    ).hexdigest().upper()

    uf = (config or {}).get("uf") or "SP"
    urls = urls_nfce(uf, tp_amb)
    qrcode = f"{urls['qrcode']}?p={parametros_sem_hash}|{hash_qrcode}"

    nfe = root
    if etree.QName(root).localname != "NFe":
        nfe = root.find(".//nfe:NFe", namespaces=ns)

    if nfe is None:
        raise Exception("Tag NFe não encontrada para inserir infNFeSupl")

    existente = nfe.find("nfe:infNFeSupl", namespaces=ns)
    if existente is not None:
        nfe.remove(existente)

    inf_supl = etree.Element(f"{{{NFE_NS}}}infNFeSupl")
    qr = etree.SubElement(inf_supl, f"{{{NFE_NS}}}qrCode")
    qr.text = etree.CDATA(qrcode)

    url_chave = etree.SubElement(inf_supl, f"{{{NFE_NS}}}urlChave")
    url_chave.text = urls["consulta"] 

    signature = nfe.find(f"{{{DS_NS}}}Signature")

    if signature is not None:
        nfe.insert(nfe.index(signature), inf_supl)
    else:
        nfe.append(inf_supl)

# def assinar_xml(xml_str: str, cert_pem: bytes, key_pem: bytes, certificate, additional_certs):
#     parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False)
#     root = etree.fromstring(xml_str.encode("utf-8"), parser=parser)

#     ns = {"nfe": NFE_NS}

#     if etree.QName(root).localname != "NFe":
#         nfe = root.find(".//nfe:NFe", namespaces=ns)
#         if nfe is None:
#             raise Exception("Tag NFe não encontrada")
#         root = nfe

#     inf_nfe = root.find("nfe:infNFe", namespaces=ns)
#     if inf_nfe is None:
#         raise Exception("Tag infNFe não encontrada")

#     id_nfe = inf_nfe.get("Id")
#     if not id_nfe or not id_nfe.startswith("NFe"):
#         raise Exception("Atributo Id da infNFe inválido")

#     validar_uf_webservice(root, uf="SP")
#     validar_cnpj_base_emitente_certificado(root, certificate)
#     ajustar_homologacao(root)

#     assinatura_antiga = root.find(f"{{{DS_NS}}}Signature")
#     if assinatura_antiga is not None:
#         root.remove(assinatura_antiga)
        
#     inf_supl_antiga = root.find("nfe:infNFeSupl", namespaces=ns)
#     if inf_supl_antiga is not None:
#         root.remove(inf_supl_antiga)

#     signer = NFeXMLSigner(
#         method=methods.enveloped,
#         signature_algorithm="rsa-sha1",
#         digest_algorithm="sha1",
#         c14n_algorithm=CanonicalizationMethod.CANONICAL_XML_1_0
#     )
# #     signer = NFeXMLSigner(
# #     method=methods.enveloped,
# #     signature_algorithm="rsa-sha256",
# #     digest_algorithm="sha256",
# #     c14n_algorithm=CanonicalizationMethod.CANONICAL_XML_1_0
# # )

#     root_assinado = signer.sign(
#         root,
#         key=key_pem,
#         cert=cert_pem,
#         reference_uri="#" + id_nfe,
#         id_attribute="Id"
#     )
    

#     # Adicionar certificados intermediários ao KeyInfo
#     x509_data = root_assinado.find(f".//{{{DS_NS}}}X509Data")
#     if x509_data is not None:
#         for cert in additional_certs:
#             cert_der = cert.public_bytes(Encoding.DER)
#             cert_base64 = base64.b64encode(cert_der).decode("ascii")
#             extra_node = etree.SubElement(x509_data, f"{{{DS_NS}}}X509Certificate")
#             extra_node.text = "".join(cert_base64.split())
    
    
    
#     x509_node = root_assinado.find(f".//{{{DS_NS}}}X509Certificate")
#     if x509_node is not None and x509_node.text:
#         x509_node.text = "".join(x509_node.text.split())

#     validar_assinatura_signxml(root_assinado, cert_pem)
#     montar_qrcode_nfce(root_assinado)


#     print("CERT PFX :", fingerprint_certificado(certificate))
#     print("CERT XML :", fingerprint_xml(root_assinado))

#     return etree.tostring(
#         root_assinado,
#         encoding="utf-8",
#         xml_declaration=False,
#         pretty_print=False
#     ).decode("utf-8")
    
def aplicar_dados_emitente(root, config=None):
    config = config or {}
    ns = {"nfe": NFE_NS}
    emit = root.find(".//nfe:emit", namespaces=ns)
    if emit is None:
        raise Exception("Tag emit não encontrada no XML")
    razao_social = (config.get("razao_social") or "").strip()
    if razao_social:
        xnome = emit.find("nfe:xNome", namespaces=ns)
        if xnome is None:
            raise Exception("Tag xNome nao encontrada no XML")
        xnome.text = razao_social

    nome_fantasia = (config.get("nome_fantasia") or "").strip()
    xfant = emit.find("nfe:xFant", namespaces=ns)
    if nome_fantasia and xfant is not None:
        xfant.text = nome_fantasia

    ie_configurada = somente_digitos(config.get("ie") or "")
    if not ie_configurada:
        return

    ie = emit.find("nfe:IE", namespaces=ns)
    if ie is None:
        crt = emit.find("nfe:CRT", namespaces=ns)
        ie = etree.Element(f"{{{NFE_NS}}}IE")
        if crt is None:
            emit.append(ie)
        else:
            crt.addprevious(ie)
    ie.text = ie_configurada


def aplicar_ie_emitente(root, config=None):
    aplicar_dados_emitente(root, config)


def aplicar_csosn_padrao(root, csosn_padrao="102"):
    ns = {"nfe": NFE_NS}
    for grupo in root.findall(".//nfe:ICMSSN102", namespaces=ns):
        csosn = grupo.find("nfe:CSOSN", namespaces=ns)
        if csosn is None:
            csosn = etree.SubElement(grupo, f"{{{NFE_NS}}}CSOSN")
        if not (csosn.text or "").strip() or csosn.text.strip() == "0":
            csosn.text = csosn_padrao


def assinar_xml(xml_str: str, cert_pem: bytes, key_pem: bytes, certificate, additional_certs, config=None):
    parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False)
    root = etree.fromstring(xml_str.encode("utf-8"), parser=parser)

    ns = {"nfe": NFE_NS}

    if etree.QName(root).localname != "NFe":
        nfe = root.find(".//nfe:NFe", namespaces=ns)
        if nfe is None:
            raise Exception("Tag NFe não encontrada")
        root = nfe

    inf_nfe = root.find("nfe:infNFe", namespaces=ns)
    if inf_nfe is None:
        raise Exception("Tag infNFe não encontrada")

    id_nfe = inf_nfe.get("Id")
    if not id_nfe or not id_nfe.startswith("NFe"):
        raise Exception("Atributo Id da infNFe inválido")

    aplicar_dados_emitente(root, config)
    aplicar_csosn_padrao(root)

    validar_uf_webservice(root, uf=(config or {}).get("uf") or "SP")
    validar_cnpj_base_emitente_certificado(root, certificate)
    ajustar_homologacao(root)

    # Remover assinatura antiga e QRCode antigo
    assinatura_antiga = root.find(f"{{{DS_NS}}}Signature")
    if assinatura_antiga is not None:
        root.remove(assinatura_antiga)
    inf_supl_antiga = root.find("nfe:infNFeSupl", namespaces=ns)
    if inf_supl_antiga is not None:
        root.remove(inf_supl_antiga)
    etree.cleanup_namespaces(root)

    montar_qrcode_nfce(root, config)

    root_assinado = assinar_nfe_xmlsec(root, id_nfe, cert_pem, key_pem)
    
    validar_assinatura_xmlsec(root_assinado, cert_pem)

    print("CERT PFX :", fingerprint_certificado(certificate))
    print("CERT XML :", fingerprint_xml(root_assinado))
    xml_final = etree.tostring(
        root_assinado,
        encoding="utf-8",
        xml_declaration=False,
        pretty_print=False
    )
    validar_assinatura_xmlsec(etree.fromstring(xml_final, parser=parser), cert_pem)

    return xml_final.decode("utf-8")


def validar_assinatura_signxml_bytes(xml_bytes: bytes, cert_pem: bytes):
    XMLVerifier().verify(
        xml_bytes,
        x509_cert=cert_pem,
        expect_config=SignatureConfiguration(
            signature_methods=frozenset([SignatureMethod.RSA_SHA1]),
            digest_algorithms=frozenset([DigestAlgorithm.SHA1])
        ),
        id_attribute="Id"
    )
    print("OK -> signxml verificou assinatura e digest")    

def atualizar_signature_value(root_assinado, key_pem: bytes):
    signed_info = root_assinado.find(f".//{{{DS_NS}}}SignedInfo")
    signature_value = root_assinado.find(f".//{{{DS_NS}}}SignatureValue")

    if signed_info is None or signature_value is None:
        raise Exception("SignedInfo/SignatureValue nao encontrados na assinatura")

    private_key = serialization.load_pem_private_key(
        key_pem,
        password=None
    )
    signed_info_c14n = etree.tostring(
        signed_info,
        method="c14n",
        exclusive=False,
        with_comments=False
    )
    assinatura = private_key.sign(
        signed_info_c14n,
        padding.PKCS1v15(),
        hashes.SHA1()
    )
    signature_value.text = base64.b64encode(assinatura).decode("ascii")

def atualizar_digest_value(root_assinado):
    ns = {"nfe": NFE_NS}
    signed_info = root_assinado.find(f".//{{{DS_NS}}}SignedInfo")
    if signed_info is None:
        raise Exception("SignedInfo nao encontrado na assinatura")

    reference = signed_info.find(f".//{{{DS_NS}}}Reference")
    digest_value = signed_info.find(f".//{{{DS_NS}}}DigestValue")
    if reference is None or digest_value is None:
        raise Exception("Reference/DigestValue nao encontrados na assinatura")

    ref_uri = reference.get("URI")
    if not ref_uri or not ref_uri.startswith("#"):
        raise Exception("Reference URI invalida na assinatura")

    root = etree.fromstring(
        etree.tostring(root_assinado, encoding="utf-8", xml_declaration=False)
    )
    signature = root.find(f".//{{{DS_NS}}}Signature")
    if signature is not None:
        signature.getparent().remove(signature)

    inf_nfe = root.find(f".//nfe:infNFe[@Id='{ref_uri[1:]}']", namespaces=ns)
    if inf_nfe is None:
        raise Exception("infNFe referenciada pela assinatura nao encontrada")

    inf_c14n = etree.tostring(
        inf_nfe,
        method="c14n",
        exclusive=False,
        with_comments=False
    )
    digest_value.text = base64.b64encode(
        hashlib.sha1(inf_c14n).digest()
    ).decode("ascii")

def validar_assinatura_nfce(root_assinado):
    ns = {"nfe": NFE_NS}

    root = etree.fromstring(
        etree.tostring(root_assinado, encoding="utf-8", xml_declaration=False)
    )
    signed_info = root.find(f".//{{{DS_NS}}}SignedInfo")
    signature_value = root.findtext(f".//{{{DS_NS}}}SignatureValue")
    x509_text = root.findtext(f".//{{{DS_NS}}}X509Certificate")

    if signed_info is None or not signature_value or not x509_text:
        raise Exception("Assinatura XML incompleta")

    cert_der = b64decode("".join(x509_text.split()))
    cert = x509.load_der_x509_certificate(cert_der)
    public_key = cert.public_key()

    signed_info_c14n = etree.tostring(
        signed_info,
        method="c14n",
        exclusive=False,
        with_comments=False
    )
    public_key.verify(
        b64decode("".join(signature_value.split())),
        signed_info_c14n,
        padding.PKCS1v15(),
        hashes.SHA1()
    )

    reference = signed_info.find(f".//{{{DS_NS}}}Reference")
    digest_xml = signed_info.findtext(f".//{{{DS_NS}}}DigestValue")
    if reference is None or not digest_xml:
        raise Exception("Reference/DigestValue nao encontrados na assinatura")

    ref_uri = reference.get("URI")
    if not ref_uri or not ref_uri.startswith("#"):
        raise Exception("Reference URI invalida na assinatura")

    signature = root.find(f".//{{{DS_NS}}}Signature")
    if signature is not None:
        signature.getparent().remove(signature)

    inf_nfe = root.find(f".//nfe:infNFe[@Id='{ref_uri[1:]}']", namespaces=ns)
    if inf_nfe is None:
        raise Exception("infNFe referenciada pela assinatura nao encontrada")

    inf_c14n = etree.tostring(
        inf_nfe,
        method="c14n",
        exclusive=False,
        with_comments=False
    )
    digest_calculado = base64.b64encode(
        hashlib.sha1(inf_c14n).digest()
    ).decode("ascii")

    if digest_xml != digest_calculado:
        raise Exception(
            f"DigestValue invalido. XML={digest_xml} calculado={digest_calculado}"
        )

    print("OK -> assinatura NFC-e valida localmente")


def validar_assinatura_local(root_assinado):
    NS = {
        "ds": "http://www.w3.org/2000/09/xmldsig#",
        "nfe": "http://www.portalfiscal.inf.br/nfe"
    }

    root = etree.fromstring(
        etree.tostring(root_assinado, encoding="utf-8", xml_declaration=False)
    )

    signed_info = root.find(".//SignedInfo", NS)
    signature_value = root.findtext(".//SignatureValue", namespaces=NS)
    x509_text = root.findtext(".//X509Certificate", namespaces=NS)

    cert_der = b64decode("".join(x509_text.split()))
    cert = x509.load_der_x509_certificate(cert_der)
    public_key = cert.public_key()

    signed_info_c14n = etree.tostring(
        signed_info,
        method="c14n",
        exclusive=False,
        with_comments=False
    )

    public_key.verify(
        b64decode(signature_value),
        signed_info_c14n,
        padding.PKCS1v15(),
        hashes.SHA1()
    )

    print("OK -> SignatureValue valida SignedInfo")

    # AQUI ESTÁ A CORREÇÃO:
    # aplicar manualmente enveloped-signature antes do digest
    signature = root.find(".//Signature", NS)
    if signature is not None:
        signature.getparent().remove(signature)

    ref_uri = signed_info.find(".//Reference", NS).get("URI")
    id_ref = ref_uri[1:]

    inf_nfe = root.find(f".//nfe:infNFe[@Id='{id_ref}']", NS)

    inf_c14n = etree.tostring(
        inf_nfe,
        method="c14n",
        exclusive=False,
        with_comments=False
    )

    digest_calculado = base64.b64encode(
        hashlib.sha1(inf_c14n).digest()
    ).decode()

    digest_xml = signed_info.findtext(".//DigestValue", namespaces=NS)

    print("Digest XML:      ", digest_xml)
    print("Digest calculado:", digest_calculado)
    print("Digest confere:  ", digest_xml == digest_calculado)
    
  
def validar_assinatura_signxml(root_assinado, cert_pem):
    XMLVerifier().verify(
        root_assinado,
        x509_cert=cert_pem,
        expect_config=SignatureConfiguration(
            signature_methods=frozenset([
                SignatureMethod.RSA_SHA1
            ]),
            digest_algorithms=frozenset([
                DigestAlgorithm.SHA1
            ])
        ),
        id_attribute="Id"
    )

    print("OK -> signxml verificou assinatura e digest")
  
    
def montar_lote(xml_assinado: str):
    xml_assinado = xml_assinado.strip()

    if xml_assinado.startswith("<?xml"):
        xml_assinado = xml_assinado.split("?>", 1)[1].strip()

    id_lote = str(int(time.time()))

    return (
        f'<enviNFe xmlns="{NFE_NS}" versao="4.00">'
        f'<idLote>{id_lote}</idLote>'
        '<indSinc>1</indSinc>'
        f'{xml_assinado}'
        '</enviNFe>'
    )


# def montar_lote(xml_assinado: str):
#     xml_assinado = xml_assinado.strip()
#     if xml_assinado.startswith("<?xml"):
#         xml_assinado = xml_assinado.split("?>", 1)[1].strip()

#     id_lote = str(int(time.time()))

#     parser = etree.XMLParser(remove_blank_text=True)
#     nfe_element = etree.fromstring(xml_assinado.encode("utf-8"), parser)

#     # Criar enviNFe com nsmap explícito para evitar prefixo ns0:
#     NSMAP = {
#         None: NFE_NS,                                    # namespace padrão sem prefixo
#         "ds": "http://www.w3.org/2000/09/xmldsig#"      # ds: para assinatura
#     }

#     envi = etree.Element(
#         f"{{{NFE_NS}}}enviNFe",
#         versao="4.00",
#         nsmap=NSMAP
#     )

#     id_lote_el = etree.SubElement(envi, f"{{{NFE_NS}}}idLote")
#     id_lote_el.text = id_lote

#     ind_sinc = etree.SubElement(envi, f"{{{NFE_NS}}}indSinc")
#     ind_sinc.text = "1"

#     envi.append(nfe_element)

#     # cleanup_namespaces remove declarações redundantes
#     etree.cleanup_namespaces(envi)

#     return etree.tostring(
#         envi,
#         encoding="unicode",
#         xml_declaration=False
#     )

def montar_envelope(xml_lote: str):
    xml_lote = xml_lote.strip()

    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Envelope xmlns="http://www.w3.org/2003/05/soap-envelope">'
        '<Body>'
        '<nfeDadosMsg xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeAutorizacao4">'
        f'{xml_lote}'
        '</nfeDadosMsg>'
        '</Body>'
        '</Envelope>'
    )

def finalizar_assinatura_documento(xml_str: str, key_pem: bytes):
    parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False)
    root = etree.fromstring(xml_str.encode("utf-8"), parser=parser)
    root = remover_prefixo_ds(root)
    atualizar_digest_value(root)
    atualizar_signature_value(root, key_pem)
    return etree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=xml_str.lstrip().startswith("<?xml"),
        pretty_print=False
    ).decode("utf-8")

def finalizar_assinatura_nfe(xml_str: str, key_pem: bytes):
    parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False)
    root = etree.fromstring(xml_str.encode("utf-8"), parser=parser)
    root = remover_prefixo_ds(root)
    atualizar_digest_value(root)
    atualizar_signature_value(root, key_pem)
    xml_final = etree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=False,
        pretty_print=False
    )
    return xml_final.decode("utf-8")

def garantir_prefixo_ds(root):
    signature = root.find(f".//{{{DS_NS}}}Signature")
    if signature is None:
        encontrados = root.xpath(".//*[local-name()='Signature']")
        signature = encontrados[0] if encontrados else None

    if signature is None:
        return root

    def clonar_assinatura_com_prefixo(elemento, raiz=False):
        tag = etree.QName(elemento).localname
        novo = etree.Element(
            f"{{{DS_NS}}}{tag}",
            nsmap={"ds": DS_NS} if raiz else None
        )
        novo.text = elemento.text
        novo.tail = elemento.tail

        for nome, valor in elemento.attrib.items():
            qname = etree.QName(nome)
            if qname.namespace:
                novo.set(f"{{{qname.namespace}}}{qname.localname}", valor)
            else:
                novo.set(qname.localname, valor)

        for filho in elemento:
            novo.append(clonar_assinatura_com_prefixo(filho))

        return novo

    assinatura_normalizada = clonar_assinatura_com_prefixo(signature, raiz=True)
    pai = signature.getparent()
    if pai is None:
        return assinatura_normalizada

    indice = pai.index(signature)
    pai.remove(signature)
    pai.insert(indice, assinatura_normalizada)
    return root

def remover_prefixo_ds(root):
    signature = root.find(f".//{{{DS_NS}}}Signature")
    if signature is None:
        encontrados = root.xpath(".//*[local-name()='Signature']")
        signature = encontrados[0] if encontrados else None

    if signature is None:
        return root

    def clonar_assinatura_sem_prefixo(elemento, raiz=False):
        tag = etree.QName(elemento).localname
        novo = etree.Element(
            f"{{{DS_NS}}}{tag}",
            nsmap={None: DS_NS} if raiz else None
        )
        novo.text = elemento.text
        novo.tail = elemento.tail

        for nome, valor in elemento.attrib.items():
            qname = etree.QName(nome)
            if qname.namespace:
                novo.set(f"{{{qname.namespace}}}{qname.localname}", valor)
            else:
                novo.set(qname.localname, valor)

        for filho in elemento:
            novo.append(clonar_assinatura_sem_prefixo(filho))

        return novo

    assinatura_normalizada = clonar_assinatura_sem_prefixo(signature, raiz=True)
    pai = signature.getparent()
    if pai is None:
        return assinatura_normalizada

    indice = pai.index(signature)
    pai.remove(signature)
    pai.insert(indice, assinatura_normalizada)
    return root

def enviar_sefaz(xml_envelope: str, url: str, cert_pem: bytes, key_pem: bytes,
                 action: str = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeAutorizacao4/nfeAutorizacaoLote"):
    headers = {
        "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'
    }
    verify_tls = resolver_verify_tls_sefaz()


    cert_path = None
    key_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False) as cert_file, \
             tempfile.NamedTemporaryFile(delete=False) as key_file:

            cert_file.write(cert_pem)
            key_file.write(key_pem)
            cert_file.flush()
            key_file.flush()            

            cert_path = cert_file.name
            key_path = key_file.name

        response = requests.post(
            url,
            data=xml_envelope.encode("utf-8"),
            headers=headers,
            cert=(cert_path, key_path),
            verify=verify_tls,
            timeout=60
        )

        print("\n=== DEBUG SEFAZ ===")
        print("URL:", url)
        print("Status:", response.status_code)
        print("Resposta:", response.text[:3000])
        print("===================\n")

        return response.text

    except requests.exceptions.SSLError as exc:
        raise Exception(
            "Falha TLS ao conectar na SEFAZ. Configure SEFAZ_CA_BUNDLE com um "
            "arquivo PEM contendo a cadeia confiavel do endpoint, ou SEFAZ_CA_PEM "
            "com o PEM da CA/intermediaria. Para teste temporario, use "
            "SEFAZ_TLS_VERIFY=false. Erro original: "
            f"{exc}"
        ) from exc

    finally:
        if cert_path and os.path.exists(cert_path):
            os.remove(cert_path)

        if key_path and os.path.exists(key_path):
            os.remove(key_path)

def consultar_status_sefaz(config):
    """Consulta NFeStatusServico4 da SVRS usando o certificado configurado."""
    cert_pem, key_pem, _certificate, additional = load_cert_config(config)
    tp_amb = "1" if (config or {}).get("ambiente") != "homologacao" else "2"
    cuf = UF_CONFIG_NFE.get((config or {}).get("uf", "BA").upper(), {}).get("cUF", "29")
    xml = (f'<consStatServ xmlns="{NFE_NS}" versao="4.00">'
           f'<tpAmb>{tp_amb}</tpAmb><cUF>{cuf}</cUF><xServ>STATUS</xServ>'
           '</consStatServ>')
    envelope = (f'<?xml version="1.0" encoding="utf-8"?>'
                '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">'
                '<soap:Body><nfeDadosMsg xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeStatusServico4">'
                f'{xml}</nfeDadosMsg></soap:Body></soap:Envelope>')
    uf = (config or {}).get("uf", "BA").upper()
    ambiente = "homologacao" if tp_amb == "2" else "producao"
    # O status usa um serviço SOAP separado do autorizador.
    status_urls = {
        "producao": "https://nfe.sefaz.ba.gov.br/webservices/NFeStatusServico4/NFeStatusServico4.asmx",
        "homologacao": "https://hnfe.sefaz.ba.gov.br/webservices/NFeStatusServico4/NFeStatusServico4.asmx",
    }
    url = status_urls[ambiente]
    resposta = enviar_sefaz(
        envelope, url, montar_cert_pem_tls(cert_pem, additional), key_pem,
        "http://www.portalfiscal.inf.br/nfe/wsdl/NFeStatusServico4/nfeStatusServicoNF"
    )
    root = etree.fromstring(resposta.encode("utf-8"))
    ns = {"nfe": NFE_NS}
    return {"cStat": root.findtext(".//nfe:cStat", namespaces=ns),
            "xMotivo": root.findtext(".//nfe:xMotivo", namespaces=ns), "url": url}

def extrair_status_sefaz(resposta_xml: str):
    if not resposta_xml:
        return None, None

    try:
        root = etree.fromstring(resposta_xml.encode("utf-8"))
    except Exception:
        return None, None

    ns = {"nfe": NFE_NS}
    inf_prot = root.find(".//nfe:infProt", namespaces=ns)
    if inf_prot is not None:
        return (
            inf_prot.findtext("nfe:cStat", namespaces=ns),
            inf_prot.findtext("nfe:xMotivo", namespaces=ns),
        )

    ret_envi = root.find(".//nfe:retEnviNFe", namespaces=ns)
    if ret_envi is not None:
        return (
            ret_envi.findtext("nfe:cStat", namespaces=ns),
            ret_envi.findtext("nfe:xMotivo", namespaces=ns),
        )

    return (
        root.findtext(".//nfe:cStat", namespaces=ns),
        root.findtext(".//nfe:xMotivo", namespaces=ns),
    )

def extrair_protocolo_autorizacao(resposta_xml: str):
    if not resposta_xml:
        return None

    try:
        root = etree.fromstring(resposta_xml.encode("utf-8"))
    except Exception:
        return None

    ns = {"nfe": NFE_NS}
    prot_nfe = root.find(".//nfe:protNFe", namespaces=ns)
    if prot_nfe is None:
        return None

    inf_prot = prot_nfe.find("nfe:infProt", namespaces=ns)
    return {
        "elemento": prot_nfe,
        "nProt": inf_prot.findtext("nfe:nProt", namespaces=ns) if inf_prot is not None else None,
        "chNFe": inf_prot.findtext("nfe:chNFe", namespaces=ns) if inf_prot is not None else None,
        "cStat": inf_prot.findtext("nfe:cStat", namespaces=ns) if inf_prot is not None else None,
        "xMotivo": inf_prot.findtext("nfe:xMotivo", namespaces=ns) if inf_prot is not None else None,
    }

def montar_xml_autorizado(xml_assinado: str, resposta_xml: str):
    protocolo = extrair_protocolo_autorizacao(resposta_xml)
    if not protocolo or protocolo.get("elemento") is None:
        return None, protocolo

    parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False)
    nfe = etree.fromstring(xml_assinado.encode("utf-8"), parser=parser)
    prot_nfe = deepcopy(protocolo["elemento"])

    nfe_proc = etree.Element(
        f"{{{NFE_NS}}}nfeProc",
        versao="4.00",
        nsmap={None: NFE_NS},
    )
    nfe_proc.append(nfe)
    nfe_proc.append(prot_nfe)

    xml_autorizado = etree.tostring(
        nfe_proc,
        encoding="utf-8",
        xml_declaration=True,
        pretty_print=False,
    ).decode("utf-8")

    return xml_autorizado, protocolo

def emitir_nfce(xml: str, config=None):
    try:
        if config:
            cert_pem, key_pem, certificate, additional_certs = load_cert_config(config)
        else:
            cert_pem, key_pem, certificate, additional_certs = load_cert_env()
        xml_assinado = assinar_xml(
            xml,
            cert_pem,
            key_pem,
            certificate,
            additional_certs,
            config
        )
        
        # DEBUG: confirmar fingerprints
        root_debug = etree.fromstring(xml_assinado.encode("utf-8"))
        url_autorizacao = url_autorizacao_nfce(root_debug, config)
        fp_pfx = fingerprint_certificado(certificate)
        fp_xml = fingerprint_xml(root_debug)
        print(f"Fingerprint PFX: {fp_pfx}")
        print(f"Fingerprint XML: {fp_xml}")
        print(f"Batem: {fp_pfx == fp_xml}")
        
        if fp_pfx != fp_xml:
            raise Exception("CERTIFICADO NO XML NÃO BATE COM O PFX!")

        # print("VALIDANDO XML ASSINADO ANTES DO LOTE")
        # validar_assinatura_local(
        #     etree.fromstring(xml_assinado.encode("utf-8"))
        # )
        lote = montar_lote(xml_assinado)
        envelope = montar_envelope(lote)
        root_env = etree.fromstring(envelope.encode("utf-8"))
        nfe_final = root_env.find(f".//{{{NFE_NS}}}NFe")
        lote_final = root_env.find(f".//{{{NFE_NS}}}enviNFe")
        if nfe_final is not None:
            xml_assinado = etree.tostring(
                nfe_final,
                encoding="utf-8",
                xml_declaration=False,
                pretty_print=False
            ).decode("utf-8")
        if lote_final is not None:
            lote = etree.tostring(
                lote_final,
                encoding="utf-8",
                xml_declaration=False,
                pretty_print=False
            ).decode("utf-8")

        resposta = enviar_sefaz(
            envelope,
            url_autorizacao,
            montar_cert_pem_tls(cert_pem, additional_certs),
            key_pem
        )
        cstat, xmotivo = extrair_status_sefaz(resposta)
        autorizada = cstat in {"100", "150"}
        xml_autorizado = None
        protocolo = None
        if autorizada:
            xml_autorizado, protocolo = montar_xml_autorizado(xml_assinado, resposta)

        return {
            "sucesso": autorizada,
            "cStat": cstat,
            "xMotivo": xmotivo,
            "nProt": protocolo.get("nProt") if protocolo else None,
            "chNFe": protocolo.get("chNFe") if protocolo else None,
            "xml_assinado": xml_assinado,
            "xml_autorizado": xml_autorizado,
            "lote": lote,
            "envelope": envelope,
            "resposta_sefaz": resposta
        }

    except Exception as e:
        return {
            "sucesso": False,
            "erro": str(e) or repr(e)
}
       
# def emitir_nfce(xml: str):
#     try:
#         cert_pem, key_pem, certificate, additional_certs = load_cert_env()
        
#         xml_assinado, lote = assinar_xml(xml, cert_pem, key_pem, certificate, additional_certs)
        
#         envelope = montar_envelope(lote)
#         resposta = enviar_sefaz(envelope, URL_AUTORIZACAO_SP_HOMOLOGACAO, cert_pem, key_pem)

#         return {
#             "sucesso": True,
#             "xml_assinado": xml_assinado,
#             "lote": lote,
#             "envelope": envelope,
#             "resposta_sefaz": resposta
#         }
#     except Exception as e:
#         return {"sucesso": False, "erro": str(e) or repr(e)}
        
def fingerprint_certificado(certificate):
    der = certificate.public_bytes(Encoding.DER)
    return hashlib.sha1(der).hexdigest().upper()

def fingerprint_xml(xml_assinado):
    if isinstance(xml_assinado, etree._Element):
        root = xml_assinado
    else:
        root = etree.fromstring(xml_assinado.encode("utf-8"))

    x509_text = root.findtext(".//{http://www.w3.org/2000/09/xmldsig#}X509Certificate")

    if not x509_text:
        raise Exception("X509Certificate não encontrado no XML")

    der_xml = base64.b64decode("".join(x509_text.split()))
    cert_xml = x509.load_der_x509_certificate(der_xml)

    return hashlib.sha1(cert_xml.public_bytes(Encoding.DER)).hexdigest().upper()

def diagnosticar_assinatura(xml_assinado_str: str):
    parser = etree.XMLParser(remove_blank_text=True)
    root = etree.fromstring(xml_assinado_str.encode("utf-8"), parser)

    NS = {
        "ds": "http://www.w3.org/2000/09/xmldsig#",
        "nfe": "http://www.portalfiscal.inf.br/nfe"
    }

    # 1. Pegar DigestValue do XML
    # digest_xml = root.findtext(".//ds:DigestValue", namespaces=NS)
    digest_xml = root.findtext(".//DigestValue", namespaces=NS)

    print(f"DigestValue no XML: {digest_xml}")

    # 2. Pegar o infNFe e remover a Signature (transform enveloped)
    inf_nfe = root.find(".//nfe:infNFe", namespaces=NS)
    
    # Clonar o root sem a assinatura para calcular o digest
    import copy
    root_clone = copy.deepcopy(root)
    sig = root_clone.find(f"{{{DS_NS}}}Signature")
    if sig is not None:
        sig.getparent().remove(sig)
    
    inf_nfe_clone = root_clone.find(".//nfe:infNFe", namespaces=NS)

    # 3. Canonicalizar o infNFe
    c14n = etree.tostring(inf_nfe_clone, method="c14n", exclusive=False, with_comments=False)
    
    # 4. Calcular SHA1
    digest_calculado = base64.b64encode(hashlib.sha1(c14n).digest()).decode()
    print(f"DigestValue calculado: {digest_calculado}")
    print(f"Digest confere: {digest_xml == digest_calculado}")
    
    # 5. Mostrar início do c14n para inspeção de namespaces
    print(f"Início do C14N:\n{c14n[:500].decode()}")

def remove_ds(nota_xml):
    return nota_xml.replace("ds:", "")
