"""DANFSe (F8): documento auxiliar da NFS-e gerado a partir do XML autorizado.

Segue a NT 008 v1.02 (14/07/2026, SE/CGNFS-e), porque a API de DANFSe do ADN foi suspensa
em 03/08/2026:

- Página única A4, retrato, margens de 0,2 cm, borda de 1 pt e divisões de 0,5 pt.
- Cabeçalho, títulos de bloco, "Emitente da NFS-e" e "Valor líquido" com fundo cinza a 5%.
- Título "DANFSe v2.0" / "Documento Auxiliar da NFS-e" em negrito, 9 pt. Em produção
  restrita (``tpAmb=2``), "NFS-e SEM VALIDADE JURÍDICA" em vermelho logo abaixo.
- QR Code de no mínimo 1,52 cm em X 17,48 cm / Y 1,67 cm, apontando para
  ``https://www.nfse.gov.br/ConsultaPublica/?tpc=1&chave=<chave>``, com o texto de
  autenticidade em 3 linhas de 6 pt.
- Só imprime o que está no XML. Campo ausente vira "-". Bloco sem dado usa as frases de
  supressão da seção 2.3. Texto longo é cortado com reticências.

Desvios conhecidos (ver ``docs/FISCAL_BACKEND.md``): a fonte é Helvetica, de métrica igual à
Arial (a Microsoft Sans Serif não é livre), e a grade dos blocos segue a ordem e o conteúdo do
Anexo I da NT, mas não as coordenadas exatas de cada campo.
"""

from __future__ import annotations

import io
from datetime import datetime


from . import nfse

CONSULTA_URL = "https://www.nfse.gov.br/ConsultaPublica/?tpc=1&chave="
_N = {"n": nfse.NS}

_STATUS = {
    "100": "NFS-e Gerada",
    "102": "NFS-e de Decisão Judicial",
    "103": "NFS-e Avulsa",
    "107": "NFS-e MEI",
}
_EMITTER = {"1": "Prestador do Serviço", "2": "Tomador do Serviço", "3": "Intermediário"}
_SIMPLES = {
    "1": "Não Optante",
    "2": "Optante - Microempreendedor Individual (MEI)",
    "3": "Optante - Microempresa ou Empresa de Pequeno Porte (ME/EPP)",
}
_ISSQN = {
    "1": "Operação Tributável",
    "2": "Imunidade",
    "3": "Exportação de Serviço",
    "4": "Não Incidência",
}
_RETENTION = {
    "1": "Não Retido",
    "2": "Retido pelo Tomador",
    "3": "Retido pelo Intermediário",
}


def _t(node, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path, _N)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _tax_id(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) == 14:
        return f"{value[:2]}.{value[2:5]}.{value[5:8]}/{value[8:12]}-{value[12:]}"
    if len(value) == 11:
        return f"{value[:3]}.{value[3:6]}.{value[6:9]}-{value[9:]}"
    return value


def _date(value: str | None, *, time: bool = True) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%d/%m/%Y %H:%M:%S" if time else "%d/%m/%Y")


def _money(value: str | None) -> str | None:
    if not value:
        return None
    whole, _, cents = value.partition(".")
    groups = f"{int(whole):,}".replace(",", ".")
    return f"R$ {groups},{(cents + '00')[:2]}"


def _person(node) -> list[tuple[str, str | None]]:
    tax = _t(node, "n:CNPJ") or _t(node, "n:CPF") or _t(node, "n:NIF")
    end = node.find("n:end", _N)
    nac = end.find("n:endNac", _N) if end is not None else None
    address = None
    if end is not None:
        address = ", ".join(
            part
            for part in (
                _t(end, "n:xLgr"),
                _t(end, "n:nro"),
                _t(end, "n:xCpl"),
                _t(end, "n:xBairro"),
            )
            if part
        )
    return [
        ("CNPJ / CPF / NIF", _tax_id(tax)),
        ("Inscrição Municipal", _t(node, "n:IM")),
        ("Telefone", _t(node, "n:fone")),
        ("Nome / Nome Empresarial", _t(node, "n:xNome")),
        ("Código IBGE / CEP", " / ".join(filter(None, (_t(nac, "n:cMun"), _t(nac, "n:CEP")))) or None),
        ("Endereço", address or None),
        ("E-mail", _t(node, "n:email")),
    ]


