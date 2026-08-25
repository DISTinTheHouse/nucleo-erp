import math

from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import ValidationError, APIException
from produccion.models import OrdenesBordado, OrdenBordadoDetalle
from ventas.models import Pedido, PedidoDetalleTalla
from produccion.services.common import EPS_CANTIDAD, cantidades_asignadas
from produccion.services.common import (
    config_como_dict,
    crear_orden_con_guardia_duplicado,
    payload_duplicada,
    pendientes_por_linea,
    revisar_empresa,
    tallas_orden_trabajo_qs,
)
from produccion.utils.folios import generate_ob_folio


class OrdenBordadoDuplicada409(APIException):
    status_code = 409
    default_detail = "Ya existe una orden de bordado activa para este pedido."
    default_code = "orden_bordado_duplicada"


class OrdenBordadoService:

    @staticmethod
    def _validar_contexto(pedido, user):
        """Scope empresa/sucursal del pedido contra el usuario que solicita.

        Mismo criterio y mismos mensajes que
        ``wms.services.picking_pipeline.context.validar_contexto_picking``:
        el ``pedido`` llega como id crudo desde el body y el serializer no lo
        acota a la empresa del usuario, así que sin esta puerta un usuario de
        la empresa A podía enviar un pedido de la empresa B y el service
        estampaba la orden con ``pedido.empresa`` —creando documento, gastando
        un folio de la serie ajena y devolviendo datos de negocio de B—.

        Se ejecuta **antes de cualquier escritura** (en particular antes de
        ``generate_ob_folio``) para que un rechazo no consuma consecutivo.
        """
        resultado = revisar_empresa(user, pedido)
        if resultado == "sin_empresa":
            raise ValidationError("El usuario no tiene una empresa asignada.")
        if resultado == "otra_empresa":
            raise ValidationError("El pedido no pertenece a la empresa del usuario.")

        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )
        if not es_staff and pedido.sucursal_id not in user.sucursales_permitidas():
            raise ValidationError(
                "No tiene acceso a la sucursal del pedido para generar la orden "
                "de bordado."
            )

    @staticmethod
    def _tallas_bordado_qs(pedido_id):
        return tallas_orden_trabajo_qs(pedido_id, "lleva_bordado")

    @staticmethod
    def _payload_duplicada(existente):
        return payload_duplicada(
            existente,
            folio_field="folio_bordado",
            estatus_display="get_estatus_bordado_display",
            estatus_field="estatus_bordado",
            payload_key="orden_bordado_existente",
            tipo_label="bordado",
            dividir_label="el bordado",
        )

    @staticmethod
    def buscar_existente_full_match(pedido):
        """Devuelve una OB activa si el pedido ya está cubierto al 100%.

        "Cubierto" se mide en **piezas**, no en número de renglones: hay que
        comparar lo asignado por las OBs activas contra
        ``PedidoDetalleTalla.cantidad`` línea por línea. Contar renglones (lo
        que hacía antes) trataba una OB parcial que toca todas las líneas con
        cantidades reducidas como cobertura total, y devolvía un 409 diciendo
        "ya existe ... con el 100% de las prendas" sobre un pedido cubierto a
        medias.

        Sólo se consulta en el POST **sin** ``detalles_override``. Si queda
        cualquier saldo, devuelve ``None`` y el chequeo de cupo de ``save()``
        produce el 400 con las cantidades exactas que faltan.
        """
        tallas = list(OrdenBordadoService._tallas_bordado_qs(pedido.id))
        if not tallas:
            return None

        por_linea, sin_talla = OrdenBordadoService._cantidades_asignadas_por_linea(pedido)
        for dt in tallas:
            asignado = por_linea.get((dt.pedido_detalle_id, dt.talla_id), 0.0)
            if asignado + EPS_CANTIDAD < float(dt.cantidad or 0):
                return None
        if any(v > EPS_CANTIDAD for v in sin_talla.values()):
            # Hay piezas programadas sin talla identificable (ver
            # ``cantidades_asignadas_por_pedidos``): no se puede afirmar
            # cobertura exacta, así que no se emite el 409 de duplicado.
            return None

        return (
            OrdenesBordado.objects.filter(
                empresa=pedido.empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                activo=True,
            )
            .order_by("-id")
            .first()
        )

    @staticmethod
    def cantidades_asignadas_por_pedidos(pedido_ids):
        """``common.cantidades_asignadas`` para OBs; ver allí el contrato."""
        return cantidades_asignadas(OrdenBordadoDetalle, "ob", pedido_ids)

    @staticmethod
    def contratado_por_pedido(pedido_ids):
        """Piezas contratadas de bordado por pedido, en UNA query.

        Denominador de la cobertura: sólo las tallas con ``lleva_bordado=True``
        y ``cantidad > 0`` —el mismo criterio que ``tallas_orden_trabajo_qs``
        aplica al crear la orden—. Contar todas las líneas del pedido
        subestimaría la cobertura, porque incluiría piezas que ninguna OB puede
        cubrir (en el pedido 120: 42 piezas en total contra 40 con bordado).
        """
        filas = (
            PedidoDetalleTalla.objects
            .filter(
                pedido_detalle__pedido_id__in=list(pedido_ids),
                lleva_bordado=True,
                cantidad__gt=0,
            )
            .values("pedido_detalle__pedido_id")
            .annotate(total=Sum("cantidad"))
        )
        return {
            f["pedido_detalle__pedido_id"]: float(f["total"] or 0) for f in filas
        }

    @staticmethod
    def cobertura_por_orden(ordenes, cubierto_override=None, contratado_override=None):
        """Cobertura de cada OB sobre lo contratado por su pedido.

        Devuelve ``{ob_id: {"cubierto": int, "contratado": int, "completa": bool}}``.

        Mide la definición (a): cuánto cubre **esta** orden del total contratado
        del pedido —no si al pedido le queda saldo entre todas sus OBs, que es
        otra pregunta y rendiría el mismo valor para dos parciales distintas del
        mismo pedido—.

        Cuesta **2 queries constantes** para toda la página, sin trabajo por
        fila: una suma agrupada por ``ob_id`` (numerador) y otra agrupada por
        pedido (denominador). No se reutiliza
        ``cantidades_asignadas_por_pedidos`` aquí a propósito: devuelve una
        fila por línea, mucho más de lo que hace falta para un total.

        Los renglones con ``talla`` NULL no distorsionan nada: el numerador
        suma **todos** los renglones de la OB sin mirar la talla, así que el
        total es exacto aunque el reparto por talla no se pueda atribuir.

        ``cubierto_override`` (``{ob_id: piezas}``) y ``contratado_override``
        (``{pedido_id: piezas}``) permiten saltarse una o ambas queries cuando
        el llamador ya tiene esos totales en memoria. Es el caso del DETALLE:
        ``retrieve()`` ya prefetcheó los renglones de la OB (numerador) y ya
        resolvió las tallas contratadas del pedido vía ``partialidad_de_orden``
        (denominador, mismo predicado que ``contratado_por_pedido``). El
        LISTADO no pasa ninguno de los dos y sigue costando exactamente las 2
        queries agrupadas de siempre.
        """
        ordenes = list(ordenes)
        if not ordenes:
            return {}

        ob_ids = [o.pk for o in ordenes]
        pedido_ids = {o.pedido_id for o in ordenes}

        if cubierto_override is not None:
            cubierto_por_ob = cubierto_override
        else:
            cubierto_por_ob = {
                f["ob_id"]: float(f["total"] or 0)
                for f in (
                    OrdenBordadoDetalle.objects
                    .filter(ob_id__in=ob_ids)
                    .values("ob_id")
                    .annotate(total=Sum("cantidad"))
                )
            }
        if contratado_override is not None:
            contratado = contratado_override
        else:
            contratado = OrdenBordadoService.contratado_por_pedido(pedido_ids)

        resultado = {}
        for orden in ordenes:
            cubierto = cubierto_por_ob.get(orden.pk, 0.0)
            total = contratado.get(orden.pedido_id, 0.0)
            # PISO, no redondeo. Con redondeo, un cubierto=9.6 sobre
            # contratado=10.0 publicaba ``cubierta=10``/``contratada=10``
            # —sobre-reporta cobertura que no existe—. Con piso publica
            # ``cubierta=9``, que sub-reporta (hay 9.6, no 9) pero nunca finge
            # una pieza que no se ha cubierto. Sub-reportar es el trade-off
            # aceptado; sobre-reportar —declarar completa una orden que no lo
            # está— no lo es.
            #
            # ``+ EPS_CANTIDAD`` antes del piso compensa el ruido de punto
            # flotante de ``Sum()`` sobre ``FloatField`` (una suma que
            # matemáticamente da 10.0 puede llegar como 9.999999999999998), no
            # cantidades fraccionarias reales: la tolerancia es 1e-9, muy por
            # debajo de cualquier pieza genuina, así que un 9.6 real sigue
            # dando piso 9. Sin este ajuste, un pedido genuinamente cubierto al
            # 100% podía reportarse como 9/10 por el redondeo binario de la
            # suma, que sí sería un sub-reporte espurio, no el aceptado.
            #
            # La bandera se deriva de estos MISMOS enteros ya calculados con
            # piso, no de los floats crudos: cubierto_int/contratado_int son la
            # única fuente de verdad que ve el cliente, así que la bandera no
            # puede disentir de ellos. Fraccionarios sí son alcanzables:
            # ``OrdenBordadoDetalle.cantidad`` es ``FloatField`` y el pipeline
            # de picking escribe ``float(item["cantidad"])`` sin validar
            # enteros.
            cubierto_int = math.floor(cubierto + EPS_CANTIDAD)
            contratado_int = math.floor(total + EPS_CANTIDAD)
            resultado[orden.pk] = {
                "cubierto": cubierto_int,
                "contratado": contratado_int,
                # Un pedido sin piezas contratadas de bordado no se declara
                # "cubierto al 100%": no hay nada que cubrir y afirmarlo
                # confundiría más que el ``false``.
                "completa": contratado_int > 0 and cubierto_int >= contratado_int,
            }
        return resultado

    @staticmethod
    def partialidad_de_orden(orden):
        """Contexto de parcialidad para el DETALLE de una OB.

        Devuelve ``(por_linea, por_detalle, hermanas, reparto_aproximado)``:

        - ``por_linea``: ``{(pedido_detalle_id, talla_id): (pedido, asignada,
          pendiente)}`` con lo contratado, lo ya programado por **todas** las
          OBs activas del pedido y el saldo.
        - ``por_detalle``: lo mismo agregado por ``pedido_detalle_id``. Es el
          respaldo para los renglones de la OB cuya ``talla`` es NULL —los
          genera el pipeline de picking—, que no tienen entrada por talla
          porque el mapa se arma desde ``PedidoDetalleTalla``. Sin él esos
          renglones salían con los tres campos en ``null``, justo el caso que
          ``reparto_aproximado`` dice estar describiendo.
        - ``hermanas``: las otras OBs activas del mismo pedido.
        - ``reparto_aproximado``: ``True`` si el pedido tiene piezas
          programadas sin talla identificable. Esas piezas no se pueden
          atribuir a una talla concreta, así que ``pendientes_por_linea`` las
          drena en orden contra las líneas del mismo ``pedido_detalle``: el
          total por renglón queda exacto, pero el reparto entre tallas es
          aproximado. Se expone la bandera en vez de silenciarlo.

        Cuesta 3 queries constantes (tallas del pedido, asignado por línea,
        hermanas), independientemente del número de renglones.
        """
        tallas = list(
            OrdenBordadoService._tallas_bordado_qs(orden.pedido_id)
            # Orden explícito: ``tallas_orden_trabajo_qs`` no lo declara y
            # ``PedidoDetalleTalla`` no tiene ``Meta.ordering``, así que el
            # orden lo decidía la BD. ``pendientes_por_linea`` drena el pool sin
            # talla contra las primeras líneas que encuentra, de modo que dos
            # GET idénticos podían repartir ``asignada``/``pendiente`` distinto
            # entre renglones (los totales por renglón sí coincidían). Mismo
            # criterio que el ``order_by("id")`` del Prefetch de ``detalles``.
            .order_by("id")
            .values("pedido_detalle_id", "talla_id", "cantidad")
        )
        por_linea_asignado, sin_talla = (
            OrdenBordadoService.cantidades_asignadas_por_pedidos([orden.pedido_id])
        )
        calculado = pendientes_por_linea(
            [(t["pedido_detalle_id"], t["talla_id"], float(t["cantidad"] or 0))
             for t in tallas],
            por_linea_asignado,
            sin_talla,
        )
        por_linea = {}
        por_detalle = {}
        for t, (asignada, pendiente) in zip(tallas, calculado):
            contratado = float(t["cantidad"] or 0)
            por_linea[(t["pedido_detalle_id"], t["talla_id"])] = (
                contratado, asignada, pendiente
            )
            acumulado = por_detalle.get(t["pedido_detalle_id"], (0.0, 0.0, 0.0))
            por_detalle[t["pedido_detalle_id"]] = (
                acumulado[0] + contratado,
                acumulado[1] + asignada,
                acumulado[2] + pendiente,
            )

        hermanas = list(
            OrdenesBordado.objects
            .filter(pedido_id=orden.pedido_id, activo=True)
            .exclude(pk=orden.pk)
            .order_by("id")
            .values("id", "folio_bordado", "fecha_inicio")
        )

        reparto_aproximado = any(v > EPS_CANTIDAD for v in sin_talla.values())
        return por_linea, por_detalle, hermanas, reparto_aproximado

    @staticmethod
    def _cantidades_asignadas_por_linea(pedido):
        """``cantidades_asignadas_por_pedidos`` para un solo pedido."""
        return OrdenBordadoService.cantidades_asignadas_por_pedidos([pedido.pk])

    @staticmethod
    @transaction.atomic
    def save(data, user):
        pedido = data.get("pedido")

        OrdenBordadoService._validar_contexto(pedido, user)

        sucursal = user.sucursal_default

        if sucursal is None:
            raise ValidationError({"err": "El usuario no tiene una sucursal asignada."})

        # Candado de concurrencia. La constraint ``uq_orden_bordado_activa_por
        # _pedido`` era, además del antiduplicado, lo que impedía que dos POST
        # simultáneos se pisaran; al quitarla (migración ``0026``) el cupo por
        # línea quedó como único guardián y se lee sin bloqueo: bajo READ
        # COMMITTED ambas transacciones veían ``ya=0`` y ambas insertaban,
        # programando el doble de piezas. Se serializa por pedido tomando el
        # renglón de ``Pedido`` antes de leer lo asignado.
        # En SQLite ``select_for_update`` es no-op (``has_select_for_update``
        # False) y la suite corre igual; en Postgres es el bloqueo real.
        Pedido.objects.select_for_update().filter(pk=pedido.pk).first()

        detalle_tallas_raw = list(
            OrdenBordadoService._tallas_bordado_qs(pedido.id).select_related(
                "pedido_detalle", "talla"
            )
        )

        if not detalle_tallas_raw:
            raise ValidationError({
                 "err": "El pedido no tiene detalles con bordado para generar la orden."
            })

        # Foto de la cantidad contratada por línea **antes** de que la rama de
        # ``detalles_override`` pise ``dt.cantidad`` con lo solicitado. El
        # chequeo de cupo de abajo necesita el valor original del pedido (SSoT
        # de ``PedidoDetalleTalla.cantidad``); leerlo de ``dt`` después de la
        # mutación comparaba lo pedido contra sí mismo y anulaba el chequeo.
        # Se captura aquí en vez de re-consultar por línea como hace
        # ``OrdenReflejanteService`` para no meter un N+1: son los mismos
        # registros que ya trajo ``_tallas_bordado_qs``.
        cantidad_pedido_por_id = {
            dt.id: float(dt.cantidad or 0) for dt in detalle_tallas_raw
        }

        detalles_override = data.get("detalles_override") or []
        if detalles_override:
            by_id = {dt.id: dt for dt in detalle_tallas_raw}
            override_by_id = {
                int(item["pedido_detalle_talla_id"]): float(item["cantidad"])
                for item in detalles_override
                if item.get("pedido_detalle_talla_id") is not None
                and item.get("cantidad") is not None
            }

            detalle_tallas_sel = []
            for pdt_id, cantidad in override_by_id.items():
                if pdt_id not in by_id:
                    raise ValidationError({
                        "err": (
                            f"`pedido_detalle_talla_id={pdt_id}` no pertenece "
                            "a este pedido o no lleva servicio de bordado."
                        )
                    })
                dt = by_id[pdt_id]
                dt.cantidad = cantidad
                detalle_tallas_sel.append(dt)
            detalle_tallas = detalle_tallas_sel
        else:
            detalle_tallas = list(detalle_tallas_raw)

        if not detalle_tallas:
            raise ValidationError({
                "err": "No se seleccionaron líneas para generar la orden de bordado."
            })

        # El 409 de pedido completo va **antes** del cupo por línea: un POST sin
        # override sobre un pedido ya cubierto al 100% es el duplicado clásico y
        # debe seguir contestando 409, no el 400 de exceso —que es lo que pasaba
        # al evaluar el cupo primero, porque ese caso también agota el cupo—.
        # Sin override se pide el pedido completo tal cual salió de
        # ``_tallas_bordado_qs``, así que la condición es exactamente esa.
        if not detalles_override:
            existente = OrdenBordadoService.buscar_existente_full_match(pedido)
            if existente is not None:
                raise OrdenBordadoDuplicada409(
                    OrdenBordadoService._payload_duplicada(existente)
                )

        asignado_por_linea, asignado_sin_talla = (
            OrdenBordadoService._cantidades_asignadas_por_linea(pedido)
        )
        errores_lineas = []
        for dt in detalle_tallas:
            key = (dt.pedido_detalle_id, getattr(dt.talla, "id", None))
            disponible = cantidad_pedido_por_id.get(dt.id, 0.0)
            nuevo = float(dt.cantidad or 0)
            ya = asignado_por_linea.get(key, 0.0)
            faltante = max(0.0, disponible - ya)
            if nuevo > faltante + EPS_CANTIDAD:
                errores_lineas.append(
                    f"  - talla_id={key[1]} pedido_detalle_id={key[0]}: "
                    f"pedido={disponible}, ya_asignado={ya}, solicitado={nuevo}, "
                    f"disponible_restante={faltante}"
                )

        # Segundo corte, por ``pedido_detalle``. Absorbe las piezas ya
        # programadas sin talla identificable (``asignado_sin_talla``), que el
        # corte por línea no puede ver: sin esto, una OB generada desde picking
        # sobre una talla sin variante no consumía cupo y se podía volver a
        # programar el pedido completo.
        capacidad_por_detalle = {}
        for dt in detalle_tallas_raw:
            capacidad_por_detalle[dt.pedido_detalle_id] = (
                capacidad_por_detalle.get(dt.pedido_detalle_id, 0.0)
                + cantidad_pedido_por_id.get(dt.id, 0.0)
            )
        solicitado_por_detalle = {}
        for dt in detalle_tallas:
            solicitado_por_detalle[dt.pedido_detalle_id] = (
                solicitado_por_detalle.get(dt.pedido_detalle_id, 0.0)
                + float(dt.cantidad or 0)
            )
        asignado_por_detalle = dict(asignado_sin_talla)
        for (pedido_detalle_id, _talla_id), cantidad in asignado_por_linea.items():
            asignado_por_detalle[pedido_detalle_id] = (
                asignado_por_detalle.get(pedido_detalle_id, 0.0) + cantidad
            )
        for pedido_detalle_id, solicitado in solicitado_por_detalle.items():
            capacidad = capacidad_por_detalle.get(pedido_detalle_id, 0.0)
            ya = asignado_por_detalle.get(pedido_detalle_id, 0.0)
            faltante = max(0.0, capacidad - ya)
            if solicitado > faltante + EPS_CANTIDAD:
                errores_lineas.append(
                    f"  - pedido_detalle_id={pedido_detalle_id} (total del renglón): "
                    f"pedido={capacidad}, ya_asignado={ya}, solicitado={solicitado}, "
                    f"disponible_restante={faltante}"
                )

        if errores_lineas:
            raise ValidationError({
                "err": (
                    "No se puede generar la orden de bordado: una o más líneas "
                    "exceden la cantidad disponible del pedido (ya asignado + nuevo "
                    "> cantidad_pedido por línea)."
                ),
                "detalles_exceso": errores_lineas,
            })

        folio_bordado = generate_ob_folio(pedido.empresa_id, pedido.sucursal_id)

        orden_bordado = crear_orden_con_guardia_duplicado(
            OrdenesBordado,
            pedido,
            dict(
                empresa=pedido.empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                folio_bordado=folio_bordado,
                usuario_asignado=user,
                prioridad=data.get("prioridad", 1),
                observaciones=data.get("observaciones"),
            ),
            OrdenBordadoDuplicada409,
            OrdenBordadoService._payload_duplicada,
        )

        bulk_data = []
        for detalle_talla in detalle_tallas:
            # ``cfg_raw`` (lo que guardó ventas, sin tocar) y ``cfg`` (el mismo
            # valor normalizado a dict) se separan igual que en
            # ``OrdenReflejanteService.save``: los escalares de abajo necesitan
            # un dict para poder llamar ``.get()``, pero la foto de
            # ``configuracion`` debe conservar el valor ÍNTEGRO, sea cual sea su
            # forma —descartarlo sería volver a perder datos, que es justo lo
            # que este campo vino a evitar—.
            #
            # Antes era ``bordado_config or {}``, que sobre un config con forma
            # de arreglo reventaba en el ``.get()`` de la línea siguiente con
            # ``AttributeError: 'list' object has no attribute 'get'``. Hoy
            # ``bordado_config`` es un objeto en el 100% de las filas (96/96),
            # así que esto es un no-op sobre los datos actuales; el guardia
            # existe porque ese mismo desajuste lista/objeto ya provocó tres
            # 500 en reflejante.
            cfg_raw = detalle_talla.bordado_config
            cfg = config_como_dict(cfg_raw)
            ubicaciones = cfg.get("ubicaciones") or []
            primera_ubicacion = (
                ubicaciones[0] if isinstance(ubicaciones, list) and ubicaciones else {}
            )
            posicion = (
                cfg.get("posicion")
                or primera_ubicacion.get("codigo")
                or primera_ubicacion.get("nombre")
                or None
            )
            bulk_data.append(OrdenBordadoDetalle(
                ob=orden_bordado,
                pedido_detalle=detalle_talla.pedido_detalle,
                producto_id=detalle_talla.pedido_detalle.producto_id,
                cantidad=detalle_talla.cantidad,
                talla=detalle_talla.talla,
                color=getattr(detalle_talla.pedido_detalle, "color", None),
                posicion_bordado=posicion,
                colores_hilo=(
                    int(primera_ubicacion.get("colores_hilo") or cfg.get("colores_hilo") or 0)
                ),
                puntadas=int(cfg.get("puntadas") or primera_ubicacion.get("puntadas") or 0),
                # El ``bordado_config`` COMPLETO, con todo ``ubicaciones[]``.
                # Los tres escalares de arriba siguen saliendo de
                # ``ubicaciones[0]`` y no cambian de significado; lo que cambia
                # es que ya no son el único registro: una línea con 2
                # ubicaciones perdía la segunda —y su ``colores_hilo`` y sus
                # ``puntadas``— sin dejar rastro.
                #
                # Se guarda ``cfg_raw``, no ``cfg``: si algún día llega un
                # config con otra forma, la foto lo conserva entero en vez de
                # tirarlo (mismo criterio y mismo guardia que
                # ``OrdenReflejanteService.save``). Sobre un dict —el 100% de
                # las filas de hoy— ambos son el MISMO objeto, así que el valor
                # persistido no cambia.
                configuracion=(
                    cfg_raw if isinstance(cfg_raw, (dict, list)) and cfg_raw else None
                ),
            ))

        OrdenBordadoDetalle.objects.bulk_create(bulk_data)

        return orden_bordado

