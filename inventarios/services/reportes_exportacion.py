"""PDF y XLSX de los reportes por periodo de inventario (EC-814).

Reciben el reporte completo que calcula ``inventarios.api.reportes_periodo``
(mismo alcance, filtros y totales que el JSON) y replican el PDF que antes
armaba el front con react-pdf: A4 horizontal, Helvetica, encabezado "ERP
LAZZAR" + título + almacén, tarjetas de filtros y de generación, resumen del
periodo y la tabla con su encabezado repetido en cada página. Importes sin
símbolo de moneda (EC-606); cantidades con hasta 2 decimales.

reportlab y openpyxl son puro Python: corren igual en Vercel que en Oracle,
como ``finanzas/services/pdf_fusion_service.py``. Todo se arma en memoria.
"""
import io
from bisect import bisect_right
from decimal import ROUND_HALF_UP, Decimal
from itertools import accumulate
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.utils.text import slugify
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from reportlab.platypus import Flowable, HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from rest_framework.exceptions import ValidationError

from catalogo.models import Producto, ProductoVariante
from inventarios.api.reportes_periodo import to_decimal
from inventarios.models import Almacen
from nucleo.models import Empresa, Sucursal

ZONA_MEXICO = ZoneInfo("America/Mexico_City")
FORMATOS = ("pdf", "xlsx")
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
TIPOS_MOVIMIENTO_ETIQUETA = {
    "ENTRADA": "Entrada",
    "SALIDA": "Salida",
    "AJUSTE": "Ajuste",
    "TRANSFERENCIA": "Transferencia",
}
_MESES_CORTOS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept", "oct", "nov", "dic")
_SIN_DATO = "—"

# Paleta del PDF del front (``BasePdfColors``).
_C_BRAND = colors.HexColor("#0f172a")
_C_ACCENT = colors.HexColor("#0ea5e9")
_C_TEXT_SECONDARY = colors.HexColor("#475569")
_C_TEXT_MUTED = colors.HexColor("#94a3b8")
_C_BORDER = colors.HexColor("#e2e8f0")
_C_BORDER_LIGHT = colors.HexColor("#f1f5f9")
_C_ROW_ALT = colors.HexColor("#f8fafc")


class Exportacion:
    def __init__(self, contenido, formato, nombre_archivo):
        self.contenido = contenido
        self.content_type = CONTENT_TYPES[formato]
        self.nombre_archivo = nombre_archivo


def formato_requerido(query_params):
    """``?formato=pdf|xlsx`` (pdf por defecto), como ``?formato=`` de los CFDI.
    No es ``?format=``: ese lo reserva DRF para elegir el renderer."""
    formato = (query_params.get("formato") or "pdf").strip().lower()
    if formato not in FORMATOS:
        raise ValidationError({"formato": "Usa pdf o xlsx."})
    return formato


# --- Formato de valores (espejo de ``formatCurrency``/``formatDate`` del front) ---


def _con_miles(valor, decimales):
    q = to_decimal(valor).quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)  # sin "-0.00"
    return f"{q:,.{decimales}f}"


def formato_cantidad(valor):
    """es-MX, hasta 2 decimales sin ceros sobrantes: ``1,234.5``."""
    texto = _con_miles(valor, 2)
    return texto.rstrip("0").rstrip(".") if "." in texto else texto


def formato_dinero(valor):
    """2 decimales, sin símbolo de moneda: ``1,234.50``."""
    return _con_miles(valor, 2)


def formato_fecha(fecha):
    """``d/m/aaaa`` como ``toLocaleDateString("es-MX")``."""
    return f"{fecha.day}/{fecha.month}/{fecha.year}"


def _local(momento):
    if timezone.is_naive(momento):
        momento = timezone.make_aware(momento, ZONA_MEXICO)
    return momento.astimezone(ZONA_MEXICO)


def formato_generado(momento):
    """``dd/mm/aaaa, hh:mm`` en hora de México."""
    return _local(momento).strftime("%d/%m/%Y, %H:%M")


def formato_fecha_corta(momento):
    """``14 jul 2026`` en hora de México."""
    local = _local(momento)
    return f"{local.day:02d} {_MESES_CORTOS[local.month - 1]} {local.year}"


def _texto(valor):
    return str(valor) if valor not in (None, "") else _SIN_DATO


# --- Etiquetas de filtros ----------------------------------------------------------


