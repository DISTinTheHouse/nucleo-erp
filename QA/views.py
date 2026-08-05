import json
import logging
import uuid
from decimal import Decimal
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import truncatechars
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from catalogo.models import Producto, ProductoVariante
from compras.models import OrdenCompra, OrdenCompraDetalle, RecepcionRFIDEncuadre, RecepcionRFIDLectura
from inventarios.models import Almacen
from nucleo.models import Empresa, Sucursal, UnidadMedida
from produccion.models import (
    BomDetalle,
    ListaMaterialBom,
    OrdenProduccion,
    OrdenProduccionDetalle,
)
from wms.models import EtiquetaRFIDDetalle, RfidScan

rfid_scanner_logger = logging.getLogger(__name__)


def _empresa_qa(request):
    empresa = getattr(request.user, "empresa", None)
    if empresa:
        return empresa
    return Empresa.objects.first()


def _redirect_rfid(encuadre_id):
    return redirect(f"{reverse('qa_recepcion_rfid_workspace')}?encuadre={encuadre_id}")


def _build_producto_label_zpl(variante):
    producto = variante.producto
    nombre_producto = truncatechars((producto.nombre or "").upper(), 32)
    sku = (variante.sku or "").upper()
    color = getattr(variante.color, "nombre", "")
    talla = getattr(variante.talla, "nombre", "")
    linea_secundaria = " / ".join(
        [value for value in [color.upper(), talla.upper()] if value]
    )
    codigo = (producto.codigo or producto.cod_proscai or "").upper()

    lines = [
        "^XA",
        "^PW799",
        "^LL400",
        "^CI28",
        "^LH0,0",
        "^FO40,30^A0N,34,34^FDQA RFID - ETIQUETA PRUEBA^FS",
        f"^FO40,85^A0N,32,32^FD{nombre_producto}^FS",
        f"^FO40,130^A0N,28,28^FDSKU: {sku}^FS",
    ]
    if linea_secundaria:
        lines.append(f"^FO40,168^A0N,28,28^FD{linea_secundaria}^FS")
    if codigo:
        lines.append(f"^FO40,206^A0N,26,26^FDCOD: {codigo}^FS")
    lines.extend(
        [
            f"^FO40,245^BY3,3,90^BCN,90,Y,N,N^FD{sku}^FS",
            "^FO40,360^A0N,22,22^FDImpresion QA para prueba de escaneo local.^FS",
            "^XZ",
        ]
    )
    return "\n".join(lines)


def _build_producto_base_label_zpl(producto):
    nombre_producto = truncatechars((producto.nombre or "").upper(), 32)
    codigo_impresion = (producto.codigo or producto.cod_proscai or f"PROD-{producto.pk}").upper()
    codigo_auxiliar = (producto.cod_proscai or "").upper()

    lines = [
        "^XA",
        "^PW799",
        "^LL400",
        "^CI28",
        "^LH0,0",
        "^FO40,30^A0N,34,34^FDQA RFID - ETIQUETA PRUEBA^FS",
        f"^FO40,85^A0N,32,32^FD{nombre_producto}^FS",
        f"^FO40,130^A0N,28,28^FDCODIGO: {codigo_impresion}^FS",
    ]
    if codigo_auxiliar and codigo_auxiliar != codigo_impresion:
        lines.append(f"^FO40,168^A0N,26,26^FDPROSCAI: {codigo_auxiliar}^FS")
    lines.extend(
        [
            f"^FO40,245^BY3,3,90^BCN,90,Y,N,N^FD{codigo_impresion}^FS",
            "^FO40,360^A0N,22,22^FDImpresion QA desde catalogo de productos.^FS",
            "^XZ",
        ]
    )
    return "\n".join(lines)