def extract(xml: bytes) -> dict:
    """Campos do DANFSe a partir do XML da NFS-e (já conferido pelo provider)."""
    root = nfse.parse_xml(xml)
    inf = root.find("n:infNFSe", _N)
    dps = inf.find("n:DPS/n:infDPS", _N)
    emit = inf.find("n:emit", _N)
    ender = emit.find("n:enderNac", _N) if emit is not None else None
    prest = dps.find("n:prest", _N)
    toma = dps.find("n:toma", _N)
    serv = dps.find("n:serv", _N)
    valores = inf.find("n:valores", _N)
    dps_valores = dps.find("n:valores", _N)
    trib = dps_valores.find("n:trib/n:tribMun", _N) if dps_valores is not None else None
    key = nfse.official_key_from_nfse_id(inf.get("Id")) or ""
    address = None
    if ender is not None:
        address = ", ".join(
            p
            for p in (
                _t(ender, "n:xLgr"),
                _t(ender, "n:nro"),
                _t(ender, "n:xCpl"),
                _t(ender, "n:xBairro"),
            )
            if p
        )
    v_serv = _t(dps_valores, "n:vServPrest/n:vServ")
    return {
        "key": key,
        "restricted": _t(dps, "n:tpAmb") == "2",
        "city": _t(inf, "n:xLocEmi"),
        "identification": [
            ("Número da NFS-e", _t(inf, "n:nNFSe")),
            ("Competência da NFS-e", _date(_t(dps, "n:dCompet"), time=False)),
            ("Data e Hora da emissão da NFS-e", _date(_t(inf, "n:dhProc"))),
            ("Número da DPS", _t(dps, "n:nDPS")),
            ("Série da DPS", _t(dps, "n:serie")),
            ("Data e Hora da emissão da DPS", _date(_t(dps, "n:dhEmi"))),
            ("Emitente da NFS-e", _EMITTER.get(_t(dps, "n:tpEmit") or "")),
            ("Situação da NFS-e", _STATUS.get(_t(inf, "n:cStat") or "")),
            ("Finalidade", "NFS-e regular"),
        ],
        "provider": [
            ("CNPJ / CPF / NIF", _tax_id(_t(emit, "n:CNPJ") or _t(emit, "n:CPF"))),
            ("Inscrição Municipal", _t(emit, "n:IM")),
            ("Telefone", _t(emit, "n:fone")),
            ("Nome / Nome Empresarial", _t(emit, "n:xNome")),
            (
                "Município / UF",
                " / ".join(filter(None, (_t(inf, "n:xLocEmi"), _t(ender, "n:UF")))) or None,
            ),
            (
                "Código IBGE / CEP",
                " / ".join(filter(None, (_t(ender, "n:cMun"), _t(ender, "n:CEP")))) or None,
            ),
            ("Endereço", address or None),
            ("E-mail", _t(emit, "n:email")),
            ("Simples Nacional na competência", _SIMPLES.get(_t(prest, "n:regTrib/n:opSimpNac") or "")),
            ("Regime de Apuração Tributária pelo SN", _t(prest, "n:regTrib/n:regApTribSN")),
        ],
        "recipient": _person(toma) if toma is not None else None,
        "service": [
            ("Código de Tributação Nacional", _t(serv, "n:cServ/n:cTribNac")),
            ("Código da NBS", _t(serv, "n:cServ/n:cNBS")),
            (
                "Local da Prestação",
                _t(inf, "n:xLocPrestacao") or _t(serv, "n:locPrest/n:cLocPrestacao"),
            ),
            ("Descrição do Código de Tributação Nacional", _t(inf, "n:xTribNac")),
        ],
        "description": _t(serv, "n:cServ/n:xDescServ") or "-",
        "municipal": [
            ("Tributação do ISSQN", _ISSQN.get(_t(trib, "n:tribISSQN") or "")),
            ("Município de Incidência do ISSQN", _t(inf, "n:xLocIncid")),
            ("Retenção do ISSQN", _RETENTION.get(_t(trib, "n:tpRetISSQN") or "")),
            ("BC ISSQN", _money(_t(valores, "n:vBC"))),
            ("Alíquota Aplicada", _t(valores, "n:pAliqAplic")),
            ("ISSQN Apurado", _money(_t(valores, "n:vISSQN"))),
        ],
        "totals": [
            ("Valor do Serviço", _money(v_serv)),
            ("Desconto Incondicionado", _money(_t(dps_valores, "n:vDescCondIncond/n:vDescIncond"))),
            ("Desconto Condicionado", _money(_t(dps_valores, "n:vDescCondIncond/n:vDescCond"))),
            ("Total das Retenções", _money(_t(valores, "n:vTotalRet"))),
            ("Valor Líquido da NFS-e", _money(_t(valores, "n:vLiq"))),
        ],
        "extra": _t(inf, "n:xOutInf") or _t(serv, "n:infoCompl/n:xInfComp"),
    }


