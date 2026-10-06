import json
import logging
import uuid
from decimal import Decimal
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError as DjangoValidationError
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
    ConsumoProduccion,
    ListaMaterialBom,
    OrdenProduccion,
    OrdenProduccionDetalle,
)
from produccion.services.orden_bordado_service import OrdenBordadoService
from produccion.services.orden_corte_manga_service import OrdenCorteMangaService
from produccion.services.orden_reflejante_service import OrdenReflejanteService
from rest_framework.exceptions import APIException as DRFAPIException
from rest_framework.exceptions import ValidationError as DRFValidationError
from usuarios.models import Usuario
from ventas.models import Pedido
from ventas.scope import pedidos_base, pedidos_visibles
from wms.api.serializers import (
    EtiquetaRFIDCreateSerializer,
    EtiquetaRFIDSerializer,
)
from wms.models import EtiquetaRFIDDetalle, EtiquetaRFIDImpresion, RfidScan
from wms.services.rfid_scan_service import lector_desde_request, recibir_lecturas
from wms.services.rfid_label_service import RFIDLabelService

rfid_scanner_logger = logging.getLogger(__name__)


def _empresa_qa(request):
    return getattr(request.user, "empresa", None)


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


# VENTAS — workspace unificado de Pedido: buscar -> ver -> editar en un solo
# lienzo. Nunca hay una lista de "pedidos" ni un "crear pedido" aparte; se
# encuentra el pedido primero (por folio/cliente/RFC/OC) y se trabaja sobre
# ese único registro. Ver DOCS o pedir contexto antes de tocar esto.

PEDIDO_CAMPOS_EDITABLES = {
    "estatus": {"tipo": "choice", "choices": dict(Pedido.CHOICES_ESTATUS)},
    "clasificacion": {"tipo": "choice", "choices": dict(Pedido.Clasificacion.choices)},
    "forma_pago": {"tipo": "choice", "choices": dict(Pedido.FormaPago.choices)},
    "metodo_pago": {"tipo": "choice", "choices": dict(Pedido.MetodoPago.choices)},
    "oc": {"tipo": "texto"},
    "observaciones": {"tipo": "texto"},
    "persona_pagos": {"tipo": "texto"},
    "correo_facturas": {"tipo": "texto"},
    "telefono_pagos": {"tipo": "texto"},
    "destinatario": {"tipo": "texto"},
    "telefono_envio": {"tipo": "texto"},
    "direccion_envio": {"tipo": "texto"},
    "colonia_envio": {"tipo": "texto"},
    "ciudad_envio": {"tipo": "texto"},
    "estado_envio": {"tipo": "texto"},
    "codigo_postal": {"tipo": "texto"},
}


def _pedidos_qs_qa(user):
    return pedidos_visibles(
        pedidos_base().select_related(
            "cliente", "moneda", "sucursal", "empresa", "cliente_regimen_fiscal",
        ),
        user,
    )