def _build_label_preview(variante=None, producto=None):
    if variante is not None:
        producto_base = variante.producto
        return {
            "header": f"SKU {variante.sku} · {producto_base.nombre}",
            "title": producto_base.nombre,
            "primary_line": f"SKU: {variante.sku}",
            "secondary_line": f"{variante.color.nombre} / {variante.talla.nombre}",
            "meta_line": (
                f"COD: {producto_base.codigo or producto_base.cod_proscai}"
                if (producto_base.codigo or producto_base.cod_proscai)
                else ""
            ),
            "barcode_value": variante.sku,
        }

    if producto is not None:
        codigo_impresion = producto.codigo or producto.cod_proscai or str(producto.pk)
        meta_line = ""
        if producto.cod_proscai and producto.cod_proscai != producto.codigo:
            meta_line = f"PROSCAI: {producto.cod_proscai}"
        return {
            "header": f"COD {codigo_impresion} · {producto.nombre}",
            "title": producto.nombre,
            "primary_line": f"COD: {codigo_impresion}",
            "secondary_line": "",
            "meta_line": meta_line,
            "barcode_value": codigo_impresion,
        }

    return None


def _browserprint_asset_path(filename):
    allowed_files = {
        "BrowserPrint-3.1.250.min.js",
        "BrowserPrint-Zebra-1.1.250.min.js",
    }
    if filename not in allowed_files:
        raise Http404("Asset no permitido.")
    asset_path = Path(__file__).resolve().parent / "static" / "QA" / "js" / filename
    if not asset_path.exists():
        raise Http404("Asset no encontrado.")
    return asset_path


def _lookup_tokens(raw_tag):
    raw_tag = (raw_tag or "").strip()
    if not raw_tag:
        return []

    tokens = {raw_tag, raw_tag.upper()}
    for sep in ("|", ",", ";"):
        if sep not in raw_tag:
            continue
        for part in raw_tag.split(sep):
            value = part.strip()
            if not value:
                continue
            tokens.add(value)
            if "=" in value:
                tokens.add(value.split("=", 1)[1].strip())
            if ":" in value:
                tokens.add(value.split(":", 1)[1].strip())
    return [token for token in tokens if token]


def _resolver_tag_recepcion(encuadre, codigo_tag):
    tokens = _lookup_tokens(codigo_tag)
    if not tokens:
        return {
            "producto": None,
            "producto_variante": None,
            "orden_compra_detalle": None,
            "metadata": {"resolved": False},
        }

    producto_variante = (
        ProductoVariante.objects.select_related("producto")
        .filter(empresa=encuadre.empresa, activo=True, sku__in=tokens)
        .first()
    )

    producto = None
    if producto_variante:
        producto = producto_variante.producto
    else:
        producto = (
            Producto.objects.filter(empresa=encuadre.empresa, activo=True)
            .filter(Q(codigo__in=tokens) | Q(cod_proscai__in=tokens))
            .first()
        )

    orden_compra_detalle = None
    if encuadre.orden_compra_id and producto:
        orden_compra_detalle = (
            encuadre.orden_compra.ordencompradetalle_set.select_related("producto")
            .filter(producto_id=producto.pk)
            .order_by("id")
            .first()
        )

    return {
        "producto": producto,
        "producto_variante": producto_variante,
        "orden_compra_detalle": orden_compra_detalle,
        "metadata": {
            "resolved": bool(producto),
            "tokens": tokens,
            "source": "QA-RFID",
        },
    }


