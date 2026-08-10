import math

from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import ValidationError, APIException
from produccion.models import OrdenesReflejante, OrdenReflejanteDetalle
from ventas.models import Pedido, PedidoDetalleTalla
from produccion.services.common import EPS_CANTIDAD, cantidades_asignadas, crear_orden_con_guardia_duplicado, payload_duplicada, pendientes_por_linea, revisar_empresa, tallas_orden_trabajo_qs
from produccion.utils.folios import generate_or_folio


class OrdenReflejanteDuplicada409(APIException):
    status_code = 409
    default_detail = "Ya existe una orden de reflejante activa para este pedido."
    default_code = "orden_reflejante_duplicada"


class OrdenReflejanteService:

    @staticmethod
    def _validar_contexto(pedido, user):
        """Scope empresa/sucursal del pedido contra el usuario que solicita.

        Mismo criterio y mismos mensajes que
        ``OrdenBordadoService._validar_contexto``: sin esta puerta un usuario de
        la empresa A podía enviar un pedido de la empresa B y el service
        estampaba la orden con ``pedido.empresa`` —creando documento, gastando
        un folio de la serie ajena y devolviendo datos de negocio de B—.

        Se ejecuta **antes de cualquier escritura** (en particular antes de
        ``generate_or_folio``) para que un rechazo no consuma consecutivo.
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
                "de reflejante."
            )

    @staticmethod
    def _tallas_reflejante_qs(pedido_id):
        return tallas_orden_trabajo_qs(pedido_id, "lleva_reflejante")

    @staticmethod
    def _payload_duplicada(existente):
        return payload_duplicada(
            existente,
            folio_field="folio_reflejante",
            estatus_display="get_estatus_reflejante_display",
            estatus_field="estatus_reflejante",
            payload_key="orden_reflejante_existente",
            tipo_label="reflejante",
            dividir_label="el reflejante",
        )

    @staticmethod
    def buscar_existente_full_match(pedido):
        """Devuelve una OR activa si el pedido ya está cubierto al 100%.

        "Cubierto" se mide en **piezas**, no en número de renglones: hay que
        comparar lo asignado por las ORs activas contra
        ``PedidoDetalleTalla.cantidad`` línea por línea. Contar renglones (lo
        que hacía antes) trataba una OR parcial que toca todas las líneas con
        cantidades reducidas como cobertura total, y devolvía un 409 diciendo
        "ya existe ... con el 100% de las prendas" sobre un pedido cubierto a
        medias.

        Sólo se consulta en el POST **sin** ``detalles_override``. Si queda
        cualquier saldo, devuelve ``None`` y el chequeo de cupo de ``save()``
        produce el 400 con las cantidades exactas que faltan.

        Misma regla que ``OrdenBordadoService.buscar_existente_full_match``.
        """
        tallas = list(OrdenReflejanteService._tallas_reflejante_qs(pedido.id))
        if not tallas:
            return None

        por_linea, sin_talla = OrdenReflejanteService._cantidades_asignadas_por_linea(pedido)
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
            OrdenesReflejante.objects.filter(
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
        """``common.cantidades_asignadas`` para ORs; ver allí el contrato.

        La FK a la orden padre es ``orden_r`` (``OrdenReflejanteDetalle``); el
        filtro decía ``or_r__...``, que no existe y reventaba con
        ``FieldError: Cannot resolve keyword 'or_r'`` en cuanto se consultaba
        el cupo.
        """
        return cantidades_asignadas(OrdenReflejanteDetalle, "orden_r", pedido_ids)

    @staticmethod
    def contratado_por_pedido(pedido_ids):
        """Piezas contratadas de reflejante por pedido, en UNA query.

        Denominador de la cobertura: sólo las tallas con ``lleva_reflejante=True``
        y ``cantidad > 0`` —el mismo criterio que ``tallas_orden_trabajo_qs``
        aplica al crear la orden—. Contar todas las líneas del pedido
        subestimaría la cobertura, porque incluiría piezas que ninguna OR puede
        cubrir.

        Mismo contrato que ``OrdenBordadoService.contratado_por_pedido``.
        """
        filas = (
            PedidoDetalleTalla.objects
            .filter(
                pedido_detalle__pedido_id__in=list(pedido_ids),
                lleva_reflejante=True,
                cantidad__gt=0,
            )
            .values("pedido_detalle__pedido_id")
            .annotate(total=Sum("cantidad"))
        )
        return {
            f["pedido_detalle__pedido_id"]: float(f["total"] or 0) for f in filas
        }

    @staticmethod
    def cobertura_por_orden(ordenes):
        """Cobertura de cada OR sobre lo contratado por su pedido.

        Devuelve ``{or_id: {"cubierto": int, "contratado": int, "completa": bool}}``.

        Mide cuánto cubre **esta** orden del total contratado del pedido —no si
        al pedido le queda saldo entre todas sus ORs, que es otra pregunta y
        rendiría el mismo valor para dos parciales distintas del mismo pedido—.

        Cuesta **2 queries constantes** para todo el conjunto, sin trabajo por
        fila: una suma agrupada por ``orden_r_id`` (numerador) y otra agrupada
        por pedido (denominador). No se reutiliza
        ``cantidades_asignadas_por_pedidos`` aquí a propósito: devuelve una fila
        por línea, mucho más de lo que hace falta para un total.

        Los renglones con ``talla`` NULL no distorsionan nada: el numerador suma
        **todos** los renglones de la OR sin mirar la talla, así que el total es
        exacto aunque el reparto por talla no se pueda atribuir.

        Mismo contrato que ``OrdenBordadoService.cobertura_por_orden``, incluido
        el piso (ver el comentario de abajo).
        """
        ordenes = list(ordenes)
        if not ordenes:
            return {}

        or_ids = [o.pk for o in ordenes]
        pedido_ids = {o.pedido_id for o in ordenes}

        cubierto_por_or = {
            f["orden_r_id"]: float(f["total"] or 0)
            for f in (
                OrdenReflejanteDetalle.objects
                .filter(orden_r_id__in=or_ids)
                .values("orden_r_id")
                .annotate(total=Sum("cantidad"))
            )
        }
        contratado = OrdenReflejanteService.contratado_por_pedido(pedido_ids)

        resultado = {}
        for orden in ordenes:
            cubierto = cubierto_por_or.get(orden.pk, 0.0)
            total = contratado.get(orden.pedido_id, 0.0)
            # PISO, no redondeo. Con redondeo, un cubierto=9.6 sobre
            # contratado=10.0 publicaba ``cubierta=10``/``contratada=10``
            # —sobre-reporta cobertura que no existe—. Con piso publica
            # ``cubierta=9``, que sub-reporta (hay 9.6, no 9) pero nunca finge
            # una pieza que no se ha cubierto.
            #
            # ``+ EPS_CANTIDAD`` antes del piso compensa el ruido de punto
            # flotante de ``Sum()`` sobre ``FloatField`` (una suma que
            # matemáticamente da 10.0 puede llegar como 9.999999999999998), no
            # cantidades fraccionarias reales.
            #
            # En reflejante los fraccionarios son MÁS alcanzables que en
            # bordado: ``OrdenReflejanteDetalle.cantidad`` es ``FloatField`` y
            # —a diferencia de OB— el serializer de OR no rechazaba enteros
            # hasta este mismo cambio, así que puede haber renglones históricos
            # con decimales reales.
            #
            # La bandera se deriva de estos MISMOS enteros ya calculados con
            # piso, no de los floats crudos, para que no pueda disentir de lo
            # que ve el cliente.
            contratado_int = math.floor(total + EPS_CANTIDAD)
            # ``min(..., contratado_int)``: el numerador suma TODOS los
            # renglones de la orden, pero el denominador sólo cuenta
            # ``PedidoDetalleTalla`` que SIGUEN con ``lleva_reflejante=True`` y
            # ``cantidad > 0``. Si el pedido se edita después de crear la OR
            # (se desmarca una línea o su cantidad baja a 0), el numerador no
            # se entera y ``cubierto`` puede quedar por encima de
            # ``contratado`` —publicando algo como "16 de 10"—. Se acota al
            # denominador para que ``cubierto <= contratado`` sea siempre
            # cierto, sin fingir que el pedido devolvió piezas que no cubrió.
            cubierto_int = min(math.floor(cubierto + EPS_CANTIDAD), contratado_int)
            resultado[orden.pk] = {
                "cubierto": cubierto_int,
                "contratado": contratado_int,
                # Un pedido sin piezas contratadas de reflejante no se declara
                # "cubierto al 100%": no hay nada que cubrir y afirmarlo
                # confundiría más que el ``false``.
                "completa": contratado_int > 0 and cubierto_int >= contratado_int,
            }
        return resultado

    @staticmethod
    def partialidad_de_orden(orden):
        """Contexto de parcialidad para el DETALLE de una OR.

        Devuelve ``(por_linea, por_detalle, hermanas, reparto_aproximado)``:

        - ``por_linea``: ``{(pedido_detalle_id, talla_id): (pedido, asignada,
          pendiente)}`` con lo contratado, lo ya programado por **todas** las
          ORs activas del pedido y el saldo.
        - ``por_detalle``: lo mismo agregado por ``pedido_detalle_id``. Es el
          respaldo para los renglones de la OR cuya ``talla`` es NULL —los
          genera el pipeline de picking—, que no tienen entrada por talla
          porque el mapa se arma desde ``PedidoDetalleTalla``. Sin él esos
          renglones salían con los tres campos en ``null``, justo el caso que
          ``reparto_aproximado`` dice estar describiendo.
        - ``hermanas``: las otras ORs activas del mismo pedido.
        - ``reparto_aproximado``: ``True`` si el pedido tiene piezas programadas
          sin talla identificable. Esas piezas no se pueden atribuir a una talla
          concreta, así que ``pendientes_por_linea`` las drena en orden contra
          las líneas del mismo ``pedido_detalle``: el total por renglón queda
          exacto, pero el reparto entre tallas es aproximado.

        Cuesta 3 queries constantes (tallas del pedido, asignado por línea,
        hermanas), independientemente del número de renglones.

        Mismo contrato que ``OrdenBordadoService.partialidad_de_orden``.
        """
        tallas = list(
            OrdenReflejanteService._tallas_reflejante_qs(orden.pedido_id)
            # Orden explícito: ``tallas_orden_trabajo_qs`` no lo declara y
            # ``PedidoDetalleTalla`` no tiene ``Meta.ordering``, así que el
            # orden lo decidía la BD. ``pendientes_por_linea`` drena el pool sin
            # talla contra las primeras líneas que encuentra, de modo que dos
            # GET idénticos podían repartir ``asignada``/``pendiente`` distinto
            # entre renglones (los totales por renglón sí coincidían).
            .order_by("id")
            .values("pedido_detalle_id", "talla_id", "cantidad")
        )
        por_linea_asignado, sin_talla = (
            OrdenReflejanteService.cantidades_asignadas_por_pedidos([orden.pedido_id])
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
            OrdenesReflejante.objects
            .filter(pedido_id=orden.pedido_id, activo=True)
            .exclude(pk=orden.pk)
            .order_by("id")
            .values("id", "folio_reflejante", "fecha_inicio")
        )

        reparto_aproximado = any(v > EPS_CANTIDAD for v in sin_talla.values())
        return por_linea, por_detalle, hermanas, reparto_aproximado

    @staticmethod
    def _cantidades_asignadas_por_linea(pedido):
        """Suma lo ya programado en ORs activas por cada línea (pedido_detalle, talla).

        Retorna ``(por_linea, sin_talla)``; solo considera
        ``OrdenesReflejante.activo=True``.
        """
        return OrdenReflejanteService.cantidades_asignadas_por_pedidos([pedido.pk])

    @staticmethod
    @transaction.atomic
    def save(data, user):
        pedido = data.get("pedido")

        OrdenReflejanteService._validar_contexto(pedido, user)

        sucursal = user.sucursal_default

        if sucursal is None:
            raise ValidationError({"err": "El usuario no tiene una sucursal asignada."})

        # Candado de concurrencia. La constraint
        # ``uq_orden_reflejante_activa_por_pedido`` era, además del
        # antiduplicado, lo que impedía que dos POST simultáneos se pisaran; al
        # quitarla (migración ``0025``) el cupo por línea quedó como único
        # guardián y se lee sin bloqueo: bajo READ COMMITTED ambas transacciones
        # veían ``ya=0`` y ambas insertaban, programando el doble de piezas. Se
        # serializa por pedido tomando el renglón de ``Pedido`` antes de leer lo
        # asignado.
        # En SQLite ``select_for_update`` es no-op (``has_select_for_update``
        # False) y la suite corre igual; en Postgres es el bloqueo real.
        # Mismo candado y misma colocación que ``OrdenBordadoService.save``.
        Pedido.objects.select_for_update().filter(pk=pedido.pk).first()

        detalle_tallas_raw = list(
            OrdenReflejanteService._tallas_reflejante_qs(pedido.id).select_related(
                "pedido_detalle", "talla"
            )
        )

        if not detalle_tallas_raw:
             raise ValidationError({
                "err": "El pedido no tiene detalles con reflejante para generar la orden."
            })

        # Foto de la cantidad contratada por línea **antes** de que la rama de
        # ``detalles_override`` pise ``dt.cantidad`` con lo solicitado. El
        # chequeo de cupo de abajo necesita el valor original del pedido (SSoT
        # de ``PedidoDetalleTalla.cantidad``); leerlo de ``dt`` después de la
        # mutación compararía lo pedido contra sí mismo y anularía el chequeo.
        # Sustituye a la re-consulta por línea que hacía el loop de cupo (una
        # query por renglón, N+1): son los mismos registros que ya trajo
        # ``_tallas_reflejante_qs``.
        cantidad_pedido_por_id = {
            dt.id: float(dt.cantidad or 0) for dt in detalle_tallas_raw
        }

        detalles_override = data.get("detalles_override") or []
        if detalles_override:
            by_id = {dt.id: dt for dt in detalle_tallas_raw}
            # Se arma con un loop, no con un dict comprehension: ``int()``
            # coacciona ``5`` y ``"5"`` (o ``5.0``) a la MISMA clave, así que
            # un comprehension los colapsaba en silencio y se quedaba con el
            # último valor —el request pedía dos renglones distintos y sólo se
            # programaba uno, sin ningún error—. Aquí se detecta la colisión
            # tras coaccionar y se rechaza explícitamente.
            override_by_id = {}
            for item in detalles_override:
                raw_id = item.get("pedido_detalle_talla_id")
                raw_cantidad = item.get("cantidad")
                if raw_id is None or raw_cantidad is None:
                    continue
                pdt_id = int(raw_id)
                if pdt_id in override_by_id:
                    raise ValidationError({
                        "err": (
                            f"`pedido_detalle_talla_id={pdt_id}` repetido en "
                            "`detalles_override`."
                        )
                    })
                override_by_id[pdt_id] = float(raw_cantidad)

            detalle_tallas_sel = []
            for pdt_id, cantidad in override_by_id.items():
                # Se RECHAZA el id desconocido en vez de descartarlo en
                # silencio: la versión anterior filtraba los que no estaban en
                # ``detalle_tallas_raw`` y seguía adelante, así que un POST con
                # una línea inexistente —o con una talla en cantidad 0, que
                # ``tallas_orden_trabajo_qs`` excluye— devolvía 201 con menos
                # renglones de los pedidos, sin decir cuál se perdió.
                if pdt_id not in by_id:
                    raise ValidationError({
                        "err": (
                            f"`pedido_detalle_talla_id={pdt_id}` no pertenece "
                            "a este pedido o no lleva servicio de reflejante."
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
                "err": "No se seleccionaron líneas para generar la orden de reflejante."
            })

        # El 409 de pedido completo va **antes** del cupo por línea: un POST sin
        # override sobre un pedido ya cubierto al 100% es el duplicado clásico y
        # debe contestar 409, no el 400 de exceso —que es lo que pasaba al
        # evaluar el cupo primero, porque ese caso también agota el cupo—.
        # Sin override se pide el pedido completo tal cual salió de
        # ``_tallas_reflejante_qs``, así que la condición es exactamente esa.
        # Mismo orden que ``OrdenBordadoService.save``.
        if not detalles_override:
            existente = OrdenReflejanteService.buscar_existente_full_match(pedido)
            if existente is not None:
                raise OrdenReflejanteDuplicada409(
                    OrdenReflejanteService._payload_duplicada(existente)
                )

        asignado_por_linea, asignado_sin_talla = (
            OrdenReflejanteService._cantidades_asignadas_por_linea(pedido)
        )
        errores_lineas = []
        for dt in detalle_tallas:
            key = (dt.pedido_detalle_id, getattr(dt.talla, "id", None))
            disponible = cantidad_pedido_por_id.get(dt.id, 0.0)
            nuevo = float(getattr(dt, "cantidad", None) or 0)
            ya = asignado_por_linea.get(key, 0.0)
            faltante = max(0.0, disponible - ya)
            # ``+ EPS_CANTIDAD``: ``cantidad`` es ``FloatField`` y sumar varias
            # parcialidades deja residuos de ~1e-15, así que una asignación
            # legítima que agota el cupo exacto (4 + 6 sobre 10) podía quedar
            # un epsilon por encima de ``faltante`` y producir un 400 falso.
            if nuevo > faltante + EPS_CANTIDAD:
                errores_lineas.append(
                    f"  - talla_id={key[1]} pedido_detalle_id={key[0]}: "
                    f"pedido={disponible}, ya_asignado={ya}, solicitado={nuevo}, "
                    f"disponible_restante={faltante}"
                )

        # Segundo corte, por ``pedido_detalle``. Absorbe las piezas ya
        # programadas sin talla identificable (``asignado_sin_talla``), que el
        # corte por línea no puede ver: sin esto, una OR generada desde picking
        # sobre una talla sin variante no consumía cupo y se podía volver a
        # programar el pedido completo. Este valor se calculaba y se descartaba.
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
                    "No se puede generar la orden de reflejante: una o más líneas "
                    "exceden la cantidad disponible del pedido."
                ),
                "detalles_exceso": errores_lineas,
            })

        # El folio se consume DESPUÉS de todas las validaciones, para que un
        # rechazo no gaste consecutivo de la serie.
        folio_reflejante = generate_or_folio(pedido.empresa_id, pedido.sucursal_id)

        orden_reflejante = crear_orden_con_guardia_duplicado(
            OrdenesReflejante,
            pedido,
            dict(
                empresa=pedido.empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                folio_reflejante=folio_reflejante,
                usuario_asignado=user,
                prioridad=data.get("prioridad", 1),
                observaciones=data.get("observaciones"),
            ),
            OrdenReflejanteDuplicada409,
            OrdenReflejanteService._payload_duplicada,
        )

        bulk_data = []
        for dt in detalle_tallas:
            # ``reflejante_config`` NO siempre es un dict: en el 100% de los
            # registros reales es un ARREGLO de un elemento
            # ``[{"tipo": …, "opcion": …, "posicion": …}]``. Sin normalizarlo,
            # el ``cfg.get(...)`` de abajo reventaba con ``AttributeError:
            # 'list' object has no attribute 'get'`` y el POST respondía 500
            # antes de construir un solo renglón —de ahí que el ``metros=`` de
            # más abajo nunca llegara a ejecutarse—.
            #
            # Se toma el primer elemento del arreglo, no ``{}``: sus claves
            # ``tipo`` y ``posicion`` son las que alimentan ``tipo_reflejante``
            # y ``posicion`` del renglón (mismo nombre, mismo significado), así
            # que descartarlas guardaría la orden entera sin especificación de
            # reflejante. No se inventa ningún ``ubicaciones``/``foto``/
            # ``notas``: para reflejante ese dato no existe (ver
            # ``OrdenReflejanteDetalleSerializer._get_cfg_dict``).
            cfg_raw = getattr(dt, "reflejante_config", None)
            if isinstance(cfg_raw, dict):
                cfg = cfg_raw
            elif isinstance(cfg_raw, list) and cfg_raw and isinstance(cfg_raw[0], dict):
                cfg = cfg_raw[0]
            else:
                cfg = {}
            ubicaciones = cfg.get("ubicaciones") or []
            if isinstance(ubicaciones, list) and ubicaciones:
                primera_ubic = ubicaciones[0] or {}
            else:
                primera_ubic = {}
            posicion_sugerida = (
                cfg.get("posicion")
                or primera_ubic.get("codigo")
                or primera_ubic.get("nombre")
            )
            bulk_data.append(OrdenReflejanteDetalle(
                orden_r=orden_reflejante,
                pedido_detalle=dt.pedido_detalle,
                producto_id=dt.pedido_detalle.producto_id,
                cantidad=float(getattr(dt, "cantidad", None) or 0),
                talla=dt.talla,
                color=getattr(dt.pedido_detalle, "color", None),
                tipo_reflejante=cfg.get("tipo_reflejante") or cfg.get("tipo"),
                posicion=posicion_sugerida,
                # El campo del modelo se llama ``metros``; ``metros_reflejante``
                # no existe en ``OrdenReflejanteDetalle`` y reventaba el alta
                # entera con ``TypeError``. La expresión ya leía ``metros`` del
                # config, así que la intención siempre fue este campo.
                metros=(
                    cfg.get("metros") or cfg.get("metros_reflejante") or 0
                ),
                # El ``reflejante_config`` COMPLETO —``cfg_raw``, no ``cfg``—:
                # ``cfg`` es sólo el elemento ``[0]``, que es justo lo que se
                # quiere dejar de perder. Los tres escalares de arriba siguen
                # derivándose de ``[0]`` y no cambian.
                #
                # El guardia de ``OrdenCorteMangaService.save`` es
                # ``isinstance(cfg, dict)``: aquí NO se puede copiar literal
                # porque el config de reflejante es una LISTA, y ese guardia lo
                # convertiría en ``None`` en el 100% de los casos —el mismo
                # desajuste de forma lista/objeto que ya causó tres bugs—. Se
                # conserva su INTENCIÓN (guardar entero, ``None`` si viene
                # vacío) admitiendo ambas formas.
                configuracion=(
                    cfg_raw if isinstance(cfg_raw, (dict, list)) and cfg_raw else None
                ),
            ))

        OrdenReflejanteDetalle.objects.bulk_create(bulk_data)
        return orden_reflejante