@login_required
def pedidos_workspace(request):
    q = (request.GET.get("q") or "").strip()
    pedido_id = request.GET.get("id")
    seleccionado = None
    resultados = []

    base_qs = _pedidos_qs_qa(request.user)

    if pedido_id and str(pedido_id).isdigit():
        seleccionado = base_qs.filter(pk=int(pedido_id)).first()
        if seleccionado is None:
            messages.warning(request, f"No se encontró el pedido #{pedido_id}.")

    if seleccionado is None and q:
        busqueda = base_qs.filter(
            Q(folio__icontains=q)
            | Q(id__icontains=q)
            | Q(oc__icontains=q)
            | Q(cliente_nombre__icontains=q)
            | Q(cliente_razon_social__icontains=q)
            | Q(cliente_rfc__icontains=q)
            | Q(cliente__nombre__icontains=q)
        ).order_by("-created_at")[:30]
        if len(busqueda) == 1:
            seleccionado = busqueda[0]
        else:
            resultados = list(busqueda)

    if seleccionado is None and not q:
        resultados = list(base_qs.order_by("-created_at")[:30])

    detalles = []
    if seleccionado is not None:
        seleccionado = (
            base_qs.prefetch_related(
                "detalles__producto",
                "detalles__color",
                "detalles__tallas__talla",
                "servicios_extras",
            ).get(pk=seleccionado.pk)
        )
        for d in seleccionado.detalles.all():
            tallas = [
                {
                    "talla": t.talla.nombre if t.talla else "—",
                    "cantidad": t.cantidad,
                    "precio_unitario": t.precio_unitario if t.precio_unitario is not None else d.precio_unitario,
                    "subtotal": t.subtotal_talla,
                }
                for t in d.tallas.all()
            ]
            detalles.append({
                "id": d.id,
                "nombre": d.producto.nombre if d.producto else (d.producto_nombre_externo or "Producto sin nombre"),
                "color": d.color.nombre if d.color else None,
                "color_hex": d.color.codigo_hex if d.color else None,
                "precio_unitario": d.precio_unitario,
                "subtotal_linea": d.subtotal_linea,
                "tallas": tallas,
                "piezas": sum(t["cantidad"] for t in tallas),
            })

    context = {
        "q": q,
        "resultados": resultados,
        "pedido": seleccionado,
        "detalles": detalles,
        "servicios_extras": seleccionado.servicios_extras.all() if seleccionado else [],
        "estatus_choices": Pedido.CHOICES_ESTATUS,
        "clasificacion_choices": Pedido.Clasificacion.choices,
        "forma_pago_choices": Pedido.FormaPago.choices,
        "metodo_pago_choices": Pedido.MetodoPago.choices,
    }
    return render(request, "QA/ventas/pedido_workspace.html", context)