def _etiqueta_almacen_existencias(reporte):
    almacen_id = reporte["almacen_id"]
    if not almacen_id:
        return "Todos los almacenes"
    almacen = reporte["almacenes"].get(almacen_id)
    if almacen:
        return f"{almacen['nombre']} ({almacen['codigo']})"
    return f"Almacén #{almacen_id}"


def _filtros_existencias(reporte):
    """Filtros aplicados, en el orden del PDF. Los nombres solo se resuelven
    cuando el registro está dentro del alcance del usuario (de sus almacenes),
    para no exponer nombres ajenos: si no, se muestra el id."""
    almacenes = reporte["almacenes"].values()
    filtros = [("Almacén", _etiqueta_almacen_existencias(reporte))]

    empresa_id = reporte["empresa_id"]
    if empresa_id:
        nombre = None
        if any(a["empresa_id"] == empresa_id for a in almacenes):
            nombre = Empresa.objects.filter(pk=empresa_id).values_list("razon_social", flat=True).first()
        filtros.append(("Empresa", nombre or f"Empresa #{empresa_id}"))

    sucursal_id = reporte["sucursal_id"]
    if sucursal_id:
        nombre = None
        if any(a["sucursal_id"] == sucursal_id for a in almacenes):
            nombre = Sucursal.objects.filter(pk=sucursal_id).values_list("nombre", flat=True).first()
        filtros.append(("Sucursal", nombre or f"Sucursal #{sucursal_id}"))

    if reporte["producto_id"]:
        nombre = next((f["producto_nombre"] for f in reporte["results"] if f["producto_id"] == reporte["producto_id"]), None)
        if nombre is None and reporte["almacenes"]:
            empresas = {a["empresa_id"] for a in almacenes}
            nombre = (
                Producto.objects.filter(pk=reporte["producto_id"], empresa_id__in=empresas)
                .values_list("nombre", flat=True).first()
            )
        filtros.append(("Producto", nombre or f"Producto #{reporte['producto_id']}"))

    if reporte["producto_variante_id"]:
        sku = next((f["sku"] for f in reporte["results"] if f["producto_variante_id"] == reporte["producto_variante_id"]), None)
        if sku is None and reporte["almacenes"]:
            empresas = {a["empresa_id"] for a in almacenes}
            sku = (
                ProductoVariante.objects.filter(pk=reporte["producto_variante_id"], empresa_id__in=empresas)
                .values_list("sku", flat=True).first()
            )
        filtros.append(("Variante", sku or f"Variante #{reporte['producto_variante_id']}"))

    filtros.append(("Periodo", _periodo(reporte)))
    return filtros


def _etiqueta_almacen_movimientos(reporte):
    almacen_id = reporte["almacen_id"]
    if not almacen_id:
        return "Todos los almacenes"
    for fila in reporte["results"]:
        if fila["almacen_id"] == almacen_id and fila["almacen_nombre"]:
            return fila["almacen_nombre"]
    if not reporte["sin_almacenes"]:
        # El almacén pedido está en el alcance (si no, el reporte va vacío).
        nombre = Almacen.objects.filter(pk=almacen_id).values_list("nombre", flat=True).first()
        if nombre:
            return nombre
    return f"Almacén #{almacen_id}"


def _periodo(reporte):
    return f"{formato_fecha(reporte['fecha_inicio'])} — {formato_fecha(reporte['fecha_final'])}"


def _nombre_archivo(partes, reporte, formato):
    piezas = [slugify(str(p)) for p in partes if p]
    rango = f"{reporte['fecha_inicio'].isoformat()}-a-{reporte['fecha_final'].isoformat()}"
    return "-".join(piezas + [rango]) + f".{formato}"


def _almacen_para_archivo(reporte, codigo=None):
    if not reporte["almacen_id"]:
        return "todos"
    return codigo or f"almacen-{reporte['almacen_id']}"


# --- PDF ---------------------------------------------------------------------------

_ANCHO_PAGINA, _ALTO_PAGINA = landscape(A4)
_MARGEN_X, _MARGEN_ARRIBA, _MARGEN_ABAJO = 36, 40, 48
_ANCHO_UTIL = _ANCHO_PAGINA - 2 * _MARGEN_X
_PAD_FILA = 8  # paddingHorizontal de encabezado y filas de la tabla
_PAD_COLUMNA = 6  # paddingRight de cada columna
_PAD_VERTICAL = 6  # paddingVertical de cada fila
_INTERLINEA = 9.6  # 8 pt x 1.2
_ALTO_ENCABEZADO = 7 * 1.2 + 2 * 7  # 7 pt + paddingVertical 7

