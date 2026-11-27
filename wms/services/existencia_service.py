from collections import defaultdict
from decimal import Decimal

from django.db.models import Case, F, IntegerField, Q, Sum, When

from inventarios.models import Existencia, inventario_reservas
from wms.utils.decimales import normalizar_decimal


class ExistenciaService:
    """Cálculo de existencia física, reservada y disponible por clave de stock.

    La *clave de stock* de una línea es ``(producto_id, producto_variante_id,
    variante_produccion_id)`` —el tercero sólo en muestras, cuyo stock vive en
    ``Existencia.variante_produccion`` sin producto ni variante— y es **exacta**: ``(P, None)`` designa el stock del producto *sin variante*, no
    la suma de todas sus variantes (``Existencia.producto`` está poblado también en
    las filas que sí tienen variante, así que filtrar sólo por producto mezclaba
    ambos espacios).

    Todos los caminos —``get_existencia_agregada`` (POST), ``get_existencia_batch``
    (GET del onboarding) y la validación de ``TransferenciaService``— se apoyan en
    los mismos dos helpers, ``_sum_existencia_por_clave`` y
    ``_sum_reservas_por_clave``, para que no puedan volver a divergir.
    """

    #: Estados de reserva que bloquean existencia disponible.
    #:
    #: Sólo ``ACTIVA``. Una reserva ``APLICADA`` ya se materializó: en
    #: ``PickingService.handle_store`` la transferencia descuenta el stock físico
    #: del almacén origen *antes* de que ``apply_to_picking`` marque las reservas
    #: como aplicadas, y ambas cosas ocurren en la misma transacción. Por tanto
    #: ``Existencia.cantidad`` ya refleja ese consumo y restar además la reserva
    #: sería descontarlo dos veces. ``LIBERADA`` y ``CANCELADA`` no bloquean nada.
    ESTADOS_RESERVA_BLOQUEANTES = (inventario_reservas.Estado.ACTIVA,)

    _normalize = staticmethod(normalizar_decimal)

    @staticmethod
    def _key_filters(producto_id, producto_variante_id, variante_produccion_id=None):
        """Filtro canónico de ``Existencia`` para una clave de stock.

        Con SKU de muestra la identidad es ese SKU; con variante, la variante; sin
        variante son las filas del producto que explícitamente no tienen variante.
        """
        if variante_produccion_id:
            return {"variante_produccion_id": variante_produccion_id}
        if producto_variante_id:
            return {"producto_variante_id": producto_variante_id}
        return {"producto_id": producto_id, "producto_variante_id": None}

    @staticmethod
    def _partes(clave):
        """``(producto_id, variante_id, muestra_id)`` de una clave de 2 o 3 elementos."""
        return (tuple(clave) + (None,))[:3]

    @classmethod
    def _split_keys(cls, keys):
        """Indexa las claves pedidas por muestra, por variante y por producto-sin-variante."""
        por_variante = {}
        sin_variante = {}
        por_muestra = {}
        for clave in keys:
            producto_id, variante_id, muestra_id = cls._partes(clave)
            if muestra_id is not None:
                por_muestra[muestra_id] = clave
            elif variante_id is not None:
                por_variante[variante_id] = clave
            elif producto_id is not None:
                sin_variante[producto_id] = clave
        return por_variante, sin_variante, por_muestra

    @staticmethod
    def _or_conditions(condiciones):
        if not condiciones:
            return None
        q_cond = condiciones[0]
        for condicion in condiciones[1:]:
            q_cond = q_cond | condicion
        return q_cond

    @classmethod
    def get_existencia_rows(cls, almacen, producto, producto_variante, lock=False, variante_produccion=None):
        """Todas las filas de ``Existencia`` de la clave en el almacén, por pk.

        Un mismo producto/variante puede estar repartido en varias ubicaciones;
        quien valide o descuente stock debe recorrerlas todas, no quedarse con la
        primera. Con ``lock=True`` se bloquean para escritura (exige transacción).
        """
        almacen_id = getattr(almacen, "pk", almacen)
        producto_id = getattr(producto, "pk", producto)
        producto_variante_id = getattr(producto_variante, "pk", producto_variante)

        qs = Existencia.objects.filter(
            almacen_id=almacen_id,
            **cls._key_filters(producto_id, producto_variante_id, getattr(variante_produccion, "pk", variante_produccion)),
        )
        if lock:
            qs = qs.select_for_update()
        return list(qs.order_by("pk"))

    @staticmethod
    def _fila_es_de_clave(fila, clave):
        """Mismo criterio que ``_key_filters``, sobre una fila ya leída.

        ``fila``: un ``Existencia`` o un dict con ``producto_id``,
        ``producto_variante_id`` y ``variante_produccion_id``.
        """
        leer = fila.get if isinstance(fila, dict) else (lambda campo: getattr(fila, campo))
        producto_id, variante_id, muestra_id = clave
        if muestra_id:
            return leer("variante_produccion_id") == muestra_id
        if variante_id:
            return leer("producto_variante_id") == variante_id
        return leer("producto_id") == producto_id and leer("producto_variante_id") is None

    @classmethod
    def _filtro_claves(cls, claves):
        """``Q`` que trae, en una consulta, las filas de todas las ``claves``.

        Es la unión de los ``_key_filters`` de cada clave; quien la use reparte
        luego las filas con ``_fila_es_de_clave``.
        """
        por_variante, sin_variante, por_muestra = cls._split_keys(claves)
        condiciones = []
        if por_muestra:
            condiciones.append(Q(variante_produccion_id__in=list(por_muestra)))
        if por_variante:
            condiciones.append(Q(producto_variante_id__in=list(por_variante)))
        if sin_variante:
            condiciones.append(Q(producto_id__in=list(sin_variante), producto_variante_id__isnull=True))
        return cls._or_conditions(condiciones)

    @classmethod
    def _claves_en_lote(cls, claves):
        """Claves distintas que se pueden consultar juntas (con algún id)."""
        return [k for k in dict.fromkeys(tuple(cls._partes(k)) for k in claves) if any(k)]

    @classmethod
    def get_existencia_rows_por_clave(cls, almacen, claves, lock=False):
        """``{clave: [filas por pk]}`` de varias claves en **una** consulta.

        Equivale a ``get_existencia_rows`` clave por clave (mismo filtro, mismo
        orden por pk) con un solo viaje a la BD. Con ``lock=True`` es un solo
        ``SELECT ... FOR UPDATE`` que bloquea en orden de pk, así que dos
        operaciones sobre las mismas claves ya no se bloquean en órdenes
        cruzados. Una clave sin ningún id no se incluye: quien la necesite la
        consulta aparte con ``get_existencia_rows``, como siempre.
        """
        validas = cls._claves_en_lote(claves)
        resultado = {k: [] for k in validas}
        if not validas:
            return resultado
        qs = Existencia.objects.filter(almacen_id=getattr(almacen, "pk", almacen)).filter(
            cls._filtro_claves(validas)
        )
        if lock:
            qs = qs.select_for_update()
        for fila in qs.order_by("pk"):
            for clave in validas:
                if cls._fila_es_de_clave(fila, clave):
                    resultado[clave].append(fila)
        return resultado

    @classmethod
    def get_existencia_por_clave(cls, almacen, claves):
        """``{clave: fila destino}`` de varias claves: lo de ``get_existencia`` en lote.

        Cada clave recibe su fila de menor pk (``None`` si no tiene) y solo esa
        queda bloqueada, como en ``get_existencia``: 2 consultas sin importar el
        número de claves (una lee los pk, la otra bloquea en orden de pk; con una
        sola clave, la consulta de ``get_existencia``). Si la
        fila elegida desapareció entre ambas, esa clave se resuelve con
        ``get_existencia``, que es lo que se habría hecho antes.
        """
        almacen_id = getattr(almacen, "pk", almacen)
        claves = [tuple(cls._partes(k)) for k in claves]
        if len(set(claves)) == 1:
            # Una sola clave: la misma consulta de siempre (una, no dos).
            clave = claves[0]
            return {clave: cls.get_existencia(almacen_id, clave[0], clave[1], clave[2])}
        validas = cls._claves_en_lote(claves)
        elegido = {}
        if validas:
            filas = (
                Existencia.objects.filter(almacen_id=almacen_id)
                .filter(cls._filtro_claves(validas))
                .order_by("pk")
                .values("pk", "producto_id", "producto_variante_id", "variante_produccion_id")
            )
            for fila in filas:
                for clave in validas:
                    if clave not in elegido and cls._fila_es_de_clave(fila, clave):
                        elegido[clave] = fila["pk"]
        bloqueadas = {}
        if elegido:
            bloqueadas = {
                fila.pk: fila
                for fila in Existencia.objects.select_for_update()
                .filter(pk__in=set(elegido.values()))
                .order_by("pk")
            }
        resultado = {}
        for clave in dict.fromkeys(claves):
            pk = elegido.get(clave)
            fila = bloqueadas.get(pk) if pk is not None else None
            if fila is not None and fila.almacen_id == almacen_id and cls._fila_es_de_clave(fila, clave):
                resultado[clave] = fila
            elif clave in validas and pk is None:
                resultado[clave] = None
            else:
                resultado[clave] = cls.get_existencia(almacen_id, clave[0], clave[1], clave[2])
        return resultado

    @classmethod
    def get_existencia(cls, almacen, producto, producto_variante, variante_produccion=None):
        """Una sola fila de ``Existencia`` de la clave (la de menor pk).

        Sólo debe usarse para elegir la fila **destino** a la que sumar stock.
        Para validar o descontar del origen use ``get_existencia_rows``.
        """
        almacen_id = getattr(almacen, "pk", almacen)
        producto_id = getattr(producto, "pk", producto)
        producto_variante_id = getattr(producto_variante, "pk", producto_variante)

        return (
            Existencia.objects.select_for_update()
            .filter(
                almacen_id=almacen_id,
                **cls._key_filters(producto_id, producto_variante_id, getattr(variante_produccion, "pk", variante_produccion)),
            )
            .order_by("pk")
            .first()
        )

    @classmethod
    def _sum_existencia_por_clave(cls, almacen_id, keys):
        """Suma la existencia física del almacén agrupada por clave exacta.

        La consulta se acota a las claves pedidas y cada fila de ``Existencia``
        aporta a una sola clave.
        """
        resultado = defaultdict(lambda: Decimal("0"))
        if not keys:
            return resultado

        almacen_id = getattr(almacen_id, "pk", almacen_id)
        keys_por_variante, keys_sin_variante, keys_por_muestra = cls._split_keys(keys)

        condiciones = []
        if keys_por_muestra:
            condiciones.append(Q(variante_produccion_id__in=list(keys_por_muestra)))
        if keys_por_variante:
            condiciones.append(
                Q(producto_variante_id__in=list(keys_por_variante))
            )
        if keys_sin_variante:
            condiciones.append(
                Q(
                    producto_id__in=list(keys_sin_variante),
                    producto_variante_id__isnull=True,
                )
            )
        q_cond = cls._or_conditions(condiciones)
        if q_cond is None:
            return resultado

        rows = (
            Existencia.objects.filter(almacen_id=almacen_id)
            .filter(q_cond)
            .values("producto_id", "producto_variante_id", "variante_produccion_id")
            .annotate(total=Sum("cantidad"))
        )
        for row in rows:
            variante_id = row["producto_variante_id"]
            if row["variante_produccion_id"] is not None:
                clave = keys_por_muestra.get(row["variante_produccion_id"])
            elif variante_id is not None:
                clave = keys_por_variante.get(variante_id)
            else:
                clave = keys_sin_variante.get(row["producto_id"])
            if clave is None:
                continue
            resultado[clave] += cls._normalize(row["total"])
        return resultado

    @classmethod
    def _sum_reservas_por_clave(cls, almacen_id, keys):
        """Suma las reservas que bloquean stock, agrupadas por clave exacta.

        - **Una sola consulta**: cada fila de ``inventario_reservas`` aporta a
          *una* clave, derivada de su ``existencia`` cuando la tiene y del
          ``pedido_detalle_talla`` cuando no (``existencia`` es nullable,
          ``pedido_detalle_talla`` no). Antes se unían dos agregados solapados y
          las filas con ambos FK —el caso normal que produce
          ``create_for_picking``— se contaban dos veces.
        - Incluye reservas de **cualquier** pedido que comparta la clave en el
          almacén, no sólo las del pedido en curso.
        - Sólo los estados de ``ESTADOS_RESERVA_BLOQUEANTES``.
        - La consulta se acota a las claves pedidas: no recorre el histórico
          completo de reservas del almacén.
        """
        resultado = defaultdict(lambda: Decimal("0"))
        if not keys:
            return resultado

        almacen_id = getattr(almacen_id, "pk", almacen_id)
        keys_por_variante, keys_sin_variante, keys_por_muestra = cls._split_keys(keys)

        # Cada rama del pre-filtro selecciona la fila por una sola vía: las que
        # tienen ``existencia`` se resuelven por ella y el resto por la talla.
        # Las de muestra sólo existen ligadas a su ``existencia``.
        condiciones = []
        if keys_por_muestra:
            condiciones.append(Q(existencia__variante_produccion_id__in=list(keys_por_muestra)))
        if keys_por_variante:
            variantes = list(keys_por_variante)
            condiciones.append(Q(existencia__producto_variante_id__in=variantes))
            condiciones.append(
                Q(
                    existencia__isnull=True,
                    pedido_detalle_talla__variante_id__in=variantes,
                )
            )
        if keys_sin_variante:
            productos = list(keys_sin_variante)
            condiciones.append(
                Q(
                    existencia__producto_id__in=productos,
                    existencia__producto_variante_id__isnull=True,
                )
            )
            condiciones.append(
                Q(
                    existencia__isnull=True,
                    pedido_detalle_talla__variante_id__isnull=True,
                    pedido_detalle__producto_id__in=productos,
                )
            )
        q_cond = cls._or_conditions(condiciones)
        if q_cond is None:
            return resultado

        clave_producto = Case(
            When(existencia__isnull=False, then=F("existencia__producto_id")),
            default=F("pedido_detalle__producto_id"),
            output_field=IntegerField(),
        )
        clave_variante = Case(
            When(existencia__isnull=False, then=F("existencia__producto_variante_id")),
            default=F("pedido_detalle_talla__variante_id"),
            output_field=IntegerField(),
        )

        rows = (
            inventario_reservas.objects.filter(
                almacen_id=almacen_id,
                estado__in=cls.ESTADOS_RESERVA_BLOQUEANTES,
            )
            .filter(q_cond)
            .annotate(
                clave_producto=clave_producto, clave_variante=clave_variante,
                clave_muestra=F("existencia__variante_produccion_id"),
            )
            .values("clave_producto", "clave_variante", "clave_muestra")
            .annotate(total=Sum("cantidad"))
        )
        for row in rows:
            variante_id = row["clave_variante"]
            if row["clave_muestra"] is not None:
                clave = keys_por_muestra.get(row["clave_muestra"])
            elif variante_id is not None:
                clave = keys_por_variante.get(variante_id)
            else:
                clave = keys_sin_variante.get(row["clave_producto"])
            if clave is None:
                continue
            resultado[clave] += cls._normalize(row["total"])
        return resultado

    @classmethod
    def get_existencia_agregada(cls, almacen, producto, producto_variante, variante_produccion=None):
        """Existencia (física, reservada, disponible) de una clave en un almacén."""
        almacen_id = getattr(almacen, "pk", almacen)
        producto_id = getattr(producto, "pk", producto)
        producto_variante_id = getattr(producto_variante, "pk", producto_variante)
        clave = (producto_id, producto_variante_id, getattr(variante_produccion, "pk", variante_produccion))

        fisica = cls._normalize(
            cls._sum_existencia_por_clave(almacen_id, [clave]).get(clave)
        )
        reservada = cls._normalize(
            cls._sum_reservas_por_clave(almacen_id, [clave]).get(clave)
        )
        disponible = fisica - reservada
        if disponible < Decimal("0"):
            disponible = Decimal("0")
        return fisica, reservada, disponible

    @classmethod
    def get_disponible_por_clave(cls, almacen, claves):
        """``{clave: disponible}`` de varias claves en 2 consultas.

        Mismo número que ``get_existencia_agregada(...)[2]`` clave por clave. Los
        helpers indexan por la forma de la clave (muestra, variante o producto sin
        variante): si dos claves comparten forma, cada una recibe el total de esa
        forma, igual que si se hubieran consultado por separado.
        """
        almacen_id = getattr(almacen, "pk", almacen)
        forma_de = {}
        representante = {}
        for clave in dict.fromkeys(tuple(cls._partes(k)) for k in claves):
            producto_id, variante_id, muestra_id = clave
            if muestra_id is not None:
                forma = ("m", muestra_id)
            elif variante_id is not None:
                forma = ("v", variante_id)
            elif producto_id is not None:
                forma = ("p", producto_id)
            else:
                forma = None  # sin ningún id: 0, como en get_existencia_agregada
            forma_de[clave] = forma
            if forma is not None:
                representante.setdefault(forma, clave)
        keys = list(representante.values())
        fisica = cls._sum_existencia_por_clave(almacen_id, keys)
        reservada = cls._sum_reservas_por_clave(almacen_id, keys)
        resultado = {}
        for clave, forma in forma_de.items():
            rep = representante.get(forma)
            if rep is None:
                resultado[clave] = Decimal("0")
                continue
            disponible = cls._normalize(fisica.get(rep)) - cls._normalize(reservada.get(rep))
            if disponible < Decimal("0"):
                disponible = Decimal("0")
            resultado[clave] = disponible
        return resultado

    @classmethod
    def get_existencia_batch(cls, almacen, tallas):
        """Calcula existencia para un batch de ``PedidoDetalleTalla``.

        Retorna un dict keyed por talla_id con ``fisica``/``reservada``/
        ``disponible``. Usa los mismos helpers que ``get_existencia_agregada``,
        de modo que el máximo que anuncia el GET del onboarding y el que valida
        el POST son el mismo número.
        """
        almacen_id = getattr(almacen, "pk", almacen) if almacen else None
        result = {}
        if almacen_id is None or not tallas:
            for talla in tallas:
                talla_id = getattr(talla, "pk", talla)
                result[talla_id] = {
                    "fisica": Decimal("0"),
                    "reservada": Decimal("0"),
                    "disponible": Decimal("0"),
                }
            return result

        clave_by_talla = {}
        all_keys = set()
        for talla in tallas:
            talla_id = getattr(talla, "pk", talla)
            detalle = getattr(talla, "pedido_detalle", None)
            # Muestra: su SKU de muestra (``anotar_sku_muestra``) es la clave.
            muestra = getattr(talla, "sku_muestra_obj", None)
            clave = (None, None, muestra.pk) if muestra else (
                getattr(detalle, "producto_id", None),
                getattr(talla, "variante_id", None),
                None,
            )
            clave_by_talla[talla_id] = clave
            all_keys.add(clave)

        keys = list(all_keys)
        existencia_por_clave = cls._sum_existencia_por_clave(almacen_id, keys)
        reservada_por_clave = cls._sum_reservas_por_clave(almacen_id, keys)

        for talla in tallas:
            talla_id = getattr(talla, "pk", talla)
            clave = clave_by_talla[talla_id]
            fisica = cls._normalize(existencia_por_clave.get(clave))
            reservada = cls._normalize(reservada_por_clave.get(clave))
            disponible = fisica - reservada
            if disponible < Decimal("0"):
                disponible = Decimal("0")
            result[talla_id] = {
                "fisica": fisica,
                "reservada": reservada,
                "disponible": disponible,
            }
        return result


