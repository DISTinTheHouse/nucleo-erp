"""Fusión de OC + Recepción + factura de proveedor en un solo PDF (EC-397).

A propósito muy simple: reportlab arma una página de resumen para la OC y
otra para la recepción (puro texto/tablas, nada de plantillas ni imágenes),
y pypdf las junta con el PDF de la factura que subió el usuario. Ambas
librerías son puro Python/binarios estándar — corren igual en Vercel
serverless que en Oracle OCI el día que se migre, sin tocar nada.
"""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from pypdf import PdfReader, PdfWriter

from compras.models import OrdenCompraDetalle, RecepcionDetalle

_TABLE_STYLE = TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#333333")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ("FONTSIZE", (0, 0), (-1, -1), 9),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
])


def _render_oc_pdf(oc):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=1.5 * cm, bottomMargin=1.5 * cm)
    styles = getSampleStyleSheet()

    elementos = [
        Paragraph(f"Orden de Compra {oc.folio or oc.pk}", styles["Title"]),
        Spacer(1, 0.3 * cm),
        Paragraph(f"Proveedor: {getattr(oc.proveedor, 'nombre', '') or '-'}", styles["Normal"]),
        Paragraph(f"Sucursal: {getattr(oc.sucursal, 'nombre', '') or '-'}", styles["Normal"]),
        Paragraph(f"Fecha de generación: {oc.fecha_oc}", styles["Normal"]),
        Paragraph(f"Fecha de vencimiento: {oc.fecha_vencimiento or '-'}", styles["Normal"]),
        Spacer(1, 0.5 * cm),
    ]

    data = [["Producto", "Cantidad", "Precio", "Importe"]]
    detalles = OrdenCompraDetalle.objects.filter(orden_compra=oc).select_related("producto").order_by("id")
    for d in detalles:
        nombre = getattr(d.producto, "nombre", None) or d.descripcion or "-"
        data.append([nombre, str(d.cantidad), f"${d.precio:.2f}", f"${d.importe:.2f}"])

    tabla = Table(data, colWidths=[8 * cm, 2.5 * cm, 3 * cm, 3 * cm])
    tabla.setStyle(_TABLE_STYLE)
    elementos.append(tabla)
    elementos.append(Spacer(1, 0.5 * cm))
    elementos.append(Paragraph(f"Subtotal: ${oc.subtotal:.2f}", styles["Normal"]))
    elementos.append(Paragraph(f"IVA: ${oc.total_iva:.2f}", styles["Normal"]))
    elementos.append(Paragraph(f"Total: ${oc.gran_total:.2f}", styles["Normal"]))

    doc.build(elementos)
    return buffer.getvalue()


def _render_recepcion_pdf(recepcion):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=1.5 * cm, bottomMargin=1.5 * cm)
    styles = getSampleStyleSheet()

    elementos = [
        Paragraph(f"Recepción {recepcion.folio}", styles["Title"]),
        Spacer(1, 0.3 * cm),
        Paragraph(f"Almacén: {getattr(recepcion.almacen, 'nombre', '') or '-'}", styles["Normal"]),
        Paragraph(f"Fecha de recepción: {recepcion.fecha_recepcion}", styles["Normal"]),
        Paragraph(f"Remisión: {recepcion.remision or '-'}", styles["Normal"]),
        Spacer(1, 0.5 * cm),
    ]

    data = [["Producto", "Cantidad recibida"]]
    detalles = RecepcionDetalle.objects.filter(recepcion=recepcion).select_related("producto").order_by("id")
    for d in detalles:
        data.append([getattr(d.producto, "nombre", None) or "-", str(d.cantidad_recibida)])

    tabla = Table(data, colWidths=[10 * cm, 4 * cm])
    tabla.setStyle(_TABLE_STYLE)
    elementos.append(tabla)

    doc.build(elementos)
    return buffer.getvalue()


def fusionar_pdf_factura_proveedor(factura):
    """Devuelve los bytes de OC + Recepción (generadas aquí) + el PDF adjunto, en ese orden."""
    partes = [
        _render_oc_pdf(factura.oc),
        _render_recepcion_pdf(factura.recepcion),
        bytes(factura.pdf_adjunto),
    ]

    writer = PdfWriter()
    for parte in partes:
        reader = PdfReader(io.BytesIO(parte))
        for page in reader.pages:
            writer.add_page(page)

    salida = io.BytesIO()
    writer.write(salida)
    return salida.getvalue()
