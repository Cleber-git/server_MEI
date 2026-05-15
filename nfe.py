from lxml import etree
from signxml import XMLSigner, methods, XMLVerifier
from signxml.algorithms import CanonicalizationMethod
from cryptography.hazmat.primitives.serialization import (
    pkcs12,
    Encoding,
    PrivateFormat,
    NoEncryption
)
import base64
import hashlib
import os
import re
import requests
import tempfile
import time
from cryptography.hazmat.primitives import hashes, serialization

from cryptography import x509
from base64 import b64decode
from cryptography.hazmat.primitives.asymmetric import padding

from signxml.verifier import SignatureConfiguration
from signxml.algorithms import SignatureMethod, DigestAlgorithm
import certifi


UF_CONFIG = {
    "SP": {
        "cUF": "35",
        "url_autorizacao": "https://nfce.fazenda.sp.gov.br/ws/NFeAutorizacao4.asmx",
    },
    "RS": {
        "cUF": "43",
        "url_autorizacao": "https://nfce-homologacao.sefazrs.rs.gov.br/ws/NfeAutorizacao/NFeAutorizacao4.asmx",
    }
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

QRCODE_SP_HOMOLOG = "https://www.homologacao.nfce.fazenda.sp.gov.br/consulta"
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

def somente_digitos(valor: str) -> str:
    return re.sub(r"\D", "", valor or "")

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

    return cert_pem, key_pem, certificate, additional_certs or []

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

def montar_qrcode_nfce(root):
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

    csc_id = os.getenv("NFCE_CSC_ID")
    csc_token = os.getenv("NFCE_CSC_TOKEN")

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

    qrcode = f"{URL_QRCODE_SP_HOMOLOGACAO}?p={parametros_sem_hash}|{hash_qrcode}"

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
    url_chave.text = URL_CONSULTA_SP_HOMOLOGACAO 

    signature = nfe.find(f"{{{DS_NS}}}Signature")

    if signature is not None:
        nfe.insert(nfe.index(signature), inf_supl)
    else:
        nfe.append(inf_supl)


def assinar_xml(xml_str: str, cert_pem: bytes, key_pem: bytes, certificate, additional_certs):
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

    validar_uf_webservice(root, uf="SP")
    validar_cnpj_base_emitente_certificado(root, certificate)
    ajustar_homologacao(root)

    assinatura_antiga = root.find(f"{{{DS_NS}}}Signature")
    if assinatura_antiga is not None:
        root.remove(assinatura_antiga)
        
    inf_supl_antiga = root.find("nfe:infNFeSupl", namespaces=ns)
    if inf_supl_antiga is not None:
        root.remove(inf_supl_antiga)

    signer = NFeXMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha1",
        digest_algorithm="sha1",
        c14n_algorithm=CanonicalizationMethod.CANONICAL_XML_1_0
    )
#     signer = NFeXMLSigner(
#     method=methods.enveloped,
#     signature_algorithm="rsa-sha256",
#     digest_algorithm="sha256",
#     c14n_algorithm=CanonicalizationMethod.CANONICAL_XML_1_0
# )

    root_assinado = signer.sign(
        root,
        key=key_pem,
        cert=cert_pem,
        reference_uri="#" + id_nfe,
        id_attribute="Id"
    )
    

    
    x509_node = root_assinado.find(f".//{{{DS_NS}}}X509Certificate")
    if x509_node is not None and x509_node.text:
        x509_node.text = "".join(x509_node.text.split())

    validar_assinatura_signxml(root_assinado, cert_pem)
    montar_qrcode_nfce(root_assinado)


    print("CERT PFX :", fingerprint_certificado(certificate))
    print("CERT XML :", fingerprint_xml(root_assinado))

    return etree.tostring(
        root_assinado,
        encoding="utf-8",
        xml_declaration=False,
        pretty_print=False
    ).decode("utf-8")
    
def validar_assinatura_local(root_assinado):
    NS = {
        "ds": "http://www.w3.org/2000/09/xmldsig#",
        "nfe": "http://www.portalfiscal.inf.br/nfe"
    }

    root = etree.fromstring(
        etree.tostring(root_assinado, encoding="utf-8", xml_declaration=False)
    )

    signed_info = root.find(".//ds:SignedInfo", NS)
    signature_value = root.findtext(".//ds:SignatureValue", namespaces=NS)
    x509_text = root.findtext(".//ds:X509Certificate", namespaces=NS)

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
    signature = root.find(".//ds:Signature", NS)
    if signature is not None:
        signature.getparent().remove(signature)

    ref_uri = signed_info.find(".//ds:Reference", NS).get("URI")
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

    digest_xml = signed_info.findtext(".//ds:DigestValue", namespaces=NS)

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
    
def montar_envelope(xml_lote: str):
    xml_lote = xml_lote.strip()

    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">'
        '<soap12:Body>'
        '<nfeDadosMsg xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeAutorizacao4">'
        f'{xml_lote}'
        '</nfeDadosMsg>'
        '</soap12:Body>'
        '</soap12:Envelope>'
    )

def enviar_sefaz(xml_envelope: str, url: str, cert_pem: bytes, key_pem: bytes):
    headers = {
        "Content-Type": 'application/soap+xml; charset=utf-8; action="http://www.portalfiscal.inf.br/nfe/wsdl/NFeAutorizacao4/nfeAutorizacaoLote"'
    }


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
            verify=certifi.where(),
            timeout=60
        )

        print("\n=== DEBUG SEFAZ ===")
        print("URL:", url)
        print("Status:", response.status_code)
        print("Resposta:", response.text[:3000])
        print("===================\n")

        return response.text

    finally:
        if cert_path and os.path.exists(cert_path):
            os.remove(cert_path)

        if key_path and os.path.exists(key_path):
            os.remove(key_path)

def emitir_nfce(xml: str):
    try:
        cert_pem, key_pem, certificate, additional_certs = load_cert_env()
        xml_assinado = assinar_xml(
            xml,
            cert_pem,
            key_pem,
            certificate,
            additional_certs
        )

        # print("VALIDANDO XML ASSINADO ANTES DO LOTE")
        # validar_assinatura_local(
        #     etree.fromstring(xml_assinado.encode("utf-8"))
        # )
        lote = montar_lote(xml_assinado)
        envelope = montar_envelope(lote)

        resposta = enviar_sefaz(
            envelope,
            URL_AUTORIZACAO_SP_HOMOLOGACAO,
            cert_pem,
            key_pem
        )

        return {
            "sucesso": True,
            "xml_assinado": xml_assinado,
            "lote": lote,
            "envelope": envelope,
            "resposta_sefaz": resposta
        }

    except Exception as e:
        return {
            "sucesso": False,
            "erro": str(e) or repr(e)
}
        
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