_E_MARCA = ParagraphStyle("marca", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=_C_ACCENT, spaceAfter=4)
_E_TITULO = ParagraphStyle("titulo", fontName="Helvetica-Bold", fontSize=22, leading=26, textColor=_C_BRAND, spaceAfter=2)
_E_SUBTITULO = ParagraphStyle("subtitulo", fontName="Helvetica", fontSize=9, leading=11, textColor=_C_TEXT_SECONDARY)
_E_SECCION = ParagraphStyle("seccion", fontName="Helvetica-Bold", fontSize=9, leading=11, textColor=_C_BRAND, spaceAfter=6)


def _canvas_con_pie(texto_pie):
    """Canvas que difiere el pie hasta conocer el total de páginas
    ("Página N de M")."""

    class CanvasConPie(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._paginas = []

        def showPage(self):
            self._paginas.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._paginas)
            for estado in self._paginas:
                self.__dict__.update(estado)
                self._dibujar_pie(total)
                super().showPage()
            super().save()

        def _dibujar_pie(self, total):
            self.saveState()
            self.setStrokeColor(_C_BORDER)
            self.setLineWidth(1)
            self.line(_MARGEN_X, 33, _ANCHO_PAGINA - _MARGEN_X, 33)
            self.setFont("Helvetica", 7)
            self.setFillColor(_C_TEXT_MUTED)
            self.drawString(_MARGEN_X, 22, texto_pie)
            self.drawRightString(_ANCHO_PAGINA - _MARGEN_X, 22, f"Página {self._pageNumber} de {total}")
            self.restoreState()

    return CanvasConPie


def _tarjeta(titulo, filas, ancho):
    data = [[titulo, ""]] + [[etiqueta, valor] for etiqueta, valor in filas]
    tabla = Table(data, colWidths=[ancho / 3, ancho * 2 / 3], cornerRadii=[6, 6, 6, 6])
    tabla.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, _C_BORDER),
        ("SPAN", (0, 0), (1, 0)),
        ("FONT", (0, 0), (1, 0), "Helvetica-Bold", 7),
        ("TEXTCOLOR", (0, 0), (1, 0), _C_TEXT_MUTED),
        ("LINEBELOW", (0, 0), (1, 0), 1, _C_BORDER_LIGHT),
        ("BOTTOMPADDING", (0, 0), (1, 0), 5),
        ("FONT", (0, 1), (0, -1), "Helvetica", 8),
        ("TEXTCOLOR", (0, 1), (0, -1), _C_TEXT_MUTED),
        ("FONT", (1, 1), (1, -1), "Helvetica-Bold", 8),
        ("TEXTCOLOR", (1, 1), (1, -1), _C_BRAND),
        ("ALIGN", (1, 1), (1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, 0), 12),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 12),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return tabla


def _tarjetas(filtros, generacion):
    gap = 12
    ancho = (_ANCHO_UTIL - gap) / 2
    contenedor = Table(
        [[_tarjeta("FILTROS DEL REPORTE", filtros, ancho), "", _tarjeta("GENERACIÓN", generacion, ancho)]],
        colWidths=[ancho, gap, ancho],
    )
    contenedor.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return contenedor


def _resumen(celdas):
    gap = 8
    n = len(celdas)
    ancho = (_ANCHO_UTIL - gap * (n - 1)) / n
    fila_etiquetas, fila_valores, anchos = [], [], []
    for i, (etiqueta, valor) in enumerate(celdas):
        if i:
            fila_etiquetas.append("")
            fila_valores.append("")
            anchos.append(gap)
        fila_etiquetas.append(etiqueta)
        fila_valores.append(valor)
        anchos.append(ancho)
    tabla = Table([fila_etiquetas, fila_valores], colWidths=anchos)
    estilo = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 6.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), _C_TEXT_MUTED),
        ("FONT", (0, 1), (-1, 1), "Helvetica-Bold", 11),
        ("TEXTCOLOR", (0, 1), (-1, 1), _C_BRAND),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
        ("TOPPADDING", (0, 1), (-1, 1), 2),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
    ]
    for i in range(0, 2 * n - 1, 2):
        estilo.append(("BOX", (i, 0), (i, 1), 1, _C_BORDER))
    tabla.setStyle(TableStyle(estilo))
    return tabla