class SaldoExistenciaAlmacen:
    """Saldo en memoria de las filas de ``Existencia`` de un almacén, por clave.

    Las filas de una clave se leen —y bloquean— **una sola vez por clave**, no una
    vez por renglón, y todos los renglones que comparten esa clave consumen del
    mismo saldo acumulado.

    Sin esto, dos renglones de la misma clave obtenían objetos Python distintos de
    la *misma* fila de BD, ambos partiendo del saldo original: cada uno calculaba
    ``saldo_original - lo_suyo`` y el segundo ``save()`` pisaba la resta del
    primero. El descuadre es real y silencioso —el picking registra que movió la
    suma de los dos renglones y físicamente sólo se movió el último—, y aparece en
    cuanto dos tallas sin variante caen sobre la misma clave ``(producto, None)``.

    No persiste nada: ``consumir`` sólo reparte y anota en memoria. Quien mueve
    stock (``TransferenciaService``) guarda al final recorriendo
    ``filas_consumidas()``; quien sólo necesita la atribución
    (``ReservaInventarioService``) no guarda ninguna fila.

    Como ambos servicios recorren las filas en el mismo orden (pk ascendente) y
    parten del mismo estado, el reparto de la reserva y el del movimiento físico
    coinciden fila a fila.
    """

    def __init__(self, almacen, lock=True):
        self.almacen = almacen
        self._lock = lock
        self._filas_por_clave = {}
        self._fila_por_pk = {}
        self._saldo_por_pk = {}
        self._consumido_por_pk = {}
        self._precargadas = {}

    def precargar(self, claves):
        """Lee —y bloquea— de una vez las filas de varias claves.

        Sin esto cada clave nueva cuesta una consulta en ``filas()``; con esto la
        operación entera cuesta una, y el bloqueo se toma en orden de pk. No
        cambia el reparto: cada clave sigue viendo sus mismas filas, en el mismo
        orden, y solo entra al saldo cuando se pide por primera vez. Una clave
        que ya estaba cargada no se vuelve a leer.
        """
        pendientes = [
            clave for clave in (self._clave(*ExistenciaService._partes(k)) for k in claves)
            if clave not in self._filas_por_clave and clave not in self._precargadas
        ]
        if pendientes:
            self._precargadas.update(
                ExistenciaService.get_existencia_rows_por_clave(self.almacen, pendientes, lock=self._lock)
            )

    @staticmethod
    def _clave(producto, producto_variante, variante_produccion=None):
        return (
            getattr(producto, "pk", producto),
            getattr(producto_variante, "pk", producto_variante),
            getattr(variante_produccion, "pk", variante_produccion),
        )

    def filas(self, producto, producto_variante, variante_produccion=None):
        """Filas de la clave, ordenadas por pk. Se leen y bloquean una sola vez."""
        clave = self._clave(producto, producto_variante, variante_produccion)
        if clave not in self._filas_por_clave:
            if clave in self._precargadas:
                filas = self._precargadas.pop(clave)
            else:
                filas = ExistenciaService.get_existencia_rows(
                    self.almacen, clave[0], clave[1], lock=self._lock, variante_produccion=clave[2]
                )
            self._filas_por_clave[clave] = filas
            for fila in filas:
                self._fila_por_pk[fila.pk] = fila
                self._saldo_por_pk[fila.pk] = ExistenciaService._normalize(fila.cantidad)
        return self._filas_por_clave[clave]

    def disponible(self, producto, producto_variante, variante_produccion=None):
        """Saldo restante de la clave, ya descontado lo consumido en esta operación."""
        return sum(
            (self._saldo_por_pk[fila.pk] for fila in self.filas(producto, producto_variante, variante_produccion)),
            Decimal("0"),
        )

    def consumir(self, producto, producto_variante, cantidad, variante_produccion=None):
        """Reparte ``cantidad`` entre las filas de la clave, en orden de pk.

        Devuelve ``(asignaciones, faltante)`` donde ``asignaciones`` es una lista
        de ``(fila, cantidad_tomada)``. Si el saldo no alcanza no consume nada y
        ``faltante`` es lo que quedó por cubrir; el llamador decide qué error
        levantar con su propio mensaje de dominio.
        """
        cantidad = ExistenciaService._normalize(cantidad)
        filas = self.filas(producto, producto_variante, variante_produccion)
        disponible = sum((self._saldo_por_pk[fila.pk] for fila in filas), Decimal("0"))
        if disponible < cantidad:
            return [], cantidad - disponible

        asignaciones = []
        pendiente = cantidad
        for fila in filas:
            if pendiente <= Decimal("0"):
                break
            saldo = self._saldo_por_pk[fila.pk]
            a_tomar = min(saldo, pendiente)
            if a_tomar <= Decimal("0"):
                continue
            self._saldo_por_pk[fila.pk] = saldo - a_tomar
            self._consumido_por_pk[fila.pk] = (
                self._consumido_por_pk.get(fila.pk, Decimal("0")) + a_tomar
            )
            asignaciones.append((fila, a_tomar))
            pendiente -= a_tomar
        return asignaciones, Decimal("0")

    def filas_consumidas(self):
        """``(fila, saldo_final)`` de cada fila que perdió unidades.

        El saldo ya es acumulado sobre todos los renglones que compartían la
        clave, así que persistirlo una vez por fila es correcto y suficiente.
        """
        for pk, consumido in self._consumido_por_pk.items():
            if consumido > Decimal("0"):
                yield self._fila_por_pk[pk], self._saldo_por_pk[pk]