@login_required
def qa_pedido_actualizar_campo(request, pedido_id):
    """PATCH-por-POST de un único campo de Pedido, para edición inline.

    Whitelist ``PEDIDO_CAMPOS_EDITABLES`` — nunca setattr con un nombre de
    campo que venga crudo del cliente.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método no permitido."}, status=405)

    pedido = get_object_or_404(_pedidos_qs_qa(request.user), pk=pedido_id)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "JSON inválido."}, status=400)

    campo = body.get("campo")
    valor = body.get("valor", "")
    config = PEDIDO_CAMPOS_EDITABLES.get(campo)
    if not config:
        return JsonResponse({"ok": False, "error": f"Campo '{campo}' no es editable."}, status=400)

    if config["tipo"] == "choice":
        choices_dict = config["choices"]
        valor_normalizado = valor
        claves = list(choices_dict.keys())
        if claves and isinstance(claves[0], int):
            try:
                valor_normalizado = int(valor)
            except (TypeError, ValueError):
                return JsonResponse({"ok": False, "error": "Valor inválido."}, status=400)
        if valor_normalizado not in choices_dict:
            return JsonResponse({"ok": False, "error": "Opción no válida."}, status=400)
        setattr(pedido, campo, valor_normalizado)
        valor_mostrado = choices_dict[valor_normalizado]
        valor_respuesta = valor_normalizado
    else:
        valor = (valor or "").strip()
        setattr(pedido, campo, valor)
        valor_mostrado = valor or "—"
        valor_respuesta = valor

    pedido.save(update_fields=[campo, "updated_at"])

    return JsonResponse({
        "ok": True,
        "campo": campo,
        "valor": valor_respuesta,
        "valor_mostrado": valor_mostrado,
    })


# ORDENES DE TRABAJO (produccion.OrdenProduccion) — mismo patrón que Pedidos:
# buscar -> ver -> editar en un solo lienzo, encontrar antes de editar.

ORDEN_TRABAJO_CAMPOS_EDITABLES = {
    "estatus_op": {"tipo": "choice", "choices": dict(OrdenProduccion.EstatusOrdenProduccion.choices)},
    "prioridad": {"tipo": "numero"},
    "observaciones": {"tipo": "texto"},
}


def _ordenes_produccion_qs_qa(user):
    if getattr(user, "is_superuser", False):
        qs = OrdenProduccion.objects.all()
    else:
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return OrdenProduccion.objects.none()
        qs = OrdenProduccion.objects.filter(empresa=empresa)
    return qs.select_related("empresa", "sucursal", "pedido", "pedido__cliente", "usuario_asignado", "ruta_produccion")


@login_required
def ordenes_trabajo_workspace(request):
    base_qs = _ordenes_produccion_qs_qa(request.user)

    if request.method == "POST" and request.POST.get("action") == "crear":
        tipo = request.POST.get("tipo") or "produccion"
        pedido = get_object_or_404(
            pedidos_visibles(pedidos_base(), request.user),
            pk=request.POST.get("pedido_id"),
        )
        prioridad_raw = request.POST.get("prioridad")
        try:
            prioridad = max(1, int(prioridad_raw))
        except (TypeError, ValueError):
            prioridad = 1
        observaciones = (request.POST.get("observaciones") or "").strip()

        if tipo == "produccion":
            nueva_op = OrdenProduccion.objects.create(
                empresa=pedido.empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                folio_op=f"OP-{uuid.uuid4().hex[:8].upper()}",
                prioridad=prioridad,
                observaciones=observaciones,
            )
            messages.success(request, f"Orden {nueva_op.folio_op} creada.")
            return redirect(f"{reverse('qa_ordenes_trabajo_workspace')}?id={nueva_op.op_id}")

        servicio = {
            "bordado": OrdenBordadoService,
            "reflejante": OrdenReflejanteService,
            "corte_manga": OrdenCorteMangaService,
        }.get(tipo)
        if servicio is None:
            messages.error(request, "Tipo de orden no reconocido.")
            return redirect(f"{reverse('qa_ordenes_trabajo_workspace')}?crear=1")

        # Servicios reales de produccion.services.* — misma validación de
        # empresa/sucursal, mismo candado antiduplicado (409) y mismo cupo por
        # línea que usa la API real. Las líneas se auto-generan aquí adentro
        # desde las tallas del pedido con lleva_bordado/reflejante/corte_manga;
        # no se reinventa esa lógica en QA.
        try:
            orden_creada = servicio.save(
                {"pedido": pedido, "prioridad": prioridad, "observaciones": observaciones},
                request.user,
            )
        except DRFAPIException as exc:
            detail = getattr(exc, "detail", None)
            if isinstance(detail, dict):
                mensaje = detail.get("err") or "; ".join(str(v) for v in detail.values())
            else:
                mensaje = str(detail) if detail is not None else str(exc)
            messages.error(request, f"No se pudo crear la orden: {mensaje}")
            return redirect(f"{reverse('qa_ordenes_trabajo_workspace')}?crear=1")
        except DjangoValidationError as exc:
            mensaje = "; ".join(str(m) for m in exc.messages) if hasattr(exc, "messages") else str(exc)
            messages.error(request, f"No se pudo crear la orden: {mensaje}")
            return redirect(f"{reverse('qa_ordenes_trabajo_workspace')}?crear=1")

        piezas = sum(float(d.cantidad) for d in orden_creada.detalles.all())
        messages.success(
            request,
            f"Orden {orden_creada} creada con {orden_creada.detalles.count()} "
            f"renglón(es), {piezas:g} piezas tomadas del pedido.",
        )
        return redirect(f"{reverse('qa_ordenes_trabajo_workspace')}?crear=1")

    q = (request.GET.get("q") or "").strip()
    op_id = request.GET.get("id")
    seleccionada = None
    resultados = []

    if op_id and str(op_id).isdigit():
        seleccionada = base_qs.filter(pk=int(op_id)).first()
        if seleccionada is None:
            messages.warning(request, f"No se encontró la orden de trabajo #{op_id}.")

    if seleccionada is None and q:
        busqueda = base_qs.filter(
            Q(folio_op__icontains=q)
            | Q(op_id__icontains=q)
            | Q(pedido__folio__icontains=q)
            | Q(pedido__cliente_nombre__icontains=q)
            | Q(pedido__cliente_razon_social__icontains=q)
        ).order_by("-fecha_inicio", "-op_id")[:30]
        if len(busqueda) == 1:
            seleccionada = busqueda[0]
        else:
            resultados = list(busqueda)

    if seleccionada is None and not q:
        resultados = list(base_qs.order_by("-fecha_inicio", "-op_id")[:30])

    detalles = []
    consumos = []
    historial = []
    usuarios_empresa = []
    if seleccionada is not None:
        seleccionada = base_qs.prefetch_related(
            "orden_produccion_detalle__producto_variante__producto",
            "orden_produccion_detalle__producto_variante__color",
            "orden_produccion_detalle__producto_variante__talla",
            "orden_produccion_detalle__bom__materia_prima_detalle__componente",
            "orden_produccion_detalle__bom__materia_prima_detalle__unidad",
            "orden_produccion_detalle__unidad",
        ).get(pk=seleccionada.pk)

        for d in seleccionada.orden_produccion_detalle.all():
            componentes = [
                {
                    "nombre": bd.componente.nombre if bd.componente else "Insumo sin nombre",
                    "cantidad": bd.cantidad,
                    "unidad": bd.unidad.nombre if bd.unidad else "",
                }
                for bd in d.bom.materia_prima_detalle.all()
            ] if d.bom_id else []
            variante = d.producto_variante
            detalles.append({
                "id": d.op_detalle_id,
                "producto": variante.producto.nombre if variante and variante.producto else "—",
                "color": variante.color.nombre if variante and variante.color else None,
                "talla": variante.talla.nombre if variante and variante.talla else None,
                "cantidad": d.cantidad,
                "unidad": d.unidad.nombre if d.unidad else "",
                "observaciones": d.observaciones,
                "componentes": componentes,
            })

        for c in ConsumoProduccion.objects.filter(op=seleccionada).prefetch_related("detalles__producto"):
            for det in c.detalles.all():
                consumos.append({
                    "producto": det.producto.nombre if det.producto else "—",
                    "cantidad": det.cantidad,
                })

        history_qs = list(seleccionada.history.all().order_by("-history_date")[:40])
        tipo_labels = {"+": "Creada", "~": "Modificada", "-": "Eliminada"}
        for i, record in enumerate(history_qs):
            campos = []
            if record.history_type == "~" and i + 1 < len(history_qs):
                try:
                    campos = [c.field for c in record.diff_against(history_qs[i + 1]).changes]
                except Exception:
                    campos = []
            historial.append({
                "fecha": record.history_date,
                "usuario": getattr(record.history_user, "username", None) or "Sistema",
                "tipo": tipo_labels.get(record.history_type, record.history_type),
                "campos": campos,
            })

        usuarios_empresa = list(
            Usuario.objects.filter(empresa=seleccionada.empresa, is_active=True).order_by("username")
        )

    mostrar_form_crear = seleccionada is None and request.GET.get("crear") == "1"
    pedidos_disponibles = []
    if mostrar_form_crear:
        pedidos_disponibles = list(
            pedidos_visibles(pedidos_base(), request.user)
            .select_related("cliente")
            .order_by("-created_at")[:100]
        )

    context = {
        "q": q,
        "resultados": resultados,
        "orden": seleccionada,
        "detalles": detalles,
        "consumos": consumos,
        "historial": historial,
        "usuarios_empresa": usuarios_empresa,
        "estatus_choices": OrdenProduccion.EstatusOrdenProduccion.choices,
        "mostrar_form_crear": mostrar_form_crear,
        "pedidos_disponibles": pedidos_disponibles,
    }
    return render(request, "QA/produccion/orden_trabajo_workspace.html", context)


@login_required
def qa_orden_trabajo_actualizar_campo(request, op_id):
    """PATCH-por-POST de un único campo de OrdenProduccion, para edición inline.

    Mismo patrón que ``qa_pedido_actualizar_campo``: whitelist explícita,
    nunca setattr con un nombre de campo crudo del cliente. ``usuario_asignado``
    se resuelve aparte porque sus opciones son dinámicas (usuarios de la
    empresa), no un choices estático del modelo.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método no permitido."}, status=405)

    orden = get_object_or_404(_ordenes_produccion_qs_qa(request.user), pk=op_id)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "JSON inválido."}, status=400)

    campo = body.get("campo")
    valor = body.get("valor", "")

    if campo == "usuario_asignado":
        if not valor:
            orden.usuario_asignado = None
            valor_respuesta = None
            valor_mostrado = "Sin asignar"
        else:
            usuario = Usuario.objects.filter(pk=valor, empresa=orden.empresa).first()
            if usuario is None:
                return JsonResponse({"ok": False, "error": "Usuario no válido para esta empresa."}, status=400)
            orden.usuario_asignado = usuario
            valor_respuesta = usuario.pk
            valor_mostrado = usuario.username
        orden.save(update_fields=["usuario_asignado"])
        return JsonResponse({"ok": True, "campo": campo, "valor": valor_respuesta, "valor_mostrado": valor_mostrado})

    config = ORDEN_TRABAJO_CAMPOS_EDITABLES.get(campo)
    if not config:
        return JsonResponse({"ok": False, "error": f"Campo '{campo}' no es editable."}, status=400)

    if config["tipo"] == "choice":
        choices_dict = config["choices"]
        try:
            valor_normalizado = int(valor)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Valor inválido."}, status=400)
        if valor_normalizado not in choices_dict:
            return JsonResponse({"ok": False, "error": "Opción no válida."}, status=400)
        setattr(orden, campo, valor_normalizado)
        valor_mostrado = choices_dict[valor_normalizado]
        valor_respuesta = valor_normalizado
    elif config["tipo"] == "numero":
        try:
            valor_normalizado = int(valor)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Debe ser un número entero."}, status=400)
        if valor_normalizado < 1:
            return JsonResponse({"ok": False, "error": "La prioridad debe ser mayor a 0."}, status=400)
        setattr(orden, campo, valor_normalizado)
        valor_mostrado = str(valor_normalizado)
        valor_respuesta = valor_normalizado
    else:
        valor = (valor or "").strip()
        setattr(orden, campo, valor)
        valor_mostrado = valor or "—"
        valor_respuesta = valor

    orden.save(update_fields=[campo])

    return JsonResponse({
        "ok": True,
        "campo": campo,
        "valor": valor_respuesta,
        "valor_mostrado": valor_mostrado,
    })


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

        empresa_default = getattr(request.user, "empresa", None)
        if empresa_default is None:
            messages.error(request, "Tu usuario no tiene empresa asignada.")
            return redirect("generar_orden_produccion")

        try:
            with transaction.atomic():
                sucursal = Sucursal.objects.get(pk=sucursal_id, empresa=empresa_default)
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
    lector = lector_desde_request(request)
    if lector is None:
        return JsonResponse({"status": "error", "message": "Lector no autorizado."}, status=401)
    return recibir_lecturas(request, lector)


