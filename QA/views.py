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
from rest_framework.exceptions import ValidationError as DRFValidationError
from wms.api.serializers import (
    EtiquetaRFIDCreateSerializer,
    EtiquetaRFIDSerializer,
)
from wms.models import EtiquetaRFIDDetalle, EtiquetaRFIDImpresion, RfidScan
from wms.services.rfid_label_service import RFIDLabelService

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


def _qa_rfid_success_payload(impresion):
    data = EtiquetaRFIDSerializer(impresion).data
    zpl_individual = []
    detalles = list(
        EtiquetaRFIDDetalle.objects.filter(impresion=impresion).order_by("id")
    )
    if impresion.rfid_mode and detalles:
        for d in detalles:
            zpl_individual.append(
                RFIDLabelService._build_zpl_rfid(
                    d.epc,
                    variante=impresion.producto_variante,
                    producto=impresion.producto,
                    barcode_value=d.barcode_value,
                )
            )
    else:
        preview = RFIDLabelService._build_label_preview(
            variante=impresion.producto_variante, producto=impresion.producto
        )
        zpl_normal = RFIDLabelService._build_zpl_normal(
            variante=impresion.producto_variante,
            producto=impresion.producto,
            barcode_value=preview["barcode_value"] if preview else "",
        )
        zpl_individual = [zpl_normal] * max(1, impresion.cantidad)

    data["zpl_individual"] = zpl_individual
    data["zpl_completo"] = "\n".join(zpl_individual)
    etiquetas = []
    for d in detalles:
        etiquetas.append(
            {
                "id": d.id,
                "epc": d.epc,
                "barcode_value": d.barcode_value,
                "serial": d.serial,
                "estado": d.estado,
            }
        )
    if not etiquetas and impresion.cantidad:
        for i in range(1, impresion.cantidad + 1):
            etiquetas.append(
                {
                    "id": None,
                    "epc": None,
                    "barcode_value": (
                        RFIDLabelService._build_label_preview(
                            variante=impresion.producto_variante,
                            producto=impresion.producto,
                        ) or {}
                    ).get("barcode_value"),
                    "serial": f"{i:04d}",
                    "estado": EtiquetaRFIDDetalle.Estado.PENDIENTE,
                }
            )
    data["etiquetas"] = etiquetas
    return data


