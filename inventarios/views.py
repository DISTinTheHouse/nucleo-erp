from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.generic import ListView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from catalogo.models import Producto
from nucleo.models import Empresa, Sucursal
from .models import (
    AjusteInventario,
    Almacen,
    Existencia,
    Lote,
    MovimientoInventario,
    MovimientoInventarioDetalle,
    Serie,
    Ubicacion,
)


class SuperuserRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser


class AlmacenListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Almacen
    template_name = 'inventarios/almacen_list.html'
    context_object_name = 'almacenes'


class UbicacionListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Ubicacion
    template_name = 'inventarios/ubicacion_list.html'
    context_object_name = 'ubicaciones'


@login_required
def wms_demo(request):
    user = request.user

    if user.is_superuser:
        empresa_qs = Empresa.objects.all().order_by("codigo")
        sucursal_qs = Sucursal.objects.select_related("empresa").all().order_by("empresa__codigo", "codigo")
        producto_qs = Producto.objects.select_related("empresa").filter(activo=True).order_by("empresa__codigo", "nombre")
        almacen_qs = Almacen.objects.select_related("empresa", "sucursal").all().order_by("empresa__codigo", "sucursal__codigo", "codigo")
        ubicacion_qs = Ubicacion.objects.select_related("almacen").all().order_by("almacen__codigo", "orden_recorrido", "id_ubicacion")
    else:
        empresa_ids = []
        if getattr(user, "empresa_id", None):
            empresa_ids.append(user.empresa_id)
        empresa_ids += list(user.empresas.values_list("pk", flat=True))
        empresa_ids = list(set(empresa_ids))
        sucursal_ids = list(user.sucursales.values_list("pk", flat=True))

        empresa_qs = Empresa.objects.filter(pk__in=empresa_ids).order_by("codigo")
        sucursal_qs = Sucursal.objects.filter(pk__in=sucursal_ids).select_related("empresa").order_by("empresa__codigo", "codigo")
        producto_qs = Producto.objects.filter(empresa_id__in=empresa_ids, activo=True).select_related("empresa").order_by("empresa__codigo", "nombre")
        almacen_qs = Almacen.objects.filter(empresa_id__in=empresa_ids, sucursal_id__in=sucursal_ids).select_related("empresa", "sucursal").order_by("empresa__codigo", "sucursal__codigo", "codigo")
        ubicacion_qs = Ubicacion.objects.filter(almacen__empresa_id__in=empresa_ids, almacen__sucursal_id__in=sucursal_ids).select_related("almacen").order_by("almacen__codigo", "orden_recorrido", "id_ubicacion")

    lote_qs = Lote.objects.select_related("producto").all().order_by("id")
    serie_qs = Serie.objects.select_related("producto").all().order_by("id")

    selected_almacen_id = request.GET.get("almacen_id") or ""
    selected_producto_id = request.GET.get("producto_id") or ""
    selected_ubicacion_id = request.GET.get("ubicacion_id") or ""
    selected_movimiento_id = request.GET.get("movimiento_id") or ""

    if request.method == "POST":
        if request.POST.get("action") != "crear_ajuste":
            return HttpResponseBadRequest("Acción inválida")

        empresa_id = request.POST.get("empresa_id")
        sucursal_id = request.POST.get("sucursal_id")
        almacen_id = request.POST.get("almacen_id")

        if not empresa_id or not sucursal_id or not almacen_id:
            return HttpResponseBadRequest("empresa_id, sucursal_id y almacen_id son requeridos")

        try:
            empresa_id = int(empresa_id)
            sucursal_id = int(sucursal_id)
            almacen_id = int(almacen_id)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("IDs inválidos")

        empresa = empresa_qs.filter(pk=empresa_id).first()
        sucursal = sucursal_qs.filter(pk=sucursal_id).first()
        almacen = almacen_qs.filter(pk=almacen_id).first()

        if not empresa or not sucursal or not almacen:
            return HttpResponseBadRequest("No autorizado o recursos no encontrados")

        detalles_producto_ids = request.POST.getlist("detalle_producto_id[]")
        detalles_cantidades = request.POST.getlist("detalle_cantidad[]")
        detalles_ubicacion_origen_ids = request.POST.getlist("detalle_ubicacion_origen_id[]")
        detalles_ubicacion_destino_ids = request.POST.getlist("detalle_ubicacion_destino_id[]")
        detalles_lote_ids = request.POST.getlist("detalle_lote_id[]")
        detalles_serie_ids = request.POST.getlist("detalle_serie_id[]")

        with transaction.atomic():
            ajuste = AjusteInventario.objects.create(
                empresa=empresa,
                sucursal=sucursal,
                almacen=almacen,
            )

            pedido = None
            try:
                from ventas.models import Pedido

                pedido = Pedido.objects.filter(empresa_id=empresa.pk).order_by("-pk").first()
            except Exception:
                pedido = None

            movimiento = None
            if pedido:
                movimiento = MovimientoInventario.objects.create(
                    empresa=empresa,
                    sucursal=sucursal,
                    pedido=pedido,
                    entrega=None,
                    devolucion=None,
                    ajuste_inventario=ajuste,
                )

                rows = zip(
                    detalles_producto_ids,
                    detalles_cantidades,
                    detalles_ubicacion_origen_ids,
                    detalles_ubicacion_destino_ids,
                    detalles_lote_ids,
                    detalles_serie_ids,
                )

                for producto_id, _, ubicacion_origen_id, ubicacion_destino_id, lote_id, serie_id in rows:
                    if not producto_id:
                        continue

                    try:
                        producto_id = int(producto_id)
                        ubicacion_origen_id = int(ubicacion_origen_id) if ubicacion_origen_id else None
                        ubicacion_destino_id = int(ubicacion_destino_id) if ubicacion_destino_id else None
                        lote_id = int(lote_id) if lote_id else None
                        serie_id = int(serie_id) if serie_id else None
                    except (TypeError, ValueError):
                        continue

                    producto = producto_qs.filter(pk=producto_id).first()
                    ubicacion_origen = ubicacion_qs.filter(pk=ubicacion_origen_id).first() if ubicacion_origen_id else None
                    ubicacion_destino = ubicacion_qs.filter(pk=ubicacion_destino_id).first() if ubicacion_destino_id else None
                    lote = lote_qs.filter(pk=lote_id).first() if lote_id else None
                    serie = serie_qs.filter(pk=serie_id).first() if serie_id else None

                    if not (producto and ubicacion_origen and ubicacion_destino and lote and serie):
                        continue

                    MovimientoInventarioDetalle.objects.create(
                        movimiento_inventario=movimiento,
                        producto=producto,
                        ubicacion_origen=ubicacion_origen,
                        ubicacion_destino=ubicacion_destino,
                        lote=lote,
                        serie=serie,
                    )

        return redirect(f"{reverse('inventarios:wms_demo')}?saved=1&movimiento_id={(movimiento.pk if movimiento else '')}#existencias")

    existencias_qs = (
        Existencia.objects.select_related("producto", "almacen", "ubicacion", "lote", "serie")
        .all()
        .order_by("-id")
    )
    movimientos_qs = (
        MovimientoInventario.objects.select_related("empresa", "sucursal", "pedido", "entrega", "devolucion", "ajuste_inventario")
        .all()
        .order_by("-id")
    )

    if not user.is_superuser:
        empresa_ids = []
        if getattr(user, "empresa_id", None):
            empresa_ids.append(user.empresa_id)
        empresa_ids += list(user.empresas.values_list("pk", flat=True))
        empresa_ids = list(set(empresa_ids))
        sucursal_ids = list(user.sucursales.values_list("pk", flat=True))

        existencias_qs = existencias_qs.filter(almacen__empresa_id__in=empresa_ids, almacen__sucursal_id__in=sucursal_ids)
        movimientos_qs = movimientos_qs.filter(empresa_id__in=empresa_ids, sucursal_id__in=sucursal_ids)

    if selected_almacen_id:
        existencias_qs = existencias_qs.filter(almacen_id=selected_almacen_id)
    if selected_producto_id:
        existencias_qs = existencias_qs.filter(producto_id=selected_producto_id)
    if selected_ubicacion_id:
        existencias_qs = existencias_qs.filter(ubicacion_id=selected_ubicacion_id)

    existencias = list(existencias_qs[:200])
    movimientos = list(movimientos_qs[:50])

    movimiento_seleccionado = None
    detalles_movimiento = []
    if selected_movimiento_id:
        try:
            selected_movimiento_id_int = int(selected_movimiento_id)
        except (TypeError, ValueError):
            selected_movimiento_id_int = None

        if selected_movimiento_id_int is not None:
            movimiento_seleccionado = movimientos_qs.filter(pk=selected_movimiento_id_int).first()
            if movimiento_seleccionado:
                detalles_movimiento = list(
                    MovimientoInventarioDetalle.objects.select_related(
                        "movimiento_inventario",
                        "producto",
                        "ubicacion_origen",
                        "ubicacion_destino",
                        "lote",
                        "serie",
                    )
                    .filter(movimiento_inventario=movimiento_seleccionado)
                    .order_by("id")
                )

    context = {
        "saved": request.GET.get("saved") == "1",
        "warning_pedido_required": MovimientoInventario._meta.get_field("pedido").null is False,
        "warning_cantidad_missing": not hasattr(Existencia, "cantidad") or not hasattr(MovimientoInventarioDetalle, "cantidad"),
        "empresa_list": list(empresa_qs),
        "sucursal_list": list(sucursal_qs),
        "producto_list": list(producto_qs[:250]),
        "almacen_list": list(almacen_qs),
        "ubicacion_list": list(ubicacion_qs),
        "lote_list": list(lote_qs[:250]),
        "serie_list": list(serie_qs[:250]),
        "existencias": existencias,
        "movimientos": movimientos,
        "movimiento_seleccionado": movimiento_seleccionado,
        "detalles_movimiento": detalles_movimiento,
        "selected_almacen_id": selected_almacen_id,
        "selected_producto_id": selected_producto_id,
        "selected_ubicacion_id": selected_ubicacion_id,
    }

    return render(request, "inventarios/wms_demo.html", context)