def _build_recepcion_summary(encuadre):
    detalles = []
    total_esperado = Decimal("0")
    total_leido = Decimal("0")

    if encuadre.orden_compra_id:
        for detalle in (
            encuadre.orden_compra.ordencompradetalle_set.select_related("producto").order_by("id")
        ):
            esperado = Decimal(str(detalle.cantidad or 0))
            leido = Decimal("0")
            for lectura in encuadre.lecturas.filter(orden_compra_detalle=detalle):
                leido += Decimal(str(lectura.cantidad_leida or 0))

            total_esperado += esperado
            detalles.append(
                {
                    "detalle_id": detalle.pk,
                    "producto_id": detalle.producto_id,
                    "producto_nombre": detalle.producto.nombre,
                    "codigo": detalle.producto.codigo or detalle.producto.cod_proscai or "",
                    "esperado": esperado,
                    "leido": leido,
                    "diferencia": esperado - leido,
                }
            )

    lecturas_sin_asignar = []
    total_sin_asignar = Decimal("0")
    all_lecturas = encuadre.lecturas.select_related("producto", "producto_variante").order_by(
        "-created_at", "-id"
    )
    for lectura in all_lecturas:
        cantidad = Decimal(str(lectura.cantidad_leida or 0))
        total_leido += cantidad
        if lectura.orden_compra_detalle_id:
            continue
        total_sin_asignar += cantidad
        lecturas_sin_asignar.append(
            {
                "tag": lectura.codigo_tag,
                "cantidad": cantidad,
                "producto": getattr(lectura.producto, "nombre", None),
                "producto_variante": getattr(lectura.producto_variante, "nombre", None),
                "created_at": lectura.created_at,
            }
        )

    ultimas_lecturas = []
    for lectura in all_lecturas[:15]:
        cantidad = Decimal(str(lectura.cantidad_leida or 0))
        ultimas_lecturas.append(
            {
                "tag": lectura.codigo_tag,
                "cantidad": cantidad,
                "producto": getattr(lectura.producto, "nombre", None),
                "producto_variante": getattr(lectura.producto_variante, "nombre", None),
                "created_at": lectura.created_at,
            }
        )

    return {
        "detalle": detalles,
        "total_esperado": total_esperado,
        "total_leido": total_leido,
        "total_sin_asignar": total_sin_asignar,
        "ultimas_lecturas": ultimas_lecturas,
        "lecturas_sin_asignar": lecturas_sin_asignar,
    }


# Create your views here.
@login_required
def index(request):
    return render(request, "QA/index_QA.html")


# PRODUCCION
@login_required
def produccion_workspace(request):
    return render(request, "QA/produccion/produccion_workspace.html")


@login_required
def generar_orden_produccion(request):
    if request.method == "POST":
        sucursal_id = request.POST.get("sucursal_id")
        prioridad = request.POST.get("prioridad", 1)
        observaciones = request.POST.get("observaciones", "")

        variante_ids = request.POST.getlist("variante_ids")
        cantidades = request.POST.getlist("cantidades")

        cantidades_validas = [c for c in cantidades if c and float(c) > 0]
        if not cantidades_validas:
            messages.error(request, "Debes ingresar la cantidad de al menos un producto.")
            return redirect("generar_orden_produccion")

        try:
            with transaction.atomic():
                empresa_default = Empresa.objects.first()
                sucursal = Sucursal.objects.get(pk=sucursal_id)
                unidad_default = UnidadMedida.objects.first()

                nueva_op = OrdenProduccion.objects.create(
                    empresa=empresa_default,
                    sucursal=sucursal,
                    folio_op=f"OP-{uuid.uuid4().hex[:8].upper()}",
                    prioridad=prioridad,
                    observaciones=observaciones,
                )

                for variante_id, cantidad in zip(variante_ids, cantidades):
                    if cantidad and float(cantidad) > 0:
                        variante = ProductoVariante.objects.get(id=variante_id)
                        bom_instance = ListaMaterialBom.objects.filter(
                            producto_variante=variante,
                            activo=True,
                        ).first()

                        if bom_instance:
                            OrdenProduccionDetalle.objects.create(
                                op=nueva_op,
                                bom=bom_instance,
                                producto_variante=variante,
                                cantidad=float(cantidad),
                                unidad=unidad_default,
                            )

            messages.success(request, f"La orden {nueva_op.folio_op} se generó exitosamente.")
            return redirect("generar_orden_produccion")

        except Exception as exc:
            messages.error(request, f"Ocurrió un error al generar la orden: {str(exc)}")
            return redirect("generar_orden_produccion")

    variantes = ProductoVariante.objects.filter(activo=True)

    recetas_dict = {}
    for variante in variantes:
        bom = ListaMaterialBom.objects.filter(producto_variante=variante, activo=True).first()
        if not bom:
            continue
        detalles_reales = []
        for detalle in BomDetalle.objects.filter(bom=bom):
            detalles_reales.append(
                {
                    "insumo": detalle.componente.nombre if detalle.componente else "Insumo desconocido",
                    "cantidad_unitaria": float(detalle.cantidad),
                    "unidad": detalle.unidad.nombre if detalle.unidad else "pzas",
                }
            )
        recetas_dict[str(variante.pk)] = detalles_reales

    context = {
        "sucursales": Sucursal.objects.all(),
        "variantes": variantes,
        "recetas_json": recetas_dict,
    }

    return render(request, "QA/produccion/generar_orden_produccion.html", context)