def render(xml: bytes) -> bytes:
    """PDF do DANFSe (uma página A4)."""
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.colors import Color, black, white
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    data = extract(xml)
    gray = Color(0.95, 0.95, 0.95)
    red = Color(1, 0, 0)
    width, height = A4
    margin = 0.2 * cm
    left, right = margin, width - margin
    inner = right - left
    out = io.BytesIO()
    pdf = canvas.Canvas(out, pagesize=A4, pageCompression=1, invariant=1)
    pdf.setTitle("DANFSe")
    pdf.setAuthor("Caltech MEI")
    pdf.setLineWidth(1)
    pdf.rect(left, margin, inner, height - 2 * margin)

    def fit(text: str, font: str, size: float, limit: float) -> str:
        text = text or "-"
        if stringWidth(text, font, size) <= limit:
            return text
        while text and stringWidth(text + "...", font, size) > limit:
            text = text[:-1]
        return text + "..."

    y = height - margin
    # Cabeçalho (fundo cinza), título, município/ambiente e QR Code.
    header_h = 2.5 * cm
    pdf.setFillColor(gray)
    pdf.rect(left, y - header_h, inner, header_h, stroke=0, fill=1)
    pdf.setFillColor(black)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(left + 0.3 * cm, y - 1.0 * cm, "NFS-e")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawCentredString(width / 2, y - 0.8 * cm, "DANFSe v2.0")
    pdf.drawCentredString(width / 2, y - 1.15 * cm, "Documento Auxiliar da NFS-e")
    if data["restricted"]:
        pdf.setFillColor(red)
        pdf.drawCentredString(width / 2, y - 1.5 * cm, "NFS-e SEM VALIDADE JURÍDICA")
        pdf.setFillColor(black)
    pdf.setFont("Helvetica", 8)
    pdf.drawRightString(
        17.2 * cm, y - 0.8 * cm, fit(data["city"] or "-", "Helvetica", 8, 5 * cm)
    )
    pdf.drawRightString(
        17.2 * cm,
        y - 1.15 * cm,
        "Produção Restrita" if data["restricted"] else "Produção",
    )
    if data["key"]:
        qr = QrCodeWidget(CONSULTA_URL + data["key"])
        x0, y0, x1, y1 = qr.getBounds()
        side = 1.6 * cm
        drawing = Drawing(side, side, transform=[side / (x1 - x0), 0, 0, side / (y1 - y0), 0, 0])
        drawing.add(qr)
        renderPDF.draw(drawing, pdf, 17.48 * cm, height - 1.67 * cm - side)
    pdf.setFont("Helvetica", 6)
    for index, line in enumerate(
        (
            "A autenticidade desta NFS-e pode ser verificada",
            "pela leitura deste código QR ou pela consulta da",
            "chave de acesso no portal nacional da NFS-e",
        )
    ):
        pdf.drawString(13.2 * cm, height - 1.95 * cm - index * 7, line)
    y -= header_h
    pdf.setLineWidth(0.5)
    pdf.line(left, y, right, y)

    def title(text: str, *, shade: bool = True):
        nonlocal y
        h = 0.42 * cm
        if shade:
            pdf.setFillColor(gray)
            pdf.rect(left, y - h, inner, h, stroke=0, fill=1)
            pdf.setFillColor(black)
        pdf.setFont("Helvetica-Bold", 7)
        pdf.drawString(left + 0.15 * cm, y - h + 0.12 * cm, text)
        y -= h
        pdf.line(left, y, right, y)

    def grid(fields, columns: int = 4, shaded: tuple[str, ...] = ()):
        nonlocal y
        col_w = inner / columns
        row_h = 0.72 * cm
        for start in range(0, len(fields), columns):
            for index, (label, value) in enumerate(fields[start : start + columns]):
                x = left + index * col_w
                if label in shaded:
                    pdf.setFillColor(gray)
                    pdf.rect(x, y - row_h, col_w, row_h, stroke=0, fill=1)
                    pdf.setFillColor(black)
                pdf.setFont("Helvetica-Bold", 6)
                pdf.drawString(x + 0.12 * cm, y - 0.25 * cm, fit(label, "Helvetica-Bold", 6, col_w - 0.2 * cm))
                pdf.setFont("Helvetica", 7)
                pdf.drawString(x + 0.12 * cm, y - 0.58 * cm, fit(value or "-", "Helvetica", 7, col_w - 0.2 * cm))
            y -= row_h
        pdf.line(left, y, right, y)

    def note(text: str):
        nonlocal y
        h = 0.5 * cm
        pdf.setFont("Helvetica", 7)
        pdf.drawString(left + 0.15 * cm, y - 0.33 * cm, text)
        y -= h
        pdf.line(left, y, right, y)

    def paragraph(text: str, lines: int):
        nonlocal y
        pdf.setFont("Helvetica", 7)
        words = (text or "-").replace("\n", " \n ").split(" ")
        rows, current = [], ""
        for word in words:
            if word == "\n":
                rows.append(current)
                current = ""
                continue
            candidate = (current + " " + word).strip()
            if stringWidth(candidate, "Helvetica", 7) > inner - 0.3 * cm:
                rows.append(current)
                current = word
            else:
                current = candidate
        rows.append(current)
        if len(rows) > lines:
            rows = rows[:lines]
            rows[-1] = fit(rows[-1] + " …", "Helvetica", 7, inner - 0.3 * cm)
            if not rows[-1].endswith("..."):
                rows[-1] = rows[-1].rstrip(" …") + "..."
        for row in rows:
            pdf.drawString(left + 0.15 * cm, y - 0.3 * cm, row)
            y -= 0.3 * cm
        y -= 0.15 * cm
        pdf.line(left, y, right, y)

    title("CHAVE DE ACESSO DA NFS-e")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawCentredString(width / 2, y - 0.38 * cm, data["key"] or "-")
    y -= 0.55 * cm
    pdf.line(left, y, right, y)
    title("IDENTIFICAÇÃO DA NFS-e")
    grid(data["identification"], shaded=("Emitente da NFS-e",))
    title("PRESTADOR / FORNECEDOR")
    grid(data["provider"])
    title("TOMADOR / ADQUIRENTE DA OPERAÇÃO")
    if data["recipient"]:
        grid(data["recipient"])
    else:
        note("TOMADOR/ADQUIRENTE DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e")
    title("DESTINATÁRIO DA OPERAÇÃO")
    if data["recipient"]:
        note("O DESTINATÁRIO É O PRÓPRIO TOMADOR/ADQUIRENTE DA OPERAÇÃO")
    else:
        note("DESTINATÁRIO DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e")
    title("INTERMEDIÁRIO DA OPERAÇÃO")
    note("INTERMEDIÁRIO DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e")
    title("SERVIÇO PRESTADO")
    grid(data["service"])
    title("DESCRIÇÃO DO SERVIÇO", shade=False)
    paragraph(data["description"], 8)
    title("TRIBUTAÇÃO MUNICIPAL (ISSQN)")
    grid(data["municipal"], columns=3)
    title("TRIBUTAÇÃO FEDERAL (EXCETO CBS)")
    grid(
        [
            ("IRRF", "-"),
            ("Contribuição Previdenciária - Retida", "-"),
            ("Contribuições Sociais - Retidas", "-"),
            ("PIS / COFINS - Débito Apuração Própria", "-"),
        ]
    )
    title("TRIBUTAÇÃO IBS / CBS")
    note("-")
    title("VALOR TOTAL DA NFS-e")
    grid(data["totals"], columns=5, shaded=("Valor Líquido da NFS-e",))
    title("INFORMAÇÕES COMPLEMENTARES", shade=True)
    available = max(1, int((y - margin - 0.3 * cm) / (0.3 * cm)))
    paragraph(data["extra"] or "-", min(available, 12))
    pdf.setFillColor(white)
    pdf.showPage()
    pdf.save()
    return out.getvalue()
