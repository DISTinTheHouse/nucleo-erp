import logging
from nucleo.motivo import exigir_largo_motivo
from decimal import Decimal
from types import SimpleNamespace

from django.db import IntegrityError, transaction
from django.db.models import (
    Count, DecimalField, Exists, ExpressionWrapper, F, OuterRef, Prefetch, Q, Subquery, Sum, Value, Window,
    prefetch_related_objects,
)
from django.db.models.functions import Coalesce, RowNumber
from django.utils import timezone
from rest_framework import generics, serializers as drf_serializers, status, viewsets
from rest_framework.filters import OrderingFilter
from rest_framework.decorators import action
from rest_framework.permissions import SAFE_METHODS
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from inventarios.services import costeo
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter, OpenApiResponse, PolymorphicProxySerializer, extend_schema, extend_schema_view,
    inline_serializer,
)
from simple_history.utils import bulk_create_with_history

from auditoria.models import AuditoriaEvento
from catalogo.api.views import CODIGO_PRODUCTO_TERMINADO
from catalogo.models import Producto, ProductoVariante
from compras.models import (
    CalidadInspeccion,
    CalidadInspeccionDetalle,
    DevolucionProveedor,
    OrdenCompra,
    OrdenCompraDetalle,
    OrdenCompraRutaCritica,
    Recepcion,
    RecepcionDetalle,
    RecepcionRFIDEncuadre,
    RecepcionRFIDLectura,
)
from finanzas.models import FacturaProveedor
from compras.services.kpi_criterios import (
    MONEY,
    AlcanceCompras,
    con_importes_precio,
    cumplimiento_pendiente_por_oc,
    cumplimiento_por_unidad,
    lineas_factura_proveedor,
    misma_moneda,
    porcentaje,
    rechazos_oc,
)
from nucleo.permisos import permisos_de_la_peticion
from nucleo.kpis import TOPE_DRILL_DOWN, entero_param, marcar_tope, rango_fechas, validar_parametros
from nucleo.listados import (
    OrdenamientoEstableFilter,
    OrdenamientoNullsAlFinal,
    PaginacionOpcionalLimitOffset,
    booleano_param,
    filtrar_listado_compras,
)
from compras.api.serializers import (
    direccion_sucursal,
    CalidadInspeccionInputSerializer,
    CalidadInspeccionSerializer,
    OrdenCompraOnboardingHeaderSerializer,
    OrdenCompraOnboardingSerializer,
    OrdenCompraRetrieveSerializer,
    OrdenCompraRutaCriticaSerializer,
    OrdenCompraSerializer,
    con_pre_ordenes,
    OrdenCompraDetalleSerializer,
    RecepcionDetalleSerializer,
    RecepcionListSerializer,
    RecepcionOnboardingSerializer,
    RecepcionRetrieveSerializer,
    RecepcionSerializer,
    RecepcionRFIDEncuadreSerializer,
    RecepcionRFIDEncuadreCabeceraSerializer,
    RecepcionRFIDEncuadreSinLecturasSerializer,
    RecepcionRFIDEncuadreCreateSerializer,
    RecepcionRFIDEncuadreListSerializer,
    RecepcionRFIDLecturaSerializer,
    RecepcionRFIDLecturaInputSerializer,
    RecepcionRFIDLecturaLigeraSerializer,
    RecepcionRFIDLecturasLoteInputSerializer,
)
from compras.services.orden_compra_view_service import (
    CAMPOS_CONTABILIDAD_OC,
    SIN_PERMISO_CONTABILIDAD,
    puede_ver_contabilidad,
    quitar_montos_orden_compra,
)
from nucleo.importes import quitar_montos
from hr.models import Empleado
from inventarios.models import (
    Almacen,
    Existencia,
    Lote,
    MovimientoInventario,
    MovimientoInventarioDetalle,
    Serie,
    Ubicacion,
)
from nucleo.middleware import ip_auditoria
from nucleo.models import Moneda, SerieFolio, Sucursal
from ventas.models import Pedido
from produccion.models import OrdenProduccion, OrdenProduccionDetalle, SalidaMaquila
from compras.services.maquila import (
    CENTAVO, ESTATUS_OP_RECIBIBLES, apartado_en_maquila, facturar_maquila, marcar_recibida, origen_sm,
    renglones_sm_servicio, pendiente_de_sm, pendiente_por_renglon_sm, recibido_sm_subquery,
)
from compras.services import devolucion_proveedor
from compras.services.recepcion_pedidos import RELACIONES_PEDIDO, origen_folio, pedidos_de_recepciones
from compras.services.cancelacion_recepcion import (
    MOTIVO_CANCELACION_MAX,
    anotar_bloqueos_cancelacion,
    op_se_reabre,
    puede_cancelar_recepciones,
    validar_cancelacion_recepcion,
)
from terceros.models import Proveedor, Transportista
from wms.services.rfid_scan_service import etiquetas_por_epc, formas_equivalentes_epc, normalizar_epc

logger = logging.getLogger(__name__)

PERMISO_EDITAR_RUTA_CRITICA_OC = "E-COMPRAS-OC"
# Confirmar (aceptar/) y cancelar una OC: el código con el que el front muestra
# los dos botones (#1320).
PERMISO_AUTORIZAR_OC = "A-COMPRAS-OC"
PERMISO_CREAR_OC = "C-COMPRAS-OC"
PERMISO_EDITAR_OC = "E-COMPRAS-OC"
PERMISO_ELIMINAR_OC = "D-COMPRAS-OC"
# Onboarding de recepciones (#1295): los códigos de WMS que usa el front.
PERMISO_LEER_RECEPCIONES = "R-WMS-RECEPCIONES"
PERMISO_CREAR_RECEPCIONES = "C-WMS-RECEPCIONES"


_ip_auditoria = ip_auditoria



def _error_detalle(errores):
    """400 de recepciones con la forma de DRF: cada llave con lista de mensajes, como
    lo deja el serializer (#1189, #1296)."""
    return ValidationError({k: [v] if isinstance(v, str) else v for k, v in errores.items()})


def _ref_renglon(idx, detalle):
    """``#N (orden_compra_detalle 4812, SKU X)``: posición en el payload más una referencia
    estable que el usuario reconoce (#1188). Sin consultas: el renglón ya viene cargado."""
    campo = "orden_compra_detalle" if isinstance(detalle, OrdenCompraDetalle) else "orden_produccion_detalle"
    variante = getattr(detalle, "producto_variante", None) if campo == "orden_produccion_detalle" else None
    etiqueta = (f"SKU {variante.sku}" if variante is not None and variante.sku
                else getattr(getattr(detalle, "producto", None), "nombre", None))
    return f"#{idx + 1} ({campo} {detalle.pk}" + (f", {etiqueta}" if etiqueta else "") + ")"

class OrdenamientoSinMontosFilter(OrdenamientoEstableFilter):
    """``?ordering=`` sin los importes que el usuario no puede ver (#837).

    Ordenar por ``gran_total`` aunque la respuesta lo oculte deja deducir qué
    OC cuesta más. Esos campos se descartan en silencio y queda el orden por
    defecto de la vista, igual que con cualquier campo inválido.
    """

    def remove_invalid_fields(self, queryset, fields, view, request):
        validos = super().remove_invalid_fields(queryset, fields, view, request)
        if view.ver_montos():
            return validos
        return [f for f in validos if f.lstrip("-") not in CAMPOS_CONTABILIDAD_OC]


def _suma_por_oc(qs, campo):
    """``SUM(campo)`` de ``qs`` para la OC de la fila externa, como subconsulta correlacionada."""
    return Coalesce(
        Subquery(
            qs.order_by().values("oc_ref").annotate(total=Sum(campo)).values("total")[:1],
            output_field=DecimalField(max_digits=18, decimal_places=4),
        ),
        Value(Decimal("0")),
        output_field=DecimalField(max_digits=18, decimal_places=4),
    )