@login_required
def recepcion_rfid_workspace(request):
    empresa = _empresa_qa(request)
    if empresa is None:
        messages.error(request, "No hay empresa disponible para la prueba de QA.")
        return redirect("index_QA")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "crear_encuadre":
            orden_compra_id = request.POST.get("orden_compra_id")
            almacen_id = request.POST.get("almacen_id")
            serie_codigo = (request.POST.get("serie_codigo") or "RC").strip().upper()[:2]

            orden_compra = get_object_or_404(
                OrdenCompra.objects.select_related("sucursal", "proveedor"),
                pk=orden_compra_id,
                empresa=empresa,
                activo=True,
            )
            almacen = get_object_or_404(
                Almacen.objects.select_related("sucursal"),
                pk=almacen_id,
                empresa=empresa,
                estatus="ACTIVO",
            )

            if almacen.sucursal_id and orden_compra.sucursal_id != almacen.sucursal_id:
                messages.error(
                    request,
                    "El almacén debe pertenecer a la misma sucursal de la orden de compra.",
                )
                return redirect("qa_recepcion_rfid_workspace")

            encuadre = RecepcionRFIDEncuadre.objects.create(
                orden_compra=orden_compra,
                empresa=empresa,
                sucursal=orden_compra.sucursal,
                proveedor=orden_compra.proveedor,
                almacen=almacen,
                usuario=request.user,
                serie_codigo=serie_codigo or "RC",
                fecha_recepcion=timezone.now(),
                remision=(request.POST.get("remision") or "").strip() or None,
                factura_referencia=(request.POST.get("factura_referencia") or "").strip() or None,
                observaciones=(request.POST.get("observaciones") or "").strip() or None,
            )
            messages.success(request, f"Encuadre RFID {encuadre.pk} creado.")
            return _redirect_rfid(encuadre.pk)

        if action == "registrar_lectura":
            encuadre = get_object_or_404(
                RecepcionRFIDEncuadre.objects.select_related("orden_compra"),
                pk=request.POST.get("encuadre_id"),
                empresa=empresa,
            )
            if encuadre.estatus != RecepcionRFIDEncuadre.Estatus.PENDIENTE:
                messages.error(request, "Solo puedes escanear encuadres pendientes.")
                return _redirect_rfid(encuadre.pk)

            codigo_tag = (request.POST.get("codigo_tag") or "").strip()
            if not codigo_tag:
                messages.error(request, "Debes escanear o capturar un tag.")
                return _redirect_rfid(encuadre.pk)

            if encuadre.lecturas.filter(codigo_tag=codigo_tag).exists():
                messages.warning(request, f"El tag {codigo_tag} ya fue leído en este encuadre.")
                return _redirect_rfid(encuadre.pk)

            resolved = _resolver_tag_recepcion(encuadre, codigo_tag)
            RecepcionRFIDLectura.objects.create(
                encuadre=encuadre,
                codigo_tag=codigo_tag,
                orden_compra_detalle=resolved["orden_compra_detalle"],
                producto=resolved["producto"],
                producto_variante=resolved["producto_variante"],
                cantidad_leida=Decimal("1"),
                metadata=resolved["metadata"],
            )

            if resolved["orden_compra_detalle"]:
                messages.success(
                    request,
                    f"Tag {codigo_tag} leído y asignado a {resolved['orden_compra_detalle'].producto.nombre}.",
                )
            else:
                messages.warning(
                    request,
                    f"Tag {codigo_tag} leído, pero quedó sin asignar automáticamente.",
                )
            return _redirect_rfid(encuadre.pk)

        if action == "aceptar_encuadre":
            encuadre = get_object_or_404(
                RecepcionRFIDEncuadre,
                pk=request.POST.get("encuadre_id"),
                empresa=empresa,
            )
            encuadre.estatus = RecepcionRFIDEncuadre.Estatus.ACEPTADO
            encuadre.save(update_fields=["estatus", "updated_at"])
            messages.success(
                request,
                "Encuadre aceptado en QA. Aún no mueve inventario; solo deja el conteo validado.",
            )
            return _redirect_rfid(encuadre.pk)

    selected_encuadre = None
    encuadre_id = request.GET.get("encuadre")
    if encuadre_id:
        selected_encuadre = get_object_or_404(
            RecepcionRFIDEncuadre.objects.select_related(
                "orden_compra",
                "proveedor",
                "almacen",
                "sucursal",
            ),
            pk=encuadre_id,
            empresa=empresa,
        )

    context = {
        "ordenes_compra": (
            OrdenCompra.objects.select_related("proveedor", "sucursal")
            .filter(empresa=empresa, activo=True)
            .order_by("-id")[:30]
        ),
        "almacenes": Almacen.objects.filter(empresa=empresa, estatus="ACTIVO").order_by("nombre"),
        "selected_encuadre": selected_encuadre,
        "summary": _build_recepcion_summary(selected_encuadre) if selected_encuadre else None,
        "recent_encuadres": (
            RecepcionRFIDEncuadre.objects.select_related("orden_compra", "almacen")
            .filter(empresa=empresa)
            .order_by("-created_at")[:12]
        ),
    }
    return render(request, "QA/rfid/recepcion_rfid_workspace.html", context)