def _qa_rfid_guardar_impresion(request, variante_id, producto_id, cantidad, printer_name, printer_address):
    """Guarda una impresión y devuelve (response_json, status_code)."""
    if request.method != "POST":
        return {"ok": False, "error": "Método no permitido."}, 405

    cantidad = max(1, int(cantidad or 1))

    # NOTA: NO mandamos ``zpl_enviado`` ni ``etiquetas`` al serializer (por lo
    # tanto ``store_impresion`` recibe ``None`` para ambos) porque queremos que
    # el service:
    #   1) genere EPCs únicos por backend (intento reintento colisión),
    #   2) cree EtiquetaRFIDDetalle en DB,
    #   3) y después NOSOTROS armamos el ZPL final real (el que Browser Print
    #      va a enviar a la impresora) y lo guardamos con update_fields().
    #
    # Este paso es OBLIGATORIO: antes store_impresion guardaba ``zpl_enviado``
    # antes de haber generado los EPCs reales (vacío = NULL), por lo que el
    # admin mostraba "ZPL enviado: vacío".
    payload = {
        "producto_variante": int(variante_id) if variante_id else None,
        "producto": int(producto_id) if producto_id else None,
        "cantidad": cantidad,
        "rfid_mode": True,
        "printer_name": printer_name or None,
        "printer_address": printer_address or None,
        "status": EtiquetaRFIDImpresion.Estatus.EXITO,
    }
    try:
        serializer = EtiquetaRFIDCreateSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        impresion = RFIDLabelService.store_impresion(serializer.validated_data, request.user)
    except DRFValidationError as exc:
        rfid_scanner_logger.warning(
            "RFID guardar impresion: serializer invalid user=%s payload=%s errors=%s",
            getattr(request.user, "pk", None),
            json.dumps(payload, ensure_ascii=False, default=str)[:800],
            exc.detail if hasattr(exc, "detail") else str(exc),
        )
        return {"ok": False, "error": exc.detail if hasattr(exc, "detail") else str(exc)}, 400
    except Exception as exc:
        rfid_scanner_logger.exception(
            "RFID guardar impresion: store_impresion exception user=%s payload=%s",
            getattr(request.user, "pk", None),
            json.dumps(payload, ensure_ascii=False, default=str)[:800],
        )
        return {"ok": False, "error": str(exc)}, 400

    response_payload = _qa_rfid_success_payload(impresion)
    zpl_real = response_payload.get("zpl_completo") or ""
    etiquetas = response_payload.get("etiquetas") or []
    rfid_scanner_logger.info(
        "RFID guardar impresion: id=%s folio=%s user=%s empresa=%s sucursal=%s variante=%s producto=%s cantidad=%s etiquetas=%d zpl_len=%d printer=%s",
        impresion.pk,
        impresion.folio,
        getattr(request.user, "pk", None),
        impresion.empresa_id,
        impresion.sucursal_id,
        impresion.producto_variante_id,
        impresion.producto_id,
        impresion.cantidad,
        len(etiquetas),
        len(zpl_real),
        impresion.printer_name,
    )
    if etiquetas:
        rfid_scanner_logger.debug(
            "RFID guardar impresion EPCs id=%s folio=%s epcs=%s",
            impresion.pk,
            impresion.folio,
            json.dumps(
                [{"id": e.get("id"), "epc": e.get("epc"), "serial": e.get("serial"), "estado": e.get("estado")} for e in (etiquetas or [])],
                ensure_ascii=False,
            )[:1500],
        )
    if not zpl_real:
        rfid_scanner_logger.warning(
            "RFID guardar impresion: zpl_completo vacio id=%s folio=%s",
            impresion.pk,
            impresion.folio,
        )

    # Guardar el ZPL REAL generado DESPUÉS de crear los EPCs en EtiquetaRFIDDetalle.
    # Esto es lo que Browser Print envía a la impresora.
    try:
        upd = EtiquetaRFIDImpresion.objects.filter(pk=impresion.pk).update(
            zpl_enviado=zpl_real if zpl_real else None
        )
        rfid_scanner_logger.info(
            "RFID guardar impresion: zpl_enviado actualizado id=%s rows_updated=%s zpl_len=%d (antes len=%d)",
            impresion.pk,
            upd,
            len(zpl_real),
            len(impresion.zpl_enviado or ""),
        )
    except Exception as exc:
        # No fallamos la respuesta por esto (ya se crearon impresion+detalles);
        # solo loggeamos.
        rfid_scanner_logger.exception(
            "RFID guardar impresion: fallo update zpl_enviado impresion %s",
            impresion.pk,
        )

    return {
        "ok": True,
        "impresion": response_payload,
    }, 201


@login_required
def qa_guardar_impresion_sku(request):
    """POST {variante_id, producto_id, cantidad, printer_name, printer_address}"""
    if request.content_type and "application/json" in request.content_type:
        try:
            body = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            body = {}
    else:
        body = request.POST
    variante_id = body.get("variante_id") or body.get("producto_variante")
    producto_id = body.get("producto_id") or body.get("producto")
    cantidad = body.get("cantidad", 1)
    printer_name = body.get("printer_name")
    printer_address = body.get("printer_address")
    data, status = _qa_rfid_guardar_impresion(
        request, variante_id, producto_id, cantidad, printer_name, printer_address
    )
    return JsonResponse(data, status=status)