def _partir(texto, fuente, ancho, tamano=8):
    """Líneas que caben en ``ancho``: por palabras y, si una palabra sola no
    cabe (un SKU largo), por caracteres, como hace react-pdf."""
    lineas = []
    for linea in simpleSplit(texto, fuente, tamano, ancho) or [texto]:
        while stringWidth(linea, fuente, tamano) > ancho and len(linea) > 1:
            corte = len(linea) - 1
            while corte > 1 and stringWidth(linea[:corte], fuente, tamano) > ancho:
                corte -= 1
            lineas.append(linea[:corte])
            linea = linea[corte:]
        lineas.append(linea)
    return lineas


class _TablaDetalle(Flowable):
    """Tabla de detalle con su encabezado repetido en cada página.

    No usa ``platypus.Table``: con miles de filas, ``Table`` vuelve a medir y
    a copiar todas las filas restantes en cada salto de página (cuadrático) y
    dibuja cada celda con su propio objeto de texto. Aquí las alturas se
    calculan una vez (sumas acumuladas para partir en O(log n)) y cada página
    se dibuja con un solo objeto de texto. Mismo aspecto que la tabla del
    front: encabezado oscuro, filas alternadas y línea inferior.
    """

    def __init__(self, columnas, anchos, filas, alturas, acumuladas, inicio, fin):
        super().__init__()
        self.columnas = columnas
        self.anchos = anchos
        self.filas = filas
        self.alturas = alturas
        self.acumuladas = acumuladas
        self.inicio = inicio
        self.fin = fin

    @classmethod
    def crear(cls, columnas, filas, texto_vacio):
        """``columnas``: (encabezado, flex, alineación, negritas, envolver).
        ``filas``: listas de textos ya formateados."""
        total_flex = sum(c[1] for c in columnas)
        disponible = _ANCHO_UTIL - 2 * _PAD_FILA
        anchos = [disponible * c[1] / total_flex for c in columnas]
        anchos[0] += _PAD_FILA
        anchos[-1] += _PAD_FILA
        ultima = len(anchos) - 1

        celdas_filas = []
        for fila in filas:
            celdas = []
            for i, valor in enumerate(fila):
                encabezado, _, _, negritas, envolver = columnas[i]
                if envolver:
                    ancho = anchos[i] - _PAD_COLUMNA - (_PAD_FILA if i in (0, ultima) else 0)
                    lineas = _partir(valor, "Helvetica-Bold" if negritas else "Helvetica", ancho)
                else:
                    lineas = valor.split("\n")
                celdas.append(lineas)
            celdas_filas.append(celdas)
        if not celdas_filas:
            # Fila única con el aviso, ocupando todo el ancho.
            celdas_filas.append([[texto_vacio]] + [[] for _ in columnas[1:]])

        alturas = [max(len(c) for c in celdas) * _INTERLINEA + 2 * _PAD_VERTICAL for celdas in celdas_filas]
        acumuladas = list(accumulate(alturas, initial=0))
        return cls(columnas, anchos, celdas_filas, alturas, acumuladas, 0, len(celdas_filas))

    def _alto(self, inicio, fin):
        return _ALTO_ENCABEZADO + self.acumuladas[fin] - self.acumuladas[inicio]

    def wrap(self, ancho_disponible, alto_disponible):
        self.width = sum(self.anchos)
        self.height = self._alto(self.inicio, self.fin)
        return self.width, self.height

    def split(self, ancho_disponible, alto_disponible):
        limite = self.acumuladas[self.inicio] + alto_disponible - _ALTO_ENCABEZADO
        corte = bisect_right(self.acumuladas, limite, self.inicio, self.fin + 1) - 1
        if corte <= self.inicio:
            return []
        argumentos = (self.columnas, self.anchos, self.filas, self.alturas, self.acumuladas)
        return [
            _TablaDetalle(*argumentos, self.inicio, corte),
            _TablaDetalle(*argumentos, corte, self.fin),
        ]

    def draw(self):
        c = self.canv
        ancho_total = self.width
        ultima = len(self.anchos) - 1
        izquierdas = list(accumulate(self.anchos, initial=0))

        def x_texto(i, alineacion):
            if alineacion == "RIGHT":
                return izquierdas[i + 1] - _PAD_COLUMNA - (_PAD_FILA if i == ultima else 0)
            return izquierdas[i] + (_PAD_FILA if i == 0 else 0)

        # Encabezado.
        arriba = self.height
        c.setFillColor(_C_BRAND)
        c.roundRect(0, arriba - _ALTO_ENCABEZADO, ancho_total, _ALTO_ENCABEZADO, 4, stroke=0, fill=1)
        texto = c.beginText()
        texto.setFont("Helvetica-Bold", 7)
        texto.setFillColor(_C_TEXT_MUTED)
        base = arriba - 7 - 7 * 0.8
        for i, (encabezado, _, alineacion, _, _) in enumerate(self.columnas):
            x = x_texto(i, alineacion)
            if alineacion == "RIGHT":
                x -= stringWidth(encabezado, "Helvetica-Bold", 7)
            texto.setTextOrigin(x, base)
            texto.textOut(encabezado)
        c.drawText(texto)

        # Fondos alternados y líneas inferiores.
        arriba -= _ALTO_ENCABEZADO
        y = arriba
        c.setStrokeColor(_C_BORDER_LIGHT)
        c.setLineWidth(1)
        c.setFillColor(_C_ROW_ALT)
        for indice in range(self.inicio, self.fin):
            alto = self.alturas[indice]
            if indice % 2:
                c.rect(0, y - alto, ancho_total, alto, stroke=0, fill=1)
            c.line(0, y - alto, ancho_total, y - alto)
            y -= alto

        # Texto de las celdas, en un solo objeto de texto por página. Los
        # anchos se cachean: las columnas numéricas repiten mucho ("0").
        anchos_texto = {}

        def ancho(linea, fuente):
            clave = (linea, fuente)
            if clave not in anchos_texto:
                anchos_texto[clave] = stringWidth(linea, fuente, 8)
            return anchos_texto[clave]

        texto = c.beginText()
        fuente_actual = None
        y = arriba
        for indice in range(self.inicio, self.fin):
            primera_base = y - _PAD_VERTICAL - 8 * 0.8 - (_INTERLINEA - 8) / 2
            for i, lineas in enumerate(self.filas[indice]):
                _, _, alineacion, negritas, _ = self.columnas[i]
                fuente = "Helvetica-Bold" if negritas else "Helvetica"
                if fuente != fuente_actual:
                    texto.setFont(fuente, 8, _INTERLINEA)
                    texto.setFillColor(_C_BRAND if negritas else _C_TEXT_SECONDARY)
                    fuente_actual = fuente
                base = primera_base
                for linea in lineas:
                    x = x_texto(i, alineacion)
                    if alineacion == "RIGHT":
                        x -= ancho(linea, fuente)
                    texto.setTextOrigin(x, base)
                    texto.textOut(linea)
                    base -= _INTERLINEA
            y -= self.alturas[indice]
        c.drawText(texto)