@login_required
def qa_browserprint_asset(request, filename):
    asset_path = _browserprint_asset_path(filename)
    return FileResponse(asset_path.open("rb"), content_type="application/javascript; charset=utf-8")


@login_required
def imprimir_etiqueta_workspace(request):
    empresa = _empresa_qa(request)
    if empresa is None:
        messages.error(request, "No hay empresa disponible para la prueba de impresión.")
        return redirect("index_QA")

    q = (request.GET.get("q") or request.POST.get("q") or "").strip()
    encuadre_id = (request.GET.get("encuadre") or request.POST.get("encuadre") or "").strip()
    variante_id = request.GET.get("variante") or request.POST.get("variante_id")
    producto_id = request.GET.get("producto") or request.POST.get("producto_id")

    variantes_qs = (
        ProductoVariante.objects.select_related("producto", "color", "talla")
        .filter(empresa=empresa, activo=True)
        .order_by("sku")
    )
    productos_qs = Producto.objects.filter(empresa=empresa, activo=True).order_by("nombre")
    if q:
        variantes_qs = variantes_qs.filter(
            Q(sku__icontains=q)
            | Q(nombre__icontains=q)
            | Q(producto__nombre__icontains=q)
            | Q(producto__codigo__icontains=q)
            | Q(producto__cod_proscai__icontains=q)
        )
        productos_qs = productos_qs.filter(
            Q(nombre__icontains=q)
            | Q(codigo__icontains=q)
            | Q(cod_proscai__icontains=q)
        )

    variantes = list(variantes_qs[:30])
    variante_seleccionada = None
    producto_seleccionado = None
    if variante_id:
        variante_seleccionada = get_object_or_404(
            ProductoVariante.objects.select_related("producto", "color", "talla"),
            pk=variante_id,
            empresa=empresa,
            activo=True,
        )
        if not q:
            variantes = [variante_seleccionada] + [
                item for item in variantes if item.pk != variante_seleccionada.pk
            ]
    elif producto_id:
        producto_seleccionado = get_object_or_404(
            Producto.objects,
            pk=producto_id,
            empresa=empresa,
            activo=True,
        )

    producto_ids_con_variantes = {item.producto_id for item in variantes}
    productos = [
        producto for producto in productos_qs[:30]
        if producto.pk not in producto_ids_con_variantes
    ]
    if producto_seleccionado and not any(item.pk == producto_seleccionado.pk for item in productos):
        productos = [producto_seleccionado] + productos
    if not variante_seleccionada and not producto_seleccionado and len(productos) == 1 and not variantes:
        producto_seleccionado = productos[0]

    preview_data = _build_label_preview(
        variante=variante_seleccionada,
        producto=producto_seleccionado,
    )

    context = {
        "q": q,
        "encuadre_id": encuadre_id,
        "variantes": variantes,
        "productos": productos,
        "variante_seleccionada": variante_seleccionada,
        "producto_seleccionado": producto_seleccionado,
        "preview_data": preview_data,
        "zpl_preview": (
            _build_producto_label_zpl(variante_seleccionada)
            if variante_seleccionada
            else _build_producto_base_label_zpl(producto_seleccionado)
            if producto_seleccionado
            else ""
        ),
    }
    return render(request, "QA/rfid/imprimir_etiqueta_workspace.html", context)


