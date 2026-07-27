from decimal import Decimal
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from catalogo.models import Producto, ProductoVariante
from compras.models import OrdenCompra, RecepcionRFIDEncuadre, RecepcionRFIDLectura
from inventarios.models import Almacen
from nucleo.models import Empresa, Sucursal, UnidadMedida
from produccion.models import (
    BomDetalle,
    ListaMaterialBom,
    OrdenProduccion,
    OrdenProduccionDetalle,
)


def _empresa_qa(request):
    empresa = getattr(request.user, "empresa", None)
    if empresa:
        return empresa
    return Empresa.objects.first()


def _redirect_rfid(encuadre_id):
    return redirect(f"{reverse('qa_recepcion_rfid_workspace')}?encuadre={encuadre_id}")


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