def anotar_progreso_oc(qs):
    """Progreso de recepción de cada OC en el mismo SELECT (sin consultas extra).

    Misma regla que el pendiente de ``recepciones/onboarding``: cuenta lo
    recibido en recepciones activas no canceladas y descuenta lo que Calidad
    rechaza. Lo ordenado es ``total_piezas`` (columna de la OC). Las dos
    subconsultas van por FK indexada.
    """
    recibido = (
        RecepcionDetalle.objects.filter(
            orden_compra_detalle__orden_compra_id=OuterRef("pk"), recepcion__activo=True,
        )
        .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
        .annotate(oc_ref=F("orden_compra_detalle__orden_compra_id"))
    )
    rechazado = (
        CalidadInspeccionDetalle.objects.filter(
            recepcion_detalle__orden_compra_detalle__orden_compra_id=OuterRef("pk"),
            recepcion_detalle__recepcion__activo=True,
        )
        .exclude(recepcion_detalle__recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
        .exclude(calidad_inspeccion__estado=CalidadInspeccion.ESTADO_ANULADA)
        .annotate(oc_ref=F("recepcion_detalle__orden_compra_detalle__orden_compra_id"))
    )
    return qs.annotate(
        progreso_recibido=_suma_por_oc(recibido, "cantidad_recibida") - _suma_por_oc(rechazado, "cantidad_rechazada"),
    )


def _tipo_compra_param(request):
    """``?tipo_compra=`` de los listados: MP (serie OC) / PT (serie O); otro valor → 400."""
    tipo_compra = (request.query_params.get("tipo_compra") or "").strip().upper()
    if tipo_compra and tipo_compra not in OrdenCompra.TipoCompra.values:
        raise ValidationError({"tipo_compra": "Valor inválido. Usa MP (OC) o PT (O)."})
    return tipo_compra


# Filtros de servidor de los listados de OC y recepciones (#973): los mismos que
# leen ``_tipo_compra_param`` y ``filtrar_listado_compras``.
PARAMETROS_LISTADO_COMPRAS = [
    OpenApiParameter("tipo_compra", OpenApiTypes.STR, enum=["MP", "PT"], description="MP (serie OC) o PT (serie O)."),
    OpenApiParameter("estatus", OpenApiTypes.STR, description="Uno o varios estatus separados por coma."),
    OpenApiParameter("proveedor", OpenApiTypes.INT, description="Id del proveedor."),
    OpenApiParameter("sucursal", OpenApiTypes.INT, description="Id de la sucursal."),
    OpenApiParameter("fecha_desde", OpenApiTypes.DATE, description="Desde (YYYY-MM-DD), inclusivo."),
    OpenApiParameter("fecha_hasta", OpenApiTypes.DATE, description="Hasta (YYYY-MM-DD), inclusivo."),
]
PARAMETROS_LISTADO_OC = PARAMETROS_LISTADO_COMPRAS + [
    OpenApiParameter("moneda", OpenApiTypes.INT, description="Id de la moneda."),
    OpenApiParameter("con_proveedor", OpenApiTypes.BOOL, description="true: solo OC con proveedor; false: sin proveedor."),
]
PARAMETROS_LISTADO_RECEPCIONES = PARAMETROS_LISTADO_COMPRAS + [
    OpenApiParameter("almacen", OpenApiTypes.INT, description="Id del almacén destino."),
    OpenApiParameter("orden_compra", OpenApiTypes.INT, description="Id de la OC de origen."),
]
# Listado de encuadres RFID (EC-981): ``estatus`` es texto, no el entero de OC/recepciones.
PARAMETROS_LISTADO_ENCUADRES_RFID = [
    OpenApiParameter("estatus", OpenApiTypes.STR,
                     description="PENDIENTE, ACEPTADO o CANCELADO; uno o varios separados por coma."),
    OpenApiParameter("tipo_origen", OpenApiTypes.STR, enum=["OC", "OP"], description="OC u OP."),
    *(p for p in PARAMETROS_LISTADO_RECEPCIONES if p.name in {"proveedor", "sucursal", "almacen", "orden_compra"}),
    OpenApiParameter("op", OpenApiTypes.INT, description="Id de la OP de origen."),
    *(p for p in PARAMETROS_LISTADO_COMPRAS if p.name in {"fecha_desde", "fecha_hasta"}),
]
PARAMETROS_KPIS_RECEPCIONES = [
    p for p in PARAMETROS_LISTADO_COMPRAS if p.name in {"sucursal", "proveedor", "fecha_desde", "fecha_hasta"}
]


def _exigir_permiso_oc(request, clave):
    """403 sin ``clave``. Superusuario y admin de empresa pasan sin consultas."""
    if clave not in permisos_de_la_peticion(request):
        raise PermissionDenied(f"No tienes permiso para esta acción ({clave}).")


def _exigir_autorizar_oc(request):
    """403 sin ``A-COMPRAS-OC`` (#1320). Va después de ``get_object``: la OC de otra
    empresa sigue siendo 404."""
    _exigir_permiso_oc(request, PERMISO_AUTORIZAR_OC)


def _avisar_oc_autorizada_a_almacen(oc):
    """Aviso a Almacén (departamento WMS) de una OC recién autorizada: es quien
    recibirá la mercancía (#1317). Al confirmar la transacción de ``aceptar``."""
    from notificaciones.services.notificacion_service import crear_notificacion_por_rol
    from seguridad.role_identity import CLAVE_DEPARTAMENTO_WMS

    proveedor = getattr(oc.proveedor, "nombre", None) if oc.proveedor_id else None
    entrega = oc.fecha_entrega_estimada.isoformat() if oc.fecha_entrega_estimada else None
    crear_notificacion_por_rol(
        empresa=oc.empresa,
        codigo_rol="almacen-mp",
        titulo=f"OC autorizada por recibir: {oc.folio}",
        mensaje=f"La OC {oc.folio}" + (f" con {proveedor}" if proveedor else "") + " se autorizó"
        + (f"; entrega estimada {entrega}." if entrega else "."),
        modulo="compras",
        tipo="oc_autorizada",
        data={
            "orden_compra_id": oc.pk, "folio": oc.folio, "proveedor_nombre": proveedor,
            "sucursal_id": oc.sucursal_id, "fecha_entrega_estimada": entrega,
            "url": f"/compras/ordenes/{oc.pk}",
        },
        clave_departamento=CLAVE_DEPARTAMENTO_WMS,
    )


def _liberar_saldo_necesidades(oc):
    """Necesidades de la OC sin recibir completas: se quedan con lo recibido
    (RECIBIDA) y lo que faltaba nace como necesidad Pendiente sin OC, si su
    pre-orden sigue abierta. Regresa los ids nuevos; recalcula al confirmar."""
    from compras.models import NecesidadCompra as N
    from produccion.services.pre_orden_service import CANCELABLES, recalcular_ids

    nuevas, afectadas = [], set()
    for n in N.objects.select_for_update().filter(orden_compra=oc, estatus=N.Estatus.EN_OC).select_related("pre_orden"):
        resto = (n.cantidad or 0) - (n.cantidad_recibida or 0) - (n.sobrante or 0)
        n.cantidad = n.cantidad_recibida or 0
        n.estatus = N.Estatus.RECIBIDA
        n.save(update_fields=["cantidad", "estatus", "updated_at"])
        afectadas.add(n.pre_orden_id)
        if resto > 0 and n.pre_orden.estatus in CANCELABLES:
            nuevas.append(N.objects.create(
                empresa_id=n.empresa_id, sucursal_id=n.sucursal_id, producto_id=n.producto_id,
                unidad_id=n.unidad_id, cantidad=resto, pre_orden_id=n.pre_orden_id, pedido_id=n.pedido_id,
                producto_variante_id=n.producto_variante_id, pre_orden_detalle_id=n.pre_orden_detalle_id,
                fecha_requerida=n.fecha_requerida, estatus=N.Estatus.PENDIENTE,
            ).pk)
    if afectadas:
        transaction.on_commit(lambda ids=afectadas: recalcular_ids(ids))
    return nuevas


class OrdenCompraViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = OrdenCompra.objects.filter(activo=True)
    serializer_class = OrdenCompraSerializer
    filter_backends = [
        *[b for b in viewsets.ReadOnlyModelViewSet.filter_backends if b is not OrderingFilter],
        OrdenamientoSinMontosFilter,
    ]
    # ``?search=`` se publicaba en el schema y se ignoraba sin esto (#839).
    search_fields = ["folio", "referencia", "proveedor__nombre", "proveedor__razon_social"]
    pagination_class = PaginacionOpcionalLimitOffset  # sin ?limit= regresa la lista completa
    # Lista blanca de ``?ordering=`` (#863); los importes los quita
    # ``OrdenamientoSinMontosFilter`` a quien no puede verlos.
    ordering_fields = [
        "id", "folio", "referencia", "tipo_compra", "tipo", "fecha_oc", "fecha_entrega_estimada",
        "fecha_autorizacion", "fecha_vencimiento", "estatus", "proveedor__nombre", "sucursal__nombre",
        "moneda__codigo_iso", "pedido__folio", "total_piezas", *CAMPOS_CONTABILIDAD_OC,
        "created_at", "updated_at",
    ]

    # Máquina de estados de la OC, en un solo lugar. La recepción usa
    # ``RecepcionViewSet.ESTATUS_OC_RECIBIBLES``.
    ESTATUS_EDITABLES_ONBOARDING = {
        OrdenCompra.EstatusOrdenCompra.BORRADOR,
        OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR,
    }
    ESTATUS_ACEPTABLES = {
        OrdenCompra.EstatusOrdenCompra.BORRADOR,
        OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR,
    }
    # ``update`` (PUT) edita cualquier estatus salvo estos y CANCELADA.
    ESTATUS_CON_RECEPCIONES = {
        OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
        OrdenCompra.EstatusOrdenCompra.RECIBIDA,
    }
    ESTATUS_CANCELABLES = {
        OrdenCompra.EstatusOrdenCompra.BORRADOR,
        OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR,
        OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
    }
    ESTATUS_ELIMINABLES = {
        OrdenCompra.EstatusOrdenCompra.BORRADOR,
        OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR,
    }
    # Variantes que se compran en una OC de PT: las que ofrece el GET de
    # onboarding y las que acepta la escritura de renglones (#792).
    VARIANTE_COMPRABLE = Q(activo=True, producto__activo=True, producto__tipo__codigo=CODIGO_PRODUCTO_TERMINADO)

    def get_queryset(self):
        user = self.request.user
        # ``select_related`` cubre todas las FKs cuyo nombre legible resuelve
        # ``OrdenCompraSerializer`` (empresa_nombre, sucursal_nombre,
        # moneda_codigo/simbolo, usuario_nombre, pedido_folio). Aplica también al
        # list porque esos campos viven en el serializer padre.
        qs = super().get_queryset().select_related(
            'proveedor',
            'pedido',
            'solicitud_compra',
            'empresa',
            'sucursal',
            'almacen',
            'moneda',
            'usuario',
            'cerrada_por',  # cerrada_por_nombre (#1373)
        )
        empresa = getattr(user, "empresa", None)
        if empresa:
            qs = qs.filter(empresa=empresa)
        else:
            return qs.none()
        qs = con_pre_ordenes(qs)
        if self.action == 'retrieve':
            qs = self._con_prefetch_retrieve(qs)
        elif self.action not in ('cancelar', 'aceptar'):
            # list y las acciones de listado (historial, etc.) serializan ``pedidos``.
            qs = qs.prefetch_related('pedidos')
        if self.action in ("list", "retrieve"):
            qs = anotar_progreso_oc(qs)
        if self.action == "list":
            # Filtros de servidor (#863, #889), después del alcance por empresa.
            tipo_compra = _tipo_compra_param(self.request)
            if tipo_compra:
                qs = qs.filter(tipo_compra=tipo_compra)
            qs = filtrar_listado_compras(
                self.request, qs, OrdenCompra.EstatusOrdenCompra.values, "fecha_oc",
            )
            # ``?moneda=`` (#974): igual que el listado de pedidos.
            moneda = entero_param(self.request, "moneda")
            if moneda is not None:
                qs = qs.filter(moneda_id=moneda)
            # ``?con_proveedor=`` (EC-782): el selector de OC de facturas de proveedor
            # sólo ofrece OC con proveedor (el FK es ``SET_NULL``); sin el filtro
            # tenía que bajar la lista completa para descartarlas en memoria.
            con_proveedor = booleano_param(self.request, "con_proveedor", None)
            if con_proveedor is not None:
                qs = qs.filter(proveedor__isnull=not con_proveedor)
        # Listado más reciente primero; ``-id`` como desempate estable. Es el
        # orden de las órdenes de compra en sí: el ``order_by`` de arriba es del
        # Prefetch de recepciones anidadas y solo aplica en ``retrieve``.
        return qs.order_by("-fecha_oc", "-id")

    def _con_prefetch_retrieve(self, qs):
        # Prefetch de la forma del retrieve. Lo usa también la respuesta de
        # ``cancelar``, que devuelve esa misma forma (el de ``recepcion_set``
        # acota a activas).
        recepciones_qs = (
            Recepcion.objects.filter(
                activo=True,
                tipo_origen=Recepcion.TipoOrigen.ORDEN_COMPRA,
            )
            .select_related("sucursal", "proveedor", "almacen", "transportista")
            .prefetch_related(
                Prefetch(
                    "recepciondetalle_set",
                    queryset=RecepcionDetalle.objects.select_related(
                        "producto",
                        # La talla de ``producto_variante_talla`` va en el mismo JOIN.
                        "producto_variante__talla",
                        "variante_produccion__talla",  # renglón de muestra
                        "ubicacion",
                        "ubicacion__almacen",
                        "lote",  # lote_codigo / numero_serie (#1122)
                        "serie",
                    ).order_by("id"),
                ),
                Prefetch(
                    # ``MovimientoInventario.recepcion`` no declara
                    # ``related_name``: el accessor inverso real es
                    # ``movimientoinventario_set`` (sin guiones bajos).
                    "movimientoinventario_set",
                    # ``recepcion`` es obligatorio en el ``only``: es la
                    # columna con la que el prefetch agrupa las filas por
                    # recepción. Diferirla cuesta un SELECT por movimiento.
                    queryset=MovimientoInventario.objects.filter(activo=True).only(
                        "pk", "fecha_movimiento", "recepcion"
                    ),
                ),
            )
            .order_by("-fecha_recepcion", "-id")
        )
        prefetch_list = [
            # ``select_related("producto")`` evita el N+1 que provocaría
            # ``producto_nombre`` en cada renglón de ``detalles``.
            # ``order_by("id")`` (igual que el Prefetch de
            # ``recepciondetalle_set``) da un orden estable: el modelo no
            # define ``Meta.ordering`` y ``update``/``onboarding`` borran y
            # recrean los renglones, así que sin esto dos GET idénticos
            # pueden devolverlos en distinto orden.
            Prefetch(
                'ordencompradetalle_set',
                queryset=OrdenCompraDetalle.objects.select_related(
                    'producto', 'producto_variante__color', 'producto_variante__talla'
                ).order_by('id'),
            ),
            Prefetch("recepcion_set", queryset=recepciones_qs),
            "facturas_proveedores",
            "pedidos",
        ]
        return qs.prefetch_related(*prefetch_list)
    def get_serializer_class(self):
        if self.action in ('retrieve', 'cancelar'):
            return OrdenCompraRetrieveSerializer
        return OrdenCompraSerializer

    def ver_montos(self):
        # Una sola evaluación por petición: ``puede_ver_contabilidad`` consulta
        # los roles del usuario y antes se repetía por cada fila (#836).
        if not hasattr(self, "_ver_montos"):
            self._ver_montos = puede_ver_contabilidad(self.request.user)
        return self._ver_montos

    @extend_schema(parameters=PARAMETROS_LISTADO_OC)
    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        if self.ver_montos():
            return response
        paginado = isinstance(response.data, dict) and "results" in response.data
        for fila in response.data["results"] if paginado else response.data:
            quitar_montos_orden_compra(fila)
        return response

    def retrieve(self, request, *args, **kwargs):
        from compras.services.orden_compra_view_service import filtrar_campos_contabilidad_orden_compra
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = filtrar_campos_contabilidad_orden_compra(serializer.data, request.user)
        return Response(data)

    def _auditar_orden_compra(self, request, oc, accion, antes, despues):
        # Mismo registro que deja la recepción (``RecepcionViewSet.onboarding``);
        # el quién y el cuándo de una cancelación/baja viven aquí, no en la OC.
        user = request.user
        return AuditoriaEvento.objects.create(
            empresa_id=oc.empresa_id,
            usuario=user if getattr(user, "pk", None) else None,
            modulo="compras",
            accion=accion,
            tabla=OrdenCompra._meta.db_table,
            id_registro=str(oc.pk),
            antes_json=antes,
            despues_json=despues,
            ip=_ip_auditoria(request),
            user_agent=request.META.get("HTTP_USER_AGENT"),
        )

    @action(detail=True, methods=["post"], url_path="cancelar")
    def cancelar(self, request, pk=None):
        """Anula ante el proveedor una OC que aún no tiene nada recibido ni facturado.

        A diferencia del DELETE (baja por error de captura), la OC cancelada se
        queda visible (``activo`` sigue en True) y CANCELADA es terminal.
        """
        from compras.services.orden_compra_view_service import filtrar_campos_contabilidad_orden_compra

        # ``get_object`` acota por ``user.empresa``: la OC de otra empresa es 404.
        oc = self.get_object()
        _exigir_autorizar_oc(request)
        motivo = request.data.get("motivo_cancelacion")
        if not isinstance(motivo, str) or not motivo.strip():
            raise ValidationError({"motivo_cancelacion": "El motivo de cancelación es requerido."})
        motivo = motivo.strip()

        with transaction.atomic():
            # Todo se decide con la fila bloqueada: una recepción concurrente
            # toma este mismo lock (``RecepcionViewSet.onboarding``), así que o
            # la recepción ya existe al leerla aquí, o espera y ve CANCELADA.
            oc = OrdenCompra.objects.select_for_update().filter(pk=oc.pk, activo=True).first()
            if oc is None:
                raise NotFound("Orden de compra no encontrada.")
            if oc.estatus not in self.ESTATUS_CANCELABLES:
                raise ValidationError({"estatus": "La orden ya no puede cancelarse."})
            # Se revisan los registros reales, no solo el estatus: una recepción o
            # factura viva bloquea aunque el estatus diga que no hay nada recibido.
            # "Activa" es el mismo criterio que ``_cantidad_recibida_oc``.
            recepciones_activas = Recepcion.objects.filter(orden_compra=oc, activo=True).exclude(
                estatus=Recepcion.EstatusRecepcion.CANCELADA
            )
            if recepciones_activas.exists():
                raise ValidationError(
                    {"recepciones": "La orden tiene recepciones registradas y no puede cancelarse."}
                )
            # Cuenta cualquier factura no cancelada, dada de baja (``activo``) o
            # no: la baja no cancela la CxP que genera una factura Registrada.
            facturas_vivas = FacturaProveedor.objects.filter(oc=oc).exclude(
                estatus=FacturaProveedor.FacturaProveedorStatus.CANCELADA
            )
            if facturas_vivas.exists():
                raise ValidationError(
                    {"facturas_proveedores": "La orden tiene facturas de proveedor sin cancelar y no puede cancelarse."}
                )

            antes = {"estatus": oc.estatus, "motivo_cancelacion": oc.motivo_cancelacion}
            oc.estatus = OrdenCompra.EstatusOrdenCompra.CANCELADA
            oc.motivo_cancelacion = motivo
            oc.save(update_fields=["estatus", "motivo_cancelacion", "updated_at"])
            # Necesidades de pre-órdenes: vuelven a Pendiente o se cancelan (no quedan huérfanas).
            from produccion.services.pre_orden_service import liberar_necesidades_oc
            liberar_necesidades_oc(oc.pk)
            self._auditar_orden_compra(
                request,
                oc,
                "CANCELAR",
                antes,
                {"estatus": oc.estatus, "motivo_cancelacion": oc.motivo_cancelacion},
            )

        # El ``get_object`` de arriba va sin prefetch (solo acota empresa/404);
        # la forma del retrieve se carga una sola vez, ya con la OC cancelada.
        instance = self._con_prefetch_retrieve(self.get_queryset()).get(pk=oc.pk)
        data = filtrar_campos_contabilidad_orden_compra(self.get_serializer(instance).data, request.user)
        return Response(data, status=status.HTTP_200_OK)

    # tipo_documento de ``SerieFolio`` por tipo de compra (serie OC para MP, O para PT).
    # Se prueban el código y el nombre con que las series están dadas de alta en la
    # BD ("Orden de Compra", "Orden de Compra PT"); sin el segundo nunca se
    # encontraban y toda OC caía al folio de respaldo (#502).
    TIPO_DOCUMENTO_FOLIO = {
        OrdenCompra.TipoCompra.MATERIA_PRIMA: (("ORDEN_COMPRA", "Orden de Compra"), "OC"),
        OrdenCompra.TipoCompra.PRODUCTO_TERMINADO: (("ORDEN_COMPRA_PT", "Orden de Compra PT"), "O"),
    }

    @action(detail=True, methods=["post"], url_path="cerrar-saldo")
    def cerrar_saldo(self, request, pk=None):
        """Da por terminada una OC parcialmente recibida: el proveedor ya no surtirá
        el resto. Queda RECIBIDA con ``saldo_cerrado``; lo que faltaba de sus
        necesidades de pre-orden vuelve a Pendiente (otra OC puede cubrirlo).
        ``A-COMPRAS-OC`` y ``motivo``. No se reabre."""
        from compras.services.orden_compra_view_service import filtrar_campos_contabilidad_orden_compra

        oc = self.get_object()
        _exigir_autorizar_oc(request)
        motivo = request.data.get("motivo") if hasattr(request.data, "get") else None
        if not isinstance(motivo, str) or not motivo.strip():
            raise ValidationError({"motivo": "Indica por qué se cierra el saldo."})
        motivo = exigir_largo_motivo(motivo.strip())
        with transaction.atomic():
            oc = OrdenCompra.objects.select_for_update().filter(pk=oc.pk, activo=True).first()
            if oc is None:
                raise NotFound("Orden de compra no encontrada.")
            if oc.estatus != OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA:
                raise ValidationError({"estatus": "Solo se cierra el saldo de una OC parcialmente recibida."})
            if Recepcion.objects.filter(
                orden_compra=oc, activo=True, estatus=Recepcion.EstatusRecepcion.EN_CALIDAD
            ).exists():
                raise ValidationError({"recepciones": (
                    "La OC tiene recepciones esperando calidad: inspecciónalas o cancélalas antes de cerrar el saldo."
                )})
            antes = {"estatus": oc.estatus, "saldo_cerrado": False}
            oc.saldo_cerrado, oc.motivo_cierre = True, motivo
            oc.fecha_cierre = timezone.now()
            oc.cerrada_por = request.user if getattr(request.user, "pk", None) else None
            oc.estatus = OrdenCompra.EstatusOrdenCompra.RECIBIDA
            oc._change_reason = motivo[:100]
            oc._bitacora_omitir = True  # el renglón "Saldo cerrado" de abajo es el único (#1371)
            oc.save(update_fields=[
                "saldo_cerrado", "motivo_cierre", "fecha_cierre", "cerrada_por", "estatus", "updated_at",
            ])
            liberadas = _liberar_saldo_necesidades(oc)
            from auditoria.models import BitacoraDocumento
            BitacoraDocumento.objects.create(
                empresa_id=oc.empresa_id, sucursal_id=oc.sucursal_id, tipo_documento="orden_compra",
                documento_id=str(oc.pk), folio=(oc.folio or "")[:60], accion=BitacoraDocumento.Accion.ESTATUS,
                estatus_anterior="Parcialmente recibida", estatus_nuevo="Saldo cerrado",
                estatus_codigo="SALDO_CERRADO", total=oc.gran_total, moneda_id=oc.moneda_id,
                usuario=oc.cerrada_por,
            )
            self._auditar_orden_compra(request, oc, "CERRAR_SALDO", antes, {
                "estatus": oc.estatus, "saldo_cerrado": True, "motivo": motivo,
                "necesidades_reabiertas": liberadas,
            })
        instance = self._con_prefetch_retrieve(self.get_queryset()).get(pk=oc.pk)
        data = filtrar_campos_contabilidad_orden_compra(self.get_serializer(instance).data, request.user)
        return Response(data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get", "patch"], url_path="ruta-critica")
    def ruta_critica(self, request, pk=None):
        """Fecha real de recibo y comentario de compras. Solo informativo: no
        guarda la OC ni toca recepciones/WMS. Cada cambio queda en auditoría."""
        empresa = getattr(request.user, "empresa", None)
        oc = (
            OrdenCompra.objects.filter(pk=pk, empresa=empresa, activo=True).only("id", "folio", "empresa").first()
            if empresa is not None and str(pk).isdigit() else None
        )
        if oc is None:
            raise NotFound("Orden de compra no encontrada.")
        if request.method == "PATCH" and not request.user.tiene_permiso(PERMISO_EDITAR_RUTA_CRITICA_OC):
            raise PermissionDenied("No tienes permiso para editar la ruta crítica de la orden de compra.")
        if request.method == "GET":
            ruta = OrdenCompraRutaCritica.objects.filter(oc=oc).first() or OrdenCompraRutaCritica(oc=oc)
            return Response(OrdenCompraRutaCriticaSerializer(ruta).data)

        # La PK de la ruta es la OC: sin registro previo, dos primeros PATCH
        # simultáneos harían INSERT con la misma llave (500). Se serializa
        # bloqueando la fila de la OC y leyendo la ruta ya dentro del bloqueo (#527).
        with transaction.atomic():
            OrdenCompra.objects.select_for_update().only("id").get(pk=oc.pk)
            ruta = OrdenCompraRutaCritica.objects.filter(oc=oc).first() or OrdenCompraRutaCritica(oc=oc)
            serializer = OrdenCompraRutaCriticaSerializer(ruta, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            cambios = {
                c: v for c, v in serializer.validated_data.items() if getattr(ruta, c) != v
            }
            if cambios:
                antes = {c: getattr(ruta, c) for c in cambios}
                for campo, valor in cambios.items():
                    setattr(ruta, campo, valor)
                ruta.save()
                self._auditar_orden_compra(
                    request, oc, "RUTA_CRITICA",
                    {k: str(v) if v is not None else None for k, v in antes.items()},
                    {k: str(v) if v is not None else None for k, v in cambios.items()},
                )
        return Response(OrdenCompraRutaCriticaSerializer(ruta).data)

    def _asignar_folio_oc(self, instance, empresa):
        """Pone ``instance.folio`` sin guardar: quien llama lo incluye en su ``save``.

        Corre dentro de la transacción de ``aceptar``, así que la serie se lee con
        ``select_for_update`` y dos aceptaciones simultáneas no repiten consecutivo.
        """
        tipo_documento, prefijo_respaldo = self.TIPO_DOCUMENTO_FOLIO.get(
            instance.tipo_compra, self.TIPO_DOCUMENTO_FOLIO[OrdenCompra.TipoCompra.MATERIA_PRIMA]
        )
        serie_folio = SerieFolio.resolve(empresa.pk, instance.sucursal_id, tipo_documento, lock=True)

        # Sin respaldo silencioso (#1142): antes armaba ``OC-{empresa}-{id}``, otro
        # formato sin aviso. Sin serie, o con la serie agotada, la aceptación falla.
        nombre_doc = tipo_documento[-1] if isinstance(tipo_documento, (list, tuple)) else tipo_documento
        if serie_folio is None:
            raise ValidationError({"folio": [
                f"La sucursal no tiene serie de folios activa para {nombre_doc}. Configúrala en Series y folios."]})
        try:
            folio_formateado, nuevo_consecutivo, anio_actual = serie_folio.get_siguiente_folio()
        except ValueError as exc:
            raise ValidationError({"folio": [f"Serie {serie_folio.serie}: {exc}."]})
        serie_folio.folio_actual = nuevo_consecutivo
        serie_folio.ultimo_anio = anio_actual
        serie_folio.save(update_fields=["folio_actual", "ultimo_anio"])
        instance.folio = folio_formateado

    @staticmethod
    def _sucursales_del_usuario(user, empresa):
        """Sucursales activas de ``empresa`` que el usuario puede usar: todas para
        superusuario/admin de empresa; si no, las asignadas (#720)."""
        qs = Sucursal.objects.filter(empresa=empresa, activo=True)
        if user.is_superuser or getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(pk__in=user.sucursales_permitidas())

    def _get_default_sucursal(self, user, empresa):
        permitidas = self._sucursales_del_usuario(user, empresa)
        return permitidas.filter(pk=user.sucursal_default_id).first() or permitidas.order_by("pk").first()  # #1050: la más antigua, no por código

    def _get_default_moneda(self, empresa):
        moneda = getattr(empresa, "moneda_base", None)
        if moneda:
            return moneda
        m = Moneda.objects.filter(empresa__isnull=True, codigo_iso="MXN", activo=True).first()
        if m:
            return m
        return Moneda.objects.filter(empresa__isnull=True, activo=True).order_by("codigo_iso").first()

    def _validar_almacen_oc(self, empresa, almacen_id, sucursal_id, actual_id=None):
        """Almacén destino de la OC (EC-797): devuelve el id a guardar (``None`` limpia).

        - ``null``/vacío limpia; cualquier otro valor debe ser un id existente (``0``
          o basura → 400, nunca un IntegrityError).
        - De la empresa y de la sucursal de la OC, con la MISMA regla que la recepción
          (``RecepcionViewSet.onboarding``): un almacén sin sucursal no sirve.
        - Activo sólo si cambia: reenviar el que ya tiene, aunque se haya desactivado,
          no bloquea editar el resto de la OC.
        """
        if almacen_id is None or almacen_id == "":
            return None
        try:
            almacen_id = int(almacen_id)
        except (TypeError, ValueError):
            almacen_id = 0
        almacen = (
            Almacen.objects.filter(pk=almacen_id, empresa_id=getattr(empresa, "pk", None))
            .only("id_almacen", "sucursal_id", "estatus").first() if almacen_id > 0 else None
        )
        if almacen is None:
            raise ValidationError({"almacen": "El almacén no existe o no pertenece a la empresa de la orden."})
        if almacen.pk != actual_id and almacen.estatus != "ACTIVO":
            raise ValidationError({"almacen": "El almacén está inactivo."})
        if almacen.sucursal_id is None or str(almacen.sucursal_id) != str(sucursal_id):
            raise ValidationError({"almacen": "El almacén no pertenece a la sucursal de la orden."})
        return almacen.pk

    @staticmethod
    def _almacen_de_otra_sucursal(oc, sucursal_id):
        """La OC cambia de sucursal sin mandar ``almacen``: el suyo ya no aplica (EC-797)."""
        if not oc.almacen_id:
            return False
        suc_almacen = Almacen.objects.filter(pk=oc.almacen_id).values_list("sucursal_id", flat=True).first()
        return str(suc_almacen) != str(sucursal_id)

    @staticmethod
    def _aplicar_tipo_cambio(oc, header):
        """``tipo_cambio`` del encabezado (#809): en MXN no aplica y queda ``null``;
        en otra moneda es obligatorio (400 si falta), porque convierte el precio de la
        OC al costo en pesos del inventario que se recibe."""
        if "tipo_cambio" in header:
            oc.tipo_cambio = header.get("tipo_cambio")
        codigo = Moneda.objects.filter(pk=oc.moneda_id).values_list("codigo_iso", flat=True).first() if oc.moneda_id else None
        if (codigo or "MXN").upper() == "MXN":
            oc.tipo_cambio = None
        elif not oc.tipo_cambio:
            raise ValidationError({"tipo_cambio": f"La orden está en {codigo}: captura el tipo de cambio (pesos por {codigo})."})

    def _validar_encabezado_empresa(self, empresa, sucursal_id=None, proveedor_id=None, moneda_id=None):
        # Aislamiento multi-tenant, para TODOS (superusuario incluido): lo que
        # escribe la OC debe ser de la empresa de la orden. Solo se validan los
        # valores que se van a escribir. ``moneda`` es un catálogo híbrido: vale
        # una global (sin empresa) o una privada de la misma empresa, igual que
        # el catálogo que ofrece ``handle_get_onboarding``.
        empresa_id = getattr(empresa, "pk", None)
        if sucursal_id and (
            empresa_id is None
            or not Sucursal.objects.filter(pk=sucursal_id, empresa_id=empresa_id).exists()
        ):
            raise ValidationError({"sucursal": "La sucursal no pertenece a la empresa de la orden."})
        if proveedor_id and (
            empresa_id is None
            or not Proveedor.objects.filter(pk=proveedor_id, empresa_id=empresa_id).exists()
        ):
            raise ValidationError({"proveedor": "El proveedor no pertenece a la empresa de la orden."})
        if moneda_id and not Moneda.objects.filter(
            Q(empresa__isnull=True) | Q(empresa_id=empresa_id), pk=moneda_id
        ).exists():
            raise ValidationError({"moneda": "La moneda no está disponible para la empresa de la orden."})

    def _validar_renglones_empresa(self, empresa, detalle, tipo_compra=None, oc=None):
        # Mismo invariante para el producto de cada renglón. Se valida el lote
        # completo antes de escribir: el reemplazo de renglones borra los
        # existentes, así que un renglón ajeno no puede llegar a esa escritura.
        # ``tipo_compra`` es el efectivo (body o instancia).
        materia_prima = tipo_compra == OrdenCompra.TipoCompra.MATERIA_PRIMA
        actuales = OrdenCompraDetalle.objects.filter(orden_compra_id=getattr(oc, "pk", None))
        if detalle is None:
            # Cambio de tipo sin renglones en el body: valen los actuales.
            if materia_prima and oc is not None and oc.pk and oc.tipo_compra != tipo_compra and (
                actuales.filter(producto_variante__isnull=False).exists()
            ):
                raise ValidationError(
                    {"tipo_compra": "La orden tiene renglones por SKU; no puede ser de materia prima."}
                )
            return
        if not detalle:
            return
        if materia_prima:
            for idx, it in enumerate(detalle):
                if it.get("producto_variante"):
                    raise ValidationError(
                        {"detalle": f"El renglón #{idx + 1} trae SKU: en una OC de materia prima los renglones van por producto."}
                    )
        elif tipo_compra == OrdenCompra.TipoCompra.PRODUCTO_TERMINADO:
            # PT se compra por SKU (#792). Sin variante solo se conserva un
            # renglón legado: el mismo producto ya iba sin variante en esta OC.
            legados = set(
                actuales.filter(producto_variante__isnull=True).values_list("producto_id", flat=True)
            )
            for idx, it in enumerate(detalle):
                if not it.get("producto_variante") and it.get("producto") not in legados:
                    raise ValidationError(
                        {"detalle": f"El renglón #{idx + 1} no trae SKU: en una OC de producto terminado los renglones van por variante."}
                    )
        empresa_id = getattr(empresa, "pk", None)
        # Un renglón por SKU (``producto_variante``) toma su producto de la
        # variante; si además manda ``producto``, debe ser el mismo. Solo
        # variantes comprables, o las que la orden ya tenía (una baja posterior
        # no impide editarla).
        variantes = dict(
            ProductoVariante.objects.filter(
                pk__in=[it.get("producto_variante") for it in detalle if it.get("producto_variante")],
                empresa_id=empresa_id,
            ).filter(
                self.VARIANTE_COMPRABLE
                | Q(pk__in=actuales.filter(producto_variante__isnull=False).values("producto_variante_id"))
            ).values_list("pk", "producto_id")
        )
        for idx, it in enumerate(detalle):
            if not it.get("producto_variante"):
                continue
            producto_id = variantes.get(it["producto_variante"])
            if producto_id is None or it.get("producto") not in (None, "", producto_id):
                raise ValidationError(
                    {"detalle": f"La variante del renglón #{idx + 1} no es válida para la orden."}
                )
            it["producto"] = producto_id
        propios = set(
            Producto.objects.filter(
                pk__in=[it.get("producto") for it in detalle], empresa_id=empresa_id,
            ).values_list("pk", flat=True)
        ) if empresa_id is not None else set()
        for idx, it in enumerate(detalle):
            if it.get("producto") not in propios:
                raise ValidationError(
                    {"detalle": f"El producto del renglón #{idx + 1} no pertenece a la empresa de la orden."}
                )

    def _armar_renglones(self, oc, detalle):
        """Renglones de la OC (sin guardar) a partir del body, y sus totales."""
        renglones = []
        for it in detalle:
            cantidad = int(it.get("cantidad") or 0)
            precio = it.get("precio")
            precio = Decimal("0") if precio in (None, "") else Decimal(str(precio))
            descuento = Decimal("0")
            renglones.append(
                OrdenCompraDetalle(
                    orden_compra=oc,
                    producto_id=it.get("producto"),
                    producto_variante_id=it.get("producto_variante") or None,
                    solicitud_compra_detalle=None,
                    requisicion_detalle=None,
                    descripcion=(it.get("descripcion") or None),
                    cantidad=cantidad,
                    precio=precio,
                    descuento=descuento,
                    importe=(Decimal(cantidad) * precio) - descuento,
                    sucursal_id=oc.sucursal_id,
                    piezas=cantidad,
                )
            )
        return renglones

    def _aplicar_totales(self, oc: OrdenCompra, renglones):
        """Totales de la OC calculados en memoria (mismas reglas que antes)."""
        subtotal = sum((r.importe for r in renglones), Decimal("0"))
        piezas = sum(int(r.piezas or r.cantidad or 0) for r in renglones)
        porcentaje_iva = Decimal(str(oc.porcentaje_iva or 0))
        total_iva = (subtotal * porcentaje_iva / Decimal("100")).quantize(Decimal("0.01"))
        oc.subtotal = subtotal
        oc.gran_total = subtotal + total_iva
        oc.total = subtotal
        oc.total_piezas = piezas
        oc.impuestos = total_iva
        oc.total_iva = total_iva
        oc.porcentaje_iva = porcentaje_iva

    def _reemplazar_renglones(self, oc: OrdenCompra, renglones):
        OrdenCompraDetalle.objects.filter(orden_compra=oc).delete()
        if renglones:
            # ``bulk_create_with_history`` conserva el historial por renglón.
            bulk_create_with_history(renglones, OrdenCompraDetalle)

    def _validar_pedidos_empresa(self, empresa, header):
        """Ids de pedidos a vincular (``None`` si el body no los toca).

        ``pedidos`` manda sobre ``pedido``. Todos deben ser de la empresa de la orden.
        """
        if "pedidos" in header:
            ids = header.get("pedidos") or []
        elif "pedido" in header:
            ids = [header["pedido"]] if header.get("pedido") else []
        else:
            return None
        ids = list(dict.fromkeys(i for i in ids if i))
        empresa_id = getattr(empresa, "pk", None)
        if ids and (
            empresa_id is None
            or Pedido.objects.filter(pk__in=ids, empresa_id=empresa_id).count() != len(ids)
        ):
            raise ValidationError({"pedidos": "Algún pedido no pertenece a la empresa de la orden."})
        return ids

    def _oc_para_respuesta(self, pk):
        return con_pre_ordenes(
            OrdenCompra.objects.select_related(
                "proveedor", "pedido", "solicitud_compra", "empresa", "sucursal", "almacen", "moneda", "usuario",
                "cerrada_por",
            )
            .prefetch_related("pedidos")
        ).get(pk=pk)

    def _respuesta_oc(self, oc):
        detalles_qs = (
            OrdenCompraDetalle.objects.filter(orden_compra=oc)
            .select_related("producto_variante__color", "producto_variante__talla")
            .order_by("id")
        )
        return Response(
            {
                "orden_compra": OrdenCompraSerializer(self._oc_para_respuesta(oc.pk)).data,
                "detalle": OrdenCompraDetalleSerializer(detalles_qs, many=True).data,
            },
            status=status.HTTP_200_OK,
        )

    def handle_get_onboarding(self, request):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        # Superusuario incluido: mismo alcance que el listado de OC (#834, #862).
        if not empresa:
            raise ValidationError({"empresa": "El usuario no tiene empresa asignada."})

        def _limite(raw, default=None):
            if raw in (None, ""):
                return default
            if raw in ("all", "ALL", "0", "-1"):
                return None
            try:
                valor = int(raw)
            except Exception:
                valor = 20
            return max(1, min(valor, 1000))

        # ``limit``/``producto_q`` aplican a todas las listas, como antes; los
        # parámetros por lista los sustituyen solo en la suya (#795).
        qp = request.query_params
        # Sin ``limit`` se topa a 1000 para no volcar el catálogo entero;
        # ``limit=all`` sigue pidiendo todo de forma explícita (#795).
        limit = _limite(qp.get("limit"), 1000)
        proveedor_limit = _limite(qp.get("proveedor_limit"), limit)
        producto_limit = _limite(qp.get("producto_limit"), limit)
        variante_limit = _limite(qp.get("variante_limit"), limit)

        proveedor_q = (qp.get("proveedor_q") or "").strip()
        producto_q = (qp.get("producto_q") or "").strip()
        variante_q = (qp.get("variante_q") or "").strip() if "variante_q" in qp else producto_q

        proveedores_qs = Proveedor.objects.filter(activo=True, empresa=empresa)
        productos_qs = Producto.objects.filter(activo=True, empresa=empresa)

        if proveedor_q:
            proveedores_qs = proveedores_qs.filter(
                Q(nombre__icontains=proveedor_q)
                | Q(razon_social__icontains=proveedor_q)
                | Q(rfc__icontains=proveedor_q)
                | Q(codigo__icontains=proveedor_q)
            )
        if producto_q:
            productos_qs = productos_qs.filter(
                Q(nombre__icontains=producto_q) | Q(descripcion__icontains=producto_q)
            )

        proveedores_qs = proveedores_qs.order_by("nombre").values(
            "id", "codigo", "nombre", "razon_social", "rfc",
            # Moneda dada de alta en el proveedor: el front la preselecciona en la OC.
            "moneda_id", "moneda__codigo_iso",
        )
        productos_qs = productos_qs.order_by("nombre").values(
            "id", "codigo", "cod_proscai", "nombre", "descripcion", "precio_base"
        )

        # Se pide un elemento de más para saber si la lista quedó recortada sin
        # contar todo el catálogo (``*_truncado``, #838).
        def _recortar(qs, tope):
            if tope is None:
                return list(qs), False
            filas = list(qs[: tope + 1])
            return filas[:tope], len(filas) > tope

        proveedores, proveedores_truncado = _recortar(proveedores_qs, proveedor_limit)
        # Moneda privada de otra empresa: no se ofrece como default (#825).
        disponibles = set(
            Moneda.objects.filter(Q(empresa__isnull=True) | Q(empresa=empresa))
            .filter(pk__in={p["moneda_id"] for p in proveedores})
            .values_list("pk", flat=True)
        )
        for p in proveedores:
            if p["moneda_id"] not in disponibles:
                p["moneda_id"] = p["moneda__codigo_iso"] = None
        productos, productos_truncado = _recortar(productos_qs, producto_limit)
        # OC de Producto Terminado (``?tipo_compra=PT``): se compra por SKU, así
        # que también viajan las variantes, buscables por SKU o nombre.
        variantes = []
        variantes_truncado = False
        if (request.query_params.get("tipo_compra") or "").strip().upper() == OrdenCompra.TipoCompra.PRODUCTO_TERMINADO:
            variantes_qs = ProductoVariante.objects.filter(self.VARIANTE_COMPRABLE, empresa=empresa)
            if variante_q:
                variantes_qs = variantes_qs.filter(
                    Q(sku__icontains=variante_q) | Q(nombre__icontains=variante_q)
                    | Q(producto__nombre__icontains=variante_q)
                )
            variantes_qs = variantes_qs.order_by("sku").values(
                "id", "sku", "producto_id", "producto__nombre", "color__nombre", "talla__nombre", "precio_base",
            )
            filas_variantes, variantes_truncado = _recortar(variantes_qs, variante_limit)
            variantes = [
                {
                    "id": v["id"], "sku": v["sku"], "producto_id": v["producto_id"],
                    "producto_nombre": v["producto__nombre"], "color": v["color__nombre"],
                    "talla": v["talla__nombre"], "precio_base": v["precio_base"],
                }
                for v in filas_variantes
            ]
        sucursales = list(
            self._sucursales_del_usuario(user, empresa)
            .order_by("codigo")
            .values(
                "id_sucursal", "codigo", "nombre", "direccion_linea1", "direccion_linea2",
                "ciudad", "estado", "cp", "pais", "telefono",
            )
        )
        for suc in sucursales:
            suc["direccion"] = direccion_sucursal(SimpleNamespace(**suc))
        almacenes = list(
            Almacen.objects.filter(empresa=empresa, estatus="ACTIVO")
            .order_by("codigo")
            .values("id_almacen", "codigo", "nombre", "sucursal_id")
        )
        monedas = list(
            Moneda.objects.filter(Q(empresa__isnull=True) | Q(empresa=empresa), activo=True)
            .order_by("codigo_iso")
            .values("id", "codigo_iso", "nombre")
        )
        return {
            "user": {"id": getattr(user, "pk", None), "email": getattr(user, "email", None)},
            "empresa_id": getattr(empresa, "pk", None),
            "catalogos": {
                "sucursales": sucursales,
                "almacenes": almacenes,
                "monedas": monedas,
            },
            "busqueda": {
                "proveedores": proveedores,
                "productos": productos,
                "variantes": variantes,
                "proveedores_truncado": proveedores_truncado,
                "productos_truncado": productos_truncado,
                "variantes_truncado": variantes_truncado,
            },
        }

    @action(detail=False, methods=["get", "post"], url_path="onboarding")
    def onboarding(self, request):
        user = request.user
        empresa = getattr(user, "empresa", None)

        if request.method.lower() == "get":
            return Response(self.handle_get_onboarding(request))

        _exigir_permiso_oc(request, PERMISO_CREAR_OC)
        raw = request.data or {}
        serializer = OrdenCompraOnboardingSerializer(data=raw)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        orden_compra_id = data.get("orden_compra_id")
        header = data.get("orden_compra") or {}
        detalle = data.get("detalle")

        # Superusuario incluido, igual que el GET (#875): sin empresa se saltaba
        # la pertenencia de la OC editada.
        if not empresa:
            raise ValidationError({"empresa": "El usuario no tiene empresa asignada."})

        with transaction.atomic():
            if orden_compra_id:
                oc = (
                    OrdenCompra.objects.select_for_update()
                    .filter(pk=orden_compra_id, activo=True)
                    .first()
                )
                if not oc:
                    raise ValidationError({"orden_compra_id": "Orden de compra no encontrada."})
                if oc.empresa_id != empresa.pk:
                    raise ValidationError({"orden_compra_id": "No tienes acceso a esta orden de compra."})
                if oc.estatus not in self.ESTATUS_EDITABLES_ONBOARDING:
                    raise ValidationError({"estatus": "La orden ya no puede editarse."})
            else:
                oc = OrdenCompra()

            has_sucursal = "sucursal" in header
            has_proveedor = "proveedor" in header
            has_moneda = "moneda" in header
            has_fecha_vencimiento = "fecha_vencimiento" in header
            has_porcentaje_iva = "porcentaje_iva" in header

            sucursal_id = header.get("sucursal")
            proveedor_id = header.get("proveedor")
            moneda_id = header.get("moneda")
            fecha_vencimiento = header.get("fecha_vencimiento")
            porcentaje_iva = header.get("porcentaje_iva")

            if not sucursal_id and oc.pk:
                # Editar una OC existente sin ``sucursal``: la suya, no la por defecto
                # del usuario (EC-797).
                sucursal_id = oc.sucursal_id
            if not sucursal_id:
                suc = self._get_default_sucursal(user, empresa)
                if not suc:
                    raise ValidationError({"sucursal": "Sucursal es requerida."})
                sucursal_id = suc.pk

            if not moneda_id and proveedor_id:
                # Sin moneda en el body: la del proveedor, luego la base de la empresa.
                # Solo si la moneda está disponible para la empresa (#825).
                moneda_id = (
                    Proveedor.objects.filter(pk=proveedor_id, empresa=empresa)
                    .filter(Q(moneda__empresa__isnull=True) | Q(moneda__empresa=empresa))
                    .values_list("moneda_id", flat=True).first()
                )
            if not moneda_id:
                m = self._get_default_moneda(empresa)
                if not m:
                    raise ValidationError({"moneda": "Moneda es requerida."})
                moneda_id = m.pk

            # Antes de la primera escritura (``oc.save()``).
            self._validar_encabezado_empresa(
                empresa, sucursal_id=sucursal_id, proveedor_id=proveedor_id, moneda_id=moneda_id,
            )
            # Solo una sucursal asignada al usuario, si la OC estrena o cambia de sucursal (#720).
            if has_sucursal and str(oc.sucursal_id) != str(sucursal_id) and not (
                self._sucursales_del_usuario(user, empresa).filter(pk=sucursal_id).exists()
            ):
                raise ValidationError({"sucursal": "No tienes asignada esta sucursal."})
            has_almacen = "almacen" in header
            if has_almacen:
                almacen_id = self._validar_almacen_oc(
                    empresa, header.get("almacen"), sucursal_id, actual_id=oc.almacen_id if oc.pk else None,
                )
            self._validar_renglones_empresa(empresa, detalle, header.get("tipo_compra") or oc.tipo_compra, oc)
            pedidos_ids = self._validar_pedidos_empresa(empresa, header)

            oc.empresa = empresa
            oc.usuario = user
            if not oc.pk or has_sucursal:
                oc.sucursal_id = sucursal_id
            if has_almacen:
                oc.almacen_id = almacen_id
            elif oc.pk and has_sucursal and self._almacen_de_otra_sucursal(oc, sucursal_id):
                oc.almacen_id = None
            if not oc.pk or has_proveedor:
                oc.proveedor_id = proveedor_id
            # Al editar, ``moneda: null`` conserva la guardada, igual que el PUT (#824).
            if not oc.pk or (has_moneda and header.get("moneda")):
                oc.moneda_id = moneda_id
            if not oc.pk:
                # Fecha de generación (EC-395): la fija el servidor al crear y
                # nunca se vuelve a tocar, ni siquiera en una edición
                # posterior por este mismo endpoint.
                oc.fecha_oc = timezone.localdate()
            if has_fecha_vencimiento:
                oc.fecha_vencimiento = fecha_vencimiento
            if not oc.pk or has_porcentaje_iva:
                oc.porcentaje_iva = Decimal(str(porcentaje_iva or 0))
            if "referencia" in header:
                oc.referencia = header.get("referencia") or None
            if "observaciones" in header:
                oc.observaciones = header.get("observaciones") or None
            self._aplicar_tipo_cambio(oc, header)
            if "tipo_compra" in header:
                # Con folio asignado el tipo no puede cambiar, igual que en el PUT (#504).
                if oc.folio and header["tipo_compra"] != oc.tipo_compra:
                    raise ValidationError(
                        {"tipo_compra": "La orden ya tiene folio y no puede cambiar de tipo de compra."}
                    )
                oc.tipo_compra = header["tipo_compra"]
            if pedidos_ids is not None:
                oc.pedido_id = pedidos_ids[0] if pedidos_ids else None
            if not oc.pk:
                oc.estatus = OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR
            renglones = None
            if detalle is not None:
                renglones = self._armar_renglones(oc, detalle)
                self._aplicar_totales(oc, renglones)
            oc.save()

            if pedidos_ids is not None:
                oc.pedidos.set(pedidos_ids)
            if renglones is not None:
                self._reemplazar_renglones(oc, renglones)

        return self._respuesta_oc(oc)

    @action(detail=False, methods=["post"], url_path="desde-necesidades")
    def desde_necesidades(self, request):
        """Una OC consolidada (Borrador) desde necesidades de compra pendientes:
        ``{proveedor, necesidades: [ids]}``. Un renglón por material, redondeado
        al mínimo/múltiplo de compra del material si lo tiene."""
        from compras.api.necesidades import crear_oc_desde_necesidades
        _exigir_permiso_oc(request, PERMISO_CREAR_OC)
        oc = crear_oc_desde_necesidades(
            request.user, request.data.get("proveedor"), request.data.get("necesidades"),
        )
        return Response(
            {"orden_compra": OrdenCompraSerializer(self._oc_para_respuesta(oc.pk)).data},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="aceptar")
    def aceptar(self, request, pk=None):
        from compras.services.orden_compra_view_service import filtrar_campos_contabilidad_orden_compra

        user = request.user
        empresa = getattr(user, "empresa", None)
        oc = self.get_object()
        # ``get_object`` ya acota por ``user.empresa`` (sin empresa: 404), así que
        # esta guarda no cambia nada; se cierra para que no dependa de ello.
        if empresa is None or oc.empresa_id != empresa.pk:
            raise ValidationError({"orden_compra_id": "No tienes acceso a esta orden de compra."})
        _exigir_autorizar_oc(request)
        body_proveedor_id = request.data.get("proveedor") or request.data.get("proveedor_id")
        try:
            body_proveedor_id = int(body_proveedor_id) if body_proveedor_id not in (None, "") else None
        except Exception:
            body_proveedor_id = None

        with transaction.atomic():
            # Estatus y renglones se validan con la fila bloqueada y releída: leídos
            # antes del lock, una cancelación o edición concurrente pasaba el guard
            # y la aceptación la sobrescribía (p. ej. revivía una OC CANCELADA).
            oc = OrdenCompra.objects.select_for_update().filter(pk=oc.pk, activo=True).first()
            if oc is None:
                raise NotFound("Orden de compra no encontrada.")
            if oc.estatus not in self.ESTATUS_ACEPTABLES:
                raise ValidationError({"estatus": "La orden ya no puede aceptarse."})
            # Antes de la primera escritura (folio y ``oc.save()``).
            self._validar_encabezado_empresa(oc.empresa, proveedor_id=body_proveedor_id)

            if not OrdenCompraDetalle.objects.filter(orden_compra=oc).exists():
                raise ValidationError({"detalle": "Agrega al menos un producto antes de aceptar."})

            if body_proveedor_id:
                oc.proveedor_id = body_proveedor_id
            if not oc.proveedor_id:
                raise ValidationError({"proveedor": "Proveedor es requerido para aceptar."})
            if not oc.folio:
                self._asignar_folio_oc(oc, oc.empresa)
            oc.estatus = OrdenCompra.EstatusOrdenCompra.AUTORIZADA
            oc.fecha_autorizacion = timezone.now()
            oc.save(update_fields=["proveedor", "folio", "estatus", "fecha_autorizacion", "updated_at"])
            if oc.necesidades.exists():
                # Con pre-orden, además avisa a Producción (``AVISOS["oc_autorizada"]``, #1317).
                from produccion.models import PreOrdenEvento
                from produccion.services.pre_orden_service import eventos_oc
                eventos_oc(oc.pk, PreOrdenEvento.Evento.OC_AUTORIZADA, user)
            transaction.on_commit(lambda oc=oc: _avisar_oc_autorizada_a_almacen(oc))

        # Importes por rol, igual que ``retrieve``/``cancelar`` (#1332).
        data = filtrar_campos_contabilidad_orden_compra(
            OrdenCompraSerializer(self._oc_para_respuesta(oc.pk)).data, user
        )
        return Response({"orden_compra": data}, status=status.HTTP_200_OK)

    def update(self, request, pk=None, *args, **kwargs):
        user = request.user
        empresa = getattr(user, "empresa", None)

        _exigir_permiso_oc(request, PERMISO_EDITAR_OC)
        raw = request.data or {}
        serializer = OrdenCompraOnboardingSerializer(data=raw)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        header = data.get("orden_compra")
        if not header:
            # Encabezado plano (sin ``orden_compra``): se valida con el mismo
            # serializer que el anidado, para que un valor inválido dé 400 y no 500.
            plano = OrdenCompraOnboardingHeaderSerializer(data=raw)
            plano.is_valid(raise_exception=True)
            header = plano.validated_data
        detalle = data.get("detalle") or data.get("detalles")

        with transaction.atomic():
            oc = (
                OrdenCompra.objects.select_for_update()
                .filter(pk=pk, empresa=empresa, activo=True)
                .first()
            )
            if not oc:
                return Response(
                    {"detail": "Orden de compra no encontrada."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Una OC autorizada SÍ puede editarse: el único corte real es que ya
            # tenga recepciones (folio de recepción) contra ella, no el que esté
            # autorizada. Editarla la regresa a POR_AUTORIZAR (abajo) para que se
            # vuelva a aceptar. CANCELADA es terminal: editarla la revivía como
            # POR_AUTORIZAR.
            if oc.estatus == OrdenCompra.EstatusOrdenCompra.CANCELADA:
                raise ValidationError(
                    {"estatus": "La orden está cancelada y no puede modificarse."}
                )
            if oc.estatus in self.ESTATUS_CON_RECEPCIONES:
                raise ValidationError(
                    {"estatus": "La orden ya tiene recepciones registradas y no puede modificarse."}
                )
            # Además de lo que diga el estatus (el admin lo puede regresar a <= 3):
            # reemplazar los renglones borra ``OrdenCompraDetalle`` y en cascada los
            # ``RecepcionDetalle``/``FacturaProveedorDetalle`` que cuelgan de ellos,
            # sin importar el estatus de su recepción o factura. Mismo criterio
            # estricto que ``destroy``.
            if Recepcion.objects.filter(orden_compra=oc).exists():
                raise ValidationError(
                    {"recepciones": "La orden tiene recepciones registradas y no puede modificarse."}
                )
            if FacturaProveedor.objects.filter(oc=oc).exists():
                raise ValidationError(
                    {"facturas_proveedores": "La orden tiene facturas de proveedor y no puede modificarse."}
                )

            has_sucursal = "sucursal" in header
            has_proveedor = "proveedor" in header
            has_moneda = "moneda" in header
            has_fecha_vencimiento = "fecha_vencimiento" in header
            has_porcentaje_iva = "porcentaje_iva" in header

            sucursal_id = header.get("sucursal")
            proveedor_id = header.get("proveedor")
            moneda_id = header.get("moneda")
            fecha_vencimiento = header.get("fecha_vencimiento")
            porcentaje_iva = header.get("porcentaje_iva")

            # Antes de la primera escritura (``oc.save()``). La empresa sale de la
            # instancia; lo que no viene en el body no se toca.
            self._validar_encabezado_empresa(
                oc.empresa,
                sucursal_id=sucursal_id if has_sucursal else None,
                proveedor_id=proveedor_id if has_proveedor else None,
                moneda_id=moneda_id if has_moneda else None,
            )
            has_almacen = "almacen" in header
            if has_almacen:
                almacen_id = self._validar_almacen_oc(
                    oc.empresa, header.get("almacen"),
                    sucursal_id if (has_sucursal and sucursal_id) else oc.sucursal_id,
                    actual_id=oc.almacen_id,
                )
            self._validar_renglones_empresa(oc.empresa, detalle, header.get("tipo_compra") or oc.tipo_compra, oc)
            pedidos_ids = self._validar_pedidos_empresa(oc.empresa, header)
            # El folio sale de la serie del tipo de compra: con folio ya asignado
            # el tipo no puede cambiar (quedaría un OC-... en una compra de PT).
            if (
                oc.folio
                and header.get("tipo_compra")
                and header["tipo_compra"] != oc.tipo_compra
            ):
                raise ValidationError(
                    {"tipo_compra": "La orden ya tiene folio y no puede cambiar de tipo de compra."}
                )

            oc.usuario = user
            cambia_sucursal = bool(has_sucursal and sucursal_id and str(sucursal_id) != str(oc.sucursal_id))
            if has_sucursal and sucursal_id:
                oc.sucursal_id = sucursal_id
            if has_almacen:
                oc.almacen_id = almacen_id
            elif cambia_sucursal and self._almacen_de_otra_sucursal(oc, sucursal_id):
                # Igual que el onboarding (EC-797): sin ``almacen`` en el body, el de
                # otra sucursal no se queda.
                oc.almacen_id = None
            if has_proveedor:
                oc.proveedor_id = proveedor_id
            if has_moneda and moneda_id:
                oc.moneda_id = moneda_id
            # ``fecha_oc`` (generación) NO se toca aquí: fija, inmutable
            # después de creada (EC-395).
            if has_fecha_vencimiento:
                oc.fecha_vencimiento = fecha_vencimiento
            if has_porcentaje_iva:
                oc.porcentaje_iva = Decimal(str(porcentaje_iva or 0))
            if "referencia" in header:
                oc.referencia = header.get("referencia") or None
            if "observaciones" in header:
                oc.observaciones = header.get("observaciones") or None
            self._aplicar_tipo_cambio(oc, header)
            if "tipo_compra" in header:
                oc.tipo_compra = header["tipo_compra"]
            if pedidos_ids is not None:
                oc.pedido_id = pedidos_ids[0] if pedidos_ids else None

            oc.estatus = OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR
            renglones = None
            if detalle is not None:
                renglones = self._armar_renglones(oc, detalle)
                self._aplicar_totales(oc, renglones)
            oc.save()

            if pedidos_ids is not None:
                oc.pedidos.set(pedidos_ids)
            if renglones is not None:
                self._reemplazar_renglones(oc, renglones)

        return self._respuesta_oc(oc)

    def destroy(self, request, pk=None, *args, **kwargs):
        user = request.user
        empresa = getattr(user, "empresa", None)

        _exigir_permiso_oc(request, PERMISO_ELIMINAR_OC)
        # Baja por error de captura, no cancelación (esa es ``cancelar``): solo
        # procede sobre una OC que nunca llegó a comprometer nada con el
        # proveedor. Cualquier recepción o factura, aun cancelada, prueba que la
        # orden sí existió y debe quedar visible.
        with transaction.atomic():
            oc = (
                OrdenCompra.objects.select_for_update()
                .filter(pk=pk, empresa=empresa, activo=True)
                .first()
            )
            if not oc:
                return Response(
                    {"detail": "Orden de compra no encontrada."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            if oc.estatus not in self.ESTATUS_ELIMINABLES:
                raise ValidationError(
                    {"estatus": "Solo se puede eliminar una orden en borrador o pendiente de confirmar."}
                )
            if Recepcion.objects.filter(orden_compra=oc).exists():
                raise ValidationError(
                    {"recepciones": "La orden tiene recepciones registradas y no puede eliminarse."}
                )
            if FacturaProveedor.objects.filter(oc=oc).exists():
                raise ValidationError(
                    {"facturas_proveedores": "La orden tiene facturas de proveedor y no puede eliminarse."}
                )

            # Baja lógica a mano en vez de ``soft_delete()``: ese guarda solo
            # ``activo`` y ``auto_now`` no toca ``updated_at`` si no va en
            # ``update_fields``, como sí en las demás escrituras del viewset.
            # Replica a propósito ``StatusLifecycleModel.soft_delete()`` solo para
            # sumar ``updated_at``: si ``soft_delete()`` cambia, mantener esto igual.
            oc.activo = False
            oc.save(update_fields=["activo", "updated_at"])
            self._auditar_orden_compra(
                request,
                oc,
                "DELETE",
                {"estatus": oc.estatus, "activo": True},
                {"estatus": oc.estatus, "activo": False},
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    # ``detail=False`` fuera de ``get_queryset()`` a propósito (EC-432): mismo
    # criterio que los demás ``kpis`` -- todo con ``aggregate()``/``Sum`` en
    # DB, nunca iterando OCs fila por fila.
    ESTATUS_OC_ABIERTAS = {
        OrdenCompra.EstatusOrdenCompra.BORRADOR,
        OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR,
        OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
        OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
    }

    @extend_schema(summary="KPIs del dashboard de Compras", responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=["get"], url_path="kpis")
    def kpis(self, request):
        user = request.user
        # No acepta filtros (#950): un ``?sucursal=`` o una fecha → 400, no 200
        # con los KPIs de toda la empresa como si se hubieran aplicado. Igual
        # que ``recepciones/kpis/`` y antes de la empresa, como allá.
        validar_parametros(request, set())
        empresa = getattr(user, "empresa", None)
        # Superusuario incluido, como recepciones y proveedores (#834, #861).
        if empresa is None:
            return Response(self._kpis_vacio())

        base = OrdenCompra.objects.filter(empresa=empresa, activo=True)
        filas = self._filas_estatus_moneda(base)
        data = {
            "generado_en": timezone.now(),
            "ocs_abiertas": self._kpi_ocs_abiertas(filas),
            "ocs_vencidas_sin_recibir": self._kpi_ocs_vencidas(base),
            "ciclo_compra": {
                "disponible": False,
                "motivo": "El flujo Requisición → Solicitud de compra → OC no se usa hoy (OrdenCompra.solicitud_compra siempre queda null); no hay fecha de requisición contra qué medir el ciclo.",
            },
            "gasto_por_categoria": self._kpi_gasto_por_categoria(base),
            # Dashboard de Compras (EC-782): lo que hoy calcula en memoria sobre el
            # listado completo. Claves nuevas; las de arriba no cambian.
            "ocs_por_grupo": self._kpi_ocs_por_grupo(filas),
            "valor_ocs_activas": self._kpi_valor_ocs_activas(filas),
            "top_ocs_por_moneda": self._kpi_top_ocs_por_moneda(base),
        }
        data["ocs_en_curso_proxima_entrega"] = self._kpi_ocs_en_curso(base, data["ocs_por_grupo"]["en_curso"])
        # Señal explícita para el front (#841): no puede deducirla de las claves
        # de departamento de los roles, que no viajan en la sesión.
        data["puede_ver_importes"] = puede_ver_contabilidad(user)
        if not data["puede_ver_importes"]:
            quitar_montos(data["ocs_abiertas"], ["monto"])
            data["gasto_por_categoria"] = SIN_PERMISO_CONTABILIDAD
            quitar_montos(data["valor_ocs_activas"], ["monto"])
            # El ranking mismo delata cuáles OC valen más: se oculta completo.
            data["top_ocs_por_moneda"] = SIN_PERMISO_CONTABILIDAD
        return Response(data)

    def _kpis_vacio(self):
        motivo = {"disponible": False, "motivo": "Usuario sin empresa asignada."}
        return {
            "generado_en": timezone.now(),
            "ocs_abiertas": motivo,
            "ocs_vencidas_sin_recibir": motivo,
            "ciclo_compra": motivo,
            "gasto_por_categoria": motivo,
            "ocs_por_grupo": motivo,
            "valor_ocs_activas": motivo,
            "top_ocs_por_moneda": motivo,
            "ocs_en_curso_proxima_entrega": motivo,
            # Clave fija del contrato; sin datos no hay importes (#860).
            "puede_ver_importes": False,
        }

    def _filas_estatus_moneda(self, base):
        """``[{estatus, moneda__codigo_iso, total, monto}]`` de todas las OC de
        ``base``: UNA consulta de la que salen ``ocs_abiertas``, ``ocs_por_grupo`` y
        ``valor_ocs_activas`` (EC-782; antes eran cuatro)."""
        return list(
            base.values("estatus", "moneda__codigo_iso")
            .annotate(total=Count("id"), monto=Sum("gran_total"))
            .order_by("estatus", "moneda__codigo_iso")
        )

    @staticmethod
    def _por_moneda(filas):
        """Suma ``total`` y ``monto`` por moneda, en el orden de ``ORDER BY codigo_iso``
        de Postgres (``NULL`` al final). Los montos van por moneda (#374)."""
        por_moneda = {}
        for fila in filas:
            acumulado = por_moneda.setdefault(
                fila["moneda__codigo_iso"],
                {"moneda": fila["moneda__codigo_iso"], "total": 0, "monto": Decimal("0")},
            )
            acumulado["total"] += fila["total"]
            acumulado["monto"] += fila["monto"] or Decimal("0")
        return sorted(por_moneda.values(), key=lambda f: (f["moneda"] is None, f["moneda"] or ""))

    def _kpi_ocs_abiertas(self, filas):
        # Los montos van por moneda (#374): una OC en USD no se suma con las
        # MXN. ``total`` (conteo) sí es de todas; cada fila con ``monto``
        # trae su ``moneda``.
        abiertas = [f for f in filas if f["estatus"] in self.ESTATUS_OC_ABIERTAS]
        estatus_labels = dict(OrdenCompra.EstatusOrdenCompra.choices)
        por_estatus = [
            {
                "estatus": fila["estatus"],
                "estatus_label": estatus_labels.get(fila["estatus"], str(fila["estatus"])),
                "moneda": fila["moneda__codigo_iso"],
                "total": fila["total"],
                "monto": fila["monto"] or Decimal("0"),
            }
            for fila in abiertas
        ]
        por_moneda = self._por_moneda(abiertas)
        return {
            "disponible": True,
            "total": sum(f["total"] for f in por_moneda),
            "por_moneda": por_moneda,
            "por_estatus": por_estatus,
        }

    # Grupos del dashboard de Compras (EC-782), los mismos que pinta el front.
    ESTATUS_OC_POR_AUTORIZAR = (
        OrdenCompra.EstatusOrdenCompra.BORRADOR,
        OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR,
    )
    ESTATUS_OC_AUTORIZADAS_O_COMPLETADAS = (
        OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
        OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
        OrdenCompra.EstatusOrdenCompra.RECIBIDA,
    )
    TOPE_TOP_OCS = 5
    TOPE_OCS_EN_CURSO = 8

    def _kpi_ocs_por_grupo(self, filas):
        """Conteos por grupo de estatus. "Activas" = todo menos canceladas (6);
        "en curso" = parcialmente recibidas (4)."""
        estatus = OrdenCompra.EstatusOrdenCompra

        def contar(condicion):
            return sum(f["total"] for f in filas if condicion(f["estatus"]))

        return {
            "disponible": True,
            "activas": contar(lambda e: e != estatus.CANCELADA),
            "por_autorizar": contar(lambda e: e in self.ESTATUS_OC_POR_AUTORIZAR),
            "autorizadas_o_completadas": contar(lambda e: e in self.ESTATUS_OC_AUTORIZADAS_O_COMPLETADAS),
            "en_curso": contar(lambda e: e == estatus.PARCIALMENTE_RECIBIDA),
            "recibidas": contar(lambda e: e == estatus.RECIBIDA),
            "canceladas": contar(lambda e: e == estatus.CANCELADA),
        }

    def _kpi_valor_ocs_activas(self, filas):
        """Σ ``gran_total`` de las OC activas (≠ 6), por moneda (#374)."""
        activas = [f for f in filas if f["estatus"] != OrdenCompra.EstatusOrdenCompra.CANCELADA]
        por_moneda = self._por_moneda(activas)
        return {"disponible": True, "total": sum(f["total"] for f in por_moneda), "por_moneda": por_moneda}

    def _kpi_top_ocs_por_moneda(self, base):
        """Las ``TOPE_TOP_OCS`` OC activas de mayor ``gran_total`` DENTRO de cada
        moneda, en UNA consulta (``ROW_NUMBER`` por moneda): un importe en USD no se
        compara con uno en MXN."""
        filas = (
            base.exclude(estatus=OrdenCompra.EstatusOrdenCompra.CANCELADA)
            .annotate(
                posicion=Window(
                    RowNumber(),
                    partition_by=[F("moneda_id")],
                    order_by=[F("gran_total").desc(), F("id").desc()],
                )
            )
            .filter(posicion__lte=self.TOPE_TOP_OCS)
            .order_by("moneda__codigo_iso", "posicion")
            .values("id", "folio", "gran_total", "moneda__codigo_iso", "proveedor__nombre")
        )
        monedas = {}
        for fila in filas:
            moneda = fila["moneda__codigo_iso"]
            monedas.setdefault(moneda, []).append({
                "id": fila["id"],
                "folio": fila["folio"],
                "gran_total": fila["gran_total"],
                "moneda_codigo": moneda,
                "proveedor_nombre": fila["proveedor__nombre"],
            })
        return {"disponible": True, "monedas": [{"moneda": m, "ocs": ocs} for m, ocs in monedas.items()]}

    def _kpi_ocs_en_curso(self, base, total):
        """Las ``TOPE_OCS_EN_CURSO`` OC en estatus 4 con la ``fecha_entrega_estimada``
        más próxima; las que no tienen fecha van al final. ``drill_down_total`` y
        ``drill_down_truncado`` con ``marcar_tope``, como los demás drill-downs; el
        total es ``ocs_por_grupo.en_curso`` (``con_tope`` repetiría ese conteo en
        otra consulta)."""
        filas = (
            base.filter(estatus=OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA)
            .order_by(F("fecha_entrega_estimada").asc(nulls_last=True), "id")
            .values("id", "folio", "fecha_entrega_estimada", "proveedor__nombre")[: self.TOPE_OCS_EN_CURSO]
        )
        drill_down = [
            {
                "id": f["id"],
                "folio": f["folio"],
                "proveedor_nombre": f["proveedor__nombre"],
                "fecha_entrega_estimada": f["fecha_entrega_estimada"],
            }
            for f in filas
        ]
        return marcar_tope({"disponible": True, "drill_down": drill_down}, total, drill_down)

    def _kpi_ocs_vencidas(self, base):
        hoy = timezone.localdate()
        vencidas_qs = base.exclude(
            estatus__in=[OrdenCompra.EstatusOrdenCompra.RECIBIDA, OrdenCompra.EstatusOrdenCompra.CANCELADA]
        ).filter(fecha_entrega_estimada__lt=hoy)
        total = vencidas_qs.count()
        drill_down = [
            {
                "oc_id": row["id"],
                "folio": row["folio"],
                "fecha_entrega_estimada": row["fecha_entrega_estimada"],
                "dias_vencida": (hoy - row["fecha_entrega_estimada"]).days,
            }
            for row in vencidas_qs.order_by("fecha_entrega_estimada").values(
                "id", "folio", "fecha_entrega_estimada"
            )[:20]
        ]
        return {"disponible": True, "total": total, "drill_down": drill_down}

    def _kpi_gasto_por_categoria(self, base):
        # Solo OCs comprometidas (no borrador ni cancelada): mismo criterio que
        # ``kpi_criterios.ESTATUS_OC_COMPROMETIDAS`` (recepciones y proveedores).
        estatus_gasto = [
            OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
            OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
            OrdenCompra.EstatusOrdenCompra.RECIBIDA,
        ]
        filas = (
            OrdenCompraDetalle.objects.filter(orden_compra__in=base.filter(estatus__in=estatus_gasto))
            .values("producto__categoria_producto__nombre", "orden_compra__moneda__codigo_iso")
            .annotate(monto=Sum("importe"))
            .order_by("orden_compra__moneda__codigo_iso", "-monto", "producto__categoria_producto__nombre")
        )
        # Una fila por categoría y moneda (#374): nunca se suman monedas.
        return {
            "disponible": True,
            "categorias": [
                {
                    "categoria": fila["producto__categoria_producto__nombre"] or "Sin categoría",
                    "moneda": fila["orden_compra__moneda__codigo_iso"],
                    "monto": fila["monto"] or Decimal("0"),
                }
                for fila in filas
            ],
        }

def _ids_orden_log(data):
    """Solo los ids de la orden para el log; nunca el body completo."""
    cab = data.get("recepcion") if hasattr(data, "get") else None
    if not hasattr(cab, "get"):
        return None
    return {k: cab.get(k) for k in ("orden_compra", "orden_produccion")}


def _anotar_listado_recepciones(qs):
    """Totales e inspección por recepción en la misma consulta (subconsultas por FK
    indexada): ``piezas_recibidas``/``renglones`` y, de su última inspección,
    ``inspeccion_estado`` y ``piezas_aprobadas``/``piezas_rechazadas`` (nulos sin
    inspección)."""
    dec = DecimalField(max_digits=18, decimal_places=4)
    renglones = RecepcionDetalle.objects.filter(recepcion=OuterRef("pk")).order_by().values("recepcion")
    ultima = CalidadInspeccion.objects.filter(recepcion=OuterRef("pk")).exclude(
        estado=CalidadInspeccion.ESTADO_ANULADA
    ).order_by("-id")
    det = (
        CalidadInspeccionDetalle.objects.filter(calidad_inspeccion=OuterRef("inspeccion_id"))
        .order_by().values("calidad_inspeccion")
    )
    return qs.annotate(
        piezas_recibidas=Coalesce(Subquery(renglones.annotate(t=Sum("cantidad_recibida")).values("t")[:1], output_field=dec), Value(0), output_field=dec),
        renglones=Coalesce(Subquery(renglones.annotate(t=Count("id")).values("t")[:1]), Value(0)),
        inspeccion_id=Subquery(ultima.values("pk")[:1]),
        inspeccion_estado=Subquery(ultima.values("estado")[:1]),
    ).annotate(
        piezas_aprobadas=Subquery(det.annotate(t=Sum("cantidad_aprobada")).values("t")[:1], output_field=dec),
        piezas_rechazadas=Subquery(det.annotate(t=Sum("cantidad_rechazada")).values("t")[:1], output_field=dec),
    )


@extend_schema_view(list=extend_schema(parameters=PARAMETROS_LISTADO_RECEPCIONES))
class RecepcionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Recepcion.objects.all().select_related(
        "orden_compra",
        "op",
        "empresa",
        "sucursal",
        "proveedor",
        "almacen",
        "transportista",
        "usuario",
        "cancelado_por",  # ``cancelado_por_nombre`` (EC-926), sin consulta extra
        # SM y su origen (folio OB/OR/OP) en la misma consulta (#1237).
        "salida_maquila__ob",
        "salida_maquila__orden_r",
        "salida_maquila__op",
    )
    serializer_class = RecepcionSerializer
    http_method_names = ["get", "post"]
    # ``?search=`` se publicaba en el schema y se ignoraba sin esto (#839).
    search_fields = ["folio", "orden_compra__folio", "op__folio_op", "proveedor__nombre", "salida_maquila__folio"]
    pagination_class = PaginacionOpcionalLimitOffset  # sin ?limit= regresa la lista completa
    filter_backends = [
        *[b for b in viewsets.ReadOnlyModelViewSet.filter_backends if b is not OrderingFilter],
        OrdenamientoEstableFilter,
    ]
    # Lista blanca de ``?ordering=`` (#863).
    ordering_fields = [
        "id", "folio", "tipo_origen", "fecha_recepcion", "estatus", "remision", "factura_referencia",
        "proveedor__nombre", "almacen__nombre", "orden_compra__folio", "orden_compra__tipo_compra",
        "created_at", "updated_at",
    ]

    # Estatus de OC contra los que se puede recibir (onboarding GET y POST).
    ESTATUS_OC_RECIBIBLES = {
        OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
        OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
    }

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset.filter(activo=True).order_by("-fecha_recepcion", "-id")

        # Solo el retrieve individual anida ``detalles``: se traen los renglones
        # con sus FK en un único prefetch (producto/producto_variante y su
        # talla/ubicacion) para evitar el N+1 que tendría el shape anidado. El
        # list no lo necesita y se mantiene ligero.
        if self.action == "retrieve":
            qs = qs.prefetch_related(
                Prefetch(
                    "recepciondetalle_set",
                    queryset=RecepcionDetalle.objects.select_related(
                        "producto",
                        "producto_variante__talla",
                        "variante_produccion__talla",  # renglón de muestra
                        "ubicacion",
                        "ubicacion__almacen",
                        "lote",  # lote_codigo / numero_serie (#1122)
                        "serie",
                    ).order_by("id"),
                )
            )
            # ``puede_cancelar`` (EC-926): sus bloqueos en la misma consulta. Sin
            # permiso de cancelar ya es ``false`` y no hace falta anotar.
            if puede_cancelar_recepciones(user):
                qs = anotar_bloqueos_cancelacion(qs)

        # Filtro opcional por origen (OC / OP). Sigue la convención del proyecto:
        # query param manual validado en get_queryset (no se usa django-filter).
        # Omitirlo devuelve todas las recepciones, exactamente como antes.
        tipo_origen = (self.request.query_params.get("tipo_origen") or "").strip().upper()
        if tipo_origen:
            validos = {
                Recepcion.TipoOrigen.ORDEN_COMPRA,
                Recepcion.TipoOrigen.ORDEN_PRODUCCION,
                Recepcion.TipoOrigen.SALIDA_MAQUILA,
            }
            if tipo_origen not in validos:
                raise _error_detalle(
                    {"tipo_origen": "Valor inválido. Usa OC (orden de compra), OP (orden de producción) o SM (salida a maquila)."}
                )
            if tipo_origen == Recepcion.TipoOrigen.SALIDA_MAQUILA:
                # Toda recepción de una SM (#1238): las de OB/OR (``tipo_origen=SM``)
                # y las de OP, que se guardan como ``OP`` con ``salida_maquila``.
                qs = qs.filter(Q(tipo_origen=tipo_origen) | Q(salida_maquila__isnull=False))
            else:
                qs = qs.filter(tipo_origen=tipo_origen)

        # Siempre la empresa del usuario, superusuario incluido: mismo alcance
        # que el listado de OC, para que la pantalla de Compras cuadre (#834).
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa)

        # Filtro opcional por tipo de la OC de origen: MP (serie OC) / PT (serie O).
        # Es del listado: no altera el retrieve, y va después del alcance por
        # empresa para que un usuario sin empresa no vea un 400 (#511).
        if self.action == "list":
            tipo_compra = _tipo_compra_param(self.request)
            if tipo_compra:
                qs = qs.filter(orden_compra__tipo_compra=tipo_compra)
            # Almacén destino y OC de origen (#889); un id ajeno no trae filas.
            for nombre in ("almacen", "orden_compra"):
                valor = entero_param(self.request, nombre)
                if valor is not None:
                    qs = qs.filter(**{f"{nombre}_id": valor})
            # Filtros de servidor (#863); la fecha es el día local de la recepción.
            qs = filtrar_listado_compras(
                self.request, qs, Recepcion.EstatusRecepcion.values, "fecha_recepcion__date",
            )
            qs = _anotar_listado_recepciones(qs).select_related(*RELACIONES_PEDIDO)
        return qs

    def list(self, request, *args, **kwargs):
        # ``pedidos`` de toda la página en bloque (``pedidos_de_recepciones``: 2 consultas).
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        filas = [*(page if page is not None else queryset)]
        serializer = self.get_serializer(filas, many=True)
        serializer.context["pedidos_por_recepcion"] = pedidos_de_recepciones(filas)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def get_serializer_class(self):
        # retrieve → forma anidada dedicada (con ``detalles[]``); list conserva la
        # forma enriquecida plana intacta; el resto (p. ej. la respuesta de
        # onboarding, que instancia RecepcionSerializer directamente) conserva la
        # forma plana original.
        if self.action == "retrieve":
            return RecepcionRetrieveSerializer
        if self.action == "list":
            return RecepcionListSerializer
        return RecepcionSerializer

    @extend_schema(summary="Cancelar una recepción antes de calidad", responses=RecepcionSerializer)
    @action(detail=True, methods=["post"], url_path="cancelar")
    def cancelar(self, request, pk=None):
        """Cancela una recepción registrada por error, solo antes de calidad (#1307).

        - Solo admin de la empresa o superusuario, con ``motivo``.
        - Antes de calidad: ``EN_CALIDAD`` sin inspección ni movimiento de
          inventario; la de SM de OB/OR, que no pasa por calidad, ``CERRADA``.
          Después, 400: lo aprobado ya es existencia.
        - SM: su factura de maquila se cancela si sigue en Borrador y la SM vuelve
          a Enviada. Una factura de proveedor viva que no sea esa, 400.
        - OC: se recalcula su estatus y después se puede cancelar (el DELETE de la
          OC sigue rechazando cualquier recepción, aun cancelada). OP ya
          Completada, 400.
        - Queda en auditoría (``CANCELAR``) y en el historial, con el motivo.
        """
        user = request.user
        if not puede_cancelar_recepciones(user):
            raise PermissionDenied("Solo un administrador de la empresa puede cancelar recepciones.")
        motivo = request.data.get("motivo")
        if not isinstance(motivo, str) or not motivo.strip():
            raise ValidationError({"motivo": "El motivo de cancelación es requerido."})
        motivo = motivo.strip()
        if len(motivo) > MOTIVO_CANCELACION_MAX:
            raise ValidationError({"motivo": f"El motivo de cancelación admite a lo más {MOTIVO_CANCELACION_MAX} caracteres."})
        # ``get_object`` acota por la empresa del usuario: la de otra empresa es 404.
        recepcion = self.get_object()
        E = Recepcion.EstatusRecepcion
        Factura = FacturaProveedor.FacturaProveedorStatus

        with transaction.atomic():
            # Mismo orden de bloqueo que el onboarding (SM, luego OC/OP) y al final la
            # recepción, que es la que bloquea Calidad: o la inspección ya existe al
            # releerla aquí, o Calidad espera y la ve CANCELADA.
            sm = oc = op = None
            if recepcion.salida_maquila_id:
                sm = SalidaMaquila.objects.select_for_update().get(pk=recepcion.salida_maquila_id)
            if recepcion.orden_compra_id:
                oc = OrdenCompra.objects.select_for_update().get(pk=recepcion.orden_compra_id)
            if recepcion.op_id:
                op = OrdenProduccion.objects.select_for_update().get(pk=recepcion.op_id)
            recepcion = Recepcion.objects.select_for_update().get(pk=recepcion.pk)

            # Misma regla que ``puede_cancelar`` del retrieve (EC-926), con las facturas bloqueadas.
            facturas = validar_cancelacion_recepcion(recepcion, op, bloquear=True)

            antes = {"estatus": recepcion.estatus}
            for factura in facturas:  # factura de maquila en Borrador: sin CxP ni póliza
                factura.estatus = Factura.CANCELADA
                factura.save(update_fields=["estatus", "updated_at"])
            recepcion.estatus = E.CANCELADA
            recepcion.motivo_cancelacion = motivo
            recepcion.fecha_cancelacion = timezone.now()
            recepcion.cancelado_por = user if getattr(user, "pk", None) else None
            recepcion._change_reason = motivo[:100]  # ``history_change_reason`` (100 caracteres)
            recepcion.save(update_fields=[
                "estatus", "motivo_cancelacion", "fecha_cancelacion", "cancelado_por", "updated_at",
            ])
            despues = {"estatus": recepcion.estatus, "motivo": motivo}
            # #1441: el encuadre ligado queda libre para recibir de nuevo o cancelarse.
            liberados = list(RecepcionRFIDEncuadre.objects.filter(recepcion=recepcion).values_list("pk", flat=True))
            if liberados:
                RecepcionRFIDEncuadre.objects.filter(pk__in=liberados).update(recepcion=None, updated_at=timezone.now())
                despues["encuadres_rfid_liberados"] = liberados
            if facturas:
                despues["facturas_proveedor_canceladas"] = [f.pk for f in facturas]
            if sm is not None and sm.estatus == SalidaMaquila.Estatus.RECIBIDA:
                sm.estatus = SalidaMaquila.Estatus.ENVIADA
                sm.fecha_recepcion = None
                sm.save(update_fields=["estatus", "fecha_recepcion", "updated_at"])
                despues["salida_maquila_estatus"] = sm.estatus
            if op is not None and op_se_reabre(op, recepcion):
                # #1326: la cerró esta recepción (``cerrar_orden``) y nada pasó por calidad.
                op.estatus_op = OrdenProduccion.EstatusOrdenProduccion.REVISION
                op.fecha_fin = None
                op.save(update_fields=["estatus_op", "fecha_fin"])
                despues["op_reabierta"] = {"op_id": op.pk, "estatus_op": op.estatus_op}
            if oc is not None and oc.estatus in self.ESTATUS_OC_RECIBIBLES | {OrdenCompra.EstatusOrdenCompra.RECIBIDA}:
                self._actualizar_estatus_oc(oc)
                despues["orden_compra_estatus"] = oc.estatus
            AuditoriaEvento.objects.create(
                empresa_id=recepcion.empresa_id,
                usuario=user if getattr(user, "pk", None) else None,
                modulo="compras",
                accion="CANCELAR",
                tabla=Recepcion._meta.db_table,
                id_registro=str(recepcion.pk),
                antes_json=antes,
                despues_json=despues,
                ip=_ip_auditoria(request),
                user_agent=request.META.get("HTTP_USER_AGENT"),
            )
        return Response(RecepcionSerializer(recepcion).data, status=status.HTTP_200_OK)

    def _ligar_encuadre_rfid(self, encuadre_id, recepcion, empresa):
        """#1441: liga el encuadre RFID con el que se recibe. Debe ser de la empresa,
        estar ACEPTADO, sin recepción y ser de la misma OC/OP. Bajo lock del encuadre."""
        encuadre = (
            RecepcionRFIDEncuadre.objects.select_for_update()
            .filter(pk=encuadre_id, empresa=empresa).first()
        )
        if encuadre is None:
            raise _error_detalle({"encuadre_rfid": "Encuadre RFID no encontrado."})
        if encuadre.estatus != RecepcionRFIDEncuadre.Estatus.ACEPTADO:
            raise _error_detalle({"encuadre_rfid": "Solo se recibe con un encuadre aceptado."})
        if encuadre.recepcion_id:
            raise _error_detalle({"encuadre_rfid": "El encuadre ya está ligado a otra recepción."})
        if (encuadre.orden_compra_id or None) != (recepcion.orden_compra_id or None) or (
            (encuadre.op_id or None) != (recepcion.op_id or None)
        ):
            raise _error_detalle({"encuadre_rfid": "El encuadre es de otra orden."})
        encuadre.recepcion = recepcion
        encuadre.save(update_fields=["recepcion", "updated_at"])

    def _serie_folio_recepcion(self, empresa, sucursal, serie_codigo):
        """Serie elegida (``serie_codigo``) en la sucursal, o ``None`` si no está activa:
        no se cambia por otra sin avisar (#1292). Sin ``serie_codigo``, RC y si no hay,
        la primera de RC/RT/RZ de la sucursal, como antes."""
        qs = SerieFolio.objects.select_for_update().filter(
            empresa=empresa,
            sucursal=sucursal,
            activo=True,
        )
        serie_codigo = (serie_codigo or "").strip().upper()
        series_validas = ["RC", "RT", "RZ"]
        pedida = serie_codigo or "RC"
        serie_folio = qs.filter(
            Q(tipo_documento__iexact="RECEPCION", serie__iexact=pedida)
            | Q(tipo_documento__iexact=pedida)
        ).order_by("id_serie_folio").first()
        if serie_folio or serie_codigo:
            return serie_folio

        qs = qs.filter(
            Q(tipo_documento__iexact="RECEPCION", serie__in=series_validas)
            | Q(tipo_documento__in=series_validas)
        )
        return qs.order_by("id_serie_folio").first()

    def _sm_recibible(self, sm_id, empresa, orden_compra_id):
        if orden_compra_id:
            raise _error_detalle({"salida_maquila": "Una SM no se recibe contra una orden de compra."})
        sm = (
            SalidaMaquila.objects.select_for_update(of=("self",))
            .select_related("op", "ob", "orden_r", "centro_confeccion", "sucursal")
            .filter(pk=sm_id, empresa=empresa, activo=True)
            .first()
        )
        if sm is None:
            raise _error_detalle({"salida_maquila": "Salida a maquila no encontrada."})
        if sm.estatus != SalidaMaquila.Estatus.ENVIADA:
            raise _error_detalle({"salida_maquila": f"La SM {sm.folio} está {sm.get_estatus_display().lower()}."})
        return sm

    def _recibir_sm_servicio(self, request, sm, header, serie_codigo, detalle_raw):
        """SM de OB/OR: las prendas ya son del pedido y no entran al inventario.
        Se cuentan las piezas que regresan por renglón de la SM (``detalle`` con
        ``salida_maquila_detalle``; vacío = todo lo pendiente) contra lo pendiente
        de cada renglón. La factura de maquila es por lo que regresó; la SM queda
        Recibida al completarse o con ``cerrar_salida_maquila`` (el resto, faltante).

        Tope y cierre con el pendiente exacto, no el redondeado a centavos que se
        muestra; y a lo más 2 decimales por renglón, como la SM, para no dejar un
        residuo que se muestre como 0.00 (#1308)."""
        pendientes = pendiente_por_renglon_sm(sm.pk)
        explicito = bool(detalle_raw)
        if not detalle_raw:
            detalle_raw = [{"salida_maquila_detalle": k, "cantidad_recibida": v} for k, v in pendientes.items() if v > 0]
            if not detalle_raw:
                raise _error_detalle({"detalle": f"La SM {sm.folio} no tiene piezas pendientes."})
        acumulado = {}
        for idx, item in enumerate(detalle_raw):
            smd_id = item.get("salida_maquila_detalle")
            ref = f"#{idx + 1}" + (f" (salida_maquila_detalle {smd_id})" if smd_id else "")
            if item.get("orden_compra_detalle") or item.get("orden_produccion_detalle") or not smd_id:
                raise _error_detalle({"detalle": f"El renglón {ref} de una SM de OB/OR va con salida_maquila_detalle."})
            if smd_id not in pendientes:
                raise _error_detalle({"detalle": f"El renglón {ref} no va en la SM {sm.folio}."})
            cantidad = Decimal(str(item["cantidad_recibida"]))
            if explicito and cantidad != cantidad.quantize(CENTAVO):
                raise _error_detalle({"detalle": f"El renglón {ref} admite hasta 2 decimales, como la SM {sm.folio}."})
            acumulado[smd_id] = acumulado.get(smd_id, Decimal("0")) + cantidad
            if acumulado[smd_id] > pendientes[smd_id]:
                raise _error_detalle({"detalle": (
                    f"El renglón {ref} excede lo pendiente de la SM {sm.folio} "
                    f"({pendientes[smd_id].normalize():f} pzas).")})
        almacen = Almacen.objects.filter(pk=header["almacen"], empresa_id=sm.empresa_id,
                                         sucursal_id=sm.sucursal_id).first()
        if almacen is None:
            raise _error_detalle({"almacen": "El almacén no pertenece a la sucursal de la SM."})
        transportista_id = header.get("transportista")
        if transportista_id and not Transportista.objects.filter(pk=transportista_id, empresa_id=sm.empresa_id).exists():
            raise _error_detalle({"transportista": "Transportista no encontrado."})
        recepcion = Recepcion(
            tipo_origen=Recepcion.TipoOrigen.SALIDA_MAQUILA, salida_maquila=sm,
            empresa_id=sm.empresa_id, sucursal_id=sm.sucursal_id, proveedor_id=sm.centro_confeccion_id,
            almacen=almacen, transportista_id=transportista_id, usuario=request.user,
            fecha_recepcion=header.get("fecha_recepcion") or timezone.now(),
            remision=(header.get("remision") or None),
            factura_referencia=(header.get("factura_referencia") or None),
            observaciones=(header.get("observaciones") or None),
            estatus=Recepcion.EstatusRecepcion.CERRADA, activo=True,
        )
        self._asignar_folio_recepcion(recepcion, serie_codigo)
        recepcion.save()
        detalles = {d.pk: d for d in sm.detalles.select_related("ob_detalle", "or_detalle")}
        renglones = RecepcionDetalle.objects.bulk_create([
            RecepcionDetalle(
                recepcion=recepcion, salida_maquila_detalle=detalles[smd_id],
                producto_id=getattr(detalles[smd_id].ob_detalle or detalles[smd_id].or_detalle, "producto_id", None),
                cantidad_recibida=cantidad,
            )
            for smd_id, cantidad in acumulado.items()
        ])
        facturar_maquila(sm, recepcion, renglones)
        sm_completa = all(pendientes[k] - acumulado.get(k, 0) <= 0 for k in pendientes)
        recepcion.alcance_recepcion = (
            Recepcion.AlcanceRecepcion.COMPLETA if sm_completa else Recepcion.AlcanceRecepcion.PARCIAL
        )
        recepcion.save(update_fields=["alcance_recepcion", "updated_at"])
        if header.get("cerrar_salida_maquila") or sm_completa:
            marcar_recibida(sm)
        return Response(
            {"recepcion": RecepcionSerializer(recepcion).data, "detalle": RecepcionDetalleSerializer(renglones, many=True).data},
            status=status.HTTP_200_OK,
        )

    def _asignar_folio_recepcion(self, recepcion, serie_codigo):
        serie_folio = self._serie_folio_recepcion(
            recepcion.empresa, recepcion.sucursal, serie_codigo
        )
        if not serie_folio:
            sucursal = getattr(recepcion.sucursal, "nombre", None) or recepcion.sucursal_id
            if serie_codigo:
                mensaje = f"La serie {serie_codigo.upper()} no está activa en la sucursal {sucursal}."
            else:
                mensaje = f"La sucursal {sucursal} no tiene una serie de recepción activa (RC, RT o RZ)."
            raise _error_detalle({"serie_codigo": mensaje})

        try:
            folio_formateado, nuevo_consecutivo, anio_actual = serie_folio.get_siguiente_folio()
        except Exception:
            raise _error_detalle({"folio": "No se pudo generar el folio de la recepción."})

        serie_folio.folio_actual = nuevo_consecutivo
        serie_folio.ultimo_anio = anio_actual
        serie_folio.save(update_fields=["folio_actual", "ultimo_anio", "updated_at"])
        recepcion.folio = folio_formateado

    def _cantidad_rechazada_calidad(self, **filtro_recepcion_detalle):
        # Lo que calidad rechaza deja de contar como "recibido": vuelve a ser
        # pendiente para la OC/OP de origen (ver DOCS/arquitectura/flujo-recepcion-calidad-compras.md).
        total = (
            CalidadInspeccionDetalle.objects.filter(
                recepcion_detalle__recepcion__activo=True,
                **filtro_recepcion_detalle,
            )
            .exclude(recepcion_detalle__recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
            .exclude(calidad_inspeccion__estado=CalidadInspeccion.ESTADO_ANULADA)
            .aggregate(total=Sum("cantidad_rechazada"))
            .get("total")
        )
        return Decimal(str(total or 0))

    def _rechazado_por_detalle(self, campo, ids):
        """Rechazado por Calidad agrupado por renglón de origen, en una sola consulta."""
        filas = (
            CalidadInspeccionDetalle.objects.filter(
                recepcion_detalle__recepcion__activo=True, **{f"{campo}__in": ids}
            )
            .exclude(recepcion_detalle__recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
            .exclude(calidad_inspeccion__estado=CalidadInspeccion.ESTADO_ANULADA)
            .values(campo)
            .annotate(total=Sum("cantidad_rechazada"))
        )
        return {fila[campo]: Decimal(str(fila["total"] or 0)) for fila in filas}

    def _recibido_por_detalle(self, campo, ids):
        """``_cantidad_recibida_oc``/``_op`` para varios renglones en dos consultas.

        ``campo`` es ``orden_compra_detalle_id`` u ``orden_produccion_detalle_id``.
        """
        if not ids:
            return {}
        filas = (
            RecepcionDetalle.objects.filter(recepcion__activo=True, **{f"{campo}__in": ids})
            .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
            .values(campo)
            .annotate(total=Sum("cantidad_recibida"))
        )
        recibido = {fila[campo]: Decimal(str(fila["total"] or 0)) for fila in filas}
        for clave, total in self._rechazado_por_detalle(f"recepcion_detalle__{campo}", ids).items():
            recibido[clave] = recibido.get(clave, Decimal("0")) - total
        return recibido

    def _cantidad_recibida_oc(self, orden_compra_detalle_id):
        total = (
            RecepcionDetalle.objects.filter(
                orden_compra_detalle_id=orden_compra_detalle_id,
                recepcion__activo=True,
            )
            .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
            .aggregate(total=Sum("cantidad_recibida"))
            .get("total")
        )
        rechazado = self._cantidad_rechazada_calidad(
            recepcion_detalle__orden_compra_detalle_id=orden_compra_detalle_id
        )
        return Decimal(str(total or 0)) - rechazado

    def _cantidad_recibida_op(self, orden_produccion_detalle_id):
        total = (
            RecepcionDetalle.objects.filter(
                orden_produccion_detalle_id=orden_produccion_detalle_id,
                recepcion__activo=True,
            )
            .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
            .aggregate(total=Sum("cantidad_recibida"))
            .get("total")
        )
        rechazado = self._cantidad_rechazada_calidad(
            recepcion_detalle__orden_produccion_detalle_id=orden_produccion_detalle_id
        )
        return Decimal(str(total or 0)) - rechazado

    def _crear_renglones_recepcion(self, recepcion, detalle_payload):
        # Solo registra el conteo físico (``RecepcionDetalle``). NO toca
        # ``Existencia`` ni crea ``MovimientoInventario`` — eso ahora es
        # exclusivo de ``CalidadInspeccionViewSet``, una vez que Calidad
        # aprueba (total o parcialmente) cada renglón. Ver
        # DOCS/arquitectura/flujo-recepcion-calidad-compras.md.
        renglones = []
        for item in detalle_payload:
            ubicacion = None
            ubicacion_id = item.get("ubicacion")
            if recepcion.almacen.requiere_ubicacion and not ubicacion_id:
                raise _error_detalle(
                    {"ubicacion": "La ubicación es requerida para este almacén."}
                )
            if ubicacion_id:
                ubicacion = Ubicacion.objects.filter(
                    pk=ubicacion_id,
                    almacen_id=recepcion.almacen_id,
                ).first()
                if not ubicacion:
                    raise _error_detalle(
                        {"ubicacion": "La ubicación no pertenece al almacén seleccionado."}
                    )

            for campo, modelo in (("lote", Lote), ("serie", Serie)):
                # Lote/serie son del producto de catálogo; una muestra no tiene.
                if item.get(campo) and (item["producto"] is None or not modelo.objects.filter(
                    pk=item[campo], producto_id=item["producto"].pk
                ).exists()):
                    raise _error_detalle(
                        {campo: f"El {campo} no existe o no pertenece al producto del renglón."}
                    )

            detalle = RecepcionDetalle.objects.create(
                recepcion=recepcion,
                orden_compra_detalle=item.get("orden_compra_detalle"),
                orden_produccion_detalle=item.get("orden_produccion_detalle"),
                producto=item["producto"],
                producto_variante=item.get("producto_variante"),
                variante_produccion=item.get("variante_produccion"),
                ubicacion=ubicacion,
                lote_id=item.get("lote"),
                serie_id=item.get("serie"),
                cantidad_recibida=item["cantidad_recibida"],
            )
            renglones.append(detalle)
        return renglones

    def _actualizar_estatus_oc(self, oc):
        # Lo recibido neto de todos los renglones en bloque (``_recibido_por_detalle``,
        # 2 consultas); antes eran 2 por renglón de la OC.
        detalles = list(OrdenCompraDetalle.objects.filter(orden_compra=oc).values_list("id", "cantidad"))
        if not detalles:
            return
        recibidos = self._recibido_por_detalle("orden_compra_detalle_id", [pk for pk, _ in detalles])

        total_pendiente = Decimal("0")
        total_recibido = Decimal("0")
        for detalle_id, cantidad in detalles:
            ordered = Decimal(str(cantidad or 0))
            recibido = recibidos.get(detalle_id, Decimal("0"))
            if recibido > 0:
                total_recibido += recibido
            pendiente = ordered - recibido
            if pendiente > 0:
                total_pendiente += pendiente

        if oc.saldo_cerrado:
            # Saldo cerrado (``cerrar-saldo/``): lo que falta ya no se espera.
            oc.estatus = OrdenCompra.EstatusOrdenCompra.RECIBIDA
        elif total_pendiente > 0 and total_recibido <= 0:
            # Recibido neto 0 (p. ej. Calidad rechazó todo): vuelve a Autorizada.
            oc.estatus = OrdenCompra.EstatusOrdenCompra.AUTORIZADA
        elif total_pendiente > 0:
            oc.estatus = OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA
        else:
            oc.estatus = OrdenCompra.EstatusOrdenCompra.RECIBIDA
        oc.save(update_fields=["estatus", "updated_at"])
        if oc.pre_orden_id or oc.necesidades.exists():
            # Faltantes de pre-órdenes de producción: al confirmar la transacción
            # (ya con la existencia nueva) se reparte lo recibido entre las
            # necesidades y se recalculan sus pre-órdenes.
            from produccion.services.pre_orden_service import repartir_recepcion_oc
            transaction.on_commit(lambda oc_id=oc.pk: repartir_recepcion_oc(oc_id))

    def _actualizar_estatus_op(self, op):
        # Solo reabre: un rechazo de Calidad puede volver a dejar pendiente una
        # OP que ya se había marcado COMPLETADO. Cerrarla sigue siendo
        # decisión explícita de ``cerrar_orden`` en el onboarding de recepción,
        # esta función nunca la completa.
        if op.estatus_op != OrdenProduccion.EstatusOrdenProduccion.COMPLETADO:
            return

        detalles = OrdenProduccionDetalle.objects.filter(op=op, activo=True).only("op_detalle_id", "cantidad")
        if not detalles.exists():
            return

        total_pendiente = Decimal("0")
        for detalle in detalles:
            ordered = Decimal(str(detalle.cantidad or 0))
            recibido = self._cantidad_recibida_op(detalle.pk)
            pendiente = ordered - recibido
            if pendiente > 0:
                total_pendiente += pendiente

        if total_pendiente > 0:
            op.estatus_op = OrdenProduccion.EstatusOrdenProduccion.REVISION
            op.save(update_fields=["estatus_op"])

    def handle_get_onboarding(self, request):
        user = request.user
        empresa = getattr(user, "empresa", None)
        # Superusuario incluido: mismo alcance que el listado de recepciones (#834, #862).
        if not empresa:
            raise _error_detalle({"empresa": "El usuario no tiene empresa asignada."})

        oc_q = (request.query_params.get("q") or "").strip()
        almacen_id = request.query_params.get("almacen_id")

        ordenes_qs = (
            OrdenCompra.objects.filter(
                activo=True,
                empresa=empresa,
                estatus__in=self.ESTATUS_OC_RECIBIBLES,
            )
            .select_related("proveedor", "sucursal")
            .order_by("-updated_at", "-id")
        )
        # Filtro opcional MP (serie OC) / PT (serie O). Omitirlo devuelve ambas.
        tipo_compra = (request.query_params.get("tipo_compra") or "").strip().upper()
        if tipo_compra:
            if tipo_compra not in OrdenCompra.TipoCompra.values:
                raise _error_detalle({"tipo_compra": "Valor inválido. Usa MP (OC) o PT (O)."})
            ordenes_qs = ordenes_qs.filter(tipo_compra=tipo_compra)
        orden_compra_id = request.query_params.get("orden_compra_id")
        if orden_compra_id not in (None, ""):
            try:
                ordenes_qs = ordenes_qs.filter(pk=int(orden_compra_id))
            except (TypeError, ValueError):
                raise _error_detalle({"orden_compra_id": "Debe ser un entero."})
        if oc_q:
            ordenes_qs = ordenes_qs.filter(
                Q(folio__icontains=oc_q)
                | Q(referencia__icontains=oc_q)
                | Q(proveedor__nombre__icontains=oc_q)
                | Q(proveedor__razon_social__icontains=oc_q)
            )

        ordenes_limitadas = list(ordenes_qs[:50])
        # ── Bulk-fetch detalle for all OCs (avoid N+1) ──
        oc_ids = [oc.pk for oc in ordenes_limitadas]
        oc_detalles_map = {}  # oc_id → [detalle_dict, …]
        if oc_ids:
            detalles_qs = (
                OrdenCompraDetalle.objects
                .filter(orden_compra_id__in=oc_ids)
                .select_related("producto", "producto_variante__color", "producto_variante__talla")
                .order_by("orden_compra_id", "id")
            )
            detalles_list = list(detalles_qs)

            detalle_ids = [d.pk for d in detalles_list]
            recibido_map = {}
            if detalle_ids:
                recibido_qs = (
                    RecepcionDetalle.objects.filter(
                        orden_compra_detalle_id__in=detalle_ids,
                        recepcion__activo=True,
                    )
                    .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
                    .values("orden_compra_detalle_id")
                    .annotate(total=Sum("cantidad_recibida"))
                )
                for row in recibido_qs:
                    recibido_map[row["orden_compra_detalle_id"]] = Decimal(str(row["total"] or 0))
                # Lo que Calidad rechaza vuelve a ser pendiente, igual que en el POST (#511).
                for clave, total in self._rechazado_por_detalle(
                    "recepcion_detalle__orden_compra_detalle_id", detalle_ids
                ).items():
                    recibido_map[clave] = recibido_map.get(clave, Decimal("0")) - total

            for detalle in detalles_list:
                recibido = recibido_map.get(detalle.pk, Decimal("0"))
                ordered = Decimal(str(detalle.cantidad or 0))
                pendiente = ordered - recibido
                variante = detalle.producto_variante
                item = {
                    "id": detalle.pk,
                    "producto_id": detalle.producto_id,
                    "producto_nombre": detalle.producto.nombre,
                    "producto_codigo": detalle.producto.codigo,
                    "producto_cod_proscai": detalle.producto.cod_proscai,
                    # Mismos nombres que ``OrdenCompraDetalleReadSerializer``.
                    "producto_variante_id": detalle.producto_variante_id,
                    "producto_variante_sku": getattr(variante, "sku", None),
                    "producto_variante_color": getattr(getattr(variante, "color", None), "nombre", None),
                    "producto_variante_talla": getattr(getattr(variante, "talla", None), "nombre", None),
                    "cantidad_ordenada": str(ordered),
                    "cantidad_recibida": str(recibido),
                    "cantidad_pendiente": str(max(pendiente, Decimal("0"))),
                    "descripcion": detalle.descripcion,
                }
                oc_detalles_map.setdefault(detalle.orden_compra_id, []).append(item)

        # ── Build ordenes with embedded detalle ──
        ordenes = []
        for oc in ordenes_limitadas:
            ordenes.append(
                {
                    "id": oc.pk,
                    "folio": oc.folio,
                    "tipo_compra": oc.tipo_compra,
                    "estatus": oc.estatus,
                    "proveedor_id": oc.proveedor_id,
                    "proveedor_nombre": getattr(oc.proveedor, "nombre", None),
                    "sucursal_id": oc.sucursal_id,
                    "almacen_id": oc.almacen_id,
                    "fecha_oc": oc.fecha_oc,
                    "detalle": oc_detalles_map.get(oc.pk, []),
                }
            )

        # Mismo alcance que las SM del onboarding y que el módulo de OP (#1236):
        # empresa activa + sucursales del usuario.
        from produccion.scope import ordenes_produccion_visibles
        op_qs = (
            ordenes_produccion_visibles(
                OrdenProduccion.objects.filter(activo=True, estatus_op__in=ESTATUS_OP_RECIBIBLES), user
            )
            .select_related("pedido", "sucursal")
            .order_by("-fecha_inicio", "-op_id")
        )
        if oc_q:
            op_qs = op_qs.filter(
                Q(folio_op__icontains=oc_q)
                | Q(pedido__folio__icontains=oc_q)
                | Q(orden_produccion_detalle__producto_variante__sku__icontains=oc_q)
                | Q(orden_produccion_detalle__producto_variante__producto__nombre__icontains=oc_q)
                | Q(orden_produccion_detalle__variante_produccion__sku__icontains=oc_q)
                | Q(orden_produccion_detalle__variante_produccion__nombre__icontains=oc_q)
            ).distinct()

        ordenes_produccion = []
        ops_limitadas = list(op_qs[:50])
        op_detalles_map = {}
        op_ids = [op.pk for op in ops_limitadas]
        if op_ids:
            op_detalles_qs = (
                OrdenProduccionDetalle.objects
                .filter(op_id__in=op_ids, activo=True)
                .select_related("producto_variante__producto", "variante_produccion")
                .order_by("op_id", "op_detalle_id")
            )
            op_detalles_list = list(op_detalles_qs)

            op_detalle_ids = [d.pk for d in op_detalles_list]
            recibido_op_map = {}
            if op_detalle_ids:
                recibido_op_qs = (
                    RecepcionDetalle.objects.filter(
                        orden_produccion_detalle_id__in=op_detalle_ids,
                        recepcion__activo=True,
                    )
                    .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
                    .values("orden_produccion_detalle_id")
                    .annotate(total=Sum("cantidad_recibida"))
                )
                for row in recibido_op_qs:
                    recibido_op_map[row["orden_produccion_detalle_id"]] = Decimal(str(row["total"] or 0))
                for clave, total in self._rechazado_por_detalle(
                    "recepcion_detalle__orden_produccion_detalle_id", op_detalle_ids
                ).items():
                    recibido_op_map[clave] = recibido_op_map.get(clave, Decimal("0")) - total
            # Piezas en SM Enviadas: se reciben con la SM, no con la OP directa (#1234).
            en_maquila = apartado_en_maquila(op_detalle_ids)

            for op_detalle in op_detalles_list:
                producto_variante = op_detalle.producto_variante
                producto = getattr(producto_variante, "producto", None)
                muestra = op_detalle.variante_produccion  # fabricación especial
                cantidad = Decimal(str(op_detalle.cantidad or 0))
                recibido = recibido_op_map.get(op_detalle.pk, Decimal("0"))
                apartado, _, por_sm = en_maquila.get(op_detalle.pk, (Decimal("0"), [], []))
                pendiente = cantidad - recibido - apartado
                item = {
                    "id": op_detalle.pk,
                    "producto_id": getattr(producto, "pk", None),
                    "producto_variante_id": getattr(producto_variante, "pk", None),
                    "variante_produccion_id": getattr(muestra, "pk", None),
                    "producto_nombre": (
                        getattr(producto_variante, "nombre", None)
                        or getattr(producto, "nombre", None)
                        or getattr(muestra, "nombre", None)
                        or getattr(muestra, "sku", None)
                    ),
                    "sku": getattr(producto_variante, "sku", None) or getattr(muestra, "sku", None),
                    "producto_codigo": getattr(producto, "codigo", None),
                    "producto_cod_proscai": getattr(producto, "cod_proscai", None),
                    "cantidad_ordenada": str(cantidad),
                    "cantidad_recibida": str(recibido),
                    # Por la OP directa (sin SM): lo que no está en maquila.
                    "cantidad_pendiente": str(max(pendiente, Decimal("0"))),
                    "cantidad_en_maquila": str(apartado),
                    # Tope al recibir con cada SM: su ``cantidad_pendiente``.
                    "salidas_maquila": por_sm,
                    "descripcion": op_detalle.observaciones,
                }
                op_detalles_map.setdefault(op_detalle.op_id, []).append(item)

        for op in ops_limitadas:
            detalle = []
            for item in op_detalles_map.get(op.pk, []):
                detalle.append(item)
            # Sigue si hay algo por recibir, directo o con una SM.
            if not any(Decimal(item["cantidad_pendiente"]) > 0 or item["salidas_maquila"] for item in detalle):
                continue

            ordenes_produccion.append(
                {
                    "id": op.pk,
                    "folio": op.folio_op,
                    "estatus": op.estatus_op,
                    "pedido_id": op.pedido_id,
                    "sucursal_id": op.sucursal_id,
                    "fecha_inicio": op.fecha_inicio,
                    "cerrar_orden": op.cerrar_orden,
                    "detalle": detalle,
                }
            )

        almacenes_qs = Almacen.objects.filter(estatus="ACTIVO", empresa=empresa)
        almacenes = list(
            almacenes_qs.order_by("codigo").values(
                "id_almacen",
                "codigo",
                "nombre",
                "sucursal_id",
                "requiere_ubicacion",
            )
        )

        ubicaciones_qs = Ubicacion.objects.filter(estatus="ACTIVO")
        if almacen_id:
            ubicaciones_qs = ubicaciones_qs.filter(almacen_id=almacen_id, almacen__empresa=empresa)
        else:
            ubicaciones_qs = ubicaciones_qs.none()
        ubicaciones = list(
            ubicaciones_qs.order_by("pasillo", "rack", "nivel", "posicion").values(
                "id_ubicacion",
                "almacen_id",
                "pasillo",
                "rack",
                "nivel",
                "posicion",
            )
        )

        # Sin tope (#1291): antes ``[:50]`` de toda la empresa dejaba fuera las RT/RZ
        # de algunas sucursales; son pocas filas por sucursal. ``?sucursal_id=`` acota
        # a la sucursal de la orden que se va a recibir.
        series_qs = SerieFolio.objects.filter(activo=True, empresa=empresa)
        sucursal_serie = entero_param(request, "sucursal_id")
        if sucursal_serie is not None:
            series_qs = series_qs.filter(sucursal_id=sucursal_serie)
        series_qs = series_qs.filter(
            Q(tipo_documento__iexact="RECEPCION", serie__in=["RC", "RT", "RZ"])
            | Q(tipo_documento__in=["RC", "RT", "RZ"])
        )
        series = list(
            series_qs.order_by("sucursal_id", "serie", "tipo_documento")
            .values("id_serie_folio", "tipo_documento", "serie", "sucursal_id")
        )

        return {
            "user": {"id": getattr(user, "pk", None), "email": getattr(user, "email", None)},
            "empresa_id": getattr(empresa, "pk", None),
            "catalogos": {
                "almacenes": almacenes,
                "ubicaciones": ubicaciones,
                "series_recepcion": series,
            },
            "busqueda": {
                "ordenes_compra": ordenes,
                "ordenes_produccion": ordenes_produccion,
                "salidas_maquila": _sm_pendientes(request),
            },
        }

    @action(detail=False, methods=["get", "post"], url_path="onboarding")
    def onboarding(self, request):
        user = request.user
        empresa = getattr(user, "empresa", None)
        # Mismos códigos que el front (#1295): R- para leer el alta, C- para recibir
        # (OC/OP/SM y cerrar la SM). Superusuario y admin pasan sin consultar; la
        # resolución queda en caché de la petición (``_sm_pendientes`` la reutiliza).
        codigo = PERMISO_LEER_RECEPCIONES if request.method in SAFE_METHODS else PERMISO_CREAR_RECEPCIONES
        if codigo not in permisos_de_la_peticion(request):
            raise PermissionDenied(f"No tienes permiso para esta acción ({codigo}).")

        if request.method.lower() == "get":
            return Response(self.handle_get_onboarding(request))

        try:
            serializer = RecepcionOnboardingSerializer(data=request.data or {})
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
            header = data["recepcion"]
            detalle_raw = data["detalle"]
            # Vacío = la serie por omisión (RC o la que tenga la sucursal); una serie
            # elegida que la sucursal no tiene es 400, no se cambia por otra (#1292).
            serie_codigo = (header.get("serie_codigo") or "").strip().upper()

            # Superusuario incluido, igual que el GET (#875).
            if not empresa:
                raise _error_detalle({"empresa": "El usuario no tiene empresa asignada."})
            if serie_codigo and serie_codigo not in {"RC", "RT", "RZ"}:
                raise _error_detalle({"serie_codigo": "serie_codigo debe ser RC, RT o RZ."})

            with transaction.atomic():
                orden_compra_id = header.get("orden_compra")
                orden_produccion_id = header.get("orden_produccion")
                sm = None
                if header.get("salida_maquila"):
                    sm = self._sm_recibible(header["salida_maquila"], empresa, orden_compra_id)
                    if sm.op_id is None:  # OB/OR: sin inventario, solo se cierra
                        return self._recibir_sm_servicio(request, sm, header, serie_codigo, detalle_raw)
                    if orden_produccion_id and int(orden_produccion_id) != sm.op_id:
                        raise _error_detalle({"salida_maquila": "La SM no es de esa orden de producción."})
                    orden_produccion_id = sm.op_id
                    if not detalle_raw:
                        raise _error_detalle({"detalle": "La recepción debe tener al menos un renglón con cantidad recibida."})
                if bool(orden_compra_id) == bool(orden_produccion_id):
                    raise _error_detalle(
                        {"recepcion": "Debes enviar exactamente uno de orden_compra u orden_produccion."}
                    )

                oc = None
                op = None
                detalles_oc = {}
                detalles_op = {}
                detalle_payload = []

                if orden_compra_id:
                    oc = (
                        OrdenCompra.objects.select_for_update(of=("self",))
                        .select_related("empresa", "sucursal", "proveedor")
                        .filter(pk=orden_compra_id, activo=True)
                        .first()
                    )
                    if not oc:
                        raise _error_detalle({"orden_compra": "Orden de compra no encontrada."})
                    if oc.empresa_id != empresa.pk:
                        raise _error_detalle({"orden_compra": "No tienes acceso a esta orden de compra."})
                    if oc.estatus not in self.ESTATUS_OC_RECIBIBLES:
                        raise _error_detalle({"estatus": "La orden de compra no está disponible para recepción."})
                    if not oc.proveedor_id:
                        raise _error_detalle({"proveedor": "La orden de compra no tiene proveedor asignado."})
                else:
                    op = (
                        OrdenProduccion.objects.select_for_update()
                        .select_related("empresa", "sucursal")
                        .filter(pk=orden_produccion_id, activo=True)
                        .first()
                    )
                    if not op:
                        raise _error_detalle({"orden_produccion": "Orden de producción no encontrada."})
                    if op.empresa_id != empresa.pk:
                        raise _error_detalle({"orden_produccion": "No tienes acceso a esta orden de producción."})
                    if op.estatus_op not in ESTATUS_OP_RECIBIBLES:
                        raise _error_detalle({"estatus": "La orden de producción no está disponible para recepción."})

                almacen = (
                    Almacen.objects.select_related("empresa", "sucursal")
                    .filter(pk=header["almacen"])
                    .first()
                )
                if not almacen:
                    raise _error_detalle({"almacen": "Almacén no encontrado."})
                empresa_origen = oc.empresa if oc else op.empresa
                sucursal_origen = oc.sucursal if oc else op.sucursal
                proveedor_origen = oc.proveedor if oc else None
                # Un almacén sin empresa se rechaza: antes la guarda era
                # ``... and almacen.empresa_id and ...`` y sin empresa se saltaba.
                if almacen.empresa_id is None or almacen.empresa_id != empresa_origen.pk:
                    raise _error_detalle({"almacen": "El almacén no pertenece a la empresa de la orden."})
                if almacen.sucursal_id is None or almacen.sucursal_id != sucursal_origen.pk:
                    raise _error_detalle({"almacen": "El almacén no pertenece a la sucursal de la orden."})
                transportista_id = header.get("transportista")
                if transportista_id and not Transportista.objects.filter(
                    pk=transportista_id, empresa_id=empresa_origen.pk
                ).exists():
                    raise _error_detalle({"transportista": "Transportista no encontrado."})

                if oc:
                    detalles_oc = {
                        d.pk: d
                        for d in OrdenCompraDetalle.objects.select_related("producto")
                        .filter(orden_compra=oc)
                        .order_by("id")
                    }
                    if not detalles_oc:
                        raise _error_detalle({"detalle": "La orden de compra no tiene productos para recibir."})

                    acumulado = {}  # cantidad ya aceptada en esta petición por renglón de origen
                    for idx, item in enumerate(detalle_raw):
                        detalle_id = item.get("orden_compra_detalle")
                        if not detalle_id:
                            raise _error_detalle(
                                {"detalle": f"El renglón #{idx + 1} requiere orden_compra_detalle."}
                            )
                        if item.get("orden_produccion_detalle"):
                            raise _error_detalle(
                                {"detalle": f"El renglón #{idx + 1} (orden_compra_detalle {detalle_id}) no debe enviar orden_produccion_detalle para una OC."}
                            )

                        oc_detalle = origen_detalle = detalles_oc.get(detalle_id)
                        if not oc_detalle:
                            raise _error_detalle(
                                {"detalle": f"El renglón #{idx + 1} (orden_compra_detalle {detalle_id}) no pertenece a la orden de compra."}
                            )

                        ref = _ref_renglon(idx, origen_detalle)
                        cantidad = Decimal(str(item["cantidad_recibida"]))
                        # Defensa en profundidad: ``RecepcionOnboardingSerializer``
                        # ya rechaza esto con el mismo mensaje (EC-894).
                        if cantidad <= 0:
                            raise _error_detalle(
                                {"detalle": f"El renglón {ref} debe tener cantidad_recibida > 0."}
                            )

                        recibido = self._cantidad_recibida_oc(oc_detalle.pk)
                        ordered = Decimal(str(oc_detalle.cantidad or 0))
                        pendiente = ordered - recibido
                        if pendiente <= 0:
                            raise _error_detalle(
                                {
                                    "detalle": (
                                        f"El producto del renglón {ref} ya fue recibido completamente."
                                    )
                                }
                            )
                        # El mismo renglón puede venir varias veces (otra ubicación o lote):
                        # la suma de la petición, no cada copia, es la que se compara (#1173).
                        acumulado[oc_detalle.pk] = acumulado.get(oc_detalle.pk, Decimal("0")) + cantidad
                        if acumulado[oc_detalle.pk] > pendiente:
                            raise _error_detalle(
                                {
                                    "detalle": (
                                        f"El renglón {ref} excede la cantidad pendiente por recibir."
                                    )
                                }
                            )
                        detalle_payload.append(
                            {
                                "orden_compra_detalle": oc_detalle,
                                "orden_produccion_detalle": None,
                                "producto": oc_detalle.producto,
                                "producto_variante": oc_detalle.producto_variante,
                                "cantidad_recibida": cantidad,
                                "ubicacion": item.get("ubicacion"),
                                "lote": item.get("lote"),
                                "serie": item.get("serie"),
                            }
                        )
                else:
                    detalles_op = {
                        d.pk: d
                        for d in OrdenProduccionDetalle.objects.select_related(
                            "producto_variante__producto", "variante_produccion"
                        )
                        .filter(op=op, activo=True)
                        .order_by("op_detalle_id")
                    }
                    if not detalles_op:
                        raise _error_detalle({"detalle": "La orden de producción no tiene productos para recibir."})
                    # Piezas en maquila de OTRAS SM Enviadas: no se reciben por la OP
                    # directa ni con otra SM (#1234). Con SM, el tope es lo pendiente
                    # de la SM, no de la OP (#1232).
                    en_maquila = apartado_en_maquila(detalles_op.keys(), excluir_sm_id=getattr(sm, "pk", None))
                    pendiente_sm = pendiente_de_sm(sm) if sm is not None else None

                    acumulado = {}  # cantidad ya aceptada en esta petición por renglón de origen
                    for idx, item in enumerate(detalle_raw):
                        detalle_id = item.get("orden_produccion_detalle")
                        if not detalle_id:
                            raise _error_detalle(
                                {"detalle": f"El renglón #{idx + 1} requiere orden_produccion_detalle."}
                            )
                        if item.get("orden_compra_detalle"):
                            raise _error_detalle(
                                {"detalle": f"El renglón #{idx + 1} (orden_produccion_detalle {detalle_id}) no debe enviar orden_compra_detalle para una OP."}
                            )

                        op_detalle = origen_detalle = detalles_op.get(detalle_id)
                        if not op_detalle:
                            raise _error_detalle(
                                {"detalle": f"El renglón #{idx + 1} (orden_produccion_detalle {detalle_id}) no pertenece a la orden de producción."}
                            )

                        producto_variante = op_detalle.producto_variante
                        producto = getattr(producto_variante, "producto", None)
                        # Muestra de fabricación especial: entra como su variante de producción.
                        muestra = None if producto_variante else op_detalle.variante_produccion
                        if not muestra and (not producto_variante or not producto):
                            raise _error_detalle(
                                {"detalle": f"El renglón #{idx + 1} (orden_produccion_detalle {detalle_id}) no tiene producto terminado configurado."}
                            )

                        ref = _ref_renglon(idx, origen_detalle)
                        cantidad = Decimal(str(item["cantidad_recibida"]))
                        # Defensa en profundidad: ``RecepcionOnboardingSerializer``
                        # ya rechaza esto con el mismo mensaje (EC-894).
                        if cantidad <= 0:
                            raise _error_detalle(
                                {"detalle": f"El renglón {ref} debe tener cantidad_recibida > 0."}
                            )

                        recibido = self._cantidad_recibida_op(op_detalle.pk)
                        ordered = Decimal(str(op_detalle.cantidad or 0))
                        apartado, folios_sm, _ = en_maquila.get(op_detalle.pk, (Decimal("0"), [], []))
                        pendiente = ordered - recibido - apartado
                        if pendiente_sm is not None:
                            if op_detalle.pk not in pendiente_sm:
                                raise _error_detalle(
                                    {"detalle": f"El renglón {ref} no va en la SM {sm.folio}."}
                                )
                            if pendiente_sm[op_detalle.pk] <= 0:
                                raise _error_detalle(
                                    {"detalle": f"El renglón {ref} ya se recibió completo en la SM {sm.folio}."}
                                )
                            pendiente = min(pendiente, pendiente_sm[op_detalle.pk])
                        if pendiente <= 0 and apartado > 0:
                            raise _error_detalle(
                                {
                                    "detalle": (
                                        f"El renglón {ref} tiene {apartado.normalize():f} pzas en maquila "
                                        f"({', '.join(folios_sm)}): recíbelas con su SM."
                                    )
                                }
                            )
                        if pendiente <= 0:
                            raise _error_detalle(
                                {
                                    "detalle": (
                                        f"El producto del renglón {ref} ya fue recibido completamente."
                                    )
                                }
                            )
                        # El mismo renglón puede venir varias veces (otra ubicación o lote):
                        # la suma de la petición, no cada copia, es la que se compara (#1173).
                        acumulado[op_detalle.pk] = acumulado.get(op_detalle.pk, Decimal("0")) + cantidad
                        if acumulado[op_detalle.pk] > pendiente:
                            raise _error_detalle(
                                {
                                    "detalle": (
                                        f"El renglón {ref} excede la cantidad pendiente por recibir."
                                    )
                                }
                            )
                        detalle_payload.append(
                            {
                                "orden_compra_detalle": None,
                                "orden_produccion_detalle": op_detalle,
                                "producto": producto,
                                "producto_variante": producto_variante,
                                "variante_produccion": muestra,
                                "cantidad_recibida": cantidad,
                                "ubicacion": item.get("ubicacion"),
                                "lote": item.get("lote"),
                                "serie": item.get("serie"),
                            }
                        )

                recepcion = Recepcion(
                    tipo_origen=(
                        Recepcion.TipoOrigen.ORDEN_COMPRA
                        if oc
                        else Recepcion.TipoOrigen.ORDEN_PRODUCCION
                    ),
                    orden_compra=oc,
                    op=op,
                    empresa=empresa_origen,
                    sucursal=sucursal_origen,
                    proveedor=proveedor_origen,
                    almacen=almacen,
                    transportista_id=transportista_id,
                    usuario=user,
                    fecha_recepcion=header.get("fecha_recepcion") or timezone.now(),
                    remision=(header.get("remision") or None),
                    factura_referencia=(header.get("factura_referencia") or None),
                    observaciones=(header.get("observaciones") or None),
                    estatus=Recepcion.EstatusRecepcion.BORRADOR,
                    activo=True,
                    salida_maquila=sm,
                )
                self._asignar_folio_recepcion(recepcion, serie_codigo)
                recepcion.save()
                if header.get("encuadre_rfid"):
                    self._ligar_encuadre_rfid(header["encuadre_rfid"], recepcion, empresa)

                renglones = self._crear_renglones_recepcion(recepcion, detalle_payload)
                sm_completa = None
                if sm is not None:
                    # Factura por lo que llegó; la SM queda Recibida solo al completarse (#1232).
                    facturar_maquila(sm, recepcion, renglones)
                    sm_completa = all(v <= 0 for v in pendiente_de_sm(sm).values())
                    if header.get("cerrar_salida_maquila") or sm_completa:
                        marcar_recibida(sm)

                # ``_actualizar_estatus_oc``/cierre de OP siguen basados en
                # conteo físico (lo que ya se registró arriba), sin esperar a
                # Calidad: reflejan si a la OC/OP le falta algo por RECIBIR,
                # no si ya es stock disponible.
                orden_completa = True
                detalles_origen = detalles_oc.values() if oc else detalles_op.values()
                for detalle in detalles_origen:
                    ordered = Decimal(str(detalle.cantidad or 0))
                    recibido = (
                        self._cantidad_recibida_oc(detalle.pk)
                        if oc
                        else self._cantidad_recibida_op(detalle.pk)
                    )
                    if recibido < ordered:
                        orden_completa = False
                        break

                # Toda recepción nueva pasa a Calidad — nunca entra a
                # Existencia aquí. Solo CalidadInspeccionViewSet abona
                # inventario, y únicamente por lo aprobado.
                recepcion.estatus = Recepcion.EstatusRecepcion.EN_CALIDAD
                # Alcance contra la orden de la que se recibió: la SM si vino con una.
                completa = sm_completa if sm_completa is not None else orden_completa
                recepcion.alcance_recepcion = (
                    Recepcion.AlcanceRecepcion.COMPLETA if completa else Recepcion.AlcanceRecepcion.PARCIAL
                )
                recepcion.save(update_fields=["estatus", "alcance_recepcion", "updated_at"])
                if oc:
                    self._actualizar_estatus_oc(oc)
                elif orden_completa and op and op.cerrar_orden:
                    op.estatus_op = OrdenProduccion.EstatusOrdenProduccion.COMPLETADO
                    op.fecha_fin = op.fecha_fin or timezone.now()
                    op.save(update_fields=["estatus_op", "fecha_fin"])

            detalles_recepcion = RecepcionDetalle.objects.filter(recepcion=recepcion).order_by("id")
            return Response(
                {
                    "recepcion": RecepcionSerializer(recepcion).data,
                    "detalle": RecepcionDetalleSerializer(detalles_recepcion, many=True).data,
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as exc:
            logger.warning(
                "Recepcion onboarding validation error | user=%s | superuser=%s | empresa=%s | ids=%s | detail=%s",
                getattr(user, "username", None),
                getattr(user, "is_superuser", False),
                getattr(empresa, "pk", None),
                _ids_orden_log(request.data),
                getattr(exc, "detail", exc),
            )
            raise

    # ``detail=False`` fuera de ``get_queryset()`` a propósito (EC-434): mismo
    # criterio que los demás ``kpis`` -- todo con ``aggregate()``/``Sum`` en
    # DB, nunca iterando recepciones fila por fila. Los criterios (qué es
    # comprometida, recibido neto, rechazo, diferencia de precio) viven en
    # ``compras.services.kpi_criterios``, compartidos con el KPI de proveedores.
    PARAMS_KPIS = {"sucursal", "proveedor", "fecha_desde", "fecha_hasta"}

    @extend_schema(summary="KPIs de recepciones", parameters=PARAMETROS_KPIS_RECEPCIONES, responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=["get"], url_path="kpis")
    def kpis(self, request):
        user = request.user
        validar_parametros(request, self.PARAMS_KPIS)
        fecha_desde, fecha_hasta = rango_fechas(request)
        # Mismo alcance que el listado (``get_queryset``): la empresa del
        # usuario, superusuario incluido (#834).
        empresa = getattr(user, "empresa", None)
        if empresa is None:
            return Response(self._kpis_vacio())

        alcance = AlcanceCompras(
            empresa=empresa,
            sucursal_id=entero_param(request, "sucursal"),
            proveedor_id=entero_param(request, "proveedor"),
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
        )
        data = {
            "generado_en": timezone.now(),
            # Los filtros se aplican sobre la OC (``fecha_oc`` para el periodo).
            "filtros": alcance.como_dict(),
            "cumplimiento_cantidad": self._kpi_cumplimiento_cantidad(alcance),
            "recepciones_parciales": self._kpi_recepciones_parciales(alcance),
            "diferencia_precio": self._kpi_diferencia_precio(alcance),
            "material_rechazado": self._kpi_material_rechazado(alcance),
        }
        data["puede_ver_importes"] = puede_ver_contabilidad(user)
        if not data["puede_ver_importes"]:
            data["diferencia_precio"] = SIN_PERMISO_CONTABILIDAD
            quitar_montos(data["material_rechazado"], ["valor_rechazado", "valor_linea"])
            data["material_rechazado"].pop("por_moneda", None)
        return Response(data)

    def _kpis_vacio(self):
        motivo = {"disponible": False, "motivo": "Usuario sin empresa asignada."}
        return {
            "generado_en": timezone.now(),
            "filtros": None,
            "cumplimiento_cantidad": motivo,
            "recepciones_parciales": motivo,
            "diferencia_precio": motivo,
            "material_rechazado": motivo,
            "puede_ver_importes": False,
        }

    def _kpi_cumplimiento_cantidad(self, alcance):
        # Ordenado = renglones de OCs comprometidas. Recibido = capturado en
        # recepciones de OC menos lo rechazado por Calidad (igual que el
        # estatus de la OC). Se agrupa por unidad de medida: cantidades de
        # unidades distintas no se suman (``unidad`` null = producto sin unidad).
        por_unidad = sorted(
            cumplimiento_por_unidad(alcance).values(),
            key=lambda f: (f["unidad"] is None, f["unidad"] or ""),
        )
        for fila in por_unidad:
            fila["pct"] = porcentaje(fila["cantidad_recibida"], fila["cantidad_ordenada"])
        mezcla = len(por_unidad) > 1
        ordenado = None if mezcla else (por_unidad[0]["cantidad_ordenada"] if por_unidad else 0)
        recibido = None if mezcla else (por_unidad[0]["cantidad_recibida"] if por_unidad else 0)

        pendientes = cumplimiento_pendiente_por_oc(alcance)
        drill_down = pendientes[:TOPE_DRILL_DOWN]
        return {
            "disponible": True,
            # Totales y pct solo si hay una unidad; con varias, ver ``por_unidad``.
            "mezcla_unidades": mezcla,
            "cantidad_ordenada": ordenado,
            "cantidad_recibida": recibido,
            "pct": None if mezcla else porcentaje(recibido, ordenado),
            "por_unidad": por_unidad,
            "drill_down": drill_down,
            "drill_down_total": len(pendientes),
            "drill_down_truncado": len(pendientes) > len(drill_down),
        }

    def _kpi_recepciones_parciales(self, alcance):
        # % de OCs que ya recibieron algo (parcial o recibida) que se
        # quedaron en parcial -- no cuenta las que nunca han recibido nada.
        agg = OrdenCompra.objects.filter(
            alcance.oc(),
            estatus__in=[
                OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
                OrdenCompra.EstatusOrdenCompra.RECIBIDA,
            ],
        ).aggregate(
            parciales=Count("id", filter=Q(estatus=OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA)),
            total=Count("id"),
        )
        total = agg["total"] or 0
        parciales = agg["parciales"] or 0
        pct = round(parciales / total * 100, 1) if total else None
        return {
            "disponible": True,
            "ocs_parciales": parciales,
            "ocs_recibidas_o_parciales": total,
            "pct": pct,
        }

    def _kpi_diferencia_precio(self, alcance):
        # Costo facturado (FacturaProveedorDetalle.precio_unitario) vs. costo
        # pactado en la OC (OrdenCompraDetalle.precio), línea por línea vía
        # el FK ``oc_detalle``. Solo líneas cuya factura está en la moneda de
        # su OC; el resultado va por moneda, nunca sumado entre monedas.
        lineas = con_importes_precio(lineas_factura_proveedor(alcance))
        comparables = lineas.filter(misma_moneda())
        moneda = "factura_proveedor__moneda__codigo_iso"

        por_moneda = []
        for fila in (
            comparables.values(moneda)
            .annotate(facturado=Sum("facturado_linea"), pactado=Sum("pactado_linea"), lineas=Count("id"))
            .order_by(moneda)
        ):
            facturado = fila["facturado"] or Decimal("0")
            pactado = fila["pactado"] or Decimal("0")
            por_moneda.append({
                "moneda": fila[moneda],
                "lineas_evaluadas": fila["lineas"],
                "costo_facturado": facturado,
                "costo_pactado_oc": pactado,
                "diferencia": facturado - pactado,
                "pct": porcentaje(facturado - pactado, pactado),
            })

        con_diferencia = (
            comparables.exclude(facturado_linea=F("pactado_linea"))
            .annotate(diferencia_linea=ExpressionWrapper(F("facturado_linea") - F("pactado_linea"), output_field=MONEY))
            .order_by("-diferencia_linea", "-id")
        )
        total_diferencias = con_diferencia.count()
        drill_down = [
            {
                "id": fila["id"],
                "factura_id": fila["factura_proveedor_id"],
                "factura_folio": fila["factura_proveedor__folio"],
                "producto": fila["oc_detalle__producto__nombre"],
                "moneda": fila[moneda],
                "precio_facturado": fila["precio_unitario"],
                "precio_pactado": fila["oc_detalle__precio"],
                "diferencia_linea": fila["diferencia_linea"],
            }
            for fila in con_diferencia.values(
                "id", "factura_proveedor_id", "factura_proveedor__folio", "oc_detalle__producto__nombre",
                moneda, "precio_unitario", "oc_detalle__precio", "diferencia_linea",
            )[:TOPE_DRILL_DOWN]
        ]
        return {
            "disponible": True,
            # 0 = no hubo líneas que evaluar (distinto de diferencia 0).
            "lineas_evaluadas": sum(f["lineas_evaluadas"] for f in por_moneda),
            # Líneas cuya factura y OC están en monedas distintas: no se comparan.
            "lineas_moneda_distinta": lineas.exclude(misma_moneda()).count(),
            "por_moneda": por_moneda,
            "drill_down": drill_down,
            "drill_down_total": total_diferencias,
            "drill_down_truncado": total_diferencias > len(drill_down),
        }

    def _kpi_material_rechazado(self, alcance):
        # Rechazos de Calidad (``resultado = rechazo``) de recepciones de OC,
        # activas y no canceladas. Cantidad por unidad de medida; valor (a
        # precio pactado en la OC) por moneda de la OC.
        rechazos = rechazos_oc(alcance)
        unidad = "recepcion_detalle__orden_compra_detalle__producto__unidad_medida__clave"
        unidad_id = "recepcion_detalle__orden_compra_detalle__producto__unidad_medida_id"
        moneda = "recepcion_detalle__orden_compra_detalle__orden_compra__moneda__codigo_iso"

        por_unidad = [
            {"unidad_id": fila[unidad_id], "unidad": fila[unidad], "cantidad_rechazada": fila["t"] or Decimal("0")}
            for fila in rechazos.values(unidad_id, unidad).annotate(t=Sum("cantidad_rechazada")).order_by(unidad)
        ]
        valor = ExpressionWrapper(
            F("cantidad_rechazada") * F("recepcion_detalle__orden_compra_detalle__precio"), output_field=MONEY
        )
        por_moneda = [
            {"moneda": fila[moneda], "valor_rechazado": fila["t"] or Decimal("0")}
            for fila in rechazos.annotate(valor_linea=valor).values(moneda)
            .annotate(t=Sum("valor_linea")).order_by(moneda)
        ]
        mezcla = len(por_unidad) > 1

        filas = list(
            rechazos.annotate(valor_linea=valor)
            .order_by("-cantidad_rechazada", "-id")
            .values(
                "id", "recepcion_detalle__recepcion_id", "recepcion_detalle__recepcion__folio",
                unidad, moneda, "cantidad_rechazada", "valor_linea", "motivo_rechazo",
                articulo=Coalesce("recepcion_detalle__producto__nombre", "recepcion_detalle__variante_produccion__nombre"),
            )[:TOPE_DRILL_DOWN]
        )
        drill_down = [
            {
                "id": fila["id"],
                "recepcion_id": fila["recepcion_detalle__recepcion_id"],
                "recepcion_folio": fila["recepcion_detalle__recepcion__folio"],
                "producto": fila["articulo"],
                "unidad": fila[unidad],
                "moneda": fila[moneda],
                "cantidad_rechazada": fila["cantidad_rechazada"],
                "valor_linea": fila["valor_linea"],
                "motivo_rechazo": fila["motivo_rechazo"],
            }
            for fila in filas
        ]
        total = rechazos.count()
        return {
            "disponible": True,
            "mezcla_unidades": mezcla,
            "cantidad_rechazada": None if mezcla else (por_unidad[0]["cantidad_rechazada"] if por_unidad else Decimal("0")),
            "por_unidad": por_unidad,
            "por_moneda": por_moneda,
            "drill_down": drill_down,
            "drill_down_total": total,
            "drill_down_truncado": total > len(drill_down),
        }


class CalidadInspeccionViewSet(viewsets.ReadOnlyModelViewSet):
    """Filtro de calidad entre Recepción y Existencia.

    Una ``Recepcion`` sale de ``onboarding()`` en ``EN_CALIDAD`` sin haber
    tocado inventario. Aquí se inspecciona TODO su detalle en un solo envío
    (no hay inspección parcial/por rondas en esta primera versión) y solo lo
    aprobado —``cantidad_aprobada`` por renglón, sea el resultado
    ``liberado``, ``concesion_cc`` o ``concesion lazzar``— abona
    ``Existencia`` y genera ``MovimientoInventario``. Lo marcado ``rechazo``
    o ``cuarentena`` no entra a almacén; qué pasa después con esa cantidad no
    está definido todavía (ver DOCS/arquitectura/flujo-recepcion-calidad-compras.md).
    """

    queryset = CalidadInspeccion.objects.all().select_related(
        "recepcion", "inspector", "anulada_por",
        *(f"recepcion__{r}" for r in RELACIONES_PEDIDO),  # pedido(s) de cada recepción
    )
    serializer_class = CalidadInspeccionSerializer
    http_method_names = ["get", "post"]
    # #1411: como OC, recepciones y devoluciones. Sin ``?limit=`` la lista completa
    # (compatible con el front actual); con él, ``{count, next, previous, results}``.
    pagination_class = PaginacionOpcionalLimitOffset
    search_fields = [
        "recepcion__folio", "recepcion__orden_compra__folio", "recepcion__proveedor__nombre",
        "recepcion__proveedor__razon_social",
    ]
    filter_backends = [
        *[b for b in viewsets.ReadOnlyModelViewSet.filter_backends if b is not OrderingFilter],
        OrdenamientoEstableFilter,
    ]
    ordering_fields = ["id", "fecha", "estado", "recepcion__folio"]

    def list(self, request, *args, **kwargs):
        # ``pedidos`` de todas las recepciones de la página en bloque (2 consultas).
        qs = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(qs)
        objs = list(page if page is not None else qs)
        contexto = {**self.get_serializer_context(),
                    "pedidos_por_recepcion": pedidos_de_recepciones([o.recepcion for o in objs])}
        data = self.get_serializer_class()(objs, many=True, context=contexto).data
        return self.get_paginated_response(data) if page is not None else Response(data)

    def get_queryset(self):
        user = self.request.user
        # Producto del renglón en el mismo prefetch: sin él, cada detalle
        # disparaba 2 consultas (recepcion_detalle y su producto).
        qs = self.queryset.prefetch_related(
            Prefetch(
                "detalles",
                queryset=CalidadInspeccionDetalle.objects.select_related(
                    "recepcion_detalle__producto", "recepcion_detalle__variante_produccion"
                ),
            )
        )
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        # EC-973: misma regla que ``devoluciones-proveedor/pendientes/``, en la misma
        # consulta (EXISTS por inspección, sin N+1). La anulada sale ``false``.
        qs = qs.filter(recepcion__empresa=empresa).annotate(
            tiene_devolucion_pendiente=Exists(
                devolucion_proveedor.renglones_devolvibles().filter(
                    calidad_inspeccion=OuterRef("pk"), cantidad_pendiente__gt=0,
                )
            )
        )
        # Solo en el listado: en el detalle o una acción un filtro suelto daría 404.
        if self.action == "list":
            pendiente = booleano_param(self.request, "devolucion_pendiente", None)
            if pendiente is not None:
                qs = qs.filter(tiene_devolucion_pendiente=pendiente)
        return qs.order_by("-fecha", "-id")

    def _abonar_renglon(self, recepcion, renglon, cantidad_aprobada):
        existencia = (
            Existencia.objects.select_for_update()
            .filter(
                producto_id=renglon.producto_id,
                producto_variante_id=renglon.producto_variante_id,
                variante_produccion_id=renglon.variante_produccion_id,
                almacen_id=recepcion.almacen_id,
                ubicacion_id=renglon.ubicacion_id,
            )
            .order_by("id")
            .first()
        )
        if not existencia:
            existencia = Existencia.objects.create(
                producto_id=renglon.producto_id,
                producto_variante_id=renglon.producto_variante_id,
                variante_produccion_id=renglon.variante_produccion_id,
                almacen=recepcion.almacen,
                ubicacion_id=renglon.ubicacion_id,
                stock=0,
                cantidad=Decimal("0"),
            )

        cantidad_antes = existencia.cantidad or Decimal("0")
        cantidad_despues = cantidad_antes + cantidad_aprobada
        existencia.cantidad = cantidad_despues
        try:
            existencia.stock = int(cantidad_despues)
        except Exception:
            existencia.stock = existencia.stock or 0
        existencia.save(update_fields=["cantidad", "stock", "fecha_actualizacion"])

        return {
            "recepcion_detalle_id": renglon.pk,
            "orden_compra_detalle_id": renglon.orden_compra_detalle_id,
            "orden_produccion_detalle_id": renglon.orden_produccion_detalle_id,
            "producto_id": renglon.producto_id,
            "producto_variante_id": renglon.producto_variante_id,
            "variante_produccion_id": renglon.variante_produccion_id,
            "ubicacion_id": renglon.ubicacion_id,
            "lote_id": renglon.lote_id,
            "serie_id": renglon.serie_id,
            "cantidad_before": str(cantidad_antes),
            "cantidad_after": str(cantidad_despues),
            "delta": str(cantidad_aprobada),
        }

    def _crear_movimiento_formal(self, recepcion, movimientos):
        movimiento = MovimientoInventario.objects.create(
            empresa=recepcion.empresa,
            sucursal=recepcion.sucursal,
            pedido_id=None,
            entrega_id=None,
            devolucion_id=None,
            ajuste_inventario_id=None,
            tipo_movimiento="ENTRADA",
            usuario=recepcion.usuario,
            observaciones=recepcion.observaciones,
            recepcion=recepcion,
            transferencia_id=None,
            op_id=recepcion.op_id,
        )
        costos = self._costos_de_entrada(recepcion, movimientos)
        renglones = []
        for item in movimientos:
            detalle = MovimientoInventarioDetalle.objects.create(
                movimiento_inventario=movimiento,
                producto_id=item["producto_id"],
                producto_variante_id=item["producto_variante_id"],
                variante_produccion_id=item.get("variante_produccion_id"),
                ubicacion_origen_id=None,
                ubicacion_destino_id=item["ubicacion_id"],
                lote_id=item.get("lote_id"),
                serie_id=item.get("serie_id"),
                cantidad=Decimal(str(item["delta"] or 0)),
                costo_unitario=Decimal("0"),
                cantidad_antes=Decimal(item["cantidad_before"]),
                cantidad_despues=Decimal(item["cantidad_after"]),
            )
            renglones.append((detalle, detalle.cantidad, costos.get(item["recepcion_detalle_id"])))
        # Entrada valorada: recalcula el costo promedio y deja historial (#809).
        origen = costeo.Origen.PRODUCCION if recepcion.op_id else costeo.Origen.RECEPCION_OC
        costeo.costear(recepcion.empresa_id, renglones, origen)
        return movimiento

    def _costos_de_entrada(self, recepcion, movimientos):
        """Costo unitario en MXN por renglón de recepción (#809): contra OC, el
        importe del renglón de la OC entre su cantidad, por el tipo de cambio si no
        es MXN (sin tipo de cambio entra sin valor); contra OP, materiales + maquila."""
        if recepcion.op_id:
            from produccion.services.costo_produccion import costo_unitario_produccion

            costo = costo_unitario_produccion(recepcion.op)
            return {m["recepcion_detalle_id"]: costo for m in movimientos}
        ids = {m["orden_compra_detalle_id"] for m in movimientos if m.get("orden_compra_detalle_id")}
        lineas = {
            d.pk: d
            for d in OrdenCompraDetalle.objects.filter(pk__in=ids).select_related("orden_compra__moneda")
        }
        costos = {}
        for m in movimientos:
            linea = lineas.get(m.get("orden_compra_detalle_id"))
            if linea is None:
                continue
            unitario = (linea.importe / linea.cantidad) if linea.cantidad else linea.precio
            oc = linea.orden_compra
            if (oc.moneda.codigo_iso or "").upper() != "MXN":
                unitario = unitario * oc.tipo_cambio if oc.tipo_cambio else None
            costos[m["recepcion_detalle_id"]] = unitario
        return costos

    @action(detail=True, methods=["post"], url_path="revertir")
    def revertir(self, request, pk=None):
        """Anula una inspección liberada por error: saca del almacén lo que entró
        (SALIDA ligada a la recepción, al costo con que entró), la inspección queda
        ``anulada`` y la recepción vuelve a En calidad para inspeccionarla de nuevo
        o cancelarla. Solo admin de empresa o superusuario, con ``motivo``."""
        user = request.user
        if not puede_cancelar_recepciones(user):
            raise PermissionDenied("Solo un administrador de la empresa puede revertir calidad.")
        motivo = request.data.get("motivo") if hasattr(request.data, "get") else None
        if not isinstance(motivo, str) or not motivo.strip():
            raise ValidationError({"motivo": "Indica el motivo de la reversa."})
        motivo = exigir_largo_motivo(motivo.strip())
        inspeccion = self.get_object()  # otra empresa: 404

        with transaction.atomic():
            recepcion = Recepcion.objects.select_for_update().select_related("empresa", "sucursal", "almacen").get(
                pk=inspeccion.recepcion_id
            )
            oc = OrdenCompra.objects.select_for_update().get(pk=recepcion.orden_compra_id) if recepcion.orden_compra_id else None
            inspeccion = CalidadInspeccion.objects.select_for_update().get(pk=inspeccion.pk)
            inspeccion.recepcion = recepcion  # la bitácora lee empresa/sucursal de aquí
            if inspeccion.estado == CalidadInspeccion.ESTADO_ANULADA:
                raise ValidationError({"estado": "La inspección ya está anulada."})
            if recepcion.estatus != Recepcion.EstatusRecepcion.CERRADA or not recepcion.activo:
                raise ValidationError({"recepcion": "La recepción no está cerrada por calidad."})
            if oc is not None and oc.saldo_cerrado:
                raise ValidationError({"orden_compra": "La OC ya tiene el saldo cerrado; no se puede revertir su calidad."})
            if recepcion.op_id and recepcion.op.estatus_op == OrdenProduccion.EstatusOrdenProduccion.COMPLETADO:
                raise ValidationError({"op": f"La OP {recepcion.op.folio_op} ya está completada."})
            if FacturaProveedor.objects.filter(recepcion=recepcion).exclude(
                estatus=FacturaProveedor.FacturaProveedorStatus.CANCELADA
            ).exists():
                raise ValidationError({"facturas_proveedores": (
                    "La recepción tiene una factura de proveedor sin cancelar: cancélala primero en Finanzas."
                )})
            # Lo ya reclamado al proveedor se volvería a reclamar tras reinspeccionar (#1372).
            # Crear una devolución bloquea esta misma inspección: no hay carrera.
            if DevolucionProveedor.objects.filter(
                calidad_inspeccion=inspeccion, estatus=DevolucionProveedor.Estatus.APLICADA
            ).exists():
                raise ValidationError({"devoluciones_proveedor": (
                    "La inspección tiene devoluciones a proveedor aplicadas: cancélalas primero."
                )})

            entrada = (
                MovimientoInventario.objects.filter(recepcion=recepcion, tipo_movimiento="ENTRADA")
                .order_by("-pk").first()
            )
            items, renglones = [], []
            salida = None
            if entrada is not None:
                detalles = list(MovimientoInventarioDetalle.objects.filter(movimiento_inventario=entrada).order_by("pk"))
                filas = {}
                for d in detalles:
                    llave = (d.producto_id, d.producto_variante_id, d.variante_produccion_id, d.ubicacion_destino_id)
                    if llave not in filas:
                        filas[llave] = (
                            Existencia.objects.select_for_update().filter(
                                producto_id=d.producto_id, producto_variante_id=d.producto_variante_id,
                                variante_produccion_id=d.variante_produccion_id,
                                almacen_id=recepcion.almacen_id, ubicacion_id=d.ubicacion_destino_id,
                            ).order_by("id").first()
                        )
                    ex = filas[llave]
                    antes = Decimal(str(getattr(ex, "cantidad", 0) or 0))
                    if ex is None or antes < d.cantidad:
                        raise ValidationError({"existencia": (
                            f"Ya no hay existencia suficiente para revertir (artículo {d.producto_id}: "
                            f"{antes.normalize():f} en almacén, entraron {d.cantidad.normalize():f}). "
                            "Ya se surtió o consumió; haz un ajuste de inventario."
                        )})
                    ex.cantidad = antes - d.cantidad
                    ex.stock = int(ex.cantidad)
                    ex.save(update_fields=["cantidad", "stock", "fecha_actualizacion"])
                    renglones.append((d, antes, ex.cantidad))
                    items.append({
                        "producto_id": d.producto_id, "producto_variante_id": d.producto_variante_id,
                        "variante_produccion_id": d.variante_produccion_id, "ubicacion_id": d.ubicacion_destino_id,
                        "almacen_id": recepcion.almacen_id,
                        "cantidad_before": str(antes), "cantidad_after": str(ex.cantidad),
                        "delta": str(-d.cantidad),
                    })
                salida = MovimientoInventario.objects.create(
                    empresa=recepcion.empresa, sucursal=recepcion.sucursal, tipo_movimiento="SALIDA",
                    usuario=user if getattr(user, "pk", None) else recepcion.usuario,
                    observaciones=f"Reversa de calidad {inspeccion.pk}: {motivo}"[:150],
                    recepcion=recepcion, op_id=recepcion.op_id,
                )
                nuevos = [
                    MovimientoInventarioDetalle(
                        movimiento_inventario=salida, producto_id=d.producto_id,
                        producto_variante_id=d.producto_variante_id, variante_produccion_id=d.variante_produccion_id,
                        ubicacion_origen_id=d.ubicacion_destino_id, cantidad=d.cantidad, costo_unitario=d.costo_unitario,
                        cantidad_antes=antes, cantidad_despues=despues,
                    )
                    for d, antes, despues in renglones
                ]
                MovimientoInventarioDetalle.objects.bulk_create(nuevos)
                # Sale al costo con que entró; el promedio no se mueve por una salida.
                costeo.costear(recepcion.empresa_id, [(n, -n.cantidad, None) for n in nuevos], costeo.Origen.OPERACION)
                # Sin apartado, igual que el consumo de una OP: una pre-orden Lista de
                # la sucursal puede quedarse sin el material que salió (#1375).
                from produccion.services.pre_orden_service import recalcular_listas
                motivo_regreso = f"Se revirtió la calidad de la recepción {recepcion.folio or recepcion.pk}."
                transaction.on_commit(lambda: recalcular_listas(
                    recepcion.empresa_id, recepcion.sucursal_id, recepcion.op, motivo=motivo_regreso,
                ))

            inspeccion.estado = CalidadInspeccion.ESTADO_ANULADA
            inspeccion.motivo_anulacion, inspeccion.fecha_anulacion = motivo, timezone.now()
            inspeccion.anulada_por = user if getattr(user, "pk", None) else None
            inspeccion.save(update_fields=["estado", "motivo_anulacion", "fecha_anulacion", "anulada_por"])
            recepcion.estatus = Recepcion.EstatusRecepcion.EN_CALIDAD
            recepcion._change_reason = f"Reversa de calidad: {motivo}"[:100]
            recepcion.save(update_fields=["estatus", "updated_at"])
            recepcion_vs = RecepcionViewSet()
            if oc is not None:
                recepcion_vs._actualizar_estatus_oc(oc)
            elif recepcion.op_id:
                recepcion_vs._actualizar_estatus_op(recepcion.op)
            ev = None
            if items:
                ev = AuditoriaEvento.objects.create(
                    empresa=recepcion.empresa, usuario=user if getattr(user, "pk", None) else None,
                    modulo="inventarios", accion="SALIDA", tabla="existencias",
                    id_registro=str(recepcion.almacen_id),
                    antes_json={"items": items, "recepcion_id": recepcion.pk, "calidad_inspeccion_id": inspeccion.pk},
                    despues_json={
                        "almacen_id": recepcion.almacen_id, "sucursal_id": recepcion.sucursal_id,
                        "empresa_id": recepcion.empresa_id, "recepcion_id": recepcion.pk,
                        "calidad_inspeccion_id": inspeccion.pk, "reversa_calidad": True, "motivo": motivo,
                        "items": items,
                    },
                    ip=_ip_auditoria(request), user_agent=request.META.get("HTTP_USER_AGENT"),
                )
            AuditoriaEvento.objects.create(
                empresa=recepcion.empresa, usuario=user if getattr(user, "pk", None) else None,
                modulo="compras", accion="REVERTIR", tabla=CalidadInspeccion._meta.db_table,
                id_registro=str(inspeccion.pk),
                antes_json={"estado": "liberada", "recepcion_estatus": Recepcion.EstatusRecepcion.CERRADA},
                despues_json={"estado": inspeccion.estado, "motivo": motivo, "recepcion_id": recepcion.pk,
                              "movimiento_inventario_id": getattr(salida, "pk", None)},
                ip=_ip_auditoria(request), user_agent=request.META.get("HTTP_USER_AGENT"),
            )

        return Response({
            "calidad_inspeccion": CalidadInspeccionSerializer(self.get_queryset().get(pk=inspeccion.pk)).data,
            "recepcion_estatus": recepcion.estatus,
            "orden_compra_estatus": getattr(oc, "estatus", None),
            "movimiento_id": ev.id_evento if ev else None,
            "movimiento_inventario_id": getattr(salida, "pk", None),
        }, status=status.HTTP_200_OK)

    def _derivar_estado(self, resultados):
        aprobados = {"liberado", "concesion_cc", "concesion lazzar"}
        if all(r in aprobados for r in resultados):
            return "aprobada"
        if all(r not in aprobados for r in resultados):
            return "rechazada"
        return "aprobada_condicion"

    def handle_get_onboarding(self, request):
        user = request.user
        empresa = getattr(user, "empresa", None)
        # Superusuario incluido, igual que recepciones/onboarding (#875).
        if not empresa:
            raise ValidationError({"empresa": "El usuario no tiene empresa asignada."})

        # Renglones en un solo prefetch: antes era una consulta por recepción.
        recepciones_qs = (
            Recepcion.objects.filter(
                activo=True, estatus=Recepcion.EstatusRecepcion.EN_CALIDAD, empresa=empresa,
            )
            .select_related("proveedor", "almacen", *RELACIONES_PEDIDO)
            .prefetch_related(
                Prefetch(
                    "recepciondetalle_set",
                    queryset=RecepcionDetalle.objects.select_related("producto", "variante_produccion").order_by("id"),
                )
            )
            .order_by("-fecha_recepcion", "-id")
        )

        recepciones = []
        pendientes = list(recepciones_qs[:50])
        pedidos = pedidos_de_recepciones(pendientes)
        for r in pendientes:
            detalles = r.recepciondetalle_set.all()
            lista = pedidos.get(r.pk, [])
            recepciones.append(
                {
                    "id": r.pk,
                    "folio": r.folio,
                    "tipo_origen": r.tipo_origen,
                    "origen_folio": origen_folio(r),
                    # Pedido maestro (el principal) y todos si la OC/OP surte varios.
                    "pedido_id": lista[0]["id"] if lista else None,
                    "pedido_folio": lista[0]["folio"] if lista else None,
                    "pedidos": lista,
                    "proveedor_nombre": getattr(r.proveedor, "nombre", None),
                    "almacen_id": r.almacen_id,
                    "almacen_nombre": r.almacen.nombre,
                    "fecha_recepcion": r.fecha_recepcion,
                    "detalle": [
                        {
                            "id": d.pk,
                            "producto_id": d.producto_id,
                            "variante_produccion_id": d.variante_produccion_id,
                            "producto_nombre": d.articulo_nombre,
                            "cantidad_recibida": str(d.cantidad_recibida),
                        }
                        for d in detalles
                    ],
                }
            )

        empleados_qs = Empleado.objects.filter(activo=True, empresa=empresa)
        inspectores = list(
            empleados_qs.order_by("nombre").values(
                "id", "numero_empleado", "nombre", "apellido_paterno"
            )[:200]
        )

        return {
            "empresa_id": getattr(empresa, "pk", None),
            "recepciones_pendientes": recepciones,
            "inspectores": inspectores,
        }

    @action(detail=False, methods=["get", "post"], url_path="onboarding")
    def onboarding(self, request):
        if request.method.lower() == "get":
            return Response(self.handle_get_onboarding(request))

        user = request.user
        empresa = getattr(user, "empresa", None)
        # Sin empresa se saltaban la pertenencia de la recepción y del inspector (#875).
        if not empresa:
            raise ValidationError({"empresa": "El usuario no tiene empresa asignada."})
        # Liberar calidad es lo que mete la mercancía al inventario.
        _exigir_permiso_oc(request, PERMISO_CREAR_RECEPCIONES)

        serializer = CalidadInspeccionInputSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            recepcion = (
                Recepcion.objects.select_for_update()
                .select_related("empresa", "sucursal", "almacen", "usuario")
                .filter(pk=data["recepcion"], activo=True)
                .first()
            )
            if not recepcion:
                raise ValidationError({"recepcion": "Recepción no encontrada."})
            if recepcion.empresa_id != empresa.pk:
                raise ValidationError({"recepcion": "No tienes acceso a esta recepción."})
            if recepcion.estatus != Recepcion.EstatusRecepcion.EN_CALIDAD:
                raise ValidationError({"estatus": "La recepción no está en espera de calidad."})

            inspector = Empleado.objects.filter(pk=data["inspector"], activo=True).first()
            if not inspector:
                raise ValidationError({"inspector": "Empleado no encontrado."})
            if inspector.empresa_id != empresa.pk:
                raise ValidationError({"inspector": "El inspector no pertenece a esta empresa."})

            renglones = {
                r.pk: r
                for r in RecepcionDetalle.objects.filter(recepcion=recepcion).select_related(
                    "producto", "producto_variante"
                )
            }
            if not renglones:
                raise ValidationError({"detalle": "La recepción no tiene renglones que inspeccionar."})

            ya_inspeccionados = CalidadInspeccionDetalle.objects.filter(
                recepcion_detalle_id__in=renglones.keys()
            ).exclude(calidad_inspeccion__estado=CalidadInspeccion.ESTADO_ANULADA).exists()
            if ya_inspeccionados:
                raise ValidationError({"detalle": "Esta recepción ya tiene renglones inspeccionados."})

            detalle_in = data["detalle"]
            ids_enviados = {it["recepcion_detalle"] for it in detalle_in}
            if ids_enviados != set(renglones.keys()):
                raise ValidationError(
                    {"detalle": "Debes inspeccionar todos los renglones de la recepción en un solo envío."}
                )

            for it in detalle_in:
                renglon = renglones[it["recepcion_detalle"]]
                total = it["cantidad_aprobada"] + it["cantidad_rechazada"]
                if total <= 0:
                    raise ValidationError(
                        {"detalle": f"El renglón {renglon.pk} necesita cantidad_aprobada o cantidad_rechazada mayor a 0."}
                    )
                if total > renglon.cantidad_recibida:
                    raise ValidationError(
                        {"detalle": f"El renglón {renglon.pk} inspecciona más de lo recibido."}
                    )

            inspeccion = CalidadInspeccion.objects.create(
                recepcion=recepcion,
                inspector=inspector,
                fecha=data.get("fecha") or timezone.localdate(),
                estado="pendiente",
                observaciones=data.get("observaciones") or None,
            )

            movimientos = []
            resultados = []
            for it in detalle_in:
                renglon = renglones[it["recepcion_detalle"]]
                CalidadInspeccionDetalle.objects.create(
                    calidad_inspeccion=inspeccion,
                    recepcion_detalle=renglon,
                    cantidad_inspeccionada=it["cantidad_aprobada"] + it["cantidad_rechazada"],
                    cantidad_aprobada=it["cantidad_aprobada"],
                    cantidad_rechazada=it["cantidad_rechazada"],
                    resultado=it["resultado"],
                    motivo_rechazo=it.get("motivo_rechazo") or None,
                )
                resultados.append(it["resultado"])
                if it["cantidad_aprobada"] > 0:
                    movimientos.append(
                        self._abonar_renglon(recepcion, renglon, it["cantidad_aprobada"])
                    )

            inspeccion.estado = self._derivar_estado(resultados)
            inspeccion.save(update_fields=["estado"])

            movimiento_formal = None
            if movimientos:
                movimiento_formal = self._crear_movimiento_formal(recepcion, movimientos)

            recepcion.estatus = Recepcion.EstatusRecepcion.CERRADA
            recepcion.save(update_fields=["estatus", "updated_at"])

            # Un rechazo puede reabrir pendiente en una OC/OP que ya se había
            # marcado recibida/completa por otra Recepcion previa (ver
            # DOCS/arquitectura/flujo-recepcion-calidad-compras.md).
            recepcion_vs = RecepcionViewSet()
            if recepcion.tipo_origen == Recepcion.TipoOrigen.ORDEN_COMPRA and recepcion.orden_compra_id:
                recepcion_vs._actualizar_estatus_oc(recepcion.orden_compra)
            elif recepcion.tipo_origen == Recepcion.TipoOrigen.ORDEN_PRODUCCION and recepcion.op_id:
                recepcion_vs._actualizar_estatus_op(recepcion.op)

            ev = None
            if movimientos:
                ev = AuditoriaEvento.objects.create(
                    empresa=recepcion.empresa,
                    usuario=user if getattr(user, "pk", None) else None,
                    modulo="inventarios",
                    accion="ENTRADA",
                    tabla="existencias",
                    id_registro=str(recepcion.almacen_id),
                    antes_json={
                        "items": movimientos,
                        "recepcion_id": recepcion.pk,
                        "calidad_inspeccion_id": inspeccion.pk,
                    },
                    despues_json={
                        "almacen_id": recepcion.almacen_id,
                        "sucursal_id": recepcion.sucursal_id,
                        "empresa_id": recepcion.empresa_id,
                        "recepcion_id": recepcion.pk,
                        "calidad_inspeccion_id": inspeccion.pk,
                        "items": movimientos,
                    },
                    ip=_ip_auditoria(request),
                    user_agent=request.META.get("HTTP_USER_AGENT"),
                )

        detalles_out = inspeccion.detalles.select_related(
            "recepcion_detalle__producto", "recepcion_detalle__variante_produccion"
        ).order_by("id")
        return Response(
            {
                "calidad_inspeccion": CalidadInspeccionSerializer(inspeccion).data,
                "movimiento_id": ev.id_evento if ev else None,
                "movimiento_inventario_id": movimiento_formal.pk if movimiento_formal else None,
            },
            status=status.HTTP_200_OK,
        )


def _resolver_tag_rfid(encuadre, codigo_tag):
    """Resuelve un ``codigo_tag`` (obsoleto, EC-982) por sku/codigo/cod_proscai
    contra la OC u OP del encuadre.

    Parte el texto por espacios y comas y busca con cualquiera de las partes
    (así el frontend mandaba ``"EPC SKU"``). Si varias partes coinciden, gana
    la de ``id`` menor (``first()`` ya ordena por pk sin ``Meta.ordering``; el
    ``order_by`` lo deja explícito).
    """
    tokens = [t.strip() for t in codigo_tag.replace(",", " ").split() if t.strip()] or [codigo_tag]

    producto_variante = (
        ProductoVariante.objects.select_related("producto")
        .filter(empresa=encuadre.empresa, activo=True, sku__in=tokens)
        .order_by("pk")
        .first()
    )
    producto = producto_variante.producto if producto_variante else (
        Producto.objects.filter(empresa=encuadre.empresa, activo=True)
        .filter(Q(codigo__in=tokens) | Q(cod_proscai__in=tokens))
        .order_by("pk")
        .first()
    )
    return {
        **_renglon_tag_rfid(encuadre, producto, producto_variante),
        "codigo_tag": codigo_tag,
        "etiqueta": None,
        "metadata": {"resolved": bool(producto), "tokens": tokens, "source": "API-RFID"},
        "repetido": Q(codigo_tag=codigo_tag),
    }


def _resolver_epc_rfid(encuadre, epc):
    """Resuelve un EPC (EC-982) por la etiqueta que imprimió el ERP con él.

    Solo etiquetas de la empresa del encuadre (``etiquetas_por_epc``, que también
    acepta el EPC con relleno o sin ceros); un EPC de otra empresa es igual a uno
    desconocido. Sin filtro de sucursal. Etiqueta con variante: variante y
    producto salen de la impresión; solo con producto, la regla por producto de
    siempre. ``codigo_tag`` = el EPC de la etiqueta (forma canónica) o, si no
    está registrado, el EPC normalizado. ``repetido`` = filtro de las lecturas del
    encuadre que ya son esta pieza: cualquier forma equivalente del EPC (con o
    sin relleno, leído antes o después de registrar la impresión) o la misma
    etiqueta.
    """
    epc_normalizado = normalizar_epc(epc)
    etiqueta = etiquetas_por_epc([epc_normalizado], encuadre.empresa_id, con_producto=True).get(epc_normalizado)
    producto = producto_variante = None
    if etiqueta is not None:
        producto_variante = etiqueta.impresion.producto_variante
        producto = producto_variante.producto if producto_variante else etiqueta.impresion.producto
    metadata = {"resolved": bool(producto), "source": RecepcionRFIDLectura.FUENTE_EPC, "epc_leido": epc_normalizado}
    if etiqueta is None:
        metadata["motivo"] = "epc_no_registrado"
    elif producto is None:
        metadata["motivo"] = "etiqueta_sin_producto"
    codigo_tag = etiqueta.epc if etiqueta is not None else epc_normalizado
    repetido = Q(codigo_tag__in=formas_equivalentes_epc(epc_normalizado) | formas_equivalentes_epc(codigo_tag))
    if etiqueta is not None:
        repetido |= Q(etiqueta=etiqueta)
    return {
        **_renglon_tag_rfid(encuadre, producto, producto_variante),
        "codigo_tag": codigo_tag,
        "etiqueta": etiqueta,
        "metadata": metadata,
        "repetido": repetido,
    }


def _renglon_tag_rfid(encuadre, producto, producto_variante):
    """Renglón de la OC u OP del encuadre para un producto/variante ya resuelto."""
    orden_compra_detalle = None
    orden_produccion_detalle = None
    if producto:
        # Con variante resuelta, el renglón de esa variante; por producto solo
        # los renglones sin variante (MP u OC legadas), para no cargar una
        # talla en el renglón de otra (#794). Tag por código de producto (sin
        # variante): el primer renglón del producto, como antes.
        if encuadre.orden_compra_id:
            renglones = OrdenCompraDetalle.objects.filter(
                orden_compra_id=encuadre.orden_compra_id, producto_id=producto.pk
            ).order_by("id")
            if producto_variante:
                orden_compra_detalle = (
                    renglones.filter(producto_variante=producto_variante).first()
                    or renglones.filter(producto_variante__isnull=True).first()
                )
            else:
                orden_compra_detalle = renglones.first()
        elif encuadre.op_id:
            renglones = OrdenProduccionDetalle.objects.filter(
                op_id=encuadre.op_id, activo=True, producto_variante__producto_id=producto.pk
            ).order_by("op_detalle_id")
            if producto_variante:
                renglones = renglones.filter(producto_variante=producto_variante)
            orden_produccion_detalle = renglones.first()

    return {
        "producto": producto,
        "producto_variante": producto_variante,
        "orden_compra_detalle": orden_compra_detalle,
        "orden_produccion_detalle": orden_produccion_detalle,
    }


def _resolver_epcs_rfid(encuadre, epcs):
    """``_resolver_epc_rfid`` para una tanda (EC-998), en dos consultas fijas:
    las etiquetas de todos los EPC y los renglones de la orden de sus productos.
    Mismas reglas pieza por pieza (etiqueta de la empresa del encuadre, renglón
    de la variante o, si no hay, el del producto sin variante; ``metadata``
    igual). Devuelve una pieza por EPC, en el orden recibido, con ``formas``: las
    formas equivalentes de su EPC, para detectar la misma pieza leída antes."""
    normalizados = [normalizar_epc(epc) for epc in epcs]
    etiquetas = etiquetas_por_epc(list(dict.fromkeys(normalizados)), encuadre.empresa_id, con_producto=True)
    piezas = []
    for epc, epc_normalizado in zip(epcs, normalizados):
        etiqueta = etiquetas.get(epc_normalizado)
        producto = producto_variante = None
        if etiqueta is not None:
            producto_variante = etiqueta.impresion.producto_variante
            producto = producto_variante.producto if producto_variante else etiqueta.impresion.producto
        metadata = {"resolved": bool(producto), "source": RecepcionRFIDLectura.FUENTE_EPC, "epc_leido": epc_normalizado}
        if etiqueta is None:
            metadata["motivo"] = "epc_no_registrado"
        elif producto is None:
            metadata["motivo"] = "etiqueta_sin_producto"
        codigo_tag = etiqueta.epc if etiqueta is not None else epc_normalizado
        piezas.append({
            "epc": epc,
            "codigo_tag": codigo_tag,
            "etiqueta": etiqueta,
            "producto": producto,
            "producto_variante": producto_variante,
            "metadata": metadata,
            "formas": formas_equivalentes_epc(epc_normalizado) | formas_equivalentes_epc(codigo_tag),
            "orden_compra_detalle_id": None,
            "orden_produccion_detalle_id": None,
        })

    producto_ids = {p["producto"].pk for p in piezas if p["producto"] is not None}
    if not producto_ids:
        return piezas
    # Renglones por producto, en el orden de ``_renglon_tag_rfid``.
    por_producto = {}
    if encuadre.orden_compra_id:
        renglones = (
            OrdenCompraDetalle.objects.filter(orden_compra_id=encuadre.orden_compra_id, producto_id__in=producto_ids)
            .order_by("id").values_list("pk", "producto_id", "producto_variante_id")
        )
        campo = "orden_compra_detalle_id"
    elif encuadre.op_id:
        renglones = (
            OrdenProduccionDetalle.objects.filter(
                op_id=encuadre.op_id, activo=True, producto_variante__producto_id__in=producto_ids,
            ).order_by("op_detalle_id").values_list("pk", "producto_variante__producto_id", "producto_variante_id")
        )
        campo = "orden_produccion_detalle_id"
    else:
        return piezas
    for renglon_id, producto_id, variante_id in renglones:
        por_producto.setdefault(producto_id, []).append((renglon_id, variante_id))

    for pieza in piezas:
        if pieza["producto"] is None:
            continue
        renglones = por_producto.get(pieza["producto"].pk, [])
        variante_id = getattr(pieza["producto_variante"], "pk", None)
        if variante_id is None:
            elegido = renglones[0][0] if renglones else None
        else:
            elegido = next((r for r, v in renglones if v == variante_id), None)
            if elegido is None and encuadre.orden_compra_id:
                # OC: si la variante no tiene renglón, el del producto sin variante (#794).
                elegido = next((r for r, v in renglones if v is None), None)
        pieza[campo] = elegido
    return piezas


def prefetch_lecturas_rfid():
    """Lecturas del encuadre con producto y variante en la misma consulta (antes,
    dos consultas por lectura), en orden de captura: ``id`` explícito, porque sin
    ``order_by`` el orden dependía del plan de Postgres (con el JOIN cambiaba)."""
    return Prefetch(
        "lecturas",
        queryset=RecepcionRFIDLectura.objects.select_related("producto", "producto_variante").order_by("id"),
    )


def _renglones_oc_rfid(orden_compra_ids):
    return (
        OrdenCompraDetalle.objects.filter(orden_compra_id__in=orden_compra_ids)
        .select_related("producto", "producto_variante__talla").order_by("id")
    )


def _renglones_op_rfid(op_ids):
    return (
        OrdenProduccionDetalle.objects.filter(op_id__in=op_ids, activo=True)
        .select_related("producto_variante__producto", "producto_variante__talla").order_by("op_detalle_id")
    )


def mapas_resumen_encuadres_rfid(encuadres, ligero=False):
    """Renglones y recibido de todos los encuadres de un listado, en bloque.

    Constante en consultas: renglones de OC, renglones de OP y recibido +
    rechazado de cada lado, en lugar de tres consultas por encuadre.
    ``ligero`` (listado, EC-981): los renglones sin JOINs y solo con ``pk``, la
    orden y ``cantidad``, lo único que usa ``total_esperado_encuadre_rfid``. Mismos
    filtros y orden que ``resumen_encuadre_rfid`` encuadre por encuadre: el
    recibido se agrupa por renglón, así que juntar los renglones de varias
    órdenes en un ``IN`` da el mismo total por renglón.
    """
    oc_ids = {e.orden_compra_id for e in encuadres if e.orden_compra_id}
    op_ids = {e.op_id for e in encuadres if not e.orden_compra_id and e.op_id}
    renglones_oc, renglones_op = {}, {}
    if oc_ids:
        qs_oc = _renglones_oc_rfid(oc_ids)
        if ligero:
            qs_oc = qs_oc.select_related(None).only("orden_compra", "cantidad")
        for detalle in qs_oc:
            renglones_oc.setdefault(detalle.orden_compra_id, []).append(detalle)
    if op_ids:
        qs_op = _renglones_op_rfid(op_ids)
        if ligero:
            qs_op = qs_op.select_related(None).only("op", "cantidad")
        for detalle in qs_op:
            renglones_op.setdefault(detalle.op_id, []).append(detalle)
    recepcion_vs = RecepcionViewSet()
    return {
        "renglones_oc": renglones_oc,
        "renglones_op": renglones_op,
        "recibido_oc": recepcion_vs._recibido_por_detalle(
            "orden_compra_detalle_id", [d.pk for ds in renglones_oc.values() for d in ds]
        ),
        "recibido_op": recepcion_vs._recibido_por_detalle(
            "orden_produccion_detalle_id", [d.pk for ds in renglones_op.values() for d in ds]
        ),
    }


def _renglones_esperados_rfid(encuadre, mapas):
    """``(renglón, ordenado, ya_recibido, esperado)`` de cada renglón de la OC u OP
    del encuadre, con ``esperado = max(ordenado - ya recibido, 0)``. Única copia
    de la regla: la usan el resumen del detalle y el total del listado."""
    if encuadre.orden_compra_id:
        detalle_qs = mapas["renglones_oc"].get(encuadre.orden_compra_id, [])
        recibido_map = mapas["recibido_oc"]
    elif encuadre.op_id:
        detalle_qs = mapas["renglones_op"].get(encuadre.op_id, [])
        recibido_map = mapas["recibido_op"]
    else:
        return
    for detalle in detalle_qs:
        ordenado = Decimal(str(detalle.cantidad or 0))
        ya_recibido = recibido_map.get(detalle.pk, Decimal("0"))
        yield detalle, ordenado, ya_recibido, max(ordenado - ya_recibido, Decimal("0"))


def total_esperado_encuadre_rfid(encuadre, mapas):
    """``resumen.total_esperado`` sin leer las lecturas (listado ligero, EC-981)."""
    return sum((esperado for *_, esperado in _renglones_esperados_rfid(encuadre, mapas)), Decimal("0"))


def _completo_rfid(esperado_y_leido):
    """``completo`` (#1464): hay algo esperado y cada renglón llegó a su esperado
    con lo leído en ese renglón. Lo leído sin asignar, o de más en otro renglón,
    ya no tapa lo que falta. Única copia de la regla: detalle y listado."""
    hay_esperado = False
    for esperado, leido in esperado_y_leido:
        if esperado > 0:
            if leido < esperado:
                return False
            hay_esperado = True
    return hay_esperado


def leido_por_renglon_encuadres_rfid(encuadre_ids):
    """Lo leído por renglón de una página de encuadres, en una sola consulta
    agrupada (#1464): ``{(encuadre_id, "oc"|"op", renglón_id): cantidad}``. Las
    lecturas sin asignar no entran."""
    leido = {}
    if not encuadre_ids:
        return leido
    filas = (
        RecepcionRFIDLectura.objects.filter(encuadre_id__in=encuadre_ids)
        .filter(Q(orden_compra_detalle__isnull=False) | Q(orden_produccion_detalle__isnull=False))
        .values_list("encuadre_id", "orden_compra_detalle_id", "orden_produccion_detalle_id")
        .annotate(t=Sum("cantidad_leida")).order_by()
    )
    for encuadre_id, oc_detalle_id, op_detalle_id, cantidad in filas:
        cantidad = Decimal(str(cantidad or 0))
        for lado, renglon_id in (("oc", oc_detalle_id), ("op", op_detalle_id)):
            if renglon_id:
                clave = (encuadre_id, lado, renglon_id)
                leido[clave] = leido.get(clave, Decimal("0")) + cantidad
    return leido


def completo_encuadre_rfid(encuadre, mapas, leido):
    """``completo`` del listado, con ``leido_por_renglon_encuadres_rfid``: la misma
    regla y los mismos renglones que el resumen del detalle."""
    lado = "oc" if encuadre.orden_compra_id else "op"
    return _completo_rfid(
        (esperado, leido.get((encuadre.pk, lado, detalle.pk), Decimal("0")))
        for detalle, *_, esperado in _renglones_esperados_rfid(encuadre, mapas)
    )


def resumen_encuadre_rfid(encuadre, mapas=None):
    """Esperado = ordenado - ya recibido (no solo lo contratado, ver B1 del audit).

    Reutiliza ``RecepcionViewSet._recibido_por_detalle`` -- ya resta lo que
    Calidad rechaza, una sola fuente de verdad para "cuánto falta". Va en
    bloque: antes eran dos consultas por renglón de la orden.

    ``mapas`` (de ``mapas_resumen_encuadres_rfid``) trae los renglones y el
    recibido ya calculados para varios encuadres; sin él se calculan para este
    encuadre con la misma función, así listado y detalle no pueden divergir.
    """
    if mapas is None:
        mapas = mapas_resumen_encuadres_rfid([encuadre])
    filas = (
        (l.orden_compra_detalle_id, l.orden_produccion_detalle_id, l.cantidad_leida)
        for l in encuadre.lecturas.all()
    )
    return _armar_resumen_rfid(encuadre, mapas, filas)


def resumen_agrupado_encuadre_rfid(encuadre, mapas=None):
    """``resumen_encuadre_rfid`` sin traer las lecturas (EC-998): lo leído sale
    de una consulta agrupada por renglón, así cuesta lo mismo con 5 que con 500
    lecturas. Mismo resultado, campo por campo y con el mismo formato."""
    if mapas is None:
        mapas = mapas_resumen_encuadres_rfid([encuadre])
    filas = (
        RecepcionRFIDLectura.objects.filter(encuadre_id=encuadre.pk)
        .values_list("orden_compra_detalle_id", "orden_produccion_detalle_id")
        .annotate(t=Sum("cantidad_leida")).order_by()
    )
    return _armar_resumen_rfid(encuadre, mapas, filas)


def _armar_resumen_rfid(encuadre, mapas, filas):
    """El ``resumen`` del encuadre a partir de ``(renglón OC, renglón OP,
    cantidad)``: una fila por lectura (detalle) o por renglón (agrupado)."""
    lado = 0 if encuadre.orden_compra_id else 1
    leido_por_renglon = {}
    total_leido = Decimal("0")
    total_sin_asignar = Decimal("0")
    for fila in filas:
        cantidad = Decimal(str(fila[2] or 0))
        total_leido += cantidad
        if not fila[0] and not fila[1]:
            total_sin_asignar += cantidad
        elif fila[lado]:
            leido_por_renglon[fila[lado]] = leido_por_renglon.get(fila[lado], Decimal("0")) + cantidad

    detalles = []
    esperado_y_leido = []
    total_esperado = Decimal("0")
    for detalle, ordenado, ya_recibido, esperado in _renglones_esperados_rfid(encuadre, mapas):
        leido = leido_por_renglon.get(detalle.pk, Decimal("0"))
        if encuadre.orden_compra_id:
            producto = detalle.producto
        else:
            producto = getattr(detalle.producto_variante, "producto", None)

        total_esperado += esperado
        esperado_y_leido.append((esperado, leido))
        variante = detalle.producto_variante
        detalles.append({
            "detalle_id": detalle.pk,
            "producto_id": getattr(producto, "pk", None),
            "producto_nombre": getattr(producto, "nombre", None),
            "producto_variante_id": getattr(variante, "pk", None),
            "producto_variante_sku": getattr(variante, "sku", None),
            "producto_variante_talla": getattr(getattr(variante, "talla", None), "nombre", None),
            "ordenado": str(ordenado),
            "ya_recibido": str(ya_recibido),
            "esperado": str(esperado),
            "leido": str(leido),
            "diferencia": str(esperado - leido),
        })

    return {
        "detalle": detalles,
        "total_esperado": str(total_esperado),
        "total_leido": str(total_leido),
        "total_sin_asignar": str(total_sin_asignar),
        "completo": _completo_rfid(esperado_y_leido),
    }


@extend_schema_view(list=extend_schema(parameters=PARAMETROS_LISTADO_ENCUADRES_RFID))
class RecepcionRFIDEncuadreViewSet(viewsets.ModelViewSet):
    """API REST del encuadre RFID de recepción (OC u OP).

    "Esperado" resta lo ya recibido. Las lecturas llegan por ``epc`` (EC-982),
    resuelto con la etiqueta que imprimió el ERP, o por ``codigo_tag`` (obsoleto:
    SKU/código, para lectores tipo teclado).
    """

    queryset = RecepcionRFIDEncuadre.objects.all()
    serializer_class = RecepcionRFIDEncuadreSerializer
    http_method_names = ["get", "post"]
    # Listado (EC-981) como recepciones y calidad: sin ``?limit=`` la lista
    # completa; con él, ``{count, next, previous, results}``.
    pagination_class = PaginacionOpcionalLimitOffset
    search_fields = [
        "orden_compra__folio", "op__folio_op", "proveedor__nombre", "proveedor__razon_social",
        "remision", "factura_referencia",
    ]
    filter_backends = [
        *[b for b in viewsets.ModelViewSet.filter_backends if b is not OrderingFilter],
        OrdenamientoNullsAlFinal,  # fecha de aceptación/cancelación y proveedor pueden ser NULL
    ]
    ordering_fields = [
        "id", "created_at", "updated_at", "fecha_recepcion", "fecha_aceptacion", "fecha_cancelacion",
        "estatus", "tipo_origen", "orden_compra__folio", "op__folio_op", "proveedor__nombre", "almacen__nombre",
    ]
    TAG_REPETIDO = "Este tag ya fue leído en este encuadre."
    MENSAJE_EN_OTRO_ENCUADRE = "Esta pieza ya se leyó en el encuadre {encuadre} de la misma orden."
    UNIQUE_TAG = "uq_recepcion_rfid_lectura_encuadre_tag"
    # ``rechazadas[].motivo`` de una tanda de ``lecturas/`` (EC-998).
    MOTIVO_REPETIDO_EN_TANDA = "repetido_en_tanda"
    MOTIVO_REPETIDO_EN_ENCUADRE = "repetido_en_encuadre"
    MOTIVO_EN_OTRO_ENCUADRE = "en_otro_encuadre"

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # Lecturas (listado, detalle y onboarding) con ``R-WMS-RECEPCIONES``, como
        # ``recepciones/onboarding/`` (#1439); las escrituras piden ``C-`` en cada acción.
        if request.method in SAFE_METHODS:
            _exigir_permiso_oc(request, PERMISO_LEER_RECEPCIONES)

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset.select_related(
            "orden_compra", "op", "almacen", "proveedor", "sucursal", "usuario", "aceptado_por", "cancelado_por",
        )
        # Solo el detalle anida ``lecturas[]`` (y no con ``?sin_lecturas=1``, EC-998).
        if self.action == "retrieve" and not self._sin_lecturas():
            qs = qs.prefetch_related(prefetch_lecturas_rfid())
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa).order_by("-created_at", "-id")
        if self.action == "list":
            qs = self._filtrar_listado(qs)
        return qs

    def _filtrar_listado(self, qs):
        """Filtros del listado (EC-981), después del alcance por empresa: un id
        ajeno no trae filas. Inválidos → 400."""
        request = self.request
        estatus = (request.query_params.get("estatus") or "").strip()
        if estatus:
            lista = [parte.strip().upper() for parte in estatus.split(",") if parte.strip()]
            if not lista or any(e not in RecepcionRFIDEncuadre.Estatus.values for e in lista):
                raise ValidationError({"estatus": "Estatus inexistente. Usa PENDIENTE, ACEPTADO o CANCELADO."})
            qs = qs.filter(estatus__in=lista)
        tipo_origen = (request.query_params.get("tipo_origen") or "").strip().upper()
        if tipo_origen:
            if tipo_origen not in (Recepcion.TipoOrigen.ORDEN_COMPRA, Recepcion.TipoOrigen.ORDEN_PRODUCCION):
                raise ValidationError({"tipo_origen": "Valor inválido. Usa OC u OP."})
            qs = qs.filter(tipo_origen=tipo_origen)
        for nombre in ("proveedor", "almacen", "sucursal", "orden_compra", "op"):
            valor = entero_param(request, nombre)
            if valor is not None:
                qs = qs.filter(**{f"{nombre}_id": valor})
        # Día local del encuadre, como el listado de recepciones.
        fecha_desde, fecha_hasta = rango_fechas(request)
        if fecha_desde:
            qs = qs.filter(fecha_recepcion__date__gte=fecha_desde)
        if fecha_hasta:
            qs = qs.filter(fecha_recepcion__date__lte=fecha_hasta)
        # Lo leído, en la misma consulta (subconsulta por el FK indexado) y sin
        # traer las lecturas.
        lecturas = RecepcionRFIDLectura.objects.filter(encuadre=OuterRef("pk")).order_by().values("encuadre")
        decimal = DecimalField(max_digits=18, decimal_places=4)
        return qs.annotate(
            total_leido=Coalesce(
                Subquery(lecturas.annotate(t=Sum("cantidad_leida")).values("t")), Value(Decimal("0")),
                output_field=decimal,
            ),
            total_sin_asignar=Coalesce(
                Subquery(
                    lecturas.filter(orden_compra_detalle__isnull=True, orden_produccion_detalle__isnull=True)
                    .annotate(t=Sum("cantidad_leida")).values("t")
                ),
                Value(Decimal("0")),
                output_field=decimal,
            ),
        )

    def get_serializer_class(self):
        if self.action == "create":
            return RecepcionRFIDEncuadreCreateSerializer
        if self.action == "list":
            return RecepcionRFIDEncuadreListSerializer
        if self.action == "retrieve" and self._sin_lecturas():
            return RecepcionRFIDEncuadreSinLecturasSerializer
        return RecepcionRFIDEncuadreSerializer

    def _sin_lecturas(self):
        """``?sin_lecturas=1`` (o ``true``/``si``) en el detalle (EC-998)."""
        valor = self.request.query_params.get("sin_lecturas", "") if self.request else ""
        return str(valor).strip().lower() in {"1", "true", "si"}

    @extend_schema(parameters=[OpenApiParameter("sin_lecturas", OpenApiTypes.BOOL, description=(
        "`1`: el detalle sin `lecturas[]` (EC-998); el `resumen` es el mismo, calculado sin traer "
        "las lecturas. Las lecturas, paginadas, en `GET {id}/lecturas/`."
    ))])
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(responses=OpenApiTypes.OBJECT, description=(
        "El `resumen` del detalle (esperado contra leído por renglón y totales), con la misma forma, "
        "sin traer las lecturas (EC-998)."
    ))
    @action(detail=True, methods=["get"])
    def resumen(self, request, pk=None):
        return Response(resumen_agrupado_encuadre_rfid(self.get_object()))

    def list(self, request, *args, **kwargs):
        # Fila ligera (EC-981): sin ``lecturas[]`` ni ``resumen``. ``total_esperado``
        # sale de los renglones y el recibido de toda la página en bloque (las
        # mismas consultas que el resumen del detalle); lo leído, de la anotación,
        # y ``completo`` por renglón con una consulta agrupada de la página (#1464).
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        encuadres = list(page if page is not None else queryset)
        mapas = mapas_resumen_encuadres_rfid(encuadres, ligero=True)
        leido = leido_por_renglon_encuadres_rfid([e.pk for e in encuadres])
        context = self.get_serializer_context()
        context["totales_esperados"] = {e.pk: total_esperado_encuadre_rfid(e, mapas) for e in encuadres}
        context["completos"] = {e.pk: completo_encuadre_rfid(e, mapas, leido) for e in encuadres}
        serializer = self.get_serializer(encuadres, many=True, context=context)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def _bloquear(self, encuadre):
        """Relee el encuadre con su fila bloqueada (EC-981), en el mismo alcance.
        ``aceptar/``, ``cancelar/`` y ``lecturas/`` toman este mismo candado y
        deciden con el estatus de la fila bloqueada, no con el que leyó
        ``get_object()``: un cambio confirmado en medio ya no se pisa. Sin las
        lecturas: ``aceptar/`` y ``cancelar/`` responden solo la cabecera (EC-998).
        ``of=("self",)``: los JOIN de ``select_related`` son externos y Postgres no
        bloquea su lado NULL-able."""
        return generics.get_object_or_404(self.get_queryset().select_for_update(of=("self",)), pk=encuadre.pk)

    @action(detail=False, methods=["get"])
    def onboarding(self, request):
        # Mismos candidatos que recibir normalmente (OC/OP con pendiente) +
        # almacenes: un encuadre solo tiene sentido sobre algo que todavía
        # falta por recibir. Reusa RecepcionViewSet.handle_get_onboarding
        # para no duplicar esa lógica (pendiente ya resta rechazos de Calidad).
        return Response(RecepcionViewSet().handle_get_onboarding(request))

    def create(self, request, *args, **kwargs):
        user = request.user
        empresa = getattr(user, "empresa", None)
        # Superusuario incluido, igual que recepciones/onboarding (#875).
        if not empresa:
            raise ValidationError({"empresa": "El usuario no tiene empresa asignada."})
        _exigir_permiso_oc(request, PERMISO_CREAR_RECEPCIONES)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        oc = data.get("orden_compra")
        op = data.get("op")
        if bool(oc) == bool(op):
            raise ValidationError({"tipo_origen": "Debes enviar exactamente una de orden_compra u op."})

        origen = oc or op
        if origen.empresa_id != empresa.pk:
            raise ValidationError({"tipo_origen": "No pertenece a tu empresa."})

        almacen = data["almacen"]
        # El serializer acepta cualquier almacén: sin sucursal, uno de otra
        # empresa pasaba la guarda de abajo.
        if almacen.empresa_id != empresa.pk:
            raise ValidationError({"almacen": "El almacén no pertenece a tu empresa."})
        if almacen.sucursal_id and origen.sucursal_id != almacen.sucursal_id:
            raise ValidationError({"almacen": "Debe pertenecer a la misma sucursal de la orden."})

        encuadre = RecepcionRFIDEncuadre.objects.create(
            tipo_origen=data["tipo_origen"],
            orden_compra=oc,
            op=op,
            empresa=origen.empresa,
            sucursal=origen.sucursal,
            proveedor=getattr(origen, "proveedor", None),
            almacen=almacen,
            usuario=user,
            serie_codigo=(data.get("serie_codigo") or "RC").strip().upper()[:2] or "RC",
            fecha_recepcion=timezone.now(),
            remision=(data.get("remision") or "").strip() or None,
            factura_referencia=(data.get("factura_referencia") or "").strip() or None,
            observaciones=(data.get("observaciones") or "").strip() or None,
        )
        # Las relaciones ya están en memoria; solo se precargan las lecturas
        # (vacías), igual que en el detalle, para no consultarlas dos veces.
        prefetch_related_objects([encuadre], prefetch_lecturas_rfid())
        return Response(RecepcionRFIDEncuadreSerializer(encuadre).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        methods=["GET"],
        parameters=[
            OpenApiParameter("limit", OpenApiTypes.INT, description="Tamaño de página (tope 500)."),
            OpenApiParameter("offset", OpenApiTypes.INT, description="Desde qué lectura."),
        ],
        responses={200: RecepcionRFIDLecturaSerializer(many=True)},
        description=(
            "Lecturas del encuadre en orden de captura (`id`), las mismas de `lecturas[]` del "
            "detalle (EC-998). Sin `?limit=` la lista completa; con él, "
            "`{count, next, previous, results}`."
        ),
    )
    @extend_schema(
        methods=["POST"],
        # Dos cuerpos (EC-998): una pieza (``{epc}``/``{codigo_tag}``) o una tanda (``{epcs}``).
        request=PolymorphicProxySerializer(
            component_name="RecepcionRFIDLecturaEntrada",
            serializers=[RecepcionRFIDLecturaInputSerializer, RecepcionRFIDLecturasLoteInputSerializer],
            resource_type_field_name=None,
        ),
        responses={
            201: OpenApiResponse(RecepcionRFIDLecturaSerializer, description="Una pieza (`{epc}` o `{codigo_tag}`)."),
            200: OpenApiResponse(
                inline_serializer("RecepcionRFIDLecturasLoteRespuesta", {
                    "creadas": RecepcionRFIDLecturaLigeraSerializer(many=True),
                    "rechazadas": inline_serializer("RecepcionRFIDLecturaRechazada", {
                        "epc": drf_serializers.CharField(help_text="El EPC tal como se mandó."),
                        "motivo": drf_serializers.ChoiceField(choices=[
                            "repetido_en_tanda", "repetido_en_encuadre", "en_otro_encuadre",
                        ]),
                        "mensaje": drf_serializers.CharField(),
                        "encuadre": drf_serializers.IntegerField(
                            required=False, help_text="Solo con `en_otro_encuadre`: el encuadre que ya la tiene.",
                        ),
                    }, many=True),
                }),
                description="Una tanda (`{epcs}`): las piezas guardadas y las rechazadas por pieza.",
            ),
        },
        description=(
            "Una pieza leída. Manda exactamente uno de `epc` (recomendado) o `codigo_tag` "
            "(obsoleto); ambos o ninguno → 400. Con `epc`, la etiqueta impresa por el ERP "
            "en la empresa del encuadre da producto y variante; un EPC no registrado se "
            "guarda sin asignar (`metadata.motivo = \"epc_no_registrado\"`). Repetido en el "
            "encuadre → `400 {\"<campo enviado>\": \"Este tag ya fue leído en este encuadre.\"}`.\n\n"
            "Tanda (EC-998): `{\"epcs\": [...]}` (1 a 200) → `200 {creadas: [lectura ligera], "
            "rechazadas: [{epc, motivo, mensaje, encuadre?}]}`. Se rechaza por pieza (y el resto se "
            "guarda) lo repetido en la tanda (`repetido_en_tanda`), lo ya leído en el encuadre "
            "(`repetido_en_encuadre`) y lo leído en otro encuadre vigente de la misma orden "
            "(`en_otro_encuadre`, con `encuadre`). Lista vacía, de más de 200, EPC vacío, mandar "
            "además `epc`/`codigo_tag` o encuadre no pendiente → `400` y no se guarda nada."
        ),
    )
    @action(detail=True, methods=["get", "post"])
    def lecturas(self, request, pk=None):
        # Sin el prefetch de lecturas (solo el retrieve lo hace): solo se
        # responde la lectura nueva.
        encuadre = self.get_object()
        if request.method == "GET":
            return self._lecturas_paginadas(encuadre)
        _exigir_permiso_oc(request, PERMISO_CREAR_RECEPCIONES)
        if hasattr(request.data, "get") and "epcs" in request.data:
            return self._lecturas_lote(request, encuadre)
        with transaction.atomic():
            # Mismo candado que aceptar/ y cancelar/ (EC-981): no entra una lectura
            # a un encuadre que se acaba de aceptar o cancelar, y dos envíos del
            # mismo tag se forman en vez de chocar.
            estatus = (
                RecepcionRFIDEncuadre.objects.select_for_update()
                .values_list("estatus", flat=True).get(pk=encuadre.pk)
            )
            if estatus != RecepcionRFIDEncuadre.Estatus.PENDIENTE:
                raise ValidationError({"estatus": "Solo se puede escanear un encuadre pendiente."})

            input_serializer = RecepcionRFIDLecturaInputSerializer(data=request.data)
            input_serializer.is_valid(raise_exception=True)
            datos = input_serializer.validated_data
            # El error de repetido va en el campo que se mandó.
            campo = "epc" if "epc" in datos else "codigo_tag"
            if campo == "epc":
                resolved = _resolver_epc_rfid(encuadre, datos["epc"])
            else:
                resolved = _resolver_tag_rfid(encuadre, datos["codigo_tag"])
            codigo_tag = resolved["codigo_tag"]
            if encuadre.lecturas.filter(resolved["repetido"]).exists():
                raise ValidationError({campo: self.TAG_REPETIDO})
            # Por ``epc`` y por ``codigo_tag`` (obsoleto, #1484): la misma regla.
            self._exigir_epc_libre_en_la_orden(encuadre, resolved["repetido"], campo)

            try:
                with transaction.atomic():
                    lectura = RecepcionRFIDLectura.objects.create(
                        encuadre=encuadre,
                        codigo_tag=codigo_tag,
                        etiqueta=resolved["etiqueta"],
                        orden_compra_detalle=resolved["orden_compra_detalle"],
                        orden_produccion_detalle=resolved["orden_produccion_detalle"],
                        producto=resolved["producto"],
                        producto_variante=resolved["producto_variante"],
                        cantidad_leida=Decimal("1"),
                        metadata=resolved["metadata"],
                    )
            except IntegrityError as exc:
                # El mismo tag escrito por otro camino entre el exists() y el
                # INSERT: el 400 de un tag repetido, no un 500. Cualquier otra
                # violación (FK, NOT NULL...) sigue su curso.
                if not self._es_tag_repetido(exc):
                    raise
                raise ValidationError({campo: self.TAG_REPETIDO}) from exc
        return Response(RecepcionRFIDLecturaSerializer(lectura).data, status=status.HTTP_201_CREATED)

    @extend_schema(responses=RecepcionRFIDEncuadreCabeceraSerializer)
    @action(detail=True, methods=["post"])
    def aceptar(self, request, pk=None):
        encuadre = self.get_object()
        _exigir_permiso_oc(request, PERMISO_CREAR_RECEPCIONES)
        with transaction.atomic():
            encuadre = self._bloquear(encuadre)
            if encuadre.estatus != RecepcionRFIDEncuadre.Estatus.PENDIENTE:
                raise ValidationError({"estatus": "Solo se puede aceptar un encuadre pendiente."})
            encuadre.estatus = RecepcionRFIDEncuadre.Estatus.ACEPTADO
            encuadre.aceptado_por = request.user
            encuadre.fecha_aceptacion = timezone.now()
            encuadre.save(update_fields=["estatus", "aceptado_por", "fecha_aceptacion", "updated_at"])
            self._auditar(request, encuadre, "ACEPTAR", {"estatus": "PENDIENTE"},
                          {"estatus": encuadre.estatus, "lecturas": encuadre.lecturas.count()})
        return Response(RecepcionRFIDEncuadreCabeceraSerializer(encuadre).data)

    @extend_schema(responses=RecepcionRFIDEncuadreCabeceraSerializer)
    @action(detail=True, methods=["post"])
    def cancelar(self, request, pk=None):
        """Cancela un encuadre pendiente, o aceptado que aún no generó recepción.
        No toca inventario (el encuadre no lo mueve). ``motivo`` obligatorio."""
        encuadre = self.get_object()
        _exigir_permiso_oc(request, PERMISO_CREAR_RECEPCIONES)
        motivo = request.data.get("motivo") if hasattr(request.data, "get") else None
        if not isinstance(motivo, str) or not motivo.strip():
            raise ValidationError({"motivo": "Indica el motivo de la cancelación."})
        motivo = exigir_largo_motivo(motivo.strip())
        E = RecepcionRFIDEncuadre.Estatus
        with transaction.atomic():
            # Se escribe y se responde la fila bloqueada: un aceptar/ confirmado en
            # medio sale en la respuesta y en la bitácora (EC-981).
            encuadre = self._bloquear(encuadre)
            if encuadre.estatus == E.CANCELADO:
                raise ValidationError({"estatus": "El encuadre ya está cancelado."})
            if encuadre.recepcion_id:
                raise ValidationError({"recepcion": (
                    "El encuadre ya generó una recepción: cancela la recepción (antes de calidad)."
                )})
            antes = {"estatus": encuadre.estatus}
            encuadre.estatus = E.CANCELADO
            encuadre.motivo_cancelacion, encuadre.fecha_cancelacion = motivo, timezone.now()
            encuadre.cancelado_por = request.user
            encuadre.save(update_fields=[
                "estatus", "motivo_cancelacion", "fecha_cancelacion", "cancelado_por", "updated_at",
            ])
            self._auditar(request, encuadre, "CANCELAR", antes, {"estatus": encuadre.estatus, "motivo": motivo})
        return Response(RecepcionRFIDEncuadreCabeceraSerializer(encuadre).data)

    def _exigir_epc_libre_en_la_orden(self, encuadre, repetido, campo="epc"):
        """#1453: una pieza (EPC, o ``codigo_tag`` por el camino obsoleto, #1484)
        cuenta en un solo encuadre vigente (pendiente o aceptado) de la misma OC u
        OP. Se bloquea la orden para que dos encuadres escaneando a la vez se
        formen. 2 consultas. El error va en el campo que se mandó."""
        misma_orden = self._bloquear_orden(encuadre)
        if misma_orden is None:
            return
        otro = (
            self._lecturas_de_otros_encuadres_vigentes(encuadre, misma_orden).filter(repetido)
            .values_list("encuadre_id", flat=True).first()
        )
        if otro:
            raise ValidationError({campo: self.MENSAJE_EN_OTRO_ENCUADRE.format(encuadre=otro)})

    @staticmethod
    def _bloquear_orden(encuadre):
        """Candado de la OC u OP del encuadre (#1453), siempre después del del
        encuadre. Devuelve el filtro de las lecturas de la misma orden, o ``None``
        si el encuadre no tiene orden (no se bloquea nada)."""
        if encuadre.orden_compra_id:
            OrdenCompra.objects.select_for_update().filter(pk=encuadre.orden_compra_id).values_list("pk").first()
            return Q(encuadre__orden_compra_id=encuadre.orden_compra_id)
        if encuadre.op_id:
            OrdenProduccion.objects.select_for_update().filter(pk=encuadre.op_id).values_list("pk").first()
            return Q(encuadre__op_id=encuadre.op_id)
        return None

    @staticmethod
    def _lecturas_de_otros_encuadres_vigentes(encuadre, misma_orden):
        return (
            RecepcionRFIDLectura.objects.filter(misma_orden)
            .exclude(encuadre_id=encuadre.pk)
            .filter(encuadre__estatus__in=[RecepcionRFIDEncuadre.Estatus.PENDIENTE, RecepcionRFIDEncuadre.Estatus.ACEPTADO])
        )

    @staticmethod
    def _ya_en_el_encuadre(encuadre, formas, etiqueta_ids):
        """Lecturas del encuadre que ya son alguna pieza de la tanda: ``(codigo_tags,
        etiqueta_ids)`` de las que coinciden en cualquier forma del EPC o etiqueta."""
        filas = encuadre.lecturas.filter(Q(codigo_tag__in=formas) | Q(etiqueta_id__in=etiqueta_ids))
        tags, etiquetas = set(), set()
        for codigo_tag, etiqueta_id in filas.values_list("codigo_tag", "etiqueta_id"):
            tags.add(codigo_tag)
            if etiqueta_id:
                etiquetas.add(etiqueta_id)
        return tags, etiquetas

    def _lecturas_paginadas(self, encuadre):
        """``GET {id}/lecturas/`` (EC-998): las lecturas del detalle, paginadas con
        la paginación opcional del listado (sin ``?limit=``, todas)."""
        qs = encuadre.lecturas.select_related("producto", "producto_variante").order_by("id")
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(RecepcionRFIDLecturaSerializer(page, many=True).data)
        return Response(RecepcionRFIDLecturaSerializer(qs, many=True).data)

    def _lecturas_lote(self, request, encuadre):
        """``{"epcs": [...]}`` (EC-998): una tanda bajo un solo candado del encuadre
        y uno de la orden, en ese orden, con consultas fijas sin importar su tamaño.

        Por pieza (el resto de la tanda se guarda): repetida en la tanda o ya en
        el encuadre (cualquier forma equivalente del EPC o la misma etiqueta), o ya
        leída en otro encuadre vigente de la misma orden. La tanda completa (nada
        se guarda) con payload inválido o un encuadre no pendiente. Un EPC no
        registrado se guarda sin asignar, como en ``{epc}``."""
        entrada = RecepcionRFIDLecturasLoteInputSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        epcs = entrada.validated_data["epcs"]
        E = RecepcionRFIDEncuadre.Estatus
        with transaction.atomic():
            estatus = (
                RecepcionRFIDEncuadre.objects.select_for_update()
                .values_list("estatus", flat=True).get(pk=encuadre.pk)
            )
            if estatus != E.PENDIENTE:
                raise ValidationError({"estatus": "Solo se puede escanear un encuadre pendiente."})

            piezas = _resolver_epcs_rfid(encuadre, epcs)
            formas = set().union(*(p["formas"] for p in piezas))
            etiqueta_ids = {p["etiqueta"].pk for p in piezas if p["etiqueta"] is not None}
            tags_encuadre, etiquetas_encuadre = self._ya_en_el_encuadre(encuadre, formas, etiqueta_ids)
            otras = []
            misma_orden = self._bloquear_orden(encuadre)
            if misma_orden is not None:
                # Por ``id`` de lectura, como el ``first()`` de ``{epc}``.
                otras = list(
                    self._lecturas_de_otros_encuadres_vigentes(encuadre, misma_orden)
                    .filter(Q(codigo_tag__in=formas) | Q(etiqueta_id__in=etiqueta_ids))
                    .order_by("id").values_list("encuadre_id", "codigo_tag", "etiqueta_id")
                )

            aceptadas, rechazadas = [], []
            tags_tanda, etiquetas_tanda = set(), set()

            def es(pieza, tags, etiquetas):
                etiqueta = pieza["etiqueta"]
                return bool(pieza["formas"] & tags) or (etiqueta is not None and etiqueta.pk in etiquetas)

            for i, pieza in enumerate(piezas):
                if es(pieza, tags_encuadre, etiquetas_encuadre):
                    rechazadas.append((i, self._rechazo(pieza, self.MOTIVO_REPETIDO_EN_ENCUADRE)))
                    continue
                if es(pieza, tags_tanda, etiquetas_tanda):
                    rechazadas.append((i, self._rechazo(pieza, self.MOTIVO_REPETIDO_EN_TANDA)))
                    continue
                etiqueta = pieza["etiqueta"]
                otro = next((
                    encuadre_id for encuadre_id, codigo_tag, otra_etiqueta in otras
                    if codigo_tag in pieza["formas"] or (etiqueta is not None and otra_etiqueta == etiqueta.pk)
                ), None)
                if otro:
                    rechazadas.append((i, self._rechazo(pieza, self.MOTIVO_EN_OTRO_ENCUADRE, otro)))
                    continue
                tags_tanda.add(pieza["codigo_tag"])
                if etiqueta is not None:
                    etiquetas_tanda.add(etiqueta.pk)
                aceptadas.append((i, pieza))

            creadas = self._insertar_lote(encuadre, aceptadas, rechazadas)
        rechazadas.sort(key=lambda par: par[0])
        return Response({
            "creadas": RecepcionRFIDLecturaLigeraSerializer(creadas, many=True).data,
            "rechazadas": [rechazo for _, rechazo in rechazadas],
        }, status=status.HTTP_200_OK)

    def _insertar_lote(self, encuadre, aceptadas, rechazadas):
        """Un solo ``INSERT ... RETURNING`` para las piezas aceptadas. Bajo los dos
        candados no puede chocar con otra lectura del API; si algo escrito por otro
        camino viola ``uq_recepcion_rfid_lectura_encuadre_tag``, se releen los tags
        del encuadre, esas piezas pasan a repetidas y se intenta una vez más."""
        for intento in (1, 2):
            nuevas = [
                RecepcionRFIDLectura(
                    encuadre=encuadre,
                    codigo_tag=pieza["codigo_tag"],
                    etiqueta=pieza["etiqueta"],
                    orden_compra_detalle_id=pieza["orden_compra_detalle_id"],
                    orden_produccion_detalle_id=pieza["orden_produccion_detalle_id"],
                    producto=pieza["producto"],
                    producto_variante=pieza["producto_variante"],
                    cantidad_leida=Decimal("1"),
                    metadata=pieza["metadata"],
                )
                for _, pieza in aceptadas
            ]
            if not nuevas:
                return []
            try:
                with transaction.atomic():
                    return RecepcionRFIDLectura.objects.bulk_create(nuevas)
            except IntegrityError as exc:
                if intento == 2 or not self._es_tag_repetido(exc):
                    raise
                ya = set(
                    encuadre.lecturas.filter(codigo_tag__in=[n.codigo_tag for n in nuevas])
                    .values_list("codigo_tag", flat=True)
                )
                rechazadas.extend(
                    (i, self._rechazo(pieza, self.MOTIVO_REPETIDO_EN_ENCUADRE))
                    for i, pieza in aceptadas if pieza["codigo_tag"] in ya
                )
                aceptadas = [(i, pieza) for i, pieza in aceptadas if pieza["codigo_tag"] not in ya]
        return []

    def _rechazo(self, pieza, motivo, otro_encuadre=None):
        rechazo = {"epc": pieza["epc"], "motivo": motivo}
        if motivo == self.MOTIVO_EN_OTRO_ENCUADRE:
            rechazo["mensaje"] = self.MENSAJE_EN_OTRO_ENCUADRE.format(encuadre=otro_encuadre)
            rechazo["encuadre"] = otro_encuadre
        else:
            rechazo["mensaje"] = self.TAG_REPETIDO
        return rechazo

    @classmethod
    def _es_tag_repetido(cls, exc):
        """La violación es de ``uq_recepcion_rfid_lectura_encuadre_tag``: por el
        nombre que psycopg 3 da en ``diag.constraint_name``; el texto del error
        solo como respaldo (otro driver o un error sin ``diag``)."""
        diag = getattr(exc.__cause__, "diag", None)
        nombre = getattr(diag, "constraint_name", None)
        if nombre:
            return nombre == cls.UNIQUE_TAG
        return cls.UNIQUE_TAG in str(exc)

    @staticmethod
    def _auditar(request, encuadre, accion, antes, despues):
        AuditoriaEvento.objects.create(
            empresa_id=encuadre.empresa_id, usuario=request.user, modulo="compras", accion=accion,
            tabla=RecepcionRFIDEncuadre._meta.db_table, id_registro=str(encuadre.pk),
            antes_json=antes, despues_json=despues,
            ip=_ip_auditoria(request), user_agent=request.META.get("HTTP_USER_AGENT"),
        )



def _dec2(valor):
    """Decimal como string con 2 decimales (``"20.00"``), la forma de los
    ``DecimalField`` del serializer (#1296)."""
    return f"{Decimal(str(valor or 0)).quantize(Decimal('0.01'))}"


def _sm_pendientes(request):
    """SM Enviadas (por recibir) del alcance del usuario. Una consulta.

    - Las de OP solo si la OP sigue en estatus recibible (#1236).
    - ``piezas_pendientes``: piezas de la SM menos lo ya recibido con ella (parciales, #1232).
    - SM de OB/OR: ``detalle`` con sus renglones (SKU, talla, color y
      ``cantidad_pendiente``, el tope al recibir). Una consulta más, solo si hay.
    - ``total`` solo con ``R-PRODUCCION-COSTOS``, como en las SM (#1235)."""
    from produccion.api.serializers import PERMISO_COSTOS_PRODUCCION
    from produccion.scope import ordenes_produccion_visibles

    sms = list(
        ordenes_produccion_visibles(SalidaMaquila.objects.filter(activo=True, estatus=SalidaMaquila.Estatus.ENVIADA),
                                    request.user)
        .filter(Q(op__isnull=True) | Q(op__estatus_op__in=ESTATUS_OP_RECIBIBLES, op__activo=True))
        .select_related("op", "ob", "orden_r", "centro_confeccion")
        .annotate(piezas_recibidas=recibido_sm_subquery(OuterRef("pk")))
        .order_by("-id")[:200]
    )
    # Sin SM no se consultan permisos (una consulta menos para quien no es admin).
    ve_costos = bool(sms) and PERMISO_COSTOS_PRODUCCION in permisos_de_la_peticion(request)
    renglones = renglones_sm_servicio([sm.pk for sm in sms if sm.op_id is None])
    filas = []
    for sm in sms:
        origen, folio_origen = origen_sm(sm)
        fila = {
            "id": sm.pk, "folio": sm.folio, "folio_display": f"{sm.folio} · {folio_origen}",
            "origen": origen, "origen_folio": folio_origen, "op_id": sm.op_id,
            "centro_confeccion": sm.centro_confeccion_id, "centro_confeccion_nombre": sm.centro_confeccion.nombre,
            # Decimales como string con 2 decimales, como los del serializer (#1296).
            "sucursal_id": sm.sucursal_id, "fecha_envio": sm.fecha_envio, "total_piezas": _dec2(sm.total_piezas),
            "piezas_pendientes": _dec2(sm.total_piezas - sm.piezas_recibidas),
        }
        if sm.op_id is None:
            fila["detalle"] = renglones.get(sm.pk, [])
        if ve_costos:
            fila["total"] = _dec2(sm.total)
        filas.append(fila)
    return filas
