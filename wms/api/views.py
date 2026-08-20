from django.db.models import Prefetch
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from wms.api.serializers import (
    DespachoCreateSerializer,
    DespachoSerializer,
    EtiquetaRFIDCreateSerializer,
    EtiquetaRFIDSerializer,
    PackingCreateSerializer,
    TransferenciaListSerializer,
    TransferenciaRetrieveSerializer,
    TransferenciaSerializer,
    PickingCreateSerializer,
    PickingSerializer,
    PackingSerializer,
)
from wms.models import (
    Despacho,
    DespachoDetalle,
    EtiquetaRFIDDetalle,
    EtiquetaRFIDImpresion,
    Packing,
    PackingDetalle,
    Picking,
    PickingDetalle,
    PickingOrdenTrabajo,
    RfidScan,
    Transferencia,
    TransferenciaDetalle,
)
from wms.services.despacho_service import DespachoService
from wms.services.transferencia_service import TransferenciaService
from wms.services.picking_service import PickingService
from wms.services.packing_service import PackingService
from wms.services.rfid_label_service import RFIDLabelService


class TransferenciaViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet
):
    queryset = Transferencia.objects.all()
    serializer_class = TransferenciaSerializer

    def get_queryset(self):
        user = self.request.user
        qs = (
            super()
            .get_queryset()
            .select_related("almacen_origen", "almacen_destino", "usuario")
            .order_by("-fecha_creacion", "-id")
        )

        # Solo el retrieve individual anida ``transferencia_detalle``: los renglones
        # se traen con sus FK en un único prefetch para evitar el N+1 que tendría el
        # shape anidado. ``ubicacion_*__almacen`` viaja en el select_related porque la
        # etiqueta de una Ubicacion se compone con ``almacen.nombre``. El list no lo
        # necesita y se mantiene ligero.
        if self.action == "retrieve":
            qs = qs.prefetch_related(
                Prefetch(
                    "transferencia_detalle",
                    queryset=TransferenciaDetalle.objects.select_related(
                        "producto",
                        "producto_variante",
                        "ubicacion_origen",
                        "ubicacion_origen__almacen",
                        "ubicacion_destino",
                        "ubicacion_destino__almacen",
                    ).order_by("id"),
                )
            )

        # Aislamiento multi-tenant: sin empresa no se ve nada. list devuelve 200 []
        # fuera de alcance; retrieve de otra empresa/sucursal devuelve 404 (no 403).
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa)

        # Scope por sucursal dentro de la empresa: el admin de empresa ve todas sus
        # sucursales; el resto ve las asignadas en el M2M ``usuario.sucursales``
        # más su ``sucursal_default`` —mismo criterio que
        # ``PickingViewSet``/``PackingViewSet``/``DespachoViewSet.get_queryset()``,
        # y que ``TransferenciaService.handle_store()`` ya exige del lado de
        # escritura (la transferencia se timbra con ``user.sucursal_default``)—.
        # Sin esto un usuario cuyo único acceso fuera la sucursal por defecto podía
        # crear la transferencia pero no volver a verla en list/retrieve. Sin
        # sucursales asignadas no ve nada, igual que un usuario sin empresa: se
        # falla cerrado. Se filtra por ``Transferencia.sucursal`` (la dueña del
        # documento), no por la sucursal de los almacenes origen/destino, que
        # pueden diferir.
        if getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(sucursal_id__in=user.sucursales_permitidas())

    def get_serializer_class(self):
        # retrieve → encabezado + renglones anidados; list → forma plana ligera;
        # el resto (create) conserva la forma original de escritura.
        if self.action == "retrieve":
            return TransferenciaRetrieveSerializer
        if self.action == "list":
            return TransferenciaListSerializer
        return TransferenciaSerializer

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        res = TransferenciaService.handle_store(serializer.validated_data, request.user, request=request)
        return Response(TransferenciaSerializer(res).data, status=status.HTTP_201_CREATED)