@login_required
def scanner_rfid_get(request):
    scans = list(
        RfidScan.objects.visibles_para(request.user)
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
    if not getattr(request.user, "is_superuser", False):
        detalle_qs = detalle_qs.filter(impresion__empresa_id=request.user.empresa_id)
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


@login_required
def scanner_rfid_clear(request):
    user = request.user
    if not (getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False)):
        return JsonResponse({"status": "error", "message": "No tiene permisos para realizar esta acción."}, status=403)
    RfidScan.objects.visibles_para(user).delete()
    return JsonResponse({"status": "success"})


@login_required
def scanner_rfid_stats(request):
    """Endpoint rápido 1-clic para ver: ¿FX está mandando POSTs a receive?
    NO REQUIERE Vercel Dashboard ni FX web UI.
    Devuelve: total scans, último scan timestamp, últimas 5 filas (id/epc/antenna/rssi/ip/ts),
    y buscador query ?epc=XXXX igual que get pero lite.
    """
    user = request.user
    if not (getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False)):
        return JsonResponse({"status": "error", "message": "No tiene permisos para realizar esta acción."}, status=403)
    scans_qs = RfidScan.objects.visibles_para(user)
    total = scans_qs.count()
    last_5 = list(
        scans_qs.order_by("-created_at", "-id")[:5].values(
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
        qs_found = scans_qs.filter(epc__in=q_lookup).order_by("-created_at")[:10]
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
                "POST /QA/scanner_rfid/receive/ header X-RFID-Token: <token> (o ?token=<token>) "
                "JSON: [{\"epcId\":\"000012e32827000147c0c5f5\",\"antennaPort\":1,\"peakRssiValue\":-45}]"
            ),
        },
    }
    return JsonResponse(payload)