def _pdf(titulo_documento, titulo, subtitulo, filtros, generacion, resumen, seccion, columnas, filas, vacio, pie):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=_MARGEN_X,
        rightMargin=_MARGEN_X,
        topMargin=_MARGEN_ARRIBA,
        bottomMargin=_MARGEN_ABAJO,
        title=titulo_documento,
        author="ERP LAZZAR",
    )
    elementos = [
        Paragraph("ERP LAZZAR", _E_MARCA),
        Paragraph(escape(titulo), _E_TITULO),
        Paragraph(escape(subtitulo), _E_SUBTITULO),
        Spacer(1, 18),
        HRFlowable(width="100%", thickness=2, color=_C_BRAND, spaceBefore=0, spaceAfter=24),
        _tarjetas(filtros, generacion),
        Spacer(1, 16),
        Paragraph("RESUMEN DEL PERIODO", _E_SECCION),
        _resumen(resumen),
        Spacer(1, 16),
        Paragraph(seccion, _E_SECCION),
        _TablaDetalle.crear(columnas, filas, vacio),
    ]
    doc.build(elementos, canvasmaker=_canvas_con_pie(pie))
    return buffer.getvalue()


# --- XLSX --------------------------------------------------------------------------

_X_MARCA = Font(bold=True, color="0EA5E9", size=8)
_X_TITULO = Font(bold=True, size=14, color="0F172A")
_X_NEGRITAS = Font(bold=True, color="0F172A")
_X_SECCION = Font(bold=True, color="0F172A", size=10)
_X_ETIQUETA = Font(color="64748B")
_X_ENCABEZADO = Font(bold=True, color="FFFFFF")
_X_RELLENO_ENCABEZADO = PatternFill("solid", fgColor="0F172A")
_FMT_CANTIDAD = "#,##0.00"
_FMT_DINERO = "#,##0.00"
_FMT_ENTERO = "#,##0"
_FMT_FECHA_HORA = "dd/mm/yyyy hh:mm"