@login_required
def imprimir_orden_compra_workspace(request):
    empresa = _empresa_qa(request)
    if empresa is None:
        messages.error(request, "No hay empresa disponible para la prueba de QA.")
        return redirect("index_QA")

    q = (request.GET.get("q") or request.GET.get("folio") or "").strip()
    orden_compra_id = request.GET.get("id") or request.GET.get("orden_compra")
    selected_oc = None
    resultados = []

    oc_qs = (
        OrdenCompra.objects.select_related("empresa", "proveedor")
        .filter(empresa=empresa, activo=True)
    )

    if orden_compra_id and str(orden_compra_id).isdigit():
        selected_oc = oc_qs.filter(pk=int(orden_compra_id)).first()
        if selected_oc is None:
            messages.warning(request, f"No se encontró la orden de compra ID {orden_compra_id}.")

    if selected_oc is None and q:
        q_search = oc_qs.filter(
            Q(folio__icontains=q)
            | Q(id__icontains=q)
            | Q(referencia__icontains=q)
            | Q(proveedor__nombre__icontains=q)
        ).order_by("-id")[:30]
        if len(q_search) == 1:
            selected_oc = q_search[0]
        else:
            resultados = list(q_search)

    if selected_oc is None and not q:
        resultados = list(oc_qs.order_by("-id")[:30])

    oc_resumen = None
    renglones = []
    if selected_oc is not None:
        oc_resumen = {
            "id": selected_oc.pk,
            "folio": selected_oc.folio or f"OC-{selected_oc.pk}",
            "proveedor_nombre": (
                selected_oc.proveedor.nombre if selected_oc.proveedor else "Sin proveedor"
            ),
            "fecha_oc": selected_oc.fecha_oc.isoformat() if selected_oc.fecha_oc else None,
        }
        for d in OrdenCompraDetalle.objects.select_related("producto").filter(orden_compra=selected_oc):
            producto = d.producto
            cantidad_default = int(d.cantidad or d.piezas or 0)
            if cantidad_default <= 0:
                cantidad_default = 1
            zpl = _build_producto_base_label_zpl(producto) if producto else ""
            renglones.append({
                "detalle_id": d.pk,
                "producto_id": producto.pk if producto else None,
                "nombre": d.descripcion or (producto.nombre if producto else "Producto"),
                "codigo": getattr(producto, "codigo", None),
                "cod_proscai": getattr(producto, "cod_proscai", None),
                "cantidad_default": cantidad_default,
                "zpl": zpl,
            })

    context = {
        "q": q,
        "resultados": resultados,
        "selected_oc": selected_oc,
        "oc": oc_resumen,
        "renglones": renglones,
    }
    return render(request, "QA/compras/imprimir_orden_compra_workspace.html", context)


