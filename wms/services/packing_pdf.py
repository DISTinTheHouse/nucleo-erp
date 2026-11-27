"""PDF del packing: lista de empaque agrupada por caja y una etiqueta por caja.

Se arma desde el packing leído con las precargas de ``PackingViewSet`` (cajas y
renglones en memoria): el PDF no agrega consultas por caja ni por renglón.
Un packing sin cajas (formato anterior) sale como una sola sección "Sin caja".
"""
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from wms.api.serializers import _cajas_de_packing, PackingDetalleReadSerializer


def _cliente(packing):
    return getattr(getattr(packing.pedido, "cliente", None), "nombre", None) or "—"


def generar_pdf_packing(packing):
    estilos = getSampleStyleSheet()
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm, title=f"Packing {packing.folio}")
    pedido = getattr(packing.pedido, "folio", None) or "—"
    cliente = _cliente(packing)
    cajas = _cajas_de_packing(packing)
    el = [
        Paragraph(f"Lista de empaque {packing.folio}", estilos["Title"]),
        Paragraph(f"Pedido: {pedido} &nbsp;&nbsp; Cliente: {cliente} &nbsp;&nbsp; "
                  f"Cajas: {packing.numero_cajas} &nbsp;&nbsp; Peso total: {packing.peso_total} kg",
                  estilos["Normal"]),
        Spacer(1, 6 * mm),
    ]
    secciones = [
        (f"Caja {c['numero']} de {len(cajas)} — {c['peso']} kg — {c['piezas']} piezas", c["contenido"])
        for c in cajas
    ]
    sin_caja = [d for d in packing.packing_detalle.all() if not d.caja_id]
    if sin_caja:
        filas = PackingDetalleReadSerializer(sin_caja, many=True).data
        secciones.append(("Sin caja", [
            {"sku": f["sku"], "producto_nombre": f["producto_nombre"], "color_nombre": f["color_nombre"],
             "talla_nombre": f["talla_nombre"], "cantidad": f["cantidad_empacada"]} for f in filas]))
    for titulo, contenido in secciones:
        el.append(Paragraph(titulo, estilos["Heading3"]))
        datos = [["SKU", "Producto", "Color", "Talla", "Cantidad"]] + [
            [r.get("sku") or "—", Paragraph(r.get("producto_nombre") or "—", estilos["BodyText"]),
             r.get("color_nombre") or "—", r.get("talla_nombre") or "—", r.get("cantidad")]
            for r in contenido
        ]
        t = Table(datos, colWidths=[35 * mm, 75 * mm, 25 * mm, 20 * mm, 25 * mm], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (-1, 1), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        el += [t, Spacer(1, 5 * mm)]

    # Etiquetas: una página por caja.
    grande = estilos["Title"].clone("grande", fontSize=30, leading=36)
    for c in cajas:
        el += [
            PageBreak(),
            Paragraph(f"Caja {c['numero']} de {len(cajas)}", grande),
            Spacer(1, 8 * mm),
        ]
        datos = [["Folio", packing.folio], ["Pedido", pedido], ["Cliente", Paragraph(cliente, estilos["Normal"])],
                 ["Peso", f"{c['peso']} kg"], ["Piezas", c["piezas"]]]
        t = Table(datos, colWidths=[40 * mm, 120 * mm])
        t.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 16), ("LEADING", (0, 0), (-1, -1), 20),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("BOX", (0, 0), (-1, -1), 1, colors.black), ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        el.append(t)
    doc.build(el)
    return buffer.getvalue()