def _xlsx(titulo_hoja, titulo, subtitulo, filtros, generacion, resumen, seccion, columnas, filas):
    """``generacion``/``resumen``: (etiqueta, valor, formato). ``columnas``:
    (encabezado, ancho, formato). ``filas``: valores crudos (números reales)."""
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(titulo_hoja)
    for i, (_, ancho, _) in enumerate(columnas):
        ws.column_dimensions[get_column_letter(i + 1)].width = ancho

    def celda(valor, fuente=None, formato=None, alineacion=None, relleno=None):
        c = WriteOnlyCell(ws, value=valor)
        if isinstance(valor, str):
            # Siempre texto: openpyxl guarda como fórmula lo que empieza con "="
            # (y como error "#N/A", "#REF!"...). Un nombre de producto o de
            # usuario no debe volverse una fórmula activa al abrir el archivo.
            c.data_type = "s"
        if fuente:
            c.font = fuente
        if formato:
            c.number_format = formato
        if alineacion:
            c.alignment = alineacion
        if relleno:
            c.fill = relleno
        return c

    filas_previas = [
        [celda("ERP LAZZAR", _X_MARCA)],
        [celda(titulo, _X_TITULO)],
        [celda(subtitulo, _X_ETIQUETA)],
        [],
        [celda("FILTROS DEL REPORTE", _X_SECCION)],
        *[[celda(etiqueta, _X_ETIQUETA), celda(valor, _X_NEGRITAS)] for etiqueta, valor in filtros],
        [],
        [celda("GENERACIÓN", _X_SECCION)],
        *[[celda(etiqueta, _X_ETIQUETA), celda(valor, _X_NEGRITAS, formato)] for etiqueta, valor, formato in generacion],
        [],
        [celda("RESUMEN DEL PERIODO", _X_SECCION)],
        [celda(etiqueta, _X_ETIQUETA) for etiqueta, _, _ in resumen],
        [celda(valor, _X_NEGRITAS, formato) for _, valor, formato in resumen],
        [],
        [celda(seccion, _X_SECCION)],
    ]
    fila_encabezado = len(filas_previas) + 1
    ws.freeze_panes = f"A{fila_encabezado + 1}"
    ultima = fila_encabezado + max(len(filas), 1)
    ws.auto_filter.ref = f"A{fila_encabezado}:{get_column_letter(len(columnas))}{ultima}"

    for fila in filas_previas:
        ws.append(fila)
    derecha = Alignment(horizontal="right")
    ws.append([
        celda(encabezado, _X_ENCABEZADO, None, derecha if formato else None, _X_RELLENO_ENCABEZADO)
        for encabezado, _, formato in columnas
    ])
    for fila in filas:
        ws.append([
            celda(valor, None, formato) if (formato and valor is not None) or isinstance(valor, str) else valor
            for valor, (_, _, formato) in zip(fila, columnas)
        ])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# --- Existencias -------------------------------------------------------------------

_COLUMNAS_EXISTENCIAS_PDF = (
    # encabezado, flex (``StockReportPdfStyles``), alineación, negritas, envolver
    ("SKU", 1.6, "LEFT", True, True),
    ("Producto", 2.6, "LEFT", False, True),
    ("Color", 1.6, "LEFT", False, True),
    ("Talla", 1.4, "LEFT", False, True),
    ("Almacén", 2.6, "LEFT", False, True),
    ("Exist. Inicial", 1.4, "RIGHT", False, False),
    ("Entradas", 1.4, "RIGHT", False, False),
    ("Salidas", 1.4, "RIGHT", False, False),
    ("Exist. Final", 1.4, "RIGHT", True, False),
    ("Costo Unitario", 1.7, "RIGHT", False, False),
    ("Costo Total", 1.7, "RIGHT", True, False),
)
_COLUMNAS_EXISTENCIAS_XLSX = (
    ("SKU", 18, None),
    ("Producto", 40, None),
    ("Color", 16, None),
    ("Talla", 10, None),
    ("Almacén", 26, None),
    ("Exist. Inicial", 14, _FMT_CANTIDAD),
    ("Entradas", 12, _FMT_CANTIDAD),
    ("Salidas", 12, _FMT_CANTIDAD),
    ("Exist. Final", 14, _FMT_CANTIDAD),
    ("Costo Unitario", 15, _FMT_DINERO),
    ("Costo Total", 15, _FMT_DINERO),
)