def _es_hexadecimal_epc(value):
    s = (value or "")
    if isinstance(s, bytes):
        try:
            s = s.decode("utf-8", errors="replace")
        except Exception:
            s = ""
    s = str(s).strip().replace(" ", "").replace(":", "").replace("-", "")
    if not s:
        return False
    if len(s) < 8 or len(s) > 64:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def _extract_epc_raw(item):
    """Devuelve el EPC hex raw de un item de scan dict o string.
    Soporta:
      - str: hex directo
      - dict: {'idHex':'..'} / {'data':'..'} / {'epc':'..'} / {'tagID':'..'}
              tambien {'epc':{'idHex':'..'}} (anidado como en logs FX)
    Devuelve None si no hay EPC valido.
    Ademas retorna antenna/rssi extraidos del mismo item, separados.
    """
    if item is None:
        return None
    if isinstance(item, str):
        return item.strip()

    if not isinstance(item, dict):
        return None

    candidates = []
    for key in ("idHex", "data", "epc", "tagID", "tidHex", "epcHex", "hex"):
        v = item.get(key)
        if v is None:
            continue
        if isinstance(v, dict):
            for sub in ("idHex", "data", "epc", "tagID", "tidHex", "epcHex", "hex"):
                sv = v.get(sub)
                if sv:
                    candidates.append(sv)
        elif isinstance(v, (str, bytes)):
            candidates.append(v)
    for c in candidates:
        if isinstance(c, bytes):
            try:
                c = c.decode("utf-8", errors="replace")
            except Exception:
                continue
        if isinstance(c, str):
            s = c.strip()
            if s:
                return s
    return None


def _extract_int(value):
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_antenna_rssi(item, fallback_antenna=None, fallback_rssi=None):
    antenna = None
    rssi = None
    if isinstance(item, dict):
        antenna = _extract_int(
            item.get("antenna")
            or item.get("antennaID")
            or item.get("antennaPort")
            or item.get("antennaPortName")
            or item.get("port")
        )
        rssi = _extract_float(
            item.get("peakRssi")
            or item.get("rssi")
            or item.get("rssiDbm")
            or item.get("peakRssiDbm")
        )
    if antenna is None:
        antenna = _extract_int(fallback_antenna)
    if rssi is None:
        rssi = _extract_float(fallback_rssi)
    return antenna, rssi


@login_required
def scanner_rfid_workspace(request):
    empresa = _empresa_qa(request)
    if empresa is None:
        messages.error(request, "No hay empresa disponible para la prueba de QA.")
        return redirect("index_QA")
    return render(
        request,
        "QA/rfid/scanner_rfid_workspace.html",
        {"empresa": empresa},
    )