@login_required
def qa_guardar_impresion_oc(request, detalle_id):
    """POST {cantidad, printer_name, printer_address}"""
    empresa = _empresa_qa(request)
    if empresa is None:
        return JsonResponse({"ok": False, "error": "Sin empresa."}, status=400)

    detalle = get_object_or_404(
        OrdenCompraDetalle.objects.select_related("producto", "orden_compra"),
        pk=int(detalle_id),
        orden_compra__empresa=empresa,
        orden_compra__activo=True,
    )
    producto = detalle.producto
    if request.content_type and "application/json" in request.content_type:
        try:
            body = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            body = {}
    else:
        body = request.POST
    cantidad_raw = body.get("cantidad")
    if cantidad_raw is None or cantidad_raw == "":
        cantidad_default = int(detalle.cantidad or detalle.piezas or 0)
        cantidad_default = max(1, cantidad_default) if cantidad_default else 1
    else:
        cantidad_default = max(1, int(cantidad_raw or 1))
    printer_name = body.get("printer_name")
    printer_address = body.get("printer_address")

    if not producto:
        return JsonResponse(
            {"ok": False, "error": "Este detalle de OC no tiene producto ligado."},
            status=400,
        )
    variante = None
    producto_id = producto.pk
    data, status = _qa_rfid_guardar_impresion(
        request,
        variante.pk if variante else None,
        producto_id,
        cantidad_default,
        printer_name,
        printer_address,
    )
    if data.get("ok"):
        data["detalle_id"] = detalle.pk
        data["orden_compra_id"] = detalle.orden_compra_id
    return JsonResponse(data, status=status)


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
    cantidad_raw = request.GET.get("cantidad") or request.POST.get("cantidad") or "1"
    try:
        cantidad_default = max(1, int(cantidad_raw))
    except Exception:
        cantidad_default = 1

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
        "cantidad_default": cantidad_default,
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
      - dict top-level keys comunes FX/ZDS
      - anidado {tag:{epcHex:..}, meta:{..}} (Zebra Data Services SDK)
      - anidado {data:{hex:.., source:..}} (FX EventReport)
      - fuzzy find por substring (hex / epc / tag) con profundidad hasta 6.
    """
    if item is None:
        return None
    if isinstance(item, bytes):
        try:
            item = item.decode("utf-8", errors="replace")
        except Exception:
            item = None
    if isinstance(item, str):
        s = item.strip()
        return s if s else None

    if not isinstance(item, dict):
        return None

    top_level_keys = (
        "idHex", "data", "epc", "EPC", "tagID", "tidHex", "epcHex", "hex",
        "epcId", "epcID", "tagEpc", "tag_epc", "tid", "value", "code",
        "raw", "rawValue", "tag_id",
    )
    candidates = []
    for key in top_level_keys:
        v = item.get(key)
        if v is None:
            continue
        if isinstance(v, dict):
            for sub in top_level_keys:
                sv = v.get(sub)
                if isinstance(sv, (str, bytes)) and sv:
                    candidates.append(sv)
        elif isinstance(v, (str, bytes)):
            candidates.append(v)

    tag = item.get("tag")
    if isinstance(tag, dict):
        for sub in top_level_keys:
            sv = tag.get(sub)
            if isinstance(sv, (str, bytes)) and sv:
                candidates.append(sv)
        fuzzy_tag = _find_by_key_substr(tag, ["hex", "epc", "tag"])
        if isinstance(fuzzy_tag, (str, bytes)) and fuzzy_tag:
            candidates.append(fuzzy_tag)

    data = item.get("data")
    if isinstance(data, dict):
        for sub in top_level_keys:
            sv = data.get(sub)
            if isinstance(sv, (str, bytes)) and sv:
                candidates.append(sv)
        fuzzy_data = _find_by_key_substr(data, ["hex", "epc", "tag"])
        if isinstance(fuzzy_data, (str, bytes)) and fuzzy_data:
            candidates.append(fuzzy_data)

    reads = item.get("reads")
    if isinstance(reads, list):
        for r in reads:
            if isinstance(r, (str, bytes)) and r:
                candidates.append(r)
                break

    fuzzy_top = _find_by_key_substr(item, ["hex", "epc", "tag"])
    if isinstance(fuzzy_top, (str, bytes)) and fuzzy_top:
        candidates.append(fuzzy_top)

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
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, float):
            return int(value) if value.is_integer() else None
        return int(value)
    except (TypeError, ValueError):
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                if "." in stripped or "e" in stripped.lower():
                    as_float = float(stripped)
                    return int(as_float) if as_float.is_integer() else None
                return int(stripped)
            except (TypeError, ValueError):
                return None
        return None


def _extract_float(value):
    try:
        if value is None:
            return None
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        return float(value)
    except (TypeError, ValueError):
        if isinstance(value, str) and value.strip():
            try:
                return float(value.strip())
            except (TypeError, ValueError):
                return None
        return None


def _find_by_key_substr(d, substrings):
    """Búsqueda recursiva de keys por substring a profundidad 6.

    Retorna el primer valor (no list/tuple/dict) cuya key haga match;
    si no, retorna None.
    """
    if not isinstance(d, dict) and not isinstance(d, (list, tuple)):
        return None

    def _recurse(obj, depth):
        if depth > 6:
            return None
        if isinstance(obj, dict):
            for key, value in obj.items():
                k = str(key).lower().replace("_", "").replace("-", "")
                matched = False
                for s in substrings:
                    s_norm = s.lower().replace("_", "").replace("-", "")
                    if s_norm in k:
                        matched = True
                        break
                if matched:
                    if value is not None and not isinstance(value, (list, tuple, dict)):
                        return value
                found = _recurse(value, depth + 1)
                if found is not None:
                    return found
            return None
        if isinstance(obj, (list, tuple)):
            for x in obj:
                found = _recurse(x, depth + 1)
                if found is not None:
                    return found
            return None
        return None

    return _recurse(d, 0)


_ANTENNA_INT_KEYS = (
    "antenna", "antennaID", "antennaId", "antennaPort", "antennaPortName",
    "port", "ant", "source", "antenna_number", "antennaNumber",
    "port_no", "portNo", "antPort", "ant_port", "readerPort",
    "reader_port", "inputPort", "channel", "channelIndex",
)
_RSSI_FLOAT_KEYS = (
    "peakRssi", "rssi", "rssiDbm", "peakRssiDbm", "rssi_value",
    "rssiValue", "peak_rssi", "peakRssiValue", "signal_strength",
    "signalStrength", "rssi_db", "rssiDb", "signalDb", "signalLevel",
    "signal_level", "rssiPeak", "rxRssi", "rx_rssi", "tagRSSI",
)


def _antenna_from_value(raw):
    """Convierte enteros, floats enteros, y strings estilo 'ANT-1' / 'Port#3'."""
    as_int = _extract_int(raw)
    if as_int is not None:
        return as_int
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        import re as _re
        m = _re.search(r"\d+", stripped)
        if m:
            try:
                return int(m.group(0))
            except Exception:
                return None
    return None


def _extract_antenna_rssi(item, fallback_antenna=None, fallback_rssi=None):
    antenna = None
    rssi = None
    if isinstance(item, dict):
        for key in _ANTENNA_INT_KEYS:
            if key in item:
                v = _antenna_from_value(item.get(key))
                if v is not None:
                    antenna = v
                    break
        if antenna is None:
            meta = item.get("meta")
            if isinstance(meta, dict):
                for key in _ANTENNA_INT_KEYS:
                    if key in meta:
                        v = _antenna_from_value(meta.get(key))
                        if v is not None:
                            antenna = v
                            break
        if antenna is None:
            data = item.get("data")
            if isinstance(data, dict):
                for key in _ANTENNA_INT_KEYS:
                    if key in data:
                        v = _antenna_from_value(data.get(key))
                        if v is not None:
                            antenna = v
                            break
        if antenna is None:
            fuzzy_raw = _find_by_key_substr(item, ["ant", "port", "channel", "source"])
            antenna = _antenna_from_value(fuzzy_raw)

        for key in _RSSI_FLOAT_KEYS:
            if key in item:
                v = _extract_float(item.get(key))
                if v is not None:
                    rssi = v
                    break
        if rssi is None:
            meta = item.get("meta")
            if isinstance(meta, dict):
                for key in _RSSI_FLOAT_KEYS:
                    if key in meta:
                        v = _extract_float(meta.get(key))
                        if v is not None:
                            rssi = v
                            break
        if rssi is None:
            data = item.get("data")
            if isinstance(data, dict):
                for key in _RSSI_FLOAT_KEYS:
                    if key in data:
                        v = _extract_float(data.get(key))
                        if v is not None:
                            rssi = v
                            break
        if rssi is None:
            fuzzy_rssi = _find_by_key_substr(item, ["rssi", "signal", "dbm", "db"])
            rssi = _extract_float(fuzzy_rssi)

    if antenna is None:
        antenna = _antenna_from_value(fallback_antenna)
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
    # Declarado FUERA del try para que los logger.info/warning de abajo
    # (fuera del bloque try) no causen NameError si algo falló en medio.
    body = ""
    debug_payload = {
        "content_type": request.content_type or "",
        "method": request.method,
        "remote_addr": request.META.get("REMOTE_ADDR"),
    }
    try:
        remote_addr = request.META.get("REMOTE_ADDR")
        raw_body = request.body.decode("utf-8", errors="replace")
        body = raw_body
        debug_payload["body_len"] = len(raw_body)
        debug_payload["body_prefix"] = raw_body[:512]
        rfid_scanner_logger.info(
            "RFID receive from %s ct=%s body[:4096]=%s",
            remote_addr,
            request.content_type,
            raw_body[:4096],
        )

        # --- PARSEO MULTI CONTENT-TYPE (porque FX puede mandar text/plain, form, urlencoded, JSON)
        data = None
        parse_attempts = []

        # 1) JSON directo (mejor caso)
        try:
            if raw_body and raw_body.strip():
                data = json.loads(raw_body)
                parse_attempts.append("json_loads:OK")
        except (json.JSONDecodeError, ValueError, Exception) as e:
            parse_attempts.append(f"json_loads:FAIL:{type(e).__name__}")
            data = None

        # 2) request.POST (x-www-form-urlencoded) — FXs viejos a veces usan esto
        if data is None and request.POST:
            try:
                qd = request.POST.dict()
                # Si contiene una key llamada "data"/"payload" con JSON adentro: intentar parsearla
                for wrapper in ["data", "payload", "body", "json", "tags_json"]:
                    if wrapper in qd and isinstance(qd[wrapper], str):
                        try:
                            parsed_inner = json.loads(qd[wrapper])
                            qd[wrapper] = parsed_inner
                            break
                        except Exception:
                            pass
                data = qd
                parse_attempts.append("request.POST.dict:OK")
            except Exception as e:
                parse_attempts.append(f"request.POST.dict:FAIL:{type(e).__name__}")

        # 3) text/plain pero con EPCs separados por newlines (lista de strings sin corchetes)
        if data is None and raw_body and raw_body.strip():
            stripped = raw_body.strip()
            if "\n" in stripped or "," in stripped:
                # Lista separada por comas o saltos de línea (posiblemente con espacios)
                parts = [p.strip() for p in stripped.replace(",", "\n").splitlines() if p.strip()]
                # Si todo parece hex (chars [0-9a-fA-F: - ])
                def _looks_hex(s):
                    if not s or len(s) < 8:
                        return False
                    cl = s.replace(" ", "").replace(":", "").replace("-", "")
                    return all(c in "0123456789abcdefABCDEF" for c in cl)
                if parts and all(_looks_hex(p) for p in parts):
                    data = [{"epc": p} for p in parts]
                    parse_attempts.append("split_newlines_epc:OK")

        # 4) Si sigue None: cuerpo dict vacío {} o [] con intento final limpiar espacios
        if data is None and raw_body and raw_body.strip():
            try:
                cleaned = raw_body.strip()
                # caso texto plano con objeto JSON (sin header correcto)
                data = json.loads(cleaned)
                parse_attempts.append("json_loads_cleaned:OK")
            except Exception as e:
                parse_attempts.append(f"json_loads_cleaned:FAIL:{type(e).__name__}")
                data = None

        debug_payload["parse_attempts"] = parse_attempts

        # --- Extracción items + fallback (igual que antes)
        items = []
        fallback_antenna = None
        fallback_rssi = None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = (
                data.get("tagData")
                or data.get("tags")
                or data.get("events")
                or data.get("eventList")
                or data.get("data")
                or data.get("items")
                or data.get("reads")
                or data.get("readEvents")
                or data.get("tagReadEvents")
                or [data]
            )
            fallback_antenna = (
                data.get("antenna")
                or data.get("antennaID")
                or data.get("antennaPort")
                or data.get("port")
                or data.get("ant")
                or data.get("source")
                or data.get("antenna_number")
                or data.get("antennaNumber")
                or data.get("port_no")
                or data.get("portNo")
                or _find_by_key_substr(data, ["ant", "port"])
            )
            fallback_rssi = (
                data.get("peakRssi")
                or data.get("rssi")
                or data.get("rssiDbm")
                or data.get("peakRssiDbm")
                or data.get("rssi_value")
                or data.get("rssiValue")
                or data.get("peak_rssi")
                or data.get("peakRssiValue")
                or data.get("signal_strength")
                or data.get("signalStrength")
                or _find_by_key_substr(data, ["rssi", "signal"])
            )

            debug_payload["body_dict_keys"] = sorted(data.keys())
        else:
            debug_payload["body_dict_keys"] = None
            debug_payload["fallback_antenna_raw_type"] = type(data).__name__

        if not isinstance(items, list):
            items = []
        debug_payload["fallback_antenna_final"] = (
            str(fallback_antenna)[:200] if fallback_antenna is not None else None
        )
        debug_payload["fallback_rssi_final"] = (
            str(fallback_rssi)[:200] if fallback_rssi is not None else None
        )
        debug_payload["items_raw_len"] = len(items)

        if not isinstance(items, list):
            debug_payload["error"] = "Invalid data format"
            return JsonResponse(
                {"status": "error", "message": "Invalid data format", "debug": debug_payload},
                status=400,
            )

        tags_to_create = []
        debug_items = []
        seen_epcs_in_this_batch = set()
        for idx, item in enumerate(items):
            if isinstance(item, dict) and item.get("isHeartBeat") is True:
                continue
            epc_raw = _extract_epc_raw(item)
            if not epc_raw:
                # Debug: tag item sin EPC, igual guardamos info para saber por qué
                debug_items.append(
                    {
                        "i": idx,
                        "item_type": type(item).__name__,
                        "keys_list": sorted(item.keys())[:20] if isinstance(item, dict) else None,
                        "epc_raw": None,
                        "antenna_final": None,
                        "rssi_final": None,
                        "item_str": (str(item)[:200] if not isinstance(item, (dict, list)) else None),
                    }
                )
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
                debug_items.append(
                    {
                        "i": idx,
                        "epc_raw": epc_raw[:40] if isinstance(epc_raw, str) else epc_raw,
                        "skip_reason": f"len<8 epc={epc_norm}",
                    }
                )
                continue
            # Si ya estaba dentro del mismo request (duplicado fx reader), omitimos 2da insert
            if epc_norm in seen_epcs_in_this_batch:
                debug_items.append(
                    {"i": idx, "epc_norm": epc_norm, "skip_reason": "dup_in_batch"}
                )
                continue
            seen_epcs_in_this_batch.add(epc_norm)
            antenna, rssi = _extract_antenna_rssi(
                item, fallback_antenna=fallback_antenna, fallback_rssi=fallback_rssi
            )
            # --- DEEP debug del item (se sube a INFO para que aparezca en Vercel)
            debug_item_entry = {
                "i": idx,
                "item_type": type(item).__name__,
                "epc_raw_len": (len(epc_raw) if isinstance(epc_raw, (str, bytes)) else None),
                "epc_raw_head": epc_raw[:32] if isinstance(epc_raw, str) else None,
                "epc_norm": epc_norm,
                "antenna_final": antenna,
                "rssi_final": rssi,
                "fallback_antenna_used": (
                    str(fallback_antenna)[:80] if fallback_antenna is not None else None
                ),
                "fallback_rssi_used": (
                    str(fallback_rssi)[:80] if fallback_rssi is not None else None
                ),
            }
            if isinstance(item, dict):
                debug_item_entry["keys_top20"] = sorted(item.keys())[:20]
                try:
                    dump = json.dumps(item, ensure_ascii=False, default=str)
                except Exception:
                    dump = "<unserializable>"
                debug_item_entry["item_json_prefix"] = dump[:300]
                debug_item_entry["item_json_len"] = len(dump)
            else:
                debug_item_entry["item_str_prefix"] = str(item)[:200]

            # Dump a Vercel logs
            excerpt = dict(debug_item_entry)
            try:
                excerpt_json = json.dumps(excerpt, ensure_ascii=False, default=str)
            except Exception:
                excerpt_json = f"<excerpt_unserializable keys={list(excerpt.keys())}>"
            rfid_scanner_logger.info(
                "RFID receive tag[%s] epc=%s len=%s antenna=%s rssi=%s excerpt=%s",
                idx,
                epc_norm,
                len(epc_norm),
                antenna,
                rssi,
                excerpt_json[:1400],
            )
            debug_items.append(debug_item_entry)

            tags_to_create.append(
                RfidScan(
                    epc=epc_norm,
                    reader_ip=remote_addr,
                    antenna=antenna,
                    rssi=rssi,
                )
            )

        debug_payload["items"] = debug_items

        if tags_to_create:
            # Log resumen DEL REQUEST ENTERO (nivel INFO).
            summary = {
                "count": len(tags_to_create),
                "items_raw_len": len(items),
                "unique_epcs_sample": sorted([s.epc for s in tags_to_create[:5]]),
                "antenna_values": sorted({s.antenna for s in tags_to_create if s.antenna is not None}),
                "rssi_values_sample": sorted([float(s.rssi) for s in tags_to_create if s.rssi is not None])[:10],
            }
            rfid_scanner_logger.info(
                "RFID receive request: created=%s summary=%s body_sample=%s",
                len(tags_to_create),
                json.dumps(summary, ensure_ascii=False, default=str),
                (body[:200] if isinstance(body, str) else "(binary)"),
            )
            RfidScan.objects.bulk_create(tags_to_create, batch_size=200)
            debug_payload["created_epcs_sample"] = summary["unique_epcs_sample"]
            debug_payload["antenna_values"] = summary["antenna_values"]
            debug_payload["rssi_values_sample"] = summary["rssi_values_sample"]
        else:
            rfid_scanner_logger.warning(
                "RFID receive request: 0 tags creadas. Tipo items=%s len_items=%s body_sample=%s",
                type(items).__name__,
                len(items) if hasattr(items, "__len__") else "(n/a)",
                (body[:500] if isinstance(body, str) else "(binary)"),
            )
            debug_payload["warn"] = (
                "0 tags creadas. Tipo items=%s len=%s"
                % (type(items).__name__, len(items) if hasattr(items, "__len__") else "n/a")
            )

        return JsonResponse(
            {"status": "success", "count": len(tags_to_create), "debug": debug_payload}
        )
    except json.JSONDecodeError as e:
        debug_payload["error"] = f"JSON invalido: {str(e)}"
        return JsonResponse(
            {"status": "error", "message": f"JSON invalido: {str(e)}", "debug": debug_payload},
            status=400,
        )
    except Exception as e:
        rfid_scanner_logger.exception("RFID receive error")
        debug_payload["error"] = str(e)
        debug_payload["error_type"] = type(e).__name__
        return JsonResponse(
            {"status": "error", "message": str(e), "debug": debug_payload},
            status=500,
        )


def scanner_rfid_get(request):
    scans = list(
        RfidScan.objects.select_related()
        .order_by("-created_at", "-id")[:50]
    )

    # JOIN por epc contra EtiquetaRFIDDetalle (incluyendo impresion, producto, variante)
    epc_list = [s.epc for s in scans if s.epc]
    epc_lower_set = {e.lower() for e in epc_list if e}

    # Variantes de normalizacion robusta:
    # - algunos lectores envian EPC con padding (ceros al inicio/fin), hex con longitud 28 o 32
    # - Etiquetas impresas con EPC 24 hex (96 bits)
    def _epc_variants(epc):
        base = (epc or "").strip().lower()
        if not base:
            return set()
        vars = {base}
        vars.add(base.lstrip("0"))
        vars.add(base.rstrip("0"))
        vars.add(base.strip("0"))
        if len(base) > 24:
            # FX7500/FX9600 manda 28 chars (96b + 4 CRC/PC) o 32 chars (128b)
            vars.add(base[:24])
            vars.add(base[-24:])
            vars.add(base[:24].lstrip("0"))
            vars.add(base[-24:].lstrip("0"))
            for target_len in (28, 32):
                if len(base) >= target_len:
                    vars.add(base[:target_len])
                    vars.add(base[-target_len:])
        elif len(base) < 24:
            # Caso raro: FX manda EPC con ceros truncados a izq/der (len < 24)
            pad_left = base.rjust(24, "0")
            pad_right = base.ljust(24, "0")
            vars.add(pad_left)
            vars.add(pad_right)
            vars.add(pad_left.lstrip("0"))
            vars.add(pad_right.rstrip("0"))
        return {v for v in vars if len(v) >= 8}

    epc_search_set = set()
    for e in list(epc_lower_set):
        epc_search_set |= _epc_variants(e)

    # Busqueda lowercase: EtiquetaRFIDDetalle.epc es único, convertimos ambos lados a lower
    detalle_qs = (
        EtiquetaRFIDDetalle.objects.filter(
            epc__in=list(epc_search_set) + list({e.upper() for e in epc_search_set})
        )
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
    # Indexamos el detalle con LAS MISMAS variantes de normalizacion que los scans,
    # para que si detalle viene con 24 upper y scan con 28 lower + padding coincida.
    detalle_by_epc_variant = {}
    for d in detalle_qs:
        for v in _epc_variants(d.epc):
            detalle_by_epc_variant.setdefault(v, d)

    data = []
    for scan in scans:
        epc = scan.epc or ""
        epc_lower = epc.lower()
        detalle = None
        variant_used = None
        variants_tried = list(_epc_variants(epc_lower))
        for variant in variants_tried:
            detalle = detalle_by_epc_variant.get(variant)
            if detalle is not None:
                variant_used = variant
                break

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
                    "match_debug": {
                        "scan_epc": epc_lower,
                        "scan_epc_len": len(epc_lower),
                        "variants_tried": variants_tried,
                        "variant_used": variant_used,
                        "detalle_epc_raw": detalle.epc,
                        "detalle_epc_len": len(detalle.epc or ""),
                        "detalle_epc_variants": sorted(_epc_variants(detalle.epc)),
                    },
                }
            )
        else:
            item["match_impresion"] = False
            item["match_debug"] = {
                "scan_epc": epc_lower,
                "scan_epc_len": len(epc_lower),
                "variants_tried": variants_tried,
                "variant_used": None,
                "detalle_lookup_count": len(detalle_by_epc_variant),
            }
        # Log por scan en Vercel para depurar match=NO frecuentes
        if not detalle:
            rfid_scanner_logger.debug(
                "RFID get MATCH=NO scan_id=%s epc=%s len=%s tried=%s lookup_size=%s",
                scan.pk,
                epc_lower,
                len(epc_lower),
                json.dumps(variants_tried),
                len(detalle_by_epc_variant),
            )
        else:
            rfid_scanner_logger.info(
                "RFID get MATCH=SI scan_id=%s epc=%s variant=%s detalle=%s lab=%s sku=%s talla=%s",
                scan.pk,
                epc_lower,
                variant_used,
                detalle.id,
                (impresion.folio if impresion else None),
                sku,
                talla_nombre,
            )
        data.append(item)

    # --- INFO DEBUG TOP-LEVEL en /get/ response (sin entrar a Vercel / receive)
    # Útil para saber: ¿mi EPC LAB-000022 (000012e3...) se leyó en FX?
    epc_all_scans_lower = [s.epc.lower() for s in scans if s.epc]
    epc_all_scans_set = set(epc_all_scans_lower)

    # Busqueda manual especifica del ultimo EPC de impresion (si usuario lo pasa por query)
    q_epc = (request.GET.get("epc") or "").strip().lower()
    q_search_debug = None
    if q_epc:
        q_vars = sorted(_epc_variants(q_epc))
        hit = None
        for v in q_vars:
            if v in epc_all_scans_set:
                hit = v
                break
        q_search_debug = {
            "query_epc": q_epc,
            "query_epc_len": len(q_epc),
            "variants_count": len(q_vars),
            "variants_head5": q_vars[:5],
            "found_in_scans": bool(hit),
            "hit_variant": hit,
        }

    debug_get = {
        "scans_returned": len(data),
        "scans_total_max_50": len(scans),
        "lookup_detalle_count": len(detalle_by_epc_variant),
        "unique_epc_in_50_scans_count": len(epc_all_scans_set),
        "unique_epc_prefixes_head30": sorted({e[:4] for e in epc_all_scans_lower})[:30],
        "query_epc_search": q_search_debug,
    }
    return JsonResponse({"scans": data, "debug_get": debug_get})


def scanner_rfid_clear(request):
    RfidScan.objects.all().delete()
    return JsonResponse({"status": "success"})


def scanner_rfid_stats(request):
    """Endpoint rápido 1-clic para ver: ¿FX está mandando POSTs a receive?
    NO REQUIERE Vercel Dashboard ni FX web UI.
    Devuelve: total scans, último scan timestamp, últimas 5 filas (id/epc/antenna/rssi/ip/ts),
    y buscador query ?epc=XXXX igual que get pero lite.
    """
    total = RfidScan.objects.count()
    last_5 = list(
        RfidScan.objects.order_by("-created_at", "-id")[:5].values(
            "id", "epc", "antenna", "rssi", "reader_ip", "created_at"
        )
    )
    last_5_serializable = []
    for s in last_5:
        last_5_serializable.append({
            "id": s["id"],
            "epc": s["epc"],
            "epc_len": len(s["epc"] or ""),
            "antenna": s["antenna"],
            "rssi": s["rssi"],
            "reader_ip": s["reader_ip"],
            "ts": s["created_at"].isoformat() if s["created_at"] else None,
        })
    last_scan_ts = last_5_serializable[0]["ts"] if last_5_serializable else None
    last_scan_how_old_secs = None
    if last_scan_ts:
        try:
            from django.utils import timezone as dj_tz
            dt = dj_tz.datetime.fromisoformat(last_scan_ts.replace("Z", "+00:00"))
            last_scan_how_old_secs = int((dj_tz.now() - dt).total_seconds())
        except Exception:
            pass

    # Mini buscador ?epc=XXXX (igual que el get pero más rápido)
    q_epc = (request.GET.get("epc") or "").strip().lower()
    q_found_samples = []
    if q_epc:
        base_vars = {q_epc, q_epc.lstrip("0"), q_epc.rstrip("0"), q_epc.strip("0")}
        if len(q_epc) > 24:
            base_vars |= {q_epc[:24], q_epc[-24:]}
        if len(q_epc) < 24:
            base_vars |= {q_epc.rjust(24, "0"), q_epc.ljust(24, "0")}
        q_lookup = list(base_vars) + [v.upper() for v in base_vars]
        qs_found = RfidScan.objects.filter(epc__in=q_lookup).order_by("-created_at")[:10]
        for f in qs_found:
            q_found_samples.append({
                "id": f.id, "epc": f.epc, "epc_len": len(f.epc or ""),
                "antenna": f.antenna, "rssi": f.rssi,
                "ts": f.created_at.isoformat() if f.created_at else None,
            })

    payload = {
        "status": "ok",
        "total_rfidscan_rows": total,
        "last_scan_ts": last_scan_ts,
        "last_scan_seconds_ago": last_scan_how_old_secs,
        "last_5_scans": last_5_serializable,
        "query_epc": q_epc or None,
        "query_epc_found_count": len(q_found_samples),
        "query_epc_found_samples": q_found_samples,
        "receive_endpoint_info": {
            "method_required": "POST (no responde a GET — 'Method not allowed' es NORMAL)",
            "example_POST_test_1_tag": (
                "POST /QA/scanner_rfid/receive/ JSON: [{\"epcId\":\"000012e32827000147c0c5f5\",\"antennaPort\":1,\"peakRssiValue\":-45}]"
            ),
        },
    }
    return JsonResponse(payload)