def exportar_existencias(reporte, formato, generado_en=None):
    generado_en = generado_en or timezone.now()
    almacen = _etiqueta_almacen_existencias(reporte)
    filtros = _filtros_existencias(reporte)
    resumen = reporte["resumen"]
    filas = reporte["results"]
    codigo = (reporte["almacenes"].get(reporte["almacen_id"]) or {}).get("codigo")
    nombre = _nombre_archivo(["reporte-existencias", _almacen_para_archivo(reporte, codigo)], reporte, formato)
    titulo = "Reporte de Inventario"

    if formato == "pdf":
        contenido = _pdf(
            titulo_documento=f"{titulo} — {almacen}",
            titulo=titulo,
            subtitulo=almacen,
            filtros=filtros,
            generacion=[
                ("Registros totales", f"{len(filas):,}"),
                ("Generado el", formato_generado(generado_en)),
            ],
            resumen=[
                ("EXIST. INICIAL", formato_cantidad(resumen["existencia_inicial"])),
                ("ENTRADAS", formato_cantidad(resumen["entradas"])),
                ("SALIDAS", formato_cantidad(resumen["salidas"])),
                ("EXIST. FINAL", formato_cantidad(resumen["existencia_final"])),
                ("COSTO TOTAL", formato_dinero(resumen["costo_total_existencia_final"])),
            ],
            seccion="DETALLE DE EXISTENCIAS",
            columnas=_COLUMNAS_EXISTENCIAS_PDF,
            filas=[
                [
                    _texto(f["sku"]),
                    _texto(f["producto_nombre"]),
                    _texto(f["color"]),
                    _texto(f["talla"]),
                    _texto(f["almacen_nombre"]),
                    formato_cantidad(f["existencia_inicial"]),
                    formato_cantidad(f["entradas"]),
                    formato_cantidad(f["salidas"]),
                    formato_cantidad(f["existencia_final"]),
                    formato_dinero(f["costo_unitario_final"]),
                    formato_dinero(f["costo_existencia_final"]),
                ]
                for f in filas
            ],
            vacio="Sin existencias en el periodo.",
            pie=f"{titulo} — {almacen}",
        )
    else:
        contenido = _xlsx(
            titulo_hoja="Existencias",
            titulo=titulo,
            subtitulo=almacen,
            filtros=filtros,
            generacion=[
                ("Registros totales", len(filas), _FMT_ENTERO),
                ("Generado el", formato_generado(generado_en), None),
            ],
            resumen=[
                ("Exist. inicial", to_decimal(resumen["existencia_inicial"]), _FMT_CANTIDAD),
                ("Entradas", to_decimal(resumen["entradas"]), _FMT_CANTIDAD),
                ("Salidas", to_decimal(resumen["salidas"]), _FMT_CANTIDAD),
                ("Exist. final", to_decimal(resumen["existencia_final"]), _FMT_CANTIDAD),
                ("Costo total", to_decimal(resumen["costo_total_existencia_final"]), _FMT_DINERO),
            ],
            seccion="DETALLE DE EXISTENCIAS",
            columnas=_COLUMNAS_EXISTENCIAS_XLSX,
            filas=[
                [
                    f["sku"],
                    f["producto_nombre"],
                    f["color"],
                    f["talla"],
                    f["almacen_nombre"],
                    to_decimal(f["existencia_inicial"]),
                    to_decimal(f["entradas"]),
                    to_decimal(f["salidas"]),
                    to_decimal(f["existencia_final"]),
                    to_decimal(f["costo_unitario_final"]),
                    to_decimal(f["costo_existencia_final"]),
                ]
                for f in filas
            ],
        )
    return Exportacion(contenido, formato, nombre)


# --- Movimientos -------------------------------------------------------------------

_COLUMNAS_MOVIMIENTOS_PDF = (
    # encabezado, flex (``StockMovementReportPdfStyles``), alineación, negritas, envolver
    ("Fecha", 1.8, "LEFT", True, False),
    ("Tipo", 1.0, "LEFT", False, True),
    ("Producto", 3.2, "LEFT", False, True),
    ("Almacén", 2.4, "LEFT", False, True),
    ("Ubicación", 2.0, "LEFT", False, True),
    ("Cantidad", 1.1, "RIGHT", True, False),
    ("Costo Total", 1.4, "RIGHT", False, False),
    ("Usuario", 2.2, "LEFT", False, True),
)
_COLUMNAS_MOVIMIENTOS_XLSX = (
    ("Fecha", 17, _FMT_FECHA_HORA),
    ("Tipo", 14, None),
    ("Producto", 40, None),
    ("Almacén", 26, None),
    ("Ubicación", 30, None),
    ("Cantidad", 12, _FMT_CANTIDAD),
    ("Costo Total", 15, _FMT_DINERO),
    ("Usuario", 28, None),
)