@csrf_exempt
def scanner_rfid_receive(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Method not allowed"}, status=405)
    try:
        remote_addr = request.META.get("REMOTE_ADDR")
        raw_body = request.body.decode("utf-8", errors="replace")
        rfid_scanner_logger.info("RFID receive from %s body[:160]=%s", remote_addr, raw_body[:160])

        data = json.loads(raw_body) if raw_body else None
        items = []
        fallback_antenna = None
        fallback_rssi = None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # Zebra FX puede mandar {tagData:[...]}, {tags:[...]}, {data:[...]}, {events:[...]}
            items = (
                data.get("tagData")
                or data.get("tags")
                or data.get("events")
                or data.get("eventList")
                or data.get("data")
                or [data]
            )
            fallback_antenna = (
                data.get("antenna")
                or data.get("antennaID")
                or data.get("antennaPort")
                or data.get("port")
            )
            fallback_rssi = (
                data.get("peakRssi")
                or data.get("rssi")
                or data.get("rssiDbm")
                or data.get("peakRssiDbm")
            )

        if not isinstance(items, list):
            return JsonResponse({"status": "error", "message": "Invalid data format"}, status=400)

        tags_to_create = []
        seen_epcs_in_this_batch = set()
        for item in items:
            if isinstance(item, dict) and item.get("isHeartBeat") is True:
                continue
            epc_raw = _extract_epc_raw(item)
            if not epc_raw:
                continue
            # limpia / normaliza hex (minusculas, sin separadores)
            epc_norm = (
                epc_raw.strip()
                .replace(" ", "")
                .replace(":", "")
                .replace("-", "")
                .lower()
            )
            if not epc_norm or len(epc_norm) < 8:
                continue
            # Si ya estaba dentro del mismo request (duplicado fx reader), omitimos 2da insert
            if epc_norm in seen_epcs_in_this_batch:
                continue
            seen_epcs_in_this_batch.add(epc_norm)
            antenna, rssi = _extract_antenna_rssi(
                item, fallback_antenna=fallback_antenna, fallback_rssi=fallback_rssi
            )
            tags_to_create.append(
                RfidScan(
                    epc=epc_norm,
                    reader_ip=remote_addr,
                    antenna=antenna,
                    rssi=rssi,
                )
            )

        if tags_to_create:
            RfidScan.objects.bulk_create(tags_to_create, batch_size=200)
        return JsonResponse({"status": "success", "count": len(tags_to_create)})
    except json.JSONDecodeError as e:
        return JsonResponse({"status": "error", "message": f"JSON invalido: {str(e)}"}, status=400)
    except Exception as e:
        rfid_scanner_logger.exception("RFID receive error")
        return JsonResponse({"status": "error", "message": str(e)}, status=500)


def scanner_rfid_get(request):
    scans = list(
        RfidScan.objects.select_related()
        .order_by("-created_at", "-id")[:50]
    )

    # JOIN por epc contra EtiquetaRFIDDetalle (incluyendo impresion, producto, variante)
    epc_list = [s.epc for s in scans if s.epc]
    # Normalizamos para match tanto mayus como minus: guardaremos lower y buscaremos en lower
    epc_lower_set = {e.lower() for e in epc_list if e}

    # Busqueda lowercase: EtiquetaRFIDDetalle.epc es único, convertimos ambos lados a lower
    detalle_qs = (
        EtiquetaRFIDDetalle.objects.filter(epc__in=list(epc_lower_set) + list({e.upper() for e in epc_lower_set}))
        .select_related(
            "impresion",
            "impresion__producto",
            "impresion__producto_variante",
            "impresion__producto_variante__color",
            "impresion__producto_variante__talla",
        )
        .only(
            "epc",
            "barcode_value",
            "serial",
            "estado",
            "impresion__id",
            "impresion__producto_id",
            "impresion__producto__nombre",
            "impresion__producto__cod_proscai",
            "impresion__producto__codigo",
            "impresion__producto_variante_id",
            "impresion__producto_variante__nombre",
            "impresion__producto_variante__sku",
            "impresion__producto_variante__color_id",
            "impresion__producto_variante__color__nombre",
            "impresion__producto_variante__talla_id",
            "impresion__producto_variante__talla__nombre",
        )
    )
    detalle_by_epc_lower = {}
    for d in detalle_qs:
        detalle_by_epc_lower[(d.epc or "").lower()] = d

    data = []
    for scan in scans:
        epc = scan.epc or ""
        epc_lower = epc.lower()
        detalle = detalle_by_epc_lower.get(epc_lower)

        item = {
            "id": scan.pk,
            "epc": epc,
            "timestamp": scan.created_at.isoformat(),
            "antenna": scan.antenna,
            "rssi": scan.rssi,
            "reader_ip": scan.reader_ip,
        }

        if detalle is not None:
            impresion = detalle.impresion
            variante = impresion.producto_variante if impresion else None
            producto = impresion.producto if impresion else None

            nombre_producto = None
            sku = None
            color_nombre = None
            talla_nombre = None
            if variante:
                sku = variante.sku
                if variante.color:
                    color_nombre = variante.color.nombre
                if variante.talla:
                    talla_nombre = variante.talla.nombre
                # Variante tiene nombre completo producto-color-talla, preferimos ese
                if variante.nombre:
                    nombre_producto = variante.nombre
                elif variante.producto:
                    nombre_producto = variante.producto.nombre
            elif producto:
                nombre_producto = producto.nombre

            item.update(
                {
                    "match_impresion": True,
                    "impresion_folio": impresion.folio if impresion else None,
                    "impresion_id": impresion.id if impresion else None,
                    "producto_nombre": nombre_producto,
                    "sku": sku,
                    "color": color_nombre,
                    "talla": talla_nombre,
                    "barcode_value": detalle.barcode_value,
                    "serial": detalle.serial,
                    "estado": detalle.estado,  # IMPRESO / LEIDO / PENDIENTE / CANCELADO
                    "detalle_id": detalle.id,
                }
            )
        else:
            item["match_impresion"] = False
        data.append(item)
    return JsonResponse({"scans": data})


def scanner_rfid_clear(request):
    RfidScan.objects.all().delete()
    return JsonResponse({"status": "success"})