class PickingViewSet(mixins.RetrieveModelMixin, mixins.ListModelMixin, GenericViewSet):
    queryset = Picking.objects.all()
    serializer_class = PickingSerializer

    def get_queryset(self):
        user = self.request.user
        # Las FK del encabezado que PickingSerializer resuelve a nombre viajan en
        # un solo select_related (oleada/zona_almacen/lote solo usan campos
        # locales para su etiqueta, no hace falta profundizar más).
        #
        # A diferencia de TransferenciaViewSet (que solo anida renglones en el
        # retrieve), PickingSerializer es compartido y anida ``picking_detalle``
        # también en el list, así que el prefetch aplica a ambas acciones.
        # ``ubicacion__almacen`` viaja en el select_related porque la etiqueta de
        # una Ubicacion se compone con ``almacen.nombre``.
        qs = (
            super()
            .get_queryset()
            .select_related(
                "pedido",
                "operador",
                "almacen",
                "almacen_destino",
                "usuario",
                "oleada",
                "zona_almacen",
                "lote",
            )
            .prefetch_related(
                Prefetch(
                    "picking_detalle",
                    queryset=PickingDetalle.objects.select_related(
                        "producto",
                        "producto_variante",
                        "pedido_detalle_talla",
                        "pedido_detalle_talla__variante",
                        "pedido_detalle_talla__variante__talla",
                        "ubicacion",
                        "ubicacion__almacen",
                        "operador",
                    ).order_by("id"),
                ),
                Prefetch(
                    "ordenes_trabajo",
                    queryset=PickingOrdenTrabajo.objects.select_related(
                        "orden_bordado",
                        "orden_reflejante",
                        "orden_corte_manga",
                    ).order_by("id"),
                ),
            )
            .order_by("-created_at", "-id")
        )

        # Aislamiento multi-tenant: sin empresa no se ve nada. list devuelve 200 []
        # fuera de alcance; retrieve de otra empresa/sucursal devuelve 404 (no 403).
        # Mismo criterio que TransferenciaViewSet.get_queryset().
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa)

        # Scope por sucursal dentro de la empresa: el admin de empresa ve todas sus
        # sucursales; el resto ve las asignadas en el M2M ``usuario.sucursales``
        # más su ``sucursal_default`` —mismo criterio que
        # ``PackingViewSet``/``DespachoViewSet.get_queryset()``—. Sin esto un
        # usuario cuyo único acceso fuera la sucursal por defecto podía completar
        # el onboarding (que sí une ambos conjuntos, ver
        # ``PickingService.onboarding_payload``) y crear el picking, pero no
        # volver a verlo en list/retrieve. Sin sucursales asignadas no ve nada,
        # igual que un usuario sin empresa: se falla cerrado. Se filtra por
        # ``Picking.sucursal`` (la dueña del documento), no por la sucursal del
        # almacén, que puede diferir.
        if getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(sucursal_id__in=user.sucursales_permitidas())

    def get_serializer_class(self):
        if self.action in {"create", "onboarding"} and self.request.method == "POST":
            return PickingCreateSerializer
        return PickingSerializer

    @action(detail=False, methods=["get", "post"], url_path="onboarding", url_name="onboarding")
    def onboarding(self, request):
        if request.method == "GET":
            pedido_id = request.query_params.get("pedido") or request.query_params.get("pedido_id")
            almacen_origen_id = request.query_params.get("almacen_origen") or request.query_params.get("almacen_origen_id")
            almacen_destino_id = request.query_params.get("almacen_destino") or request.query_params.get("almacen_destino_id")
            payload = PickingService.onboarding_payload(
                request.user,
                pedido_id=pedido_id,
                almacen_origen_id=almacen_origen_id,
                almacen_destino_id=almacen_destino_id,
            )
            return Response(payload)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        picking, ordenes_trabajo = PickingService.handle_store(serializer.validated_data, request.user)
        response_data = PickingSerializer(picking).data
        response_data["ordenes_trabajo_generadas"] = ordenes_trabajo
        return Response(response_data, status=status.HTTP_201_CREATED)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        picking, ordenes_trabajo = PickingService.handle_store(serializer.validated_data, request.user)
        response_data = PickingSerializer(picking).data
        response_data["ordenes_trabajo_generadas"] = ordenes_trabajo
        return Response(response_data, status=status.HTTP_201_CREATED)

class PackingViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet):
    queryset = Packing.objects.all()
    serializer_class = PackingSerializer

    def get_queryset(self):
        user = self.request.user
        qs = (
            super()
            .get_queryset()
            .select_related("pedido", "picking", "picking__almacen", "operador", "usuario")
            .prefetch_related(
                Prefetch(
                    "packing_detalle",
                    queryset=PackingDetalle.objects.select_related(
                        "caja",
                        "picking_detalle",
                        "picking_detalle__producto",
                        "picking_detalle__producto_variante",
                        "picking_detalle__pedido_detalle_talla",
                        "picking_detalle__pedido_detalle_talla__variante",
                        "picking_detalle__pedido_detalle_talla__variante__talla",
                        "picking_detalle__ubicacion",
                        "picking_detalle__ubicacion__almacen",
                    ).order_by("id"),
                )
            )
            .order_by("-created_at", "-id")
        )

        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa)
        if getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(sucursal_id__in=user.sucursales_permitidas())

    def get_serializer_class(self):
        if self.action in {"create", "onboarding"} and self.request.method == "POST":
            return PackingCreateSerializer
        return PackingSerializer

    @action(detail=False, methods=["get", "post"], url_path="onboarding", url_name="onboarding")
    def onboarding(self, request):
        if request.method == "GET":
            picking_id = request.query_params.get("picking") or request.query_params.get(
                "picking_id"
            )
            payload = PackingService.onboarding_payload(
                request.user,
                picking_id=picking_id,
            )
            return Response(payload)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        packing_instance = PackingService.handle_store(serializer.validated_data, request.user)
        return Response(PackingSerializer(packing_instance).data, status=status.HTTP_201_CREATED)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        packing_instance = PackingService.handle_store(serializer.validated_data, request.user)
        return Response(PackingSerializer(packing_instance).data, status=status.HTTP_201_CREATED)


class DespachoViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet):
    queryset = Despacho.objects.all()
    serializer_class = DespachoSerializer

    def get_queryset(self):
        user = self.request.user
        qs = (
            super()
            .get_queryset()
            .select_related(
                "packing",
                "packing__pedido",
                "packing__pedido__cliente",
                "packing__sucursal",
                "envio",
                "envio__transportista",
            )
            .prefetch_related(
                Prefetch(
                    "despacho_detalle",
                    queryset=DespachoDetalle.objects.select_related(
                        "packing_detalle",
                        "packing_detalle__caja",
                        "packing_detalle__picking_detalle",
                        "packing_detalle__picking_detalle__producto",
                        "packing_detalle__picking_detalle__producto_variante",
                        "packing_detalle__picking_detalle__pedido_detalle_talla",
                        "packing_detalle__picking_detalle__pedido_detalle_talla__variante",
                        "packing_detalle__picking_detalle__pedido_detalle_talla__variante__talla",
                        "packing_detalle__picking_detalle__pedido_detalle_talla__variante__color",
                        "packing_detalle__picking_detalle__ubicacion",
                        "packing_detalle__picking_detalle__ubicacion__almacen",
                    ).order_by("id"),
                )
            )
            # ``Despacho`` no tiene fecha propia: el más reciente primero se
            # resuelve por la fecha de alta del ``Packing`` del que cuelga (ya
            # viene en el ``select_related``, así que no añade consultas).
            .order_by("-packing__created_at", "-id")
        )

        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(packing__empresa=empresa)
        if getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(packing__sucursal_id__in=user.sucursales_permitidas())

    def get_serializer_class(self):
        if self.action in {"create", "onboarding"} and self.request.method == "POST":
            return DespachoCreateSerializer
        return DespachoSerializer

    @action(detail=False, methods=["get", "post"], url_path="onboarding", url_name="onboarding")
    def onboarding(self, request):
        if request.method == "GET":
            packing_id = request.query_params.get("packing") or request.query_params.get(
                "packing_id"
            )
            payload = DespachoService.onboarding_payload(
                request.user,
                packing_id=packing_id,
            )
            return Response(payload)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        despacho_instance = DespachoService.handle_store(serializer.validated_data, request.user)
        return Response(DespachoSerializer(despacho_instance).data, status=status.HTTP_201_CREATED)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        despacho_instance = DespachoService.handle_store(serializer.validated_data, request.user)
        return Response(DespachoSerializer(despacho_instance).data, status=status.HTTP_201_CREATED)


class EtiquetaRFIDViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet):
    queryset = EtiquetaRFIDImpresion.objects.all()
    serializer_class = EtiquetaRFIDSerializer

    def get_queryset(self):
        user = self.request.user
        qs = (
            super()
            .get_queryset()
            .select_related("empresa", "sucursal", "usuario", "producto", "producto_variante")
            .prefetch_related(
                Prefetch(
                    "etiquetas",
                    queryset=EtiquetaRFIDDetalle.objects.order_by("id"),
                )
            )
            .order_by("-created_at", "-id")
        )

        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa)
        if getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(sucursal_id__in=user.sucursales_permitidas())

    def get_serializer_class(self):
        if self.action in {"create", "registrar_impresion", "onboarding"} and self.request.method == "POST":
            return EtiquetaRFIDCreateSerializer
        return EtiquetaRFIDSerializer

    @action(
        detail=False,
        methods=["get"],
        url_path="buscar",
        url_name="buscar",
    )
    def buscar(self, request):
        """Buscador simple de SKU/producto (mismo criterio que QA/imprimir_etiqueta).

        Scope: multi-tenant por empresa + sucursales permitidas.
        Filtra con OR por:
          - ProductoVariante: sku, nombre, producto.nombre, producto.codigo, producto.cod_proscai
          - Producto: nombre, codigo, cod_proscai

        Respuesta lista simple tipo QA:
        [{
          "tipo": "variante" | "producto",
          "id",
          "producto_variante_id" | null,
          "producto_id",
          "label": "10005032XC - CAMISA MANGA LARGA THAI PREMIUM · VINO · 2XC",
          "sku",
          "nombre",
          "color_nombre",
          "talla_nombre",
          "codigo",
          "cod_proscai"
        }]
        """
        from django.db.models import Q
        from catalogo.models import Producto, ProductoVariante

        user = request.user
        empresa = getattr(user, "empresa", None)
        q = (request.query_params.get("q") or "").strip()

        empty = {"q": q, "resultados": []}
        if empresa is None:
            return Response(empty)

        variantes_qs = (
            ProductoVariante.objects.select_related("producto", "color", "talla")
            .filter(empresa=empresa, activo=True)
            .order_by("sku")
        )
        productos_qs = Producto.objects.filter(empresa=empresa, activo=True).order_by(
            "nombre"
        )

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

        sucursal_ids = (
            None
            if (getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False))
            else user.sucursales_permitidas()
        )
        resultados = []

        for v in variantes_qs[:30]:
            p = v.producto
            color_n = getattr(v.color, "nombre", None)
            talla_n = getattr(v.talla, "nombre", None)
            sec = " · ".join([x for x in [color_n, talla_n] if x])
            label_prefix = f"{v.sku} - " if v.sku else ""
            label = f"{label_prefix}{p.nombre}"
            if sec:
                label = f"{label} · {sec}"
            resultados.append({
                "tipo": "variante",
                "id": v.pk,
                "producto_variante_id": v.pk,
                "producto_id": p.pk,
                "label": label,
                "sku": v.sku,
                "nombre": p.nombre,
                "color_nombre": color_n,
                "talla_nombre": talla_n,
                "codigo": p.codigo,
                "cod_proscai": p.cod_proscai,
            })

        producto_ids_con_variantes = {item["producto_id"] for item in resultados}
        for p in productos_qs[:30]:
            if p.pk in producto_ids_con_variantes:
                continue
            codigo_impresion = p.codigo or p.cod_proscai or f"PROD-{p.pk}"
            resultados.append({
                "tipo": "producto",
                "id": p.pk,
                "producto_variante_id": None,
                "producto_id": p.pk,
                "label": f"{codigo_impresion} - {p.nombre}",
                "sku": None,
                "nombre": p.nombre,
                "color_nombre": None,
                "talla_nombre": None,
                "codigo": p.codigo,
                "cod_proscai": p.cod_proscai,
            })

        return Response({
            "q": q,
            "sucursal_ids": sucursal_ids,
            "resultados": resultados,
        })

    @action(
        detail=False,
        methods=["get", "post"],
        url_path="onboarding",
        url_name="onboarding",
    )
    def onboarding(self, request):
        """Onboarding para Impresión de Etiquetas RFID (1 modal, 1 URL).

        PATRÓN: mismo onboarding que WMS picking/packing/despacho / Producción
        orden bordado/reflejante/corte-manga.

        Qué hace internamente (le ahorra TODO este trabajo a Next.js):
          - GET vacío              =>  devuelve buscador (resultados sugeridos),
                                        sin preview (está abierto el modal sin
                                        seleccionar SKU).
          - GET ?q=XXXX            =>  corre BUSCADOR por texto (mismo filtro
                                        Q de QA / imprimir_etiqueta).
          - GET ?variante=X&cantidad=3
                    o ?producto=Y  =>  devuelve buscador + **PREVIEW COMPLETO
                                        CON TODOS LOS ZPL INDIVIDUALES YA ARMADOS
                                        POR CADA ETIQUETA** (el frontend **no**
                                        tiene que reconstruir ZPL ni reemplazar
                                        EPCs —sólo itera zpl_individual[] y
                                        se lo envía a Browser Print uno por uno).
          - POST                   =>  misma escritura que registrar-impresion:
                                        crea impresión + detalle EPCs, y así la
                                        lista ``/api/v1/wms/etiquetas-rfid/``
                                        deja de estar vacía.

        Autenticación: sesión/token del usuario; scope empresa + sucursales.
        """
        if request.method == "GET":
            user = request.user
            empresa = getattr(user, "empresa", None)

            q = (request.query_params.get("q") or "").strip()
            variante_id = request.query_params.get("variante") or request.query_params.get("variante_id")
            producto_id = request.query_params.get("producto") or request.query_params.get("producto_id")
            cantidad_raw = request.query_params.get("cantidad")
            rfid_mode_raw = request.query_params.get("rfid_mode", "true")
            cantidad = int(cantidad_raw) if (cantidad_raw and str(cantidad_raw).isdigit()) else 1
            rfid_mode = str(rfid_mode_raw).lower() not in {"0", "false", "no", "off"}

            buscar_payload = self.buscar(request).data
            if not isinstance(buscar_payload, dict):
                buscar_payload = {"q": q, "resultados": []}

            empty = {
                "q": q,
                "resultados": buscar_payload.get("resultados", []),
                "sucursal_ids": buscar_payload.get("sucursal_ids"),
                "tiene_seleccion": False,
                "preview": None,
            }
            if empresa is None:
                return Response(empty)
            if not variante_id and not producto_id:
                return Response(empty)

            try:
                preview_payload = RFIDLabelService.onboarding_preview(
                    request.user,
                    variante_id=variante_id,
                    producto_id=producto_id,
                    cantidad=cantidad,
                    rfid_mode=rfid_mode,
                )
            except ValidationError:
                return Response(
                    {
                        **empty,
                        "error": "Solicitud inválida. Verifica los datos enviados.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            return Response({
                "q": q,
                "resultados": buscar_payload.get("resultados", []),
                "sucursal_ids": buscar_payload.get("sucursal_ids"),
                "tiene_seleccion": True,
                "preview": preview_payload,
                "mensaje": (
                    "Next.js: iterar preview.zpl_individual[] y enviar cada ZPL "
                    "a Browser Print. Al terminar hacer POST a este mismo "
                    "endpoint con los campos de impresora/estatus/etiquetas."
                ),
            })

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        impresion = RFIDLabelService.store_impresion(
            serializer.validated_data, request.user
        )
        return Response(
            EtiquetaRFIDSerializer(impresion).data, status=status.HTTP_201_CREATED
        )

    @action(
        detail=False,
        methods=["get"],
        url_path="preview",
        url_name="preview",
    )
    def preview(self, request):
        variante_id = request.query_params.get("variante") or request.query_params.get(
            "variante_id"
        )
        producto_id = request.query_params.get("producto") or request.query_params.get(
            "producto_id"
        )
        cantidad_raw = request.query_params.get("cantidad")
        rfid_mode_raw = request.query_params.get("rfid_mode", "true")
        cantidad = int(cantidad_raw) if (cantidad_raw and str(cantidad_raw).isdigit()) else 1
        rfid_mode = str(rfid_mode_raw).lower() not in {"0", "false", "no", "off"}

        payload = RFIDLabelService.onboarding_preview(request.user, variante_id=variante_id, producto_id=producto_id, cantidad=cantidad, rfid_mode=rfid_mode)
        return Response(payload)

    @action(
        detail=False,
        methods=["post"],
        url_path="registrar-impresion",
        url_name="registrar_impresion",
    )
    def registrar_impresion(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        impresion = RFIDLabelService.store_impresion(
            serializer.validated_data, request.user
        )
        return Response(
            EtiquetaRFIDSerializer(impresion).data, status=status.HTTP_201_CREATED
        )

    def create(self, request):
        return self.registrar_impresion(request)

    # ── SCANNER / LECTOR RFID ───────────────────────────────────────────
    # Next.js NO necesita usar routes /QA/* para el scanner.
    # Consume estos 3 endpoints del V1 que ya respetan scope empresa/sucursales.
    # (El receive/ lo usa SOLO el lector FX Zebra sin token — POST /QA/scanner_rfid/receive/)

    @action(
        detail=False,
        methods=["get"],
        url_path="scans",
        url_name="scans",
    )
    def scans(self, request):
        """Polling endpoint p/ Next.js: últimas 50 lecturas del FX + match automático.

        Igual que /QA/scanner_rfid/get/ pero filtrado empresa (sucursales permitidas)
        y enrutado vía DRF ViewSet V1.

        Query params (debug opcional, sin auth):
            epc (opcional): si enviamos un EPC (hex), se busca directamente y en
            debug_get.query_epc_search devuelve found_in_scans true/false + variant.

        Response shape:
            {
              scans: [
                {id, epc, timestamp, antenna, rssi, reader_ip,
                 match_impresion: bool,
                 impresion_folio, impresion_id, producto_nombre, sku, color, talla,
                 barcode_value, serial, estado, detalle_id,
                 match_debug: {scan_epc, scan_epc_len, variants_tried, variant_used,
                               detalle_epc_raw, detalle_epc_len, detalle_epc_variants} |
                              {scan_epc, scan_epc_len, variants_tried, variant_used:null,
                               detalle_lookup_count:int}
                }, ...
              ],
              debug_get: {...lookup info, query_epc_search si lo mandaste...}
            }
        """
        scans = list(
            RfidScan.objects.order_by("-created_at", "-id")[:50]
        )
        epc_list = [s.epc for s in scans if s.epc]
        epc_lower_set = {e.lower() for e in epc_list if e}

        def _epc_variants(epc):
            base = (epc or "").strip().lower()
            if not base:
                return set()
            vars = {base}
            vars.add(base.lstrip("0"))
            vars.add(base.rstrip("0"))
            vars.add(base.strip("0"))
            if len(base) > 24:
                vars.add(base[:24])
                vars.add(base[-24:])
                vars.add(base[:24].lstrip("0"))
                vars.add(base[-24:].lstrip("0"))
                for target_len in (28, 32):
                    if len(base) >= target_len:
                        vars.add(base[:target_len])
                        vars.add(base[-target_len:])
            elif len(base) < 24:
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

        user = request.user
        detalle_qs = (
            EtiquetaRFIDDetalle.objects.filter(
                epc__in=list(epc_search_set) + list({v.upper() for v in epc_search_set})
            )
            .select_related(
                "impresion",
                "impresion__producto",
                "impresion__producto_variante",
                "impresion__producto_variante__color",
                "impresion__producto_variante__talla",
            )
            .only(
                "epc", "barcode_value", "serial", "estado",
                "impresion__id",
                "impresion__producto_id", "impresion__producto__nombre",
                "impresion__producto_variante_id", "impresion__producto_variante__nombre",
                "impresion__producto_variante__sku",
                "impresion__producto_variante__color_id",
                "impresion__producto_variante__color__nombre",
                "impresion__producto_variante__talla_id",
                "impresion__producto_variante__talla__nombre",
            )
        )
        # Scope empresa (solo ver scans que hagan match con detalles de mi empresa)
        empresa = getattr(user, "empresa", None)
        if empresa and not getattr(user, "is_superuser", False):
            detalle_qs = detalle_qs.filter(impresion__empresa=empresa)
        sucursales_ok = None
        if not (getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False)):
            sucursales_ok = user.sucursales_permitidas()
            if sucursales_ok:
                detalle_qs = detalle_qs.filter(impresion__sucursal_id__in=sucursales_ok)

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
                    nombre_producto = variante.nombre or (producto.nombre if producto else None)
                elif producto:
                    nombre_producto = producto.nombre
                item.update({
                    "match_impresion": True,
                    "impresion_folio": impresion.folio if impresion else None,
                    "impresion_id": impresion.id if impresion else None,
                    "producto_nombre": nombre_producto,
                    "sku": sku,
                    "color": color_nombre,
                    "talla": talla_nombre,
                    "barcode_value": detalle.barcode_value,
                    "serial": detalle.serial,
                    "estado": detalle.estado,
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
                })
            else:
                item["match_impresion"] = False
                item["match_debug"] = {
                    "scan_epc": epc_lower,
                    "scan_epc_len": len(epc_lower),
                    "variants_tried": variants_tried,
                    "variant_used": None,
                    "detalle_lookup_count": len(detalle_by_epc_variant),
                }
            data.append(item)

        epc_all_scans_lower = [s.epc.lower() for s in scans if s.epc]
        epc_all_scans_set = set(epc_all_scans_lower)
        q_epc = (request.query_params.get("epc") or "").strip().lower()
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
        return Response({"scans": data, "debug_get": debug_get})

    @action(
        detail=False,
        methods=["get"],
        url_path="scanner-stats",
        url_name="scanner_stats",
    )
    def scanner_stats(self, request):
        """Endpoint 1-clic para saber si el lector FX esta vivo SIN entrar a Vercel.

        Query params:
            epc (opcional): buscar si un EPC (ej recién impreso) existe en RfidScan.

        Response shape:
            {
              total_rfidscan_rows, last_scan_ts, last_scan_seconds_ago,
              last_5_scans: [{id,epc,epc_len,antenna,rssi,reader_ip,ts}, ...],
              query_epc_found_count, query_epc_found_samples,
              receive_endpoint_info: {fx_post_url_required, example_POST_test_1_tag}
            }
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

        q_epc = (request.query_params.get("epc") or "").strip().lower()
        q_found_samples = []
        if q_epc:
            def _v(epc):
                base = epc
                vars = {base, base.lstrip("0"), base.rstrip("0"), base.strip("0")}
                if len(base) > 24:
                    vars |= {base[:24], base[-24:]}
                if len(base) < 24:
                    vars |= {base.rjust(24, "0"), base.ljust(24, "0")}
                return vars
            base_vars = _v(q_epc)
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
                "fx_post_url_required": "POST https://TU-BACKEND/QA/scanner_rfid/receive/ (el FX llama aquí, NO Next.js)",
                "method_required": "POST (FX no manda token; esta ruta es @csrf_exempt. Next.js NO usa receive/)",
                "example_POST_test_1_tag": (
                    "Invoke-RestMethod -Uri 'https://nucleo-erp.vercel.app/QA/scanner_rfid/receive/' "
                    "-Method POST -ContentType 'application/json' -Body "
                    "ConvertTo-Json(@(@{epcId='000012e32827000147c0c5f5';antennaPort=1;peakRssiValue=-45}))"
                ),
                "note": "Next.js solo consume scans/ (polling), scans/clear (purge) y scanner-stats (debug).",
            },
        }
        return Response(payload)

    @action(
        detail=False,
        methods=["post"],
        url_path="scans/clear",
        url_name="scans_clear",
    )
    def scans_clear(self, request):
        """Purge list: borra todos los renglones de RfidScan (lecturas).
        Respuesta: {"status": "success", "deleted": N}
        """
        deleted, _ = RfidScan.objects.all().delete()
        return Response({"status": "success", "deleted": deleted})