def _fecha_hora_excel(momento):
    # Excel no guarda zona horaria: hora de México, sin tz.
    return _local(momento).replace(tzinfo=None, microsecond=0) if momento else None


def exportar_movimientos(reporte, formato, generado_en=None):
    generado_en = generado_en or timezone.now()
    tipo = TIPOS_MOVIMIENTO_ETIQUETA[reporte["tipo_movimiento"]]
    almacen = _etiqueta_almacen_movimientos(reporte)
    filas = reporte["results"]
    resumen = reporte["resumen"]
    filtros = [("Tipo de movimiento", tipo), ("Almacén", almacen), ("Periodo", _periodo(reporte))]
    codigo = next((f["almacen_codigo"] for f in filas if f["almacen_id"] == reporte["almacen_id"]), None)
    nombre = _nombre_archivo(
        ["reporte-movimientos", reporte["tipo_movimiento"], _almacen_para_archivo(reporte, codigo)], reporte, formato,
    )
    titulo = "Reporte de Movimientos de Inventario"
    subtitulo = f"{tipo} · {almacen}"

    def producto(f):
        return f["producto_nombre"] or f["producto_base_nombre"]

    if formato == "pdf":
        contenido = _pdf(
            titulo_documento=f"{titulo} — {subtitulo}",
            titulo=titulo,
            subtitulo=subtitulo,
            filtros=filtros,
            generacion=[
                ("Registros totales", f"{len(filas):,}"),
                ("Generado el", formato_generado(generado_en)),
            ],
            resumen=[
                ("TOTAL MOVIMIENTOS", formato_cantidad(resumen["total_movimientos"])),
                ("TOTAL REGISTROS", formato_cantidad(resumen["total_registros"])),
                ("CANTIDAD TOTAL", formato_cantidad(resumen["total_cantidad"])),
            ],
            seccion="DETALLE DE MOVIMIENTOS",
            columnas=_COLUMNAS_MOVIMIENTOS_PDF,
            filas=[
                [
                    (
                        f"{formato_fecha_corta(f['fecha_movimiento'])}\n{_local(f['fecha_movimiento']).strftime('%H:%M')}"
                        if f["fecha_movimiento"] else _SIN_DATO
                    ),
                    TIPOS_MOVIMIENTO_ETIQUETA.get(f["tipo_movimiento"], _texto(f["tipo_movimiento"])),
                    _texto(producto(f)),
                    _texto(f["almacen_nombre"]),
                    _texto(f["ubicacion_nombre"]),
                    formato_cantidad(f["cantidad"]),
                    formato_dinero(f["costo_total"]),
                    _texto(f["usuario_nombre"]),
                ]
                for f in filas
            ],
            vacio="Sin movimientos en el periodo.",
            pie=f"Reporte de Movimientos — {subtitulo}",
        )
    else:
        contenido = _xlsx(
            titulo_hoja="Movimientos",
            titulo=titulo,
            subtitulo=subtitulo,
            filtros=filtros,
            generacion=[
                ("Registros totales", len(filas), _FMT_ENTERO),
                ("Generado el", formato_generado(generado_en), None),
            ],
            resumen=[
                ("Total movimientos", resumen["total_movimientos"], _FMT_ENTERO),
                ("Total registros", resumen["total_registros"], _FMT_ENTERO),
                ("Cantidad total", to_decimal(resumen["total_cantidad"]), _FMT_CANTIDAD),
            ],
            seccion="DETALLE DE MOVIMIENTOS",
            columnas=_COLUMNAS_MOVIMIENTOS_XLSX,
            filas=[
                [
                    _fecha_hora_excel(f["fecha_movimiento"]),
                    TIPOS_MOVIMIENTO_ETIQUETA.get(f["tipo_movimiento"], f["tipo_movimiento"]),
                    producto(f),
                    f["almacen_nombre"],
                    f["ubicacion_nombre"],
                    to_decimal(f["cantidad"]),
                    to_decimal(f["costo_total"]),
                    f["usuario_nombre"],
                ]
                for f in filas
            ],
        )
    return Exportacion(contenido, formato, nombre)
