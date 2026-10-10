"""Tests de las órdenes de trabajo de producción (Reflejante, Bordado, OCM).

Cubren dos cosas:

* El antiduplicado: el bug de conteo (tallas en cantidad 0 desalineaban
  ``buscar_existente_full_match`` contra la creación del detalle, permitiendo
  esquivar el 409) y la constraint parcial que lo respalda a nivel BD.
* El scope multi-tenant del ``pedido`` en la creación (``_validar_contexto``).

Ejecutar SIEMPRE con una BD desechable; el ``.env`` del repo apunta a Supabase
de producción. Ejemplo con un settings de override a SQLite en memoria:

    python manage.py test produccion --settings=sqlite_settings
"""

import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APIClient

from catalogo.models import Color, Producto, ProductoVariante, Talla, VarianteProductoProduccion
from inventarios.models import (
    Almacen,
    Existencia,
    MovimientoInventario,
    MovimientoInventarioDetalle,
    Ubicacion,
)
from nucleo.models import Empresa, Moneda, SerieFolio, Sucursal, UnidadMedida
from produccion.models import (
    BomDetalle,
    BordadoAvances,
    BordadoIncidencias,
    ConsumoProduccion,
    ListaMaterialBom,
    OrdenBordadoDetalle,
    OrdenesBordado,
    OrdenCorteMangaDetalle,
    OrdenesCorteManga,
    OrdenesReflejante,
    OrdenProduccion,
    OrdenProduccionDetalle,
    OrdenProduccionRutaCritica,
    OrdenReflejanteDetalle,
    ProductoTerminadoEntradas,
    ReflejanteAvances,
    ReflejanteIncidencias,
    RutaProduccion,
)
from produccion.services.common import config_como_dict
from produccion.services.orden_produccion_service import OrdenProduccionService
from produccion.services.orden_bordado_service import (
    OrdenBordadoDuplicada409,
    OrdenBordadoService,
)
from produccion.services.orden_corte_manga_service import (
    OrdenCorteMangaDuplicada409,
    OrdenCorteMangaService,
)
from produccion.services.orden_reflejante_service import (
    OrdenReflejanteDuplicada409,
    OrdenReflejanteService,
)
from terceros.models import Cliente
from usuarios.models import Usuario
from ventas.models import Pedido, PedidoDetalle, PedidoDetalleTalla

#: tipo_documento de SerieFolio por tipo de orden. Las claves coinciden con las
#: que prueban ``generate_ob_folio`` / ``generate_or_folio`` / ``generate_ocm_folio``.
SERIES = (
    ("ORDEN_BORDADO", "OB"),
    ("ORDEN_REFLEJANTE", "OR"),
    ("ORDEN_CORTE_MANGA", "OCM"),
)

DESTINOS = ("BORDADO", "REFLEJANTE", "CORTE_MANGA")


def _programar(pedido, destinos=DESTINOS):
    """Mesa de Control programa el pedido: el POST de onboarding lo exige (#278)."""
    pedido.programacion_conf = {"programaciones": [{"destino": d, "cantidad": 1} for d in destinos]}
    pedido.save(update_fields=["programacion_conf"])


class BaseOrdenTrabajoTests:
    """Batería compartida por los tres tipos de orden de trabajo.

    Mixin a propósito (no hereda ``TestCase``) para que el runner no la
    recolecte suelta: sin las clases de abajo no tiene modelo ni service.
    Cada subclase declara ``FLAG`` (campo de ``PedidoDetalleTalla``),
    ``SERVICE``, ``MODEL``, ``DUPLICADA`` (la excepción 409 del módulo),
    ``FOLIO_FIELD``, ``ESTATUS_FIELD`` y ``CANCELADO``.
    """

    FLAG = None
    SERVICE = None
    MODEL = None
    DUPLICADA = None

    #: ¿El modelo conserva la constraint parcial "una orden activa por pedido"?
    #: Los tres tipos la tuvieron; se fueron quitando al habilitar parcialidades
    #: por renglón (``detalles_override[]``), porque impedía la segunda orden
    #: parcial sobre el mismo pedido: Reflejante y OCM en la migración ``0025``,
    #: Bordado en la ``0026``. Las subclases que ya no la tienen lo declaran y
    #: los dos tests de más abajo se saltan; el resto de la batería (incluido el
    #: 409 de pedido completo, que es puro Python) sigue aplicando a todas.
    CONSTRAINT_ACTIVA = True

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        for tipo_documento, serie in SERIES:
            SerieFolio.objects.create(
                empresa=cls.empresa,
                sucursal=cls.sucursal,
                tipo_documento=tipo_documento,
                serie=serie,
            )
        cls.usuario = Usuario.objects.create(
            username="operador",
            email="operador@acme.test",
            empresa=cls.empresa,
            sucursal_default=cls.sucursal,
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla_ch = Talla.objects.create(nombre="CH")
        cls.talla_m = Talla.objects.create(nombre="M")
        cls.talla_g = Talla.objects.create(nombre="G")

    def _crear_pedido(self, empresa=None, sucursal=None, cliente=None):
        pedido = Pedido.objects.create(
            empresa=empresa or self.empresa,
            sucursal=sucursal or self.sucursal,
            cliente=cliente or self.cliente,
            moneda=self.moneda,
            persona_pagos="Pagos",
            correo_facturas="pagos@acme.test",
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )
        detalle = PedidoDetalle.objects.create(pedido=pedido, producto=self.producto)
        return pedido, detalle

    def _talla(self, detalle, talla, cantidad, marcada=True):
        return PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle,
            talla=talla,
            cantidad=cantidad,
            **{self.FLAG: marcada},
        )

    def _save(self, pedido, user=None):
        return self.SERVICE.save({"pedido": pedido}, user or self.usuario)


    # --- regresión: el caso que ya funcionaba debe seguir funcionando --------

    def test_segunda_orden_sobre_pedido_ya_cubierto_devuelve_409(self):
        """Duplicado genuino (sin tallas en cantidad 0): sigue dando 409."""
        pedido, detalle = self._crear_pedido()
        self._talla(detalle, self.talla_ch, 10)
        self._talla(detalle, self.talla_m, 5)

        primera = self._save(pedido)
        self.assertEqual(primera.detalles.count(), 2)

        with self.assertRaises(self.DUPLICADA) as ctx:
            self._save(pedido)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.MODEL.objects.filter(pedido=pedido).count(), 1)

    def test_pedido_sin_tallas_marcadas_no_genera_orden(self):
        pedido, detalle = self._crear_pedido()
        self._talla(detalle, self.talla_ch, 10, marcada=False)

        with self.assertRaises(Exception) as ctx:
            self._save(pedido)

        self.assertNotIsInstance(ctx.exception, self.DUPLICADA)
        self.assertEqual(self.MODEL.objects.filter(pedido=pedido).count(), 0)

    # --- el bypass reportado --------------------------------------------------

    def test_talla_en_cantidad_cero_ya_no_permite_esquivar_el_409(self):
        """El caso del reporte: una talla marcada y con cantidad 0.

        Antes: la orden nacía con 3 detalles contra 2 esperados, ``full_match``
        nunca volvía a coincidir y el segundo POST creaba una orden duplicada.
        """
        pedido, detalle = self._crear_pedido()
        self._talla(detalle, self.talla_ch, 10)
        self._talla(detalle, self.talla_m, 5)
        self._talla(detalle, self.talla_g, 0)  # marcada, pero sin piezas

        primera = self._save(pedido)

        # El detalle ya no incluye la talla de cantidad 0: 2, no 3.
        self.assertEqual(primera.detalles.count(), 2)
        self.assertNotIn(
            self.talla_g.id,
            list(primera.detalles.values_list("talla_id", flat=True)),
        )

        with self.assertRaises(self.DUPLICADA) as ctx:
            self._save(pedido)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.MODEL.objects.filter(pedido=pedido).count(), 1)

    def test_pedido_solo_con_tallas_en_cantidad_cero_no_genera_orden(self):
        pedido, detalle = self._crear_pedido()
        self._talla(detalle, self.talla_ch, 0)

        with self.assertRaises(Exception) as ctx:
            self._save(pedido)

        self.assertNotIsInstance(ctx.exception, self.DUPLICADA)
        self.assertEqual(self.MODEL.objects.filter(pedido=pedido).count(), 0)

    # --- constraint de BD -----------------------------------------------------

    def test_constraint_bloquea_segunda_orden_activa_saltandose_el_service(self):
        """La red de seguridad: INSERT directo por ORM, sin pasar por el 409."""
        if not self.CONSTRAINT_ACTIVA:
            self.skipTest("El modelo ya no tiene la constraint (parcialidades habilitadas).")
        pedido, detalle = self._crear_pedido()
        self._talla(detalle, self.talla_ch, 10)
        self._save(pedido)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.MODEL.objects.create(
                    empresa=self.empresa,
                    sucursal=self.sucursal,
                    pedido=pedido,
                    **{self.FOLIO_FIELD: "FOLIO-DUPLICADO"},
                )

        self.assertEqual(self.MODEL.objects.filter(pedido=pedido).count(), 1)

    def test_carrera_traduce_integrityerror_a_409_sin_depender_del_texto(self):
        """El INSERT que choca con la constraint parcial se traduce al 409 del
        módulo, no propaga un IntegrityError crudo (500).

        Reproduce la rama de carrera: una orden activa preexistente que NO es
        full-match (0 detalles vs. 2 tallas esperadas), así que
        ``buscar_existente_full_match`` la ignora y ``save()`` llega al INSERT,
        donde la constraint dispara. La traducción a 409 ya no depende del
        nombre de la constraint en ``str(IntegrityError)`` (que SQLite omite),
        por lo que esta rama es verificable en la suite.
        """
        if not self.CONSTRAINT_ACTIVA:
            self.skipTest("El modelo ya no tiene la constraint (parcialidades habilitadas).")
        pedido, detalle = self._crear_pedido()
        self._talla(detalle, self.talla_ch, 10)
        self._talla(detalle, self.talla_m, 5)

        # Activa pero sin detalles: detail_count=0 != 2 esperadas.
        self.MODEL.objects.create(
            empresa=self.empresa,
            sucursal=self.sucursal,
            pedido=pedido,
            **{self.FOLIO_FIELD: "PREEXISTENTE"},
        )

        with self.assertRaises(self.DUPLICADA) as ctx:
            self._save(pedido)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.MODEL.objects.filter(pedido=pedido).count(), 1)

    def test_orden_cancelada_pero_activa_sigue_bloqueando(self):
        """Fija la semántica vigente: CANCELADO no libera el pedido.

        ``buscar_existente_full_match`` sólo mira ``activo``, y la constraint
        usa ese mismo criterio a propósito. Cancelar una orden sin darla de baja
        (soft delete) deja el pedido tomado en ambas capas. Si negocio quiere
        lo contrario, hay que cambiar los dos lados juntos.
        """
        pedido, detalle = self._crear_pedido()
        self._talla(detalle, self.talla_ch, 10)

        primera = self._save(pedido)
        setattr(primera, self.ESTATUS_FIELD, self.CANCELADO)
        primera.save(update_fields=[self.ESTATUS_FIELD])

        with self.assertRaises(self.DUPLICADA):
            self._save(pedido)

        self.assertEqual(self.MODEL.objects.filter(pedido=pedido).count(), 1)

    def test_sin_constraint_dos_ordenes_activas_por_pedido_conviven(self):
        """Inverso del test de arriba: fija el estado que dejan ``0025``/``0026``.

        Sin esta aserción, quitar la constraint sólo se notaba de rebote (un 409
        inesperado en los tests de parcialidades); si la migración se revirtiera
        o nunca se aplicara en un entorno, nada apuntaría a la causa.
        """
        if self.CONSTRAINT_ACTIVA:
            self.skipTest("El modelo aún declara la constraint.")
        pedido, detalle = self._crear_pedido()
        self._talla(detalle, self.talla_ch, 10)

        for i in range(2):
            self.MODEL.objects.create(
                empresa=self.empresa,
                sucursal=self.sucursal,
                pedido=pedido,
                **{self.FOLIO_FIELD: f"FOLIO-PARCIAL-{i}"},
            )

        self.assertEqual(
            self.MODEL.objects.filter(pedido=pedido, activo=True).count(), 2
        )

    def test_orden_con_soft_delete_libera_el_pedido_para_una_nueva(self):
        pedido, detalle = self._crear_pedido()
        self._talla(detalle, self.talla_ch, 10)

        primera = self._save(pedido)
        primera.soft_delete()

        segunda = self._save(pedido)

        self.assertNotEqual(primera.pk, segunda.pk)
        self.assertTrue(segunda.activo)


class OrdenReflejanteAntiduplicadoTests(BaseOrdenTrabajoTests, TestCase):
    FLAG = "lleva_reflejante"
    SERVICE = OrdenReflejanteService
    MODEL = OrdenesReflejante
    DUPLICADA = OrdenReflejanteDuplicada409
    FOLIO_FIELD = "folio_reflejante"
    ESTATUS_FIELD = "estatus_reflejante"
    CANCELADO = OrdenesReflejante.EstatusReflejante.CANCELADO
    CONSTRAINT_ACTIVA = False  # removida en 0025


class OrdenBordadoAntiduplicadoTests(BaseOrdenTrabajoTests, TestCase):
    FLAG = "lleva_bordado"
    SERVICE = OrdenBordadoService
    MODEL = OrdenesBordado
    DUPLICADA = OrdenBordadoDuplicada409
    FOLIO_FIELD = "folio_bordado"
    ESTATUS_FIELD = "estatus_bordado"
    CANCELADO = OrdenesBordado.EstatusBordado.CANCELADO_LEGACY
    CONSTRAINT_ACTIVA = False  # removida en 0026


class OrdenCorteMangaAntiduplicadoTests(BaseOrdenTrabajoTests, TestCase):
    FLAG = "lleva_corte_manga"
    SERVICE = OrdenCorteMangaService
    MODEL = OrdenesCorteManga
    DUPLICADA = OrdenCorteMangaDuplicada409
    FOLIO_FIELD = "folio_ocm"
    ESTATUS_FIELD = "estatus_corte"
    CANCELADO = OrdenesCorteManga.EstatusCorte.CANCELADO
    CONSTRAINT_ACTIVA = False  # removida en 0025


class OrdenBordadoScopeTenantTests(BaseOrdenTrabajoTests, TestCase):
    """``OrdenBordadoService._validar_contexto``: el pedido debe ser del usuario.

    Sin esta puerta, un usuario de la empresa A podía mandar un pedido de la
    empresa B, gastarle un folio de su serie y recibir de vuelta sus productos,
    tallas y cantidades.
    """

    FLAG = "lleva_bordado"
    SERVICE = OrdenBordadoService
    MODEL = OrdenesBordado
    DUPLICADA = OrdenBordadoDuplicada409
    FOLIO_FIELD = "folio_bordado"
    ESTATUS_FIELD = "estatus_bordado"
    CANCELADO = OrdenesBordado.EstatusBordado.CANCELADO_LEGACY
    CONSTRAINT_ACTIVA = False  # removida en 0026

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Segundo tenant completo: empresa B, su sucursal, su cliente y su serie.
        cls.empresa_b = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        cls.sucursal_b = Sucursal.objects.create(
            empresa=cls.empresa_b, codigo="GDL", nombre="Guadalajara"
        )
        cls.serie_b = SerieFolio.objects.create(
            empresa=cls.empresa_b,
            sucursal=cls.sucursal_b,
            tipo_documento="ORDEN_BORDADO",
            serie="OB",
        )
        cls.cliente_b = Cliente.objects.create(empresa=cls.empresa_b, nombre="Cliente B")
        # Sucursal adicional de la empresa A a la que el operador NO tiene acceso.
        cls.sucursal_a2 = Sucursal.objects.create(
            empresa=cls.empresa, codigo="CDMX", nombre="Ciudad de Mexico"
        )
        SerieFolio.objects.create(
            empresa=cls.empresa,
            sucursal=cls.sucursal_a2,
            tipo_documento="ORDEN_BORDADO",
            serie="OB",
        )

    def _pedido_de_empresa_b(self):
        pedido, detalle = self._crear_pedido(
            empresa=self.empresa_b, sucursal=self.sucursal_b, cliente=self.cliente_b
        )
        self._talla(detalle, self.talla_ch, 10)
        return pedido

    def test_pedido_de_otra_empresa_es_rechazado(self):
        pedido_b = self._pedido_de_empresa_b()

        with self.assertRaises(DRFValidationError) as ctx:
            self._save(pedido_b)  # self.usuario pertenece a la empresa A

        self.assertIn("no pertenece a la empresa", str(ctx.exception.detail))
        self.assertEqual(OrdenesBordado.objects.filter(pedido=pedido_b).count(), 0)

    def test_rechazo_cross_tenant_no_consume_folio_ajeno(self):
        """El rechazo ocurre antes de ``generate_ob_folio``."""
        pedido_b = self._pedido_de_empresa_b()
        folio_actual_previo = self.serie_b.folio_actual

        with self.assertRaises(DRFValidationError):
            self._save(pedido_b)

        self.serie_b.refresh_from_db()
        self.assertEqual(self.serie_b.folio_actual, folio_actual_previo)

    def test_pedido_de_sucursal_no_permitida_es_rechazado(self):
        """Segundo paso del check: misma empresa, sucursal fuera de alcance."""
        pedido, detalle = self._crear_pedido(sucursal=self.sucursal_a2)
        self._talla(detalle, self.talla_ch, 10)

        with self.assertRaises(DRFValidationError) as ctx:
            self._save(pedido)

        self.assertIn("sucursal", str(ctx.exception.detail))
        self.assertEqual(OrdenesBordado.objects.filter(pedido=pedido).count(), 0)

    def test_admin_empresa_puede_usar_cualquier_sucursal_de_su_empresa(self):
        admin = Usuario.objects.create(
            username="admin_a",
            email="admin@acme.test",
            empresa=self.empresa,
            sucursal_default=self.sucursal,
            is_admin_empresa=True,
        )
        pedido, detalle = self._crear_pedido(sucursal=self.sucursal_a2)
        self._talla(detalle, self.talla_ch, 10)

        orden = self._save(pedido, user=admin)

        self.assertEqual(orden.sucursal_id, self.sucursal_a2.pk)

    def test_pedido_propio_sigue_funcionando(self):
        """No regresión: mismo tenant, misma sucursal → se crea normal."""
        pedido, detalle = self._crear_pedido()
        self._talla(detalle, self.talla_ch, 10)

        orden = self._save(pedido)

        self.assertEqual(orden.empresa_id, self.empresa.pk)
        self.assertEqual(orden.sucursal_id, self.sucursal.pk)
        self.assertEqual(orden.detalles.count(), 1)


#: Los 4 ViewSets satélite: endpoint, modelo, FK a la orden padre y los campos
#: requeridos / parcheables propios de cada uno.
SATELITES = (
    {
        "nombre": "bordado-avances",
        "url": "/api/v1/produccion/bordado-avances/",
        "modelo": BordadoAvances,
        "fk": "ob",
        "extra": {"cantidad_bordada": 5},
        "patch": {"comentario": "tocado"},
        # A diferencia del resto de satélites, ``BordadoAvancesViewSet``
        # fuerza ``usuario=request.user`` en ``perform_create`` y el serializer
        # lo declara read_only: la autoría NO es client-supplied. El flujo
        # "supervisor en nombre de otro operador" no aplica a este endpoint.
        "usuario_read_only": True,
    },
    {
        "nombre": "bordado-incidencias",
        "url": "/api/v1/produccion/bordado-incidencias/",
        "modelo": BordadoIncidencias,
        "fk": "ob",
        "extra": {"tipo_incidencia": 1},
        "patch": {"descripcion": "tocado"},
    },
    {
        "nombre": "reflejante-avances",
        "url": "/api/v1/produccion/reflejante-avances/",
        "modelo": ReflejanteAvances,
        "fk": "orden_r",
        "extra": {"cantidad_aplicada": 5},
        "patch": {"comentario": "tocado"},
    },
    {
        "nombre": "reflejante-incidencias",
        "url": "/api/v1/produccion/reflejante-incidencias/",
        "modelo": ReflejanteIncidencias,
        "fk": "orden_r",
        "extra": {},
        "patch": {"descripcion": "tocado"},
    },
)


class SatelitesScopeTenantTests(TestCase):
    """Aislamiento multi-tenant de Avances/Incidencias de Bordado y Reflejante.

    Estos modelos no tienen ``empresa``/``sucursal`` propios: el tenant sólo se
    alcanza atravesando la FK a la orden padre (``ob`` / ``orden_r``). Sin
    ``get_queryset()``, los 4 ViewSets exponían CRUD completo sobre los
    registros de cualquier empresa.
    """

    @classmethod
    def _tenant(cls, codigo, codigo_sucursal, email):
        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        sucursal = Sucursal.objects.create(
            empresa=empresa, codigo=codigo_sucursal, nombre=codigo_sucursal
        )
        cliente = Cliente.objects.create(empresa=empresa, nombre=f"Cliente {codigo}")
        usuario = Usuario.objects.create(
            username=email, email=email, empresa=empresa, sucursal_default=sucursal
        )
        pedido = Pedido.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            cliente=cliente,
            moneda=cls.moneda,
            persona_pagos="Pagos",
            correo_facturas=email,
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )
        return {
            "empresa": empresa,
            "sucursal": sucursal,
            "usuario": usuario,
            "ob": OrdenesBordado.objects.create(
                empresa=empresa,
                sucursal=sucursal,
                pedido=pedido,
                folio_bordado=f"OB-{codigo}",
            ),
            "orden_r": OrdenesReflejante.objects.create(
                empresa=empresa,
                sucursal=sucursal,
                pedido=pedido,
                folio_reflejante=f"OR-{codigo}",
            ),
        }

    @classmethod
    def setUpTestData(cls):
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.a = cls._tenant("acme", "MTY", "a@acme.test")
        cls.b = cls._tenant("globex", "GDL", "b@globex.test")

        cls.sin_empresa = Usuario.objects.create(
            username="huerfano", email="huerfano@nowhere.test"
        )
        cls.superuser = Usuario.objects.create(
            username="root", email="root@nowhere.test", is_superuser=True
        )

        # Un registro satélite de cada tipo por tenant.
        cls.registros = {}
        for cfg in SATELITES:
            for etiqueta, tenant in (("a", cls.a), ("b", cls.b)):
                cls.registros[(cfg["nombre"], etiqueta)] = cfg["modelo"].objects.create(
                    usuario=tenant["usuario"],
                    **{cfg["fk"]: tenant[cfg["fk"]]},
                    **cfg["extra"],
                )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_list_solo_devuelve_registros_de_la_propia_empresa(self):
        client = self._client(self.a["usuario"])
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                resp = client.get(cfg["url"])
                self.assertEqual(resp.status_code, 200)
                ids = [row["id"] for row in resp.json()]
                self.assertEqual(ids, [self.registros[(cfg["nombre"], "a")].pk])

    def test_retrieve_de_otra_empresa_devuelve_404(self):
        client = self._client(self.a["usuario"])
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                ajeno = self.registros[(cfg["nombre"], "b")]
                self.assertEqual(
                    client.get(f"{cfg['url']}{ajeno.pk}/").status_code, 404
                )

    def test_patch_de_otra_empresa_devuelve_404_y_no_modifica(self):
        client = self._client(self.a["usuario"])
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                ajeno = self.registros[(cfg["nombre"], "b")]
                resp = client.patch(
                    f"{cfg['url']}{ajeno.pk}/", cfg["patch"], format="json"
                )
                self.assertEqual(resp.status_code, 404)
                ajeno.refresh_from_db()
                campo, valor = next(iter(cfg["patch"].items()))
                self.assertNotEqual(getattr(ajeno, campo), valor)

    def test_delete_de_otra_empresa_devuelve_404_y_no_da_de_baja(self):
        client = self._client(self.a["usuario"])
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                ajeno = self.registros[(cfg["nombre"], "b")]
                self.assertEqual(
                    client.delete(f"{cfg['url']}{ajeno.pk}/").status_code, 404
                )
                ajeno.refresh_from_db()
                self.assertTrue(ajeno.activo)

    def test_registro_propio_sigue_siendo_accesible(self):
        """No regresión: el dueño conserva retrieve y patch."""
        client = self._client(self.a["usuario"])
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                propio = self.registros[(cfg["nombre"], "a")]
                self.assertEqual(
                    client.get(f"{cfg['url']}{propio.pk}/").status_code, 200
                )
                resp = client.patch(
                    f"{cfg['url']}{propio.pk}/", cfg["patch"], format="json"
                )
                self.assertEqual(resp.status_code, 200)
                propio.refresh_from_db()
                campo, valor = next(iter(cfg["patch"].items()))
                self.assertEqual(getattr(propio, campo), valor)

    def test_usuario_sin_empresa_no_ve_nada(self):
        client = self._client(self.sin_empresa)
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                resp = client.get(cfg["url"])
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json(), [])

    def test_superuser_ve_ambas_empresas(self):
        client = self._client(self.superuser)
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                ids = [row["id"] for row in client.get(cfg["url"]).json()]
                self.assertCountEqual(
                    ids,
                    [
                        self.registros[(cfg["nombre"], "a")].pk,
                        self.registros[(cfg["nombre"], "b")].pk,
                    ],
                )

    def test_usuario_sin_acceso_a_la_sucursal_no_ve_el_registro(self):
        """Tercer nivel del scope: misma empresa, sucursal fuera de alcance."""
        otra_sucursal = Sucursal.objects.create(
            empresa=self.a["empresa"], codigo="CDMX", nombre="Ciudad de Mexico"
        )
        forastero = Usuario.objects.create(
            username="forastero",
            email="forastero@acme.test",
            empresa=self.a["empresa"],
            sucursal_default=otra_sucursal,
        )
        client = self._client(forastero)
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                self.assertEqual(client.get(cfg["url"]).json(), [])


class SatelitesSuperficieEscribibleTests(TestCase):
    """Candados de la superficie escribible de los 4 serializers satélite.

    ``activo`` es read-only (no se puede apagar por PATCH; sólo soft-delete) y
    la FK a la orden padre (``ob``/``orden_r``) es write-once (settable al crear,
    ignorada en update). Los campos de contenido siguen escribibles.

    DRF **ignora en silencio** los campos read-only en escritura (no rechaza):
    la respuesta es 200/201 y el valor enviado simplemente se descarta. Los
    tests aseveran ese comportamiento real, no un 400.
    """

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.usuario = Usuario.objects.create(
            username="operador",
            email="operador@acme.test",
            empresa=cls.empresa,
            sucursal_default=cls.sucursal,
        )
        # Dos pedidos: la constraint antiduplicado del bug previo prohíbe dos
        # órdenes activas del mismo tipo sobre un mismo pedido, así que la orden
        # "otra" (destino ilegítimo de la reasignación) vive en su propio pedido.
        pedido = cls._pedido()
        pedido_otro = cls._pedido()

        # Dos órdenes de cada tipo: una donde vive el registro, otra como
        # destino ilegítimo para el intento de reasignación write-once.
        cls.ob = OrdenesBordado.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, pedido=pedido, folio_bordado="OB-1"
        )
        cls.ob_otra = OrdenesBordado.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, pedido=pedido_otro, folio_bordado="OB-2"
        )
        cls.orden_r = OrdenesReflejante.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, pedido=pedido, folio_reflejante="OR-1"
        )
        cls.orden_r_otra = OrdenesReflejante.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, pedido=pedido_otro, folio_reflejante="OR-2"
        )
        cls.padres = {"ob": cls.ob, "orden_r": cls.orden_r}
        cls.padres_otra = {"ob": cls.ob_otra, "orden_r": cls.orden_r_otra}

    @classmethod
    def _pedido(cls):
        return Pedido.objects.create(
            empresa=cls.empresa,
            sucursal=cls.sucursal,
            cliente=cls.cliente,
            moneda=cls.moneda,
            persona_pagos="Pagos",
            correo_facturas="pagos@acme.test",
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    def _crear(self, cfg):
        return cfg["modelo"].objects.create(
            usuario=self.usuario,
            **{cfg["fk"]: self.padres[cfg["fk"]]},
            **cfg["extra"],
        )

    def test_create_via_api_sigue_funcionando(self):
        """Los candados no rompen el POST legítimo (regresión de creación)."""
        client = self._client()
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                body = {
                    cfg["fk"]: self.padres[cfg["fk"]].pk,
                    "usuario": self.usuario.pk,
                    **cfg["extra"],
                }
                resp = client.post(cfg["url"], body, format="json")
                self.assertEqual(resp.status_code, 201, resp.content)
                creado = cfg["modelo"].objects.get(pk=resp.json()["id"])
                self.assertTrue(creado.activo)
                self.assertEqual(
                    getattr(creado, f"{cfg['fk']}_id"), self.padres[cfg["fk"]].pk
                )

    def test_create_ignora_activo_false_del_cliente(self):
        """activo es read-only: llega ``activo=false`` pero nace activo=True."""
        client = self._client()
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                body = {
                    cfg["fk"]: self.padres[cfg["fk"]].pk,
                    "usuario": self.usuario.pk,
                    "activo": False,
                    **cfg["extra"],
                }
                resp = client.post(cfg["url"], body, format="json")
                self.assertEqual(resp.status_code, 201, resp.content)
                self.assertTrue(cfg["modelo"].objects.get(pk=resp.json()["id"]).activo)

    def test_patch_activo_false_se_ignora(self):
        """No se puede dar de baja por PATCH; el registro sigue activo."""
        client = self._client()
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                reg = self._crear(cfg)
                resp = client.patch(
                    f"{cfg['url']}{reg.pk}/", {"activo": False}, format="json"
                )
                self.assertEqual(resp.status_code, 200, resp.content)
                reg.refresh_from_db()
                self.assertTrue(reg.activo)

    def test_patch_no_reasigna_la_orden_padre(self):
        """La FK a la orden padre es write-once: el PATCH la ignora."""
        client = self._client()
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                reg = self._crear(cfg)
                original_id = getattr(reg, f"{cfg['fk']}_id")
                destino = self.padres_otra[cfg["fk"]]
                self.assertNotEqual(original_id, destino.pk)
                resp = client.patch(
                    f"{cfg['url']}{reg.pk}/", {cfg["fk"]: destino.pk}, format="json"
                )
                self.assertEqual(resp.status_code, 200, resp.content)
                reg.refresh_from_db()
                self.assertEqual(getattr(reg, f"{cfg['fk']}_id"), original_id)

    def test_campos_de_contenido_siguen_escribibles(self):
        """No regresión: los campos de contenido sí se pueden editar."""
        contenido = {
            "bordado-avances": {"cantidad_bordada": 99, "comentario": "ok"},
            "bordado-incidencias": {"tipo_incidencia": 3, "descripcion": "ok"},
            "reflejante-avances": {"cantidad_aplicada": 99, "comentario": "ok"},
            "reflejante-incidencias": {"descripcion": "ok"},
        }
        client = self._client()
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                reg = self._crear(cfg)
                patch = contenido[cfg["nombre"]]
                resp = client.patch(
                    f"{cfg['url']}{reg.pk}/", patch, format="json"
                )
                self.assertEqual(resp.status_code, 200, resp.content)
                reg.refresh_from_db()
                for campo, valor in patch.items():
                    self.assertEqual(getattr(reg, campo), valor)


class SatelitesUsuarioTenantTests(TestCase):
    """``usuario`` (autoría) debe pertenecer a la misma empresa que la orden
    padre, aun cuando sea distinto de ``request.user`` — el flujo de
    supervisor registrando en nombre de otro operador está confirmado como
    legítimo y debe seguir funcionando para usuarios del mismo tenant.
    """

    @classmethod
    def setUpTestData(cls):
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")

        cls.empresa_a = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal_a = Sucursal.objects.create(
            empresa=cls.empresa_a, codigo="MTY", nombre="Monterrey"
        )
        cls.cliente_a = Cliente.objects.create(empresa=cls.empresa_a, nombre="Cliente A")
        cls.solicitante = Usuario.objects.create(
            username="supervisor",
            email="supervisor@acme.test",
            empresa=cls.empresa_a,
            sucursal_default=cls.sucursal_a,
        )
        cls.operador_mismo_tenant = Usuario.objects.create(
            username="operador_a", email="operador_a@acme.test", empresa=cls.empresa_a
        )

        pedido_a = Pedido.objects.create(
            empresa=cls.empresa_a,
            sucursal=cls.sucursal_a,
            cliente=cls.cliente_a,
            moneda=cls.moneda,
            persona_pagos="Pagos",
            correo_facturas="pagos@acme.test",
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )
        cls.ob = OrdenesBordado.objects.create(
            empresa=cls.empresa_a, sucursal=cls.sucursal_a, pedido=pedido_a, folio_bordado="OB-A"
        )
        cls.orden_r = OrdenesReflejante.objects.create(
            empresa=cls.empresa_a, sucursal=cls.sucursal_a, pedido=pedido_a, folio_reflejante="OR-A"
        )
        cls.padres = {"ob": cls.ob, "orden_r": cls.orden_r}

        cls.empresa_b = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        cls.operador_otro_tenant = Usuario.objects.create(
            username="operador_b", email="operador_b@globex.test", empresa=cls.empresa_b
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.solicitante)
        return client

    def _crear(self, cfg, usuario=None):
        return cfg["modelo"].objects.create(
            usuario=usuario or self.solicitante,
            **{cfg["fk"]: self.padres[cfg["fk"]]},
            **cfg["extra"],
        )

    def test_create_rechaza_usuario_de_otra_empresa(self):
        client = self._client()
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                body = {
                    cfg["fk"]: self.padres[cfg["fk"]].pk,
                    "usuario": self.operador_otro_tenant.pk,
                    **cfg["extra"],
                }
                resp = client.post(cfg["url"], body, format="json")
                if cfg.get("usuario_read_only"):
                    # ``usuario`` read_only: el valor del cliente se ignora en
                    # vez de rechazarse. El registro se crea a nombre del
                    # solicitante (request.user), nunca del operador ajeno, así
                    # que el cross-tenant queda cerrado por construcción.
                    self.assertEqual(resp.status_code, 201, resp.content)
                    creado = cfg["modelo"].objects.get(pk=resp.json()["id"])
                    self.assertEqual(creado.usuario_id, self.solicitante.pk)
                    creado.delete()
                    continue
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertIn("usuario", resp.json())
                self.assertEqual(cfg["modelo"].objects.count(), 0)

    def test_create_acepta_usuario_de_la_misma_empresa_distinto_al_solicitante(self):
        """No regresión: supervisor-en-nombre-de-otro-operador sigue funcionando
        para los satélites con autoría client-supplied. En bordado-avances la
        autoría la fuerza el ViewSet (usuario read_only), así que el operador
        indicado se ignora y queda el solicitante."""
        client = self._client()
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                body = {
                    cfg["fk"]: self.padres[cfg["fk"]].pk,
                    "usuario": self.operador_mismo_tenant.pk,
                    **cfg["extra"],
                }
                resp = client.post(cfg["url"], body, format="json")
                self.assertEqual(resp.status_code, 201, resp.content)
                creado = cfg["modelo"].objects.get(pk=resp.json()["id"])
                esperado = (
                    self.solicitante.pk
                    if cfg.get("usuario_read_only")
                    else self.operador_mismo_tenant.pk
                )
                self.assertEqual(creado.usuario_id, esperado)
                creado.delete()

    def test_patch_rechaza_reasignar_a_usuario_de_otra_empresa(self):
        """Caso real: PATCH que sólo toca ``usuario`` (corrige autoría) sin
        tocar la orden padre — que de todas formas es inmutable."""
        client = self._client()
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                reg = self._crear(cfg)
                resp = client.patch(
                    f"{cfg['url']}{reg.pk}/",
                    {"usuario": self.operador_otro_tenant.pk},
                    format="json",
                )
                if cfg.get("usuario_read_only"):
                    # read_only: el PATCH ignora ``usuario`` en vez de
                    # rechazarlo; no hay 400 y la autoría no cambia.
                    self.assertEqual(resp.status_code, 200, resp.content)
                else:
                    self.assertEqual(resp.status_code, 400, resp.content)
                    self.assertIn("usuario", resp.json())
                reg.refresh_from_db()
                self.assertNotEqual(reg.usuario_id, self.operador_otro_tenant.pk)

    def test_patch_acepta_reasignar_a_usuario_de_la_misma_empresa(self):
        client = self._client()
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                reg = self._crear(cfg)
                resp = client.patch(
                    f"{cfg['url']}{reg.pk}/",
                    {"usuario": self.operador_mismo_tenant.pk},
                    format="json",
                )
                self.assertEqual(resp.status_code, 200, resp.content)
                reg.refresh_from_db()
                if cfg.get("usuario_read_only"):
                    # read_only: la reasignación se ignora; la autoría queda en
                    # el solicitante con que ``_crear`` levantó el registro.
                    self.assertEqual(reg.usuario_id, self.solicitante.pk)
                else:
                    self.assertEqual(reg.usuario_id, self.operador_mismo_tenant.pk)


class SatelitesOrdenPadreTenantTests(TestCase):
    """La orden padre (``ob``/``orden_r``) debe pertenecer a la empresa del
    solicitante en creación.

    Gap que dejaba abierto el chequeo de ``usuario`` de la sesión anterior:
    ``create()`` no pasa por ``get_queryset()`` (eso sólo filtra list/
    retrieve/update/destroy vía ``get_object()``), así que sin este chequeo
    un usuario de la empresa A podía crear un avance/incidencia apuntando a
    una orden de la empresa B.
    """

    @classmethod
    def setUpTestData(cls):
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")

        cls.empresa_a = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal_a = Sucursal.objects.create(
            empresa=cls.empresa_a, codigo="MTY", nombre="Monterrey"
        )
        cls.cliente_a = Cliente.objects.create(empresa=cls.empresa_a, nombre="Cliente A")
        cls.solicitante = Usuario.objects.create(
            username="operador_a",
            email="operador_a@acme.test",
            empresa=cls.empresa_a,
            sucursal_default=cls.sucursal_a,
        )
        cls.superuser_sin_empresa = Usuario.objects.create(
            username="root", email="root@nowhere.test", is_superuser=True
        )

        pedido_a = Pedido.objects.create(
            empresa=cls.empresa_a,
            sucursal=cls.sucursal_a,
            cliente=cls.cliente_a,
            moneda=cls.moneda,
            persona_pagos="Pagos",
            correo_facturas="pagos@acme.test",
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )
        cls.ob_propia = OrdenesBordado.objects.create(
            empresa=cls.empresa_a, sucursal=cls.sucursal_a, pedido=pedido_a, folio_bordado="OB-A"
        )
        cls.orden_r_propia = OrdenesReflejante.objects.create(
            empresa=cls.empresa_a, sucursal=cls.sucursal_a, pedido=pedido_a, folio_reflejante="OR-A"
        )
        cls.padres_propios = {"ob": cls.ob_propia, "orden_r": cls.orden_r_propia}

        cls.empresa_b = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        cls.sucursal_b = Sucursal.objects.create(
            empresa=cls.empresa_b, codigo="GDL", nombre="Guadalajara"
        )
        cls.cliente_b = Cliente.objects.create(empresa=cls.empresa_b, nombre="Cliente B")
        pedido_b = Pedido.objects.create(
            empresa=cls.empresa_b,
            sucursal=cls.sucursal_b,
            cliente=cls.cliente_b,
            moneda=cls.moneda,
            persona_pagos="Pagos",
            correo_facturas="pagos@globex.test",
            telefono_pagos="8100000001",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )
        cls.ob_ajena = OrdenesBordado.objects.create(
            empresa=cls.empresa_b, sucursal=cls.sucursal_b, pedido=pedido_b, folio_bordado="OB-B"
        )
        cls.orden_r_ajena = OrdenesReflejante.objects.create(
            empresa=cls.empresa_b, sucursal=cls.sucursal_b, pedido=pedido_b, folio_reflejante="OR-B"
        )
        cls.padres_ajenos = {"ob": cls.ob_ajena, "orden_r": cls.orden_r_ajena}

    def _client(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.solicitante)
        return client

    def test_create_rechaza_orden_padre_de_otra_empresa(self):
        client = self._client()
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                body = {
                    cfg["fk"]: self.padres_ajenos[cfg["fk"]].pk,
                    "usuario": self.solicitante.pk,
                    **cfg["extra"],
                }
                resp = client.post(cfg["url"], body, format="json")
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertIn(cfg["fk"], resp.json())
                self.assertEqual(cfg["modelo"].objects.count(), 0)

    def test_create_acepta_orden_padre_de_la_propia_empresa(self):
        """No regresión: creación normal contra una orden del propio tenant."""
        client = self._client()
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                body = {
                    cfg["fk"]: self.padres_propios[cfg["fk"]].pk,
                    "usuario": self.solicitante.pk,
                    **cfg["extra"],
                }
                resp = client.post(cfg["url"], body, format="json")
                self.assertEqual(resp.status_code, 201, resp.content)
                creado = cfg["modelo"].objects.get(pk=resp.json()["id"])
                self.assertEqual(
                    getattr(creado, f"{cfg['fk']}_id"), self.padres_propios[cfg["fk"]].pk
                )
                creado.delete()

    def test_superuser_sin_empresa_tambien_es_rechazado(self):
        """Mismo criterio que ``_validar_contexto``: el chequeo de empresa
        corre incondicional, sin excepción de superuser (sólo la sucursal
        tiene bypass de ``es_staff`` en el service, fuera de alcance aquí)."""
        client = self._client(user=self.superuser_sin_empresa)
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                body = {
                    cfg["fk"]: self.padres_propios[cfg["fk"]].pk,
                    "usuario": self.solicitante.pk,
                    **cfg["extra"],
                }
                resp = client.post(cfg["url"], body, format="json")
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertIn(cfg["fk"], resp.json())
                self.assertEqual(cfg["modelo"].objects.count(), 0)

    def test_falla_limpio_cuando_orden_y_usuario_son_ambos_cross_tenant(self):
        """No conflictúa con el chequeo de ``usuario`` de la sesión anterior:
        un body donde AMBOS chequeos fallarían de evaluarse por separado
        (orden de empresa B, usuario de empresa A) produce un solo error
        claro —el de la orden, que se evalúa primero y cortocircuita— y no
        una mezcla confusa ni un 500."""
        client = self._client()
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                body = {
                    cfg["fk"]: self.padres_ajenos[cfg["fk"]].pk,
                    "usuario": self.solicitante.pk,
                    **cfg["extra"],
                }
                resp = client.post(cfg["url"], body, format="json")
                self.assertEqual(resp.status_code, 400, resp.content)
                payload = resp.json()
                self.assertIn(cfg["fk"], payload)
                self.assertNotIn("usuario", payload)
                self.assertEqual(cfg["modelo"].objects.count(), 0)


class OrdenReflejanteListShapeTests(TestCase):
    """Etiquetas legibles ``empresa_nombre``/``sucursal_nombre`` en el shape.

    Aditivo: los ids crudos ``empresa``/``sucursal`` siguen viajando igual; se
    suman las etiquetas para que el frontend no muestre ``#1``. Mismo patrón
    ``source=`` que ``pedido_folio``.
    """

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla = Talla.objects.create(nombre="CH")
        cls.usuario = Usuario.objects.create(
            username="operador",
            email="operador@acme.test",
            empresa=cls.empresa,
            sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )

    def _crear_orden(self, i, n_detalles=2):
        pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente,
            moneda=self.moneda, persona_pagos="Pagos",
            correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        pd = PedidoDetalle.objects.create(pedido=pedido, producto=self.producto)
        orden = OrdenesReflejante.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=pedido,
            folio_reflejante=f"OR-{i}", usuario_asignado=self.usuario,
        )
        for _ in range(n_detalles):
            OrdenReflejanteDetalle.objects.create(
                orden_r=orden, pedido_detalle=pd, producto=self.producto,
                cantidad=1, talla=self.talla,
            )
        return orden

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    def test_list_expone_empresa_y_sucursal_con_id_y_nombre(self):
        """Aditivo: los ids crudos siguen, más las etiquetas legibles."""
        self._crear_orden(1)
        fila = self._client().get("/api/v1/produccion/orden-reflejante/").json()[0]

        self.assertEqual(fila["empresa"], self.empresa.pk)
        self.assertEqual(fila["empresa_nombre"], "ACME SA")
        self.assertEqual(fila["sucursal"], self.sucursal.pk)
        self.assertEqual(fila["sucursal_nombre"], "Monterrey")
        # No se rompió lo que ya resolvía el serializer.
        self.assertIn("pedido_folio", fila)
        self.assertIn("usuario_nombre", fila)

    def test_retrieve_expone_los_mismos_campos_que_el_list(self):
        orden = self._crear_orden(1)
        detalle = self._client().get(
            f"/api/v1/produccion/orden-reflejante/{orden.pk}/"
        ).json()

        self.assertEqual(detalle["empresa_nombre"], "ACME SA")
        self.assertEqual(detalle["sucursal_nombre"], "Monterrey")

    def test_varias_ordenes_resuelven_sus_etiquetas(self):
        """Con varias órdenes en la lista, todas traen sus etiquetas."""
        for i in range(3):
            self._crear_orden(i)

        filas = self._client().get("/api/v1/produccion/orden-reflejante/").json()

        self.assertEqual(len(filas), 3)
        for fila in filas:
            self.assertEqual(fila["empresa_nombre"], "ACME SA")
            self.assertEqual(fila["sucursal_nombre"], "Monterrey")

    #: 1 query de órdenes (con los ``select_related`` en el JOIN) + 1 del
    #: ``Prefetch`` de ``detalles`` + las 2 agrupadas de la cobertura
    #: (``cobertura_por_orden``: suma por ``orden_r_id`` y contratado por
    #: pedido). No incluye las 2-3 queries de auth/sesión del request, que se
    #: aíslan usando ``CaptureQueriesContext`` sólo sobre el GET ya autenticado.
    #:
    #: Era 2 antes de exponer la cobertura en el listado. Las 2 nuevas son
    #: CONSTANTES —no crecen con el nº de órdenes ni de renglones, que es lo que
    #: sigue aseverando ``test_list_sin_n_mas_1_constante`` comparando 3 contra
    #: 12 órdenes— y son el mismo costo que ya paga el listado de Bordado.
    QUERIES_LIST = 4

    def test_list_sin_n_mas_1_constante(self):
        """El N+1 del serializer queda cortado: el nº de queries no crece con
        la cantidad de órdenes ni de renglones de detalle.

        El serializer resuelve por orden ``empresa``/``sucursal``/``pedido``/
        ``usuario_asignado`` y por renglón ``producto``/``talla``/``color``;
        sin el ``select_related``/``Prefetch`` del ViewSet cada uno dispararía
        una query suelta.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        client = self._client()

        for i in range(3):
            self._crear_orden(i, n_detalles=2)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get("/api/v1/produccion/orden-reflejante/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 3)
        queries_con_3 = len(ctx.captured_queries)

        # Cuádruple de órdenes y el doble de renglones por orden.
        for i in range(3, 12):
            self._crear_orden(i, n_detalles=4)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get("/api/v1/produccion/orden-reflejante/")
        self.assertEqual(len(resp.json()), 12)
        queries_con_12 = len(ctx.captured_queries)

        # Constante: mismo nº de queries con 3 y con 12 órdenes.
        self.assertEqual(queries_con_3, queries_con_12)
        # Y además fija el número exacto. Con ``force_authenticate`` no hay
        # queries de auth, así que el total es el del ORM del list: 1 de
        # órdenes + 1 del ``Prefetch`` de ``detalles``. Se asevera directo (no
        # restando un "overhead" derivado de la propia medición) para que una
        # query constante de más —un ``.count()`` colado, un prefetch sin
        # batch— haga fallar el test en vez de absorberse.
        self.assertEqual(queries_con_3, self.QUERIES_LIST)
        self.assertEqual(queries_con_12, self.QUERIES_LIST)


class OrdenBordadoListQueriesTests(TestCase):
    """N+1 del list de orden-bordado, mismo diagnóstico que Reflejante.

    ``OrdenBordadoSerializer`` resuelve ``pedido`` (``pedido_folio``),
    ``usuario_asignado`` (``usuario_nombre``) y ``empresa``/``sucursal``
    (``empresa_nombre``/``sucursal_nombre``) por orden, y
    ``producto``/``talla``/``color`` por renglón de ``detalles``. Mismo shape
    que Reflejante ahora; CorteManga sigue sin las etiquetas de
    ``empresa``/``sucursal`` (gap reportado, no cubierto aquí).
    """

    #: 1 query de órdenes + 1 del ``Prefetch`` de ``detalles``.
    QUERIES_LIST = 2

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla = Talla.objects.create(nombre="CH")
        cls.usuario = Usuario.objects.create(
            username="operador",
            email="operador@acme.test",
            empresa=cls.empresa,
            sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )

    def _crear_orden(self, i, n_detalles=2):
        pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente,
            moneda=self.moneda, persona_pagos="Pagos",
            correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        pd = PedidoDetalle.objects.create(pedido=pedido, producto=self.producto)
        orden = OrdenesBordado.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=pedido,
            folio_bordado=f"OB-{i}", usuario_asignado=self.usuario,
        )
        for _ in range(n_detalles):
            OrdenBordadoDetalle.objects.create(
                ob=orden, pedido_detalle=pd, producto=self.producto,
                cantidad=1, talla=self.talla,
            )
        return orden

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    def test_list_expone_usuario_nombre(self):
        """Campo nuevo: aparece en el list con el fallback get_full_name()/email."""
        self._crear_orden(1)
        fila = self._client().get("/api/v1/produccion/orden-bordado/").json()[0]

        self.assertIn("usuario_nombre", fila)
        self.assertEqual(fila["usuario_nombre"], self.usuario.get_full_name().strip() or self.usuario.email)
        self.assertEqual(fila["usuario_asignado"], self.usuario.pk)

    def test_retrieve_expone_usuario_nombre(self):
        """Mismo shape en retrieve que en list (misma identidad de serializer)."""
        orden = self._crear_orden(1)
        detalle = self._client().get(f"/api/v1/produccion/orden-bordado/{orden.pk}/").json()

        self.assertEqual(detalle["usuario_nombre"], self.usuario.get_full_name().strip() or self.usuario.email)

    def test_list_expone_empresa_y_sucursal_con_id_y_nombre(self):
        """Aditivo: los ids crudos siguen, más las etiquetas legibles."""
        self._crear_orden(1)
        fila = self._client().get("/api/v1/produccion/orden-bordado/").json()[0]

        self.assertEqual(fila["empresa"], self.empresa.pk)
        self.assertEqual(fila["empresa_nombre"], "ACME SA")
        self.assertEqual(fila["sucursal"], self.sucursal.pk)
        self.assertEqual(fila["sucursal_nombre"], "Monterrey")

    def test_retrieve_expone_empresa_y_sucursal_con_id_y_nombre(self):
        """Mismo shape en retrieve que en list (misma identidad de serializer)."""
        orden = self._crear_orden(1)
        detalle = self._client().get(f"/api/v1/produccion/orden-bordado/{orden.pk}/").json()

        self.assertEqual(detalle["empresa_nombre"], "ACME SA")
        self.assertEqual(detalle["sucursal_nombre"], "Monterrey")

    def test_list_sin_n_mas_1_constante(self):
        """El nº de queries no crece con la cantidad de órdenes ni de
        renglones de detalle. Cada orden tiene ``usuario_asignado``/``empresa``/
        ``sucursal`` poblados (ver ``_crear_orden``), así que esto ejercita
        realmente el ``select_related`` agregado junto con ``usuario_nombre``/
        ``empresa_nombre``/``sucursal_nombre`` — sin él, este test falla
        (verificado manualmente: 5 vs. 14 queries al agregar
        ``usuario_asignado``, y 8 vs. 26 al agregar ``empresa``/``sucursal``,
        entre 3 y 12 órdenes en ambos casos)."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        client = self._client()

        for i in range(3):
            self._crear_orden(i, n_detalles=2)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get("/api/v1/produccion/orden-bordado/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 3)
        queries_con_3 = len(ctx.captured_queries)

        for i in range(3, 12):
            self._crear_orden(i, n_detalles=4)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get("/api/v1/produccion/orden-bordado/")
        self.assertEqual(len(resp.json()), 12)
        queries_con_12 = len(ctx.captured_queries)

        self.assertEqual(queries_con_3, queries_con_12)
        overhead = queries_con_3 - self.QUERIES_LIST
        self.assertGreaterEqual(overhead, 0)
        self.assertEqual(queries_con_12 - overhead, self.QUERIES_LIST)


class OrdenesCorteMangaListQueriesTests(TestCase):
    """N+1 del list de orden-corte-manga, mismo diagnóstico que Reflejante.

    ``OrdenesCorteMangaSerializer`` resuelve ``pedido`` (``pedido_folio``),
    ``usuario_asignado`` (``usuario_nombre``) y ``empresa``/``sucursal``
    (``empresa_nombre``/``sucursal_nombre``) por orden, y
    ``producto``/``talla``/``color`` por renglón de ``detalles``.
    ``OrdenCorteMangaDetalle.configuracion`` es un ``JSONField`` plano —no una
    FK—, así que no necesita ``select_related``. Mismo shape que
    Reflejante/Bordado ahora: paridad completa entre los tres serializers.
    """

    #: 1 query de órdenes + 1 del ``Prefetch`` de ``detalles``.
    QUERIES_LIST = 2

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla = Talla.objects.create(nombre="CH")
        cls.usuario = Usuario.objects.create(
            username="operador",
            email="operador@acme.test",
            empresa=cls.empresa,
            sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )

    def _crear_orden(self, i, n_detalles=2):
        pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente,
            moneda=self.moneda, persona_pagos="Pagos",
            correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        pd = PedidoDetalle.objects.create(pedido=pedido, producto=self.producto)
        orden = OrdenesCorteManga.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=pedido,
            folio_ocm=f"OCM-{i}", usuario_asignado=self.usuario,
        )
        for _ in range(n_detalles):
            OrdenCorteMangaDetalle.objects.create(
                ocm=orden, pedido_detalle=pd, producto=self.producto,
                cantidad=1, talla=self.talla, configuracion={"nota": "test"},
            )
        return orden

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    def test_list_expone_empresa_y_sucursal_con_id_y_nombre(self):
        """Aditivo: los ids crudos siguen, más las etiquetas legibles."""
        self._crear_orden(1)
        fila = self._client().get("/api/v1/produccion/orden-corte-manga/").json()[0]

        self.assertEqual(fila["empresa"], self.empresa.pk)
        self.assertEqual(fila["empresa_nombre"], "ACME SA")
        self.assertEqual(fila["sucursal"], self.sucursal.pk)
        self.assertEqual(fila["sucursal_nombre"], "Monterrey")

    def test_retrieve_expone_empresa_y_sucursal_con_id_y_nombre(self):
        """Mismo shape en retrieve que en list (misma identidad de serializer)."""
        orden = self._crear_orden(1)
        detalle = self._client().get(f"/api/v1/produccion/orden-corte-manga/{orden.pk}/").json()

        self.assertEqual(detalle["empresa_nombre"], "ACME SA")
        self.assertEqual(detalle["sucursal_nombre"], "Monterrey")

    def test_list_sin_n_mas_1_constante(self):
        """El nº de queries no crece con la cantidad de órdenes ni de
        renglones de detalle. Cada orden tiene ``usuario_asignado``/``empresa``/
        ``sucursal`` poblados (ver ``_crear_orden``), así que esto ejercita
        realmente el ``select_related`` completo — sin ``empresa``/``sucursal``
        en él, este test falla (verificado manualmente: 8 vs. 26 queries entre
        3 y 12 órdenes)."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        client = self._client()

        for i in range(3):
            self._crear_orden(i, n_detalles=2)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get("/api/v1/produccion/orden-corte-manga/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 3)
        queries_con_3 = len(ctx.captured_queries)

        for i in range(3, 12):
            self._crear_orden(i, n_detalles=4)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get("/api/v1/produccion/orden-corte-manga/")
        self.assertEqual(len(resp.json()), 12)
        queries_con_12 = len(ctx.captured_queries)

        self.assertEqual(queries_con_3, queries_con_12)
        overhead = queries_con_3 - self.QUERIES_LIST
        self.assertGreaterEqual(overhead, 0)
        self.assertEqual(queries_con_12 - overhead, self.QUERIES_LIST)


class OrdenBordadoParcialidadesTests(TestCase):
    """OBs parciales sobre un mismo pedido vía ``detalles_override[]``.

    Cubre lo que la constraint ``uq_orden_bordado_activa_por_pedido`` impedía
    (segunda OB parcial sobre el mismo pedido, removida en ``0026``) y el cupo
    por línea que la respalda: ``ya_asignado + nuevo <=
    PedidoDetalleTalla.cantidad``. Ese chequeo existía pero era inocuo con
    override, porque leía ``dt.cantidad`` *después* de que la rama de override
    lo pisa con lo solicitado, y terminaba comparando lo pedido contra sí mismo.

    Fixture de referencia (mismos números en todos los tests de abajo):

        pedido -> pedido_detalle -> talla CH: cantidad 10
                                 -> talla M : cantidad 6
    """

    CANTIDAD_CH = 10
    CANTIDAD_M = 6

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        SerieFolio.objects.create(
            empresa=cls.empresa,
            sucursal=cls.sucursal,
            tipo_documento="ORDEN_BORDADO",
            serie="OB",
        )
        cls.usuario = Usuario.objects.create(
            username="operador",
            email="operador@acme.test",
            empresa=cls.empresa,
            sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla_ch = Talla.objects.create(nombre="CH")
        cls.talla_m = Talla.objects.create(nombre="M")
        cls.talla_g = Talla.objects.create(nombre="G")

    def setUp(self):
        self.pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente,
            moneda=self.moneda, persona_pagos="Pagos",
            correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        self.detalle = PedidoDetalle.objects.create(
            pedido=self.pedido, producto=self.producto
        )
        self.pdt_ch = PedidoDetalleTalla.objects.create(
            pedido_detalle=self.detalle, talla=self.talla_ch,
            cantidad=self.CANTIDAD_CH, lleva_bordado=True,
        )
        self.pdt_m = PedidoDetalleTalla.objects.create(
            pedido_detalle=self.detalle, talla=self.talla_m,
            cantidad=self.CANTIDAD_M, lleva_bordado=True,
        )
        # El onboarding de OB sólo ofrece pedidos que mesa de control ya
        # programó hacia BORDADO (ver ``_payload_pedidos_onboarding``); sin
        # esto el pedido de este fixture no aparecería en ningún GET.
        self.pedido.programacion_conf = {
            "programaciones": [
                {"destino": "BORDADO", "cantidad": self.CANTIDAD_CH + self.CANTIDAD_M}
            ]
        }
        self.pedido.save(update_fields=["programacion_conf"])

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    def _post(self, overrides):
        """POST de onboarding con ``detalles_override``; ``overrides`` es
        ``[(PedidoDetalleTalla, cantidad), ...]``."""
        return self._client().post(
            "/api/v1/produccion/orden-bordado/onboarding/",
            {
                "pedido": self.pedido.pk,
                "detalles_override": [
                    {"pedido_detalle_talla_id": pdt.pk, "cantidad": cantidad}
                    for pdt, cantidad in overrides
                ],
            },
            format="json",
        )

    def _lineas_del_get(self):
        """``{talla_id: linea}`` del pedido de este test en el GET de onboarding,
        o ``None`` si el pedido ya no aparece en el catálogo."""
        data = self._client().get(
            "/api/v1/produccion/orden-bordado/onboarding/"
        ).json()
        pedido = next(
            (p for p in data["pedidos"] if p["id"] == self.pedido.pk), None
        )
        if pedido is None:
            return None
        return {linea["talla_id"]: linea for linea in pedido["detalles"]}

    # --- creación de parcialidades -------------------------------------------

    def test_dos_obs_parciales_secuenciales_sobre_lineas_distintas(self):
        """El caso que la constraint bloqueaba: dos OBs activas para un pedido.

        CH (10) va en la primera OB y M (6) en la segunda; no se solapan, así
        que ninguna consume cupo de la otra.
        """
        primera = self._post([(self.pdt_ch, self.CANTIDAD_CH)])
        self.assertEqual(primera.status_code, 201, primera.data)

        segunda = self._post([(self.pdt_m, self.CANTIDAD_M)])
        self.assertEqual(segunda.status_code, 201, segunda.data)

        self.assertNotEqual(primera.data["id"], segunda.data["id"])
        self.assertEqual(
            OrdenesBordado.objects.filter(pedido=self.pedido, activo=True).count(), 2
        )
        # Cada OB llevó sólo su línea, con la cantidad solicitada.
        self.assertEqual(
            [(d["talla"], d["cantidad"]) for d in primera.data["detalles"]],
            [(self.talla_ch.pk, float(self.CANTIDAD_CH))],
        )
        self.assertEqual(
            [(d["talla"], d["cantidad"]) for d in segunda.data["detalles"]],
            [(self.talla_m.pk, float(self.CANTIDAD_M))],
        )

    def test_dos_obs_parciales_sobre_la_misma_linea_hasta_agotar_el_cupo(self):
        """Fraccionar una misma línea: 4 + 6 = 10, exactamente la cantidad
        contratada de CH. El segundo POST debe pasar (cupo restante 6)."""
        primera = self._post([(self.pdt_ch, 4)])
        self.assertEqual(primera.status_code, 201, primera.data)

        segunda = self._post([(self.pdt_ch, 6)])
        self.assertEqual(segunda.status_code, 201, segunda.data)

        total = sum(
            d.cantidad
            for d in OrdenBordadoDetalle.objects.filter(
                ob__pedido=self.pedido, ob__activo=True, talla=self.talla_ch
            )
        )
        self.assertEqual(total, float(self.CANTIDAD_CH))

    def test_segunda_ob_que_excede_el_pendiente_de_la_linea_se_rechaza(self):
        """El bug del cupo: con CH ya cubierto en 4, pedir 7 más excede el
        pendiente (6) y debe dar 400 con el detalle del exceso —no 201 ni 500—.

        Ojo: 7 <= 10 (la cantidad del pedido), así que el chequeo del serializer
        lo deja pasar; quien tiene que atajarlo es el cupo del service.
        """
        self.assertEqual(self._post([(self.pdt_ch, 4)]).status_code, 201)

        resp = self._post([(self.pdt_ch, 7)])

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("detalles_exceso", resp.data)
        exceso = " ".join(str(x) for x in resp.data["detalles_exceso"])
        self.assertIn("ya_asignado=4.0", exceso)
        self.assertIn("solicitado=7.0", exceso)
        self.assertIn("disponible_restante=6.0", exceso)
        # No se creó una segunda OB.
        self.assertEqual(
            OrdenesBordado.objects.filter(pedido=self.pedido, activo=True).count(), 1
        )

    def test_ob_parcial_que_excede_sin_ob_previa_se_rechaza(self):
        """Sin OB previa el cupo es la cantidad del pedido; pedir 11 de CH (10)
        lo corta el serializer antes de llegar al service."""
        resp = self._post([(self.pdt_ch, 11)])

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(
            OrdenesBordado.objects.filter(pedido=self.pedido).count(), 0
        )

    # --- GET de onboarding: pendiente y exclusión ----------------------------

    def test_get_expone_asignada_y_pendiente_por_linea(self):
        """Tras una OB parcial de 4 sobre CH: CH queda 10/4/6 y M intacta 6/0/6."""
        self.assertEqual(self._post([(self.pdt_ch, 4)]).status_code, 201)

        lineas = self._lineas_del_get()

        self.assertIsNotNone(lineas)
        self.assertEqual(lineas[self.talla_ch.pk]["cantidad_pedido"], 10.0)
        self.assertEqual(lineas[self.talla_ch.pk]["cantidad_asignada"], 4.0)
        self.assertEqual(lineas[self.talla_ch.pk]["cantidad_pendiente"], 6.0)
        self.assertEqual(lineas[self.talla_m.pk]["cantidad_pedido"], 6.0)
        self.assertEqual(lineas[self.talla_m.pk]["cantidad_asignada"], 0.0)
        self.assertEqual(lineas[self.talla_m.pk]["cantidad_pendiente"], 6.0)

    def test_get_sin_ob_previa_reporta_pendiente_igual_a_lo_pedido(self):
        lineas = self._lineas_del_get()

        self.assertIsNotNone(lineas)
        for pdt, talla in ((self.pdt_ch, self.talla_ch), (self.pdt_m, self.talla_m)):
            linea = lineas[talla.pk]
            self.assertEqual(linea["cantidad_asignada"], 0.0)
            self.assertEqual(linea["cantidad_pendiente"], float(pdt.cantidad))

    def test_pedido_totalmente_cubierto_desaparece_del_get(self):
        """CH (10) y M (6) cubiertas al 100%: no queda nada que bordar."""
        resp = self._post(
            [(self.pdt_ch, self.CANTIDAD_CH), (self.pdt_m, self.CANTIDAD_M)]
        )
        self.assertEqual(resp.status_code, 201, resp.data)

        self.assertIsNone(self._lineas_del_get())

    def test_pedido_parcialmente_cubierto_sigue_en_el_get_con_lineas_agotadas(self):
        """CH agotada (10/10) pero M pendiente: el pedido sigue apareciendo y
        viaja con **las dos** líneas, para que el frontend marque la agotada."""
        self.assertEqual(
            self._post([(self.pdt_ch, self.CANTIDAD_CH)]).status_code, 201
        )

        lineas = self._lineas_del_get()

        self.assertIsNotNone(lineas)
        self.assertEqual(len(lineas), 2)
        self.assertEqual(lineas[self.talla_ch.pk]["cantidad_asignada"], 10.0)
        self.assertEqual(lineas[self.talla_ch.pk]["cantidad_pendiente"], 0.0)
        self.assertEqual(lineas[self.talla_m.pk]["cantidad_pendiente"], 6.0)

    def test_ob_dada_de_baja_devuelve_el_cupo_y_el_pedido_al_get(self):
        """El soft delete libera cupo: ``_cantidades_asignadas_por_linea`` sólo
        cuenta OBs ``activo=True``."""
        resp = self._post(
            [(self.pdt_ch, self.CANTIDAD_CH), (self.pdt_m, self.CANTIDAD_M)]
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIsNone(self._lineas_del_get())

        OrdenesBordado.objects.get(pk=resp.data["id"]).soft_delete()

        lineas = self._lineas_del_get()
        self.assertIsNotNone(lineas)
        self.assertEqual(lineas[self.talla_ch.pk]["cantidad_pendiente"], 10.0)
        self.assertEqual(lineas[self.talla_m.pk]["cantidad_pendiente"], 6.0)


class OrdenBordadoRevisionParcialidadesTests(OrdenBordadoParcialidadesTests):
    """Defectos encontrados en la revisión de las parcialidades de OB.

    Hereda el fixture de ``OrdenBordadoParcialidadesTests``
    (CH cantidad=10, M cantidad=6, ambas ``lleva_bordado=True``).
    """

    def test_ob_parcial_en_todas_las_lineas_no_dispara_409_falso(self):
        """``buscar_existente_full_match`` contaba renglones, no piezas.

        Una OB parcial que toca las dos líneas con cantidades reducidas tiene el
        mismo número de renglones que una completa, así que el POST siguiente
        sin override recibía un 409 diciendo "ya existe ... con el 100% de las
        prendas" sobre un pedido cubierto a medias. Debe ser un 400 de exceso,
        que sí dice cuántas piezas quedan.
        """
        self.assertEqual(self._post([(self.pdt_ch, 5), (self.pdt_m, 3)]).status_code, 201)

        resp = self._client().post(
            "/api/v1/produccion/orden-bordado/onboarding/",
            {"pedido": self.pedido.pk},
            format="json",
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("detalles_exceso", resp.data)
        exceso = " ".join(str(x) for x in resp.data["detalles_exceso"])
        self.assertIn("disponible_restante=5.0", exceso)
        self.assertIn("disponible_restante=3.0", exceso)

    def test_pedido_cubierto_al_100_por_ciento_sigue_dando_409(self):
        """El 409 legítimo no se pierde: cobertura real del 100% en piezas."""
        self.assertEqual(
            self._post(
                [(self.pdt_ch, self.CANTIDAD_CH), (self.pdt_m, self.CANTIDAD_M)]
            ).status_code,
            201,
        )

        resp = self._client().post(
            "/api/v1/produccion/orden-bordado/onboarding/",
            {"pedido": self.pedido.pk},
            format="json",
        )

        self.assertEqual(resp.status_code, 409, resp.data)

    def test_renglones_sin_talla_consumen_cupo(self):
        """``OrdenBordadoDetalle.talla`` es ``SET_NULL`` y el picking escribe
        NULL cuando la talla no trae variante; esas piezas existen.

        Antes caían en la clave ``(pd_id, None)`` que ningún lookup buscaba, así
        que contaban como cero y se podía reprogramar el pedido completo.
        El renglón tiene 16 piezas en total (CH 10 + M 6); con 10 ya
        programadas sin talla, sólo quedan 6.
        """
        ob = OrdenesBordado.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=self.pedido,
            folio_bordado="OB-PICKING-SIN-TALLA",
        )
        OrdenBordadoDetalle.objects.create(
            ob=ob, pedido_detalle=self.detalle, producto=self.producto,
            cantidad=10, talla=None,
        )

        resp = self._post([(self.pdt_ch, self.CANTIDAD_CH)])

        self.assertEqual(resp.status_code, 400, resp.data)
        exceso = " ".join(str(x) for x in resp.data["detalles_exceso"])
        self.assertIn("total del renglón", exceso)
        self.assertIn("ya_asignado=10.0", exceso)
        self.assertIn("disponible_restante=6.0", exceso)

    def test_renglones_sin_talla_se_reflejan_en_el_pendiente_del_get(self):
        """El total pendiente del renglón baja aunque no se sepa la talla."""
        ob = OrdenesBordado.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=self.pedido,
            folio_bordado="OB-PICKING-SIN-TALLA",
        )
        OrdenBordadoDetalle.objects.create(
            ob=ob, pedido_detalle=self.detalle, producto=self.producto,
            cantidad=10, talla=None,
        )

        lineas = self._lineas_del_get()

        self.assertIsNotNone(lineas)
        total_pendiente = sum(l["cantidad_pendiente"] for l in lineas.values())
        self.assertEqual(total_pendiente, 6.0)

    def test_cantidad_fraccionaria_se_rechaza(self):
        """``PedidoDetalleTalla.cantidad`` es entero: no se bordan medias prendas."""
        resp = self._post([(self.pdt_ch, 2.5)])

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("entero", str(resp.data["detalles_override"]))
        self.assertEqual(OrdenesBordado.objects.filter(pedido=self.pedido).count(), 0)

    def test_get_no_ofrece_lineas_en_cantidad_cero(self):
        """El GET filtraba sólo por ``lleva_bordado``; el service exige además
        ``cantidad > 0``, así que ofrecía renglones que el POST rechazaba con un
        'no pertenece a este pedido' falso."""
        pdt_g = PedidoDetalleTalla.objects.create(
            pedido_detalle=self.detalle, talla=self.talla_g,
            cantidad=0, lleva_bordado=True,
        )

        lineas = self._lineas_del_get()

        self.assertIsNotNone(lineas)
        self.assertNotIn(self.talla_g.pk, lineas)
        self.assertNotIn(
            pdt_g.pk, [l["pedido_detalle_talla_id"] for l in lineas.values()]
        )

    def test_get_no_dispara_una_query_por_pedido_detalle(self):
        """El ``Prefetch`` filtrado sustituye al ``det.tallas.filter(...)`` que
        ignoraba la caché y consultaba una vez por ``PedidoDetalle``.

        Se afirma que el número de queries **no crece** con los renglones (mismo
        criterio que ``test_list_sin_n_mas_1_constante``), no un número mágico.
        """
        def _agregar_detalles(n):
            for _ in range(n):
                det = PedidoDetalle.objects.create(
                    pedido=self.pedido, producto=self.producto
                )
                PedidoDetalleTalla.objects.create(
                    pedido_detalle=det, talla=self.talla_ch,
                    cantidad=5, lleva_bordado=True,
                )

        def _queries():
            client = self._client()
            with CaptureQueriesContext(connection) as ctx:
                client.get("/api/v1/produccion/orden-bordado/onboarding/")
            return len(ctx)

        _agregar_detalles(1)
        con_2 = _queries()
        _agregar_detalles(8)
        con_10 = _queries()

        self.assertEqual(con_2, con_10)


class OrdenReflejanteOnboardingGetTests(TestCase):
    """El GET de OR debe exponer el mismo pendiente que el de OB.

    Los tres onboardings se mantienen simétricos a propósito; sólo OB tenía
    ``cantidad_asignada``/``cantidad_pendiente``, así que OR y OCM seguían
    ofreciendo el total contratado aunque ya estuviera programado.
    """

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        SerieFolio.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal,
            tipo_documento="ORDEN_REFLEJANTE", serie="OR",
        )
        cls.usuario = Usuario.objects.create(
            username="operador", email="operador@acme.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla_ch = Talla.objects.create(nombre="CH")

    def _lineas(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        data = client.get("/api/v1/produccion/orden-reflejante/onboarding/").json()
        pedido = next((p for p in data["pedidos"] if p["id"] == self.pedido.pk), None)
        return None if pedido is None else pedido["detalles"]

    def setUp(self):
        self.pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente,
            moneda=self.moneda, persona_pagos="Pagos",
            correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        self.detalle = PedidoDetalle.objects.create(
            pedido=self.pedido, producto=self.producto
        )
        PedidoDetalleTalla.objects.create(
            pedido_detalle=self.detalle, talla=self.talla_ch,
            cantidad=10, lleva_reflejante=True,
        )
        # El onboarding de OR sólo ofrece pedidos programados hacia
        # REFLEJANTE (ver ``_payload_pedidos_onboarding``).
        self.pedido.programacion_conf = {
            "programaciones": [{"destino": "REFLEJANTE", "cantidad": 10}]
        }
        self.pedido.save(update_fields=["programacion_conf"])

    def test_get_expone_asignada_y_pendiente(self):
        orden = OrdenesReflejante.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=self.pedido,
            folio_reflejante="OR-PARCIAL-1",
        )
        OrdenReflejanteDetalle.objects.create(
            orden_r=orden, pedido_detalle=self.detalle, producto=self.producto,
            cantidad=4, talla=self.talla_ch,
        )

        lineas = self._lineas()

        self.assertIsNotNone(lineas)
        self.assertEqual(lineas[0]["cantidad_pedido"], 10.0)
        self.assertEqual(lineas[0]["cantidad_asignada"], 4.0)
        self.assertEqual(lineas[0]["cantidad_pendiente"], 6.0)

    def test_pedido_cubierto_desaparece_del_get(self):
        orden = OrdenesReflejante.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=self.pedido,
            folio_reflejante="OR-COMPLETA",
        )
        OrdenReflejanteDetalle.objects.create(
            orden_r=orden, pedido_detalle=self.detalle, producto=self.producto,
            cantidad=10, talla=self.talla_ch,
        )

        self.assertIsNone(self._lineas())


class OrdenReflejanteParcialidadesTests(TestCase):
    """ORs parciales sobre un mismo pedido vía ``detalles_override[]``.

    Análogo de ``OrdenBordadoParcialidadesTests`` para reflejante: OR tenía la
    mecánica de override y el cupo por línea, pero ni un solo test que la
    ejercitara.

    Fixture de referencia (mismos números en todos los tests de abajo):

        pedido -> pedido_detalle -> talla CH: cantidad 10
                                 -> talla M : cantidad 6
    """

    CANTIDAD_CH = 10
    CANTIDAD_M = 6

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        SerieFolio.objects.create(
            empresa=cls.empresa,
            sucursal=cls.sucursal,
            tipo_documento="ORDEN_REFLEJANTE",
            serie="OR",
        )
        cls.usuario = Usuario.objects.create(
            username="operador",
            email="operador@acme.test",
            empresa=cls.empresa,
            sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla_ch = Talla.objects.create(nombre="CH")
        cls.talla_m = Talla.objects.create(nombre="M")
        cls.talla_g = Talla.objects.create(nombre="G")

    def setUp(self):
        self.pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente,
            moneda=self.moneda, persona_pagos="Pagos",
            correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        self.detalle = PedidoDetalle.objects.create(
            pedido=self.pedido, producto=self.producto
        )
        self.pdt_ch = PedidoDetalleTalla.objects.create(
            pedido_detalle=self.detalle, talla=self.talla_ch,
            cantidad=self.CANTIDAD_CH, lleva_reflejante=True,
        )
        self.pdt_m = PedidoDetalleTalla.objects.create(
            pedido_detalle=self.detalle, talla=self.talla_m,
            cantidad=self.CANTIDAD_M, lleva_reflejante=True,
        )
        # El onboarding de OR sólo ofrece pedidos programados hacia
        # REFLEJANTE (ver ``_payload_pedidos_onboarding``).
        self.pedido.programacion_conf = {
            "programaciones": [
                {"destino": "REFLEJANTE", "cantidad": self.CANTIDAD_CH + self.CANTIDAD_M}
            ]
        }
        self.pedido.save(update_fields=["programacion_conf"])

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    def _post(self, overrides):
        """POST de onboarding con ``detalles_override``; ``overrides`` es
        ``[(PedidoDetalleTalla, cantidad), ...]``."""
        return self._client().post(
            "/api/v1/produccion/orden-reflejante/onboarding/",
            {
                "pedido": self.pedido.pk,
                "detalles_override": [
                    {"pedido_detalle_talla_id": pdt.pk, "cantidad": cantidad}
                    for pdt, cantidad in overrides
                ],
            },
            format="json",
        )

    def _lineas_del_get(self):
        """``{talla_id: linea}`` del pedido de este test en el GET de onboarding,
        o ``None`` si el pedido ya no aparece en el catálogo."""
        data = self._client().get(
            "/api/v1/produccion/orden-reflejante/onboarding/"
        ).json()
        pedido = next(
            (p for p in data["pedidos"] if p["id"] == self.pedido.pk), None
        )
        if pedido is None:
            return None
        return {linea["talla_id"]: linea for linea in pedido["detalles"]}

    # --- creación de parcialidades -------------------------------------------

    def test_dos_ors_parciales_secuenciales_sobre_lineas_distintas(self):
        """Dos ORs activas para un pedido: CH (10) en la primera y M (6) en la
        segunda; no se solapan, así que ninguna consume cupo de la otra."""
        primera = self._post([(self.pdt_ch, self.CANTIDAD_CH)])
        self.assertEqual(primera.status_code, 201, primera.data)

        segunda = self._post([(self.pdt_m, self.CANTIDAD_M)])
        self.assertEqual(segunda.status_code, 201, segunda.data)

        self.assertNotEqual(primera.data["id"], segunda.data["id"])
        self.assertEqual(
            OrdenesReflejante.objects.filter(pedido=self.pedido, activo=True).count(), 2
        )
        self.assertEqual(
            [(d["talla"], d["cantidad"]) for d in primera.data["detalles"]],
            [(self.talla_ch.pk, float(self.CANTIDAD_CH))],
        )
        self.assertEqual(
            [(d["talla"], d["cantidad"]) for d in segunda.data["detalles"]],
            [(self.talla_m.pk, float(self.CANTIDAD_M))],
        )

    def test_dos_ors_parciales_sobre_la_misma_linea_hasta_agotar_el_cupo(self):
        """Fraccionar una misma línea: 4 + 6 = 10, exactamente la cantidad
        contratada de CH. El segundo POST debe pasar (cupo restante 6).

        Es también el test de la tolerancia ``EPS_CANTIDAD``: sin ella, el
        residuo de coma flotante de la suma podía dejar el segundo POST un
        epsilon por encima del pendiente y devolver un 400 falso.
        """
        primera = self._post([(self.pdt_ch, 4)])
        self.assertEqual(primera.status_code, 201, primera.data)

        segunda = self._post([(self.pdt_ch, 6)])
        self.assertEqual(segunda.status_code, 201, segunda.data)

        total = sum(
            d.cantidad
            for d in OrdenReflejanteDetalle.objects.filter(
                orden_r__pedido=self.pedido, orden_r__activo=True, talla=self.talla_ch
            )
        )
        self.assertEqual(total, float(self.CANTIDAD_CH))

    def test_segunda_or_que_excede_el_pendiente_de_la_linea_se_rechaza(self):
        """Con CH ya cubierto en 4, pedir 7 más excede el pendiente (6) y debe
        dar 400 con el detalle del exceso —no 201 ni 500—.

        Ojo: 7 <= 10 (la cantidad del pedido), así que el chequeo del serializer
        lo deja pasar; quien tiene que atajarlo es el cupo del service.
        """
        self.assertEqual(self._post([(self.pdt_ch, 4)]).status_code, 201)

        resp = self._post([(self.pdt_ch, 7)])

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("detalles_exceso", resp.data)
        exceso = " ".join(str(x) for x in resp.data["detalles_exceso"])
        self.assertIn("ya_asignado=4.0", exceso)
        self.assertIn("solicitado=7.0", exceso)
        self.assertIn("disponible_restante=6.0", exceso)
        self.assertEqual(
            OrdenesReflejante.objects.filter(pedido=self.pedido, activo=True).count(), 1
        )

    def test_or_parcial_que_excede_sin_or_previa_se_rechaza(self):
        """Sin OR previa el cupo es la cantidad del pedido; pedir 11 de CH (10)
        lo corta el serializer antes de llegar al service."""
        resp = self._post([(self.pdt_ch, 11)])

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(
            OrdenesReflejante.objects.filter(pedido=self.pedido).count(), 0
        )

    # --- GET de onboarding: pendiente y exclusión ----------------------------

    def test_get_expone_asignada_y_pendiente_por_linea(self):
        """Tras una OR parcial de 4 sobre CH: CH queda 10/4/6 y M intacta 6/0/6."""
        self.assertEqual(self._post([(self.pdt_ch, 4)]).status_code, 201)

        lineas = self._lineas_del_get()

        self.assertIsNotNone(lineas)
        self.assertEqual(lineas[self.talla_ch.pk]["cantidad_pedido"], 10.0)
        self.assertEqual(lineas[self.talla_ch.pk]["cantidad_asignada"], 4.0)
        self.assertEqual(lineas[self.talla_ch.pk]["cantidad_pendiente"], 6.0)
        self.assertEqual(lineas[self.talla_m.pk]["cantidad_pedido"], 6.0)
        self.assertEqual(lineas[self.talla_m.pk]["cantidad_asignada"], 0.0)
        self.assertEqual(lineas[self.talla_m.pk]["cantidad_pendiente"], 6.0)

    def test_pedido_totalmente_cubierto_desaparece_del_get(self):
        """CH (10) y M (6) cubiertas al 100%: no queda nada que reflejar."""
        resp = self._post(
            [(self.pdt_ch, self.CANTIDAD_CH), (self.pdt_m, self.CANTIDAD_M)]
        )
        self.assertEqual(resp.status_code, 201, resp.data)

        self.assertIsNone(self._lineas_del_get())

    def test_or_dada_de_baja_devuelve_el_cupo_y_el_pedido_al_get(self):
        """El soft delete libera cupo: ``_cantidades_asignadas_por_linea`` sólo
        cuenta ORs ``activo=True``."""
        resp = self._post(
            [(self.pdt_ch, self.CANTIDAD_CH), (self.pdt_m, self.CANTIDAD_M)]
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIsNone(self._lineas_del_get())

        OrdenesReflejante.objects.get(pk=resp.data["id"]).soft_delete()

        lineas = self._lineas_del_get()
        self.assertIsNotNone(lineas)
        self.assertEqual(lineas[self.talla_ch.pk]["cantidad_pendiente"], 10.0)
        self.assertEqual(lineas[self.talla_m.pk]["cantidad_pendiente"], 6.0)

    # --- endurecimiento portado de OB ----------------------------------------

    def test_or_parcial_en_todas_las_lineas_no_dispara_409_falso(self):
        """Una OR parcial que toca las dos líneas con cantidades reducidas tiene
        el mismo número de renglones que una completa; el POST siguiente sin
        override debe dar el 400 de exceso —que sí dice cuántas piezas quedan—,
        no un 409 de "ya existe con el 100% de las prendas"."""
        self.assertEqual(self._post([(self.pdt_ch, 5), (self.pdt_m, 3)]).status_code, 201)

        resp = self._client().post(
            "/api/v1/produccion/orden-reflejante/onboarding/",
            {"pedido": self.pedido.pk},
            format="json",
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("detalles_exceso", resp.data)
        exceso = " ".join(str(x) for x in resp.data["detalles_exceso"])
        self.assertIn("disponible_restante=5.0", exceso)
        self.assertIn("disponible_restante=3.0", exceso)

    def test_pedido_cubierto_al_100_por_ciento_sigue_dando_409(self):
        """El 409 legítimo no se pierde: cobertura real del 100% en piezas."""
        self.assertEqual(
            self._post(
                [(self.pdt_ch, self.CANTIDAD_CH), (self.pdt_m, self.CANTIDAD_M)]
            ).status_code,
            201,
        )

        resp = self._client().post(
            "/api/v1/produccion/orden-reflejante/onboarding/",
            {"pedido": self.pedido.pk},
            format="json",
        )

        self.assertEqual(resp.status_code, 409, resp.data)

    def test_renglones_sin_talla_consumen_cupo(self):
        """Fix (c): ``_asignado_sin_talla`` se calculaba y se descartaba.

        ``OrdenReflejanteDetalle.talla`` es ``SET_NULL`` y el picking escribe
        NULL cuando la talla no trae variante; esas piezas existen. Sin el
        segundo corte por ``pedido_detalle`` caían en una clave que ningún
        lookup buscaba y contaban como cero, así que se podía reprogramar el
        pedido completo. El renglón tiene 16 piezas (CH 10 + M 6); con 10 ya
        programadas sin talla, sólo quedan 6.
        """
        orden = OrdenesReflejante.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=self.pedido,
            folio_reflejante="OR-PICKING-SIN-TALLA",
        )
        OrdenReflejanteDetalle.objects.create(
            orden_r=orden, pedido_detalle=self.detalle, producto=self.producto,
            cantidad=10, talla=None,
        )

        resp = self._post([(self.pdt_ch, self.CANTIDAD_CH)])

        self.assertEqual(resp.status_code, 400, resp.data)
        exceso = " ".join(str(x) for x in resp.data["detalles_exceso"])
        self.assertIn("total del renglón", exceso)
        self.assertIn("ya_asignado=10.0", exceso)
        self.assertIn("disponible_restante=6.0", exceso)

    def test_cantidad_fraccionaria_se_rechaza(self):
        """Fix (b): ``PedidoDetalleTalla.cantidad`` es entero, no se reflejan
        medias prendas. Antes de este arreglo el POST devolvía 201."""
        resp = self._post([(self.pdt_ch, 2.5)])

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("entero", str(resp.data["detalles_override"]))
        self.assertEqual(
            OrdenesReflejante.objects.filter(pedido=self.pedido).count(), 0
        )

    def test_pedido_detalle_talla_desconocido_se_rechaza(self):
        """Fix (e): el id que no está entre las tallas elegibles se RECHAZA.

        Antes se descartaba en silencio y el POST devolvía 201 con menos
        renglones de los pedidos. Una talla marcada pero en cantidad 0 es
        justamente ese caso: ``tallas_orden_trabajo_qs`` la excluye, así que
        pasa el serializer (existe, pertenece al pedido, ``lleva_reflejante``)
        y sólo el service puede atajarla.
        """
        pdt_g = PedidoDetalleTalla.objects.create(
            pedido_detalle=self.detalle, talla=self.talla_g,
            cantidad=0, lleva_reflejante=True,
        )

        resp = self._post([(self.pdt_ch, 5), (pdt_g, 3)])

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn(str(pdt_g.pk), str(resp.data))
        self.assertEqual(
            OrdenesReflejante.objects.filter(pedido=self.pedido).count(), 0
        )

    def test_cupo_no_dispara_una_query_por_linea(self):
        """Fix (e), segunda mitad: el loop de cupo re-consultaba
        ``PedidoDetalleTalla`` una vez por renglón. Ahora usa la foto tomada
        antes del loop, así que el nº de queries del SERVICE no crece con el nº
        de líneas.

        Se mide el service directamente, no el POST HTTP: el ``validate`` del
        serializer hace su propio ``.get()`` por renglón de ``detalles_override``
        —comportamiento preexistente, idéntico en ``OrdenBordadoSerializer``—
        que enmascararía la medición. Lo que este test fija es el service.
        """
        def _queries_para(overrides):
            with CaptureQueriesContext(connection) as ctx:
                OrdenReflejanteService.save(
                    {
                        "pedido": self.pedido,
                        "detalles_override": [
                            {"pedido_detalle_talla_id": pdt.pk, "cantidad": cantidad}
                            for pdt, cantidad in overrides
                        ],
                    },
                    self.usuario,
                )
            return len(ctx)

        con_1 = _queries_para([(self.pdt_ch, 1)])
        con_2 = _queries_para([(self.pdt_ch, 1), (self.pdt_m, 1)])

        self.assertEqual(con_1, con_2)


class OrdenReflejanteCoberturaParcialidadTests(OrdenReflejanteParcialidadesTests):
    """Cobertura en el listado y parcialidad en el detalle de la OR.

    Hereda el fixture de ``OrdenReflejanteParcialidadesTests``
    (CH cantidad=10, M cantidad=6, ambas ``lleva_reflejante=True``; total
    contratado = 16 piezas).
    """

    def _fila_del_list(self, orden_id):
        filas = self._client().get("/api/v1/produccion/orden-reflejante/").json()
        return next((f for f in filas if f["id"] == orden_id), None)

    def _detalle(self, orden_id):
        return self._client().get(
            f"/api/v1/produccion/orden-reflejante/{orden_id}/"
        ).json()

    # --- listado -------------------------------------------------------------

    def test_list_expone_cobertura_parcial(self):
        """Una OR de 4 piezas sobre un pedido que contrató 16."""
        resp = self._post([(self.pdt_ch, 4)])
        self.assertEqual(resp.status_code, 201, resp.data)

        fila = self._fila_del_list(resp.data["id"])

        self.assertIsNotNone(fila)
        self.assertEqual(fila["cantidad_cubierta"], 4)
        self.assertEqual(fila["cantidad_contratada"], 16)
        self.assertFalse(fila["cobertura_completa"])

    def test_list_expone_cobertura_completa(self):
        resp = self._post(
            [(self.pdt_ch, self.CANTIDAD_CH), (self.pdt_m, self.CANTIDAD_M)]
        )
        self.assertEqual(resp.status_code, 201, resp.data)

        fila = self._fila_del_list(resp.data["id"])

        self.assertEqual(fila["cantidad_cubierta"], 16)
        self.assertEqual(fila["cantidad_contratada"], 16)
        self.assertTrue(fila["cobertura_completa"])

    def test_cobertura_no_suma_las_ordenes_hermanas(self):
        """Cada OR reporta LO QUE ELLA cubre, no el saldo del pedido: dos
        parciales distintas del mismo pedido no pueden dar el mismo número."""
        primera = self._post([(self.pdt_ch, 4)])
        segunda = self._post([(self.pdt_m, 6)])

        self.assertEqual(self._fila_del_list(primera.data["id"])["cantidad_cubierta"], 4)
        self.assertEqual(self._fila_del_list(segunda.data["id"])["cantidad_cubierta"], 6)

    def test_cobertura_usa_piso_y_no_sobre_reporta(self):
        """Piso, no redondeo: 9.6 sobre 16 publica 9, nunca 10.

        Se escribe el renglón directo porque el serializer ya no acepta
        fraccionarios (fix b); el pipeline de picking sí puede generarlos.
        """
        orden = OrdenesReflejante.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=self.pedido,
            folio_reflejante="OR-FRACCION",
        )
        OrdenReflejanteDetalle.objects.create(
            orden_r=orden, pedido_detalle=self.detalle, producto=self.producto,
            cantidad=9.6, talla=self.talla_ch,
        )

        fila = self._fila_del_list(orden.pk)

        self.assertEqual(fila["cantidad_cubierta"], 9)
        self.assertFalse(fila["cobertura_completa"])

    def test_pedido_sin_piezas_contratadas_no_se_declara_completo(self):
        """Sin nada que cubrir, ``cobertura_completa`` es False (no True vacío)."""
        pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente,
            moneda=self.moneda, persona_pagos="Pagos",
            correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        orden = OrdenesReflejante.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=pedido,
            folio_reflejante="OR-VACIA",
        )

        fila = self._fila_del_list(orden.pk)

        self.assertEqual(fila["cantidad_contratada"], 0)
        self.assertEqual(fila["cantidad_cubierta"], 0)
        self.assertFalse(fila["cobertura_completa"])

    # --- detalle -------------------------------------------------------------

    def test_retrieve_expone_parcialidad_por_linea(self):
        """La OR programa 4 de CH; la línea contrató 10, así que quedan 6."""
        resp = self._post([(self.pdt_ch, 4)])
        self.assertEqual(resp.status_code, 201, resp.data)

        detalle = self._detalle(resp.data["id"])

        self.assertEqual(len(detalle["detalles"]), 1)
        linea = detalle["detalles"][0]
        self.assertEqual(linea["cantidad"], 4.0)
        self.assertEqual(linea["cantidad_pedido"], 10.0)
        self.assertEqual(linea["cantidad_asignada"], 4.0)
        self.assertEqual(linea["cantidad_pendiente"], 6.0)

    def test_retrieve_tambien_trae_la_cobertura(self):
        """El detalle hereda del serializer de listado: puede enunciar
        "cubre 4 de 16" sin leer además la fila del listado."""
        resp = self._post([(self.pdt_ch, 4)])

        detalle = self._detalle(resp.data["id"])

        self.assertEqual(detalle["cantidad_cubierta"], 4)
        self.assertEqual(detalle["cantidad_contratada"], 16)
        self.assertFalse(detalle["cobertura_completa"])

    def test_retrieve_expone_las_ordenes_hermanas(self):
        primera = self._post([(self.pdt_ch, self.CANTIDAD_CH)])
        segunda = self._post([(self.pdt_m, self.CANTIDAD_M)])

        detalle = self._detalle(primera.data["id"])

        hermanas = detalle["otras_ordenes_del_pedido"]
        self.assertEqual([h["id"] for h in hermanas], [segunda.data["id"]])
        self.assertIn("folio_reflejante", hermanas[0])
        self.assertIn("fecha_inicio", hermanas[0])
        # No se incluye a sí misma.
        self.assertNotIn(primera.data["id"], [h["id"] for h in hermanas])

    def test_retrieve_sin_hermanas_devuelve_lista_vacia(self):
        resp = self._post([(self.pdt_ch, 4)])

        detalle = self._detalle(resp.data["id"])

        self.assertEqual(detalle["otras_ordenes_del_pedido"], [])
        self.assertFalse(detalle["reparto_por_talla_aproximado"])

    def test_retrieve_marca_reparto_aproximado_con_renglones_sin_talla(self):
        """Y el renglón sin talla NO sale con los tres campos en ``null``: cae
        al total del ``pedido_detalle`` (respaldo ``partialidad_por_detalle``)."""
        orden = OrdenesReflejante.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=self.pedido,
            folio_reflejante="OR-SIN-TALLA",
        )
        OrdenReflejanteDetalle.objects.create(
            orden_r=orden, pedido_detalle=self.detalle, producto=self.producto,
            cantidad=4, talla=None,
        )

        detalle = self._detalle(orden.pk)

        self.assertTrue(detalle["reparto_por_talla_aproximado"])
        linea = detalle["detalles"][0]
        self.assertIsNotNone(linea["cantidad_pedido"])
        self.assertEqual(linea["cantidad_pedido"], 16.0)
        self.assertEqual(linea["cantidad_asignada"], 4.0)
        self.assertEqual(linea["cantidad_pendiente"], 12.0)

    def test_retrieve_no_crece_con_el_numero_de_renglones(self):
        """El detalle cuesta lo mismo con 1 renglón que con 2: delta 0.

        El contexto de parcialidad siempre se resolvió UNA vez para todos los
        renglones (3 queries constantes), así que el delta medía únicamente el
        crecimiento del serializer. Ese crecimiento era 1 query por renglón:
        ``OrdenReflejanteDetalleSerializer`` re-leía ``PedidoDetalleTalla`` por
        fila para resolver ``reflejante_config``, y su
        ``_pdt_reflejante_cache`` nunca acertaba porque la clave
        ``(pedido_detalle_id, talla_id)`` es única por renglón.

        Ahora ``OrdenReflejanteViewSet.retrieve`` resuelve el config de todo el
        pedido en UNA query y lo pasa por ``context["pdt_reflejante_config_map"]``, de modo
        que el endpoint quedó PLANO. Este test fija esa propiedad —más fuerte
        que la anterior—: ni la parcialidad ni el renglón añaden nada por fila.
        """
        def _queries(orden_id):
            client = self._client()
            with CaptureQueriesContext(connection) as ctx:
                client.get(f"/api/v1/produccion/orden-reflejante/{orden_id}/")
            return len(ctx)

        una_linea = self._post([(self.pdt_ch, 4)])
        dos_lineas = self._post([(self.pdt_ch, 6), (self.pdt_m, 6)])

        delta = _queries(dos_lineas.data["id"]) - _queries(una_linea.data["id"])

        self.assertEqual(delta, 0)


#: Formas REALES de los tres ``*_config`` de ``PedidoDetalleTalla``, tomadas de
#: la base de producción (no inventadas). El resto de los fixtures de este
#: archivo crea las tallas SIN config, así que ``cfg`` siempre quedaba en ``{}``
#: y ninguna prueba tocaba nunca el camino del arreglo: por eso el 500 del GET
#: de onboarding de reflejante sobrevivió a toda la suite.
#:
#: - ``bordado_config``     -> objeto (92/92 filas reales).
#: - ``corte_manga_config`` -> objeto (36/36 filas reales).
#: - ``reflejante_config``  -> ARREGLO (55/55 filas reales), y no siempre de un
#:   solo elemento: 6 filas traen 2 posiciones distintas.
BORDADO_CONFIG_REAL = {
    "notas": "",
    "ubicaciones": [{
        "dtf": False,
        "codigo": "A",
        "imagen": "https://example.invalid/cat.jpeg",
        "alto_cm": 0,
        "ancho_cm": 0,
        "pantones": None,
        "revelado": False,
        "sublimado": False,
        "color_hilo": "",
        "serigrafia": False,
        "nuevo_ponchado": False,
        "descripcion_posicion": None,
    }],
}
REFLEJANTE_CONFIG_REAL = [
    {"tipo": "costurable-plata-1", "opcion": "catalogo", "posicion": "ESPALDA"},
]
REFLEJANTE_CONFIG_REAL_MULTI = [
    {"tipo": "costurable-plata-1", "opcion": "catalogo", "posicion": "HOMBROS"},
    {"tipo": "costurable-plata-1", "opcion": "catalogo", "posicion": "FRENTE"},
]
CORTE_MANGA_CONFIG_REAL = {"tipo": "1"}

#: Configs MULTI copiados verbatim de P-00027-2026 (pedido real creado para
#: ejercitar justo este caso): la misma línea lleva 2 ubicaciones de bordado y
#: 3 elementos de reflejante que abarcan DOS materiales distintos
#: (``ignifuga-plata-1`` y ``costurable-plata-1``). Nótese que el primer
#: elemento de cada uno es el que alimenta los escalares del renglón, así que
#: todo lo demás es exactamente lo que se perdía.
def _ubicacion(codigo, imagen):
    return {
        "dtf": False,
        "codigo": codigo,
        "imagen": imagen,
        "alto_cm": 10,
        "ancho_cm": 10,
        "pantones": "",
        "revelado": False,
        "sublimado": False,
        "color_hilo": "",
        "serigrafia": False,
        "nuevo_ponchado": False,
        "descripcion_posicion": None,
    }


BORDADO_CONFIG_REAL_MULTI = {
    "notas": "",
    "ubicaciones": [
        _ubicacion("B", "https://example.invalid/cat-jpeg.jpeg"),
        _ubicacion("A", "https://example.invalid/Bordado-de-Cachorro-Infantil.jpg"),
    ],
}
REFLEJANTE_CONFIG_REAL_MULTI_3 = [
    {"tipo": "ignifuga-plata-1", "opcion": "catalogo", "posicion": "HOMBROS"},
    {"tipo": "ignifuga-plata-1", "opcion": "catalogo", "posicion": "BRAZOS"},
    {"tipo": "costurable-plata-1", "opcion": "catalogo", "posicion": "TIRANTES"},
]


class ConfiguracionCompletaEnDetalleTests(TestCase):
    """El renglón de la orden guarda el ``*_config`` ÍNTEGRO.

    Los escalares (``posicion_bordado``/``colores_hilo``/``puntadas`` y
    ``tipo_reflejante``/``posicion``/``metros``) se derivan del elemento ``[0]``
    y NO cambian: siguen siendo el atajo. Lo que cambia es que dejan de ser el
    único registro, porque una línea con varias ubicaciones/elementos perdía
    todo menos el primero (en reflejante, incluso un material distinto).

    La cardinalidad NO cambia: sigue habiendo UN renglón por
    (``pedido_detalle``, ``talla``) por más configs que traiga la línea.
    """

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        for tipo_documento, serie in SERIES:
            SerieFolio.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal,
                tipo_documento=tipo_documento, serie=serie,
            )
        cls.usuario = Usuario.objects.create(
            username="operador", email="operador@acme.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Chamarra")
        cls.talla_ch = Talla.objects.create(nombre="CH")

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    def _pedido(self, flag, campo_config, valor):
        pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente,
            moneda=self.moneda, persona_pagos="Pagos",
            correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        detalle = PedidoDetalle.objects.create(pedido=pedido, producto=self.producto)
        PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=self.talla_ch, cantidad=5,
            **{flag: True, campo_config: valor},
        )
        _programar(pedido)
        return pedido

    def _crear(self, url, pedido):
        resp = self._client().post(url, {"pedido": pedido.pk}, format="json")
        self.assertIn(resp.status_code, (200, 201), resp.data)
        return resp.data["id"]

    # --- BORDADO -------------------------------------------------------------

    def test_ob_multi_ubicacion_guarda_todas_las_ubicaciones(self):
        pedido = self._pedido(
            "lleva_bordado", "bordado_config", BORDADO_CONFIG_REAL_MULTI
        )

        ob_id = self._crear(
            "/api/v1/produccion/orden-bordado/onboarding/", pedido
        )

        renglones = list(OrdenBordadoDetalle.objects.filter(ob_id=ob_id))
        # La cardinalidad no cambia: 2 ubicaciones siguen siendo UN renglón.
        self.assertEqual(len(renglones), 1)
        renglon = renglones[0]
        # El config completo, con LAS DOS ubicaciones.
        self.assertEqual(renglon.configuracion, BORDADO_CONFIG_REAL_MULTI)
        self.assertEqual(
            [u["codigo"] for u in renglon.configuracion["ubicaciones"]], ["B", "A"]
        )
        # Y los escalares siguen derivándose de ``ubicaciones[0]``.
        self.assertEqual(renglon.posicion_bordado, "B")

    def test_ob_una_ubicacion_no_cambia_de_comportamiento(self):
        """Guardia de regresión: la línea de siempre se comporta igual."""
        pedido = self._pedido(
            "lleva_bordado", "bordado_config", BORDADO_CONFIG_REAL
        )

        ob_id = self._crear(
            "/api/v1/produccion/orden-bordado/onboarding/", pedido
        )

        renglon = OrdenBordadoDetalle.objects.get(ob_id=ob_id)
        self.assertEqual(renglon.posicion_bordado, "A")
        self.assertEqual(renglon.configuracion, BORDADO_CONFIG_REAL)

    def test_ob_configuracion_distingue_config_ausente_de_presente(self):
        """``None`` sólo cuando el pedido NO trae config.

        Antes esta prueba sólo afirmaba ``assertIsNone(configuracion)``, que se
        cumple igual si el service nunca asigna el campo —un ``JSONField``
        nulable sin asignar también es ``None``—, así que pasaba con y sin el
        arreglo y no protegía nada. Ahora contrasta las dos ramas en la misma
        prueba: la de config ausente es la que da ``None``, y la de config
        presente TIENE que guardarlo.
        """
        pedido_sin = self._pedido("lleva_bordado", "bordado_config", None)
        pedido_con = self._pedido(
            "lleva_bordado", "bordado_config", BORDADO_CONFIG_REAL
        )

        ob_sin = self._crear(
            "/api/v1/produccion/orden-bordado/onboarding/", pedido_sin
        )
        ob_con = self._crear(
            "/api/v1/produccion/orden-bordado/onboarding/", pedido_con
        )

        renglon_sin = OrdenBordadoDetalle.objects.get(ob_id=ob_sin)
        renglon_con = OrdenBordadoDetalle.objects.get(ob_id=ob_con)
        self.assertIsNone(renglon_sin.configuracion)
        self.assertIsNone(renglon_sin.posicion_bordado)
        self.assertEqual(renglon_con.configuracion, BORDADO_CONFIG_REAL)
        self.assertNotEqual(renglon_sin.configuracion, renglon_con.configuracion)

    # --- REFLEJANTE ----------------------------------------------------------

    def test_or_multi_elemento_guarda_el_arreglo_completo(self):
        pedido = self._pedido(
            "lleva_reflejante", "reflejante_config", REFLEJANTE_CONFIG_REAL_MULTI_3
        )

        or_id = self._crear(
            "/api/v1/produccion/orden-reflejante/onboarding/", pedido
        )

        renglones = list(OrdenReflejanteDetalle.objects.filter(orden_r_id=or_id))
        self.assertEqual(len(renglones), 1)
        renglon = renglones[0]
        # Los TRES elementos, incluido el material distinto del tercero.
        self.assertEqual(renglon.configuracion, REFLEJANTE_CONFIG_REAL_MULTI_3)
        self.assertEqual(
            [(e["tipo"], e["posicion"]) for e in renglon.configuracion],
            [("ignifuga-plata-1", "HOMBROS"),
             ("ignifuga-plata-1", "BRAZOS"),
             ("costurable-plata-1", "TIRANTES")],
        )
        # Y los escalares siguen saliendo del elemento [0].
        self.assertEqual(renglon.tipo_reflejante, "ignifuga-plata-1")
        self.assertEqual(renglon.posicion, "HOMBROS")

    def test_or_un_elemento_no_cambia_de_comportamiento(self):
        pedido = self._pedido(
            "lleva_reflejante", "reflejante_config", REFLEJANTE_CONFIG_REAL
        )

        or_id = self._crear(
            "/api/v1/produccion/orden-reflejante/onboarding/", pedido
        )

        renglon = OrdenReflejanteDetalle.objects.get(orden_r_id=or_id)
        self.assertEqual(renglon.tipo_reflejante, "costurable-plata-1")
        self.assertEqual(renglon.posicion, "ESPALDA")
        self.assertEqual(renglon.configuracion, REFLEJANTE_CONFIG_REAL)

    def test_or_configuracion_distingue_config_ausente_de_presente(self):
        """Ver ``test_ob_configuracion_distingue_config_ausente_de_presente``:
        la versión anterior pasaba con y sin el arreglo."""
        pedido_sin = self._pedido("lleva_reflejante", "reflejante_config", None)
        pedido_con = self._pedido(
            "lleva_reflejante", "reflejante_config", REFLEJANTE_CONFIG_REAL
        )

        or_sin = self._crear(
            "/api/v1/produccion/orden-reflejante/onboarding/", pedido_sin
        )
        or_con = self._crear(
            "/api/v1/produccion/orden-reflejante/onboarding/", pedido_con
        )

        renglon_sin = OrdenReflejanteDetalle.objects.get(orden_r_id=or_sin)
        renglon_con = OrdenReflejanteDetalle.objects.get(orden_r_id=or_con)
        self.assertIsNone(renglon_sin.configuracion)
        self.assertIsNone(renglon_sin.tipo_reflejante)
        self.assertEqual(renglon_con.configuracion, REFLEJANTE_CONFIG_REAL)
        self.assertNotEqual(renglon_sin.configuracion, renglon_con.configuracion)

    # --- superficie API: detalle sí, listado no ------------------------------

    def test_retrieve_publica_configuracion_y_el_listado_no(self):
        pedido_ob = self._pedido(
            "lleva_bordado", "bordado_config", BORDADO_CONFIG_REAL_MULTI
        )
        ob_id = self._crear(
            "/api/v1/produccion/orden-bordado/onboarding/", pedido_ob
        )
        pedido_or = self._pedido(
            "lleva_reflejante", "reflejante_config", REFLEJANTE_CONFIG_REAL_MULTI_3
        )
        or_id = self._crear(
            "/api/v1/produccion/orden-reflejante/onboarding/", pedido_or
        )

        for url, oid, esperado in (
            ("/api/v1/produccion/orden-bordado/", ob_id, BORDADO_CONFIG_REAL_MULTI),
            ("/api/v1/produccion/orden-reflejante/", or_id,
             REFLEJANTE_CONFIG_REAL_MULTI_3),
        ):
            detalle = self._client().get(f"{url}{oid}/").json()
            renglon = detalle["detalles"][0]
            self.assertIn("configuracion", renglon, url)
            self.assertEqual(renglon["configuracion"], esperado, url)

            fila = next(
                f for f in self._client().get(url).json() if f["id"] == oid
            )
            self.assertNotIn("configuracion", fila["detalles"][0], url)


class ConfigComoDictTests(TestCase):
    """``config_como_dict``: el punto único de normalización de ``*_config``."""

    def test_dict_pasa_intacto(self):
        self.assertEqual(
            config_como_dict(BORDADO_CONFIG_REAL), BORDADO_CONFIG_REAL
        )

    def test_arreglo_de_reflejante_no_revienta(self):
        """El caso que rompía: un arreglo no tiene ``.get``."""
        self.assertEqual(config_como_dict(REFLEJANTE_CONFIG_REAL), {})
        self.assertEqual(config_como_dict(REFLEJANTE_CONFIG_REAL_MULTI), {})

    def test_none_y_escalares(self):
        for valor in (None, "", 0, "texto", 5, []):
            self.assertEqual(config_como_dict(valor), {})


class OnboardingGetConfigFormaRealTests(TestCase):
    """El GET de onboarding contra ``*_config`` POBLADO con su forma real.

    Los demás fixtures del archivo dejan el config en ``None``, así que el
    ``cfg.get(...)`` del payload nunca veía un arreglo. Estas pruebas fijan el
    contrato con datos de la misma forma que producción: sin el guardia de
    ``config_como_dict``, la de reflejante responde 500 (``AttributeError``:
    un arreglo no tiene ``.get``).
    """

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        for tipo_documento, serie in SERIES:
            SerieFolio.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal,
                tipo_documento=tipo_documento, serie=serie,
            )
        cls.usuario = Usuario.objects.create(
            username="operador", email="operador@acme.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla_ch = Talla.objects.create(nombre="CH")

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    # Destino de ``programar`` correspondiente a cada flag de servicio, para
    # que el pedido del fixture aparezca en su onboarding (ver
    # ``_payload_pedidos_onboarding``: sólo ofrece pedidos ya programados).
    _DESTINO_POR_FLAG = {
        "lleva_bordado": "BORDADO",
        "lleva_reflejante": "REFLEJANTE",
        "lleva_corte_manga": "CORTE_MANGA",
    }

    def _pedido_con_config(self, flag, campo_config, valor):
        pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente,
            moneda=self.moneda, persona_pagos="Pagos",
            correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        detalle = PedidoDetalle.objects.create(pedido=pedido, producto=self.producto)
        PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=self.talla_ch, cantidad=10,
            **{flag: True, campo_config: valor},
        )
        pedido.programacion_conf = {
            "programaciones": [{"destino": self._DESTINO_POR_FLAG[flag], "cantidad": 10}]
        }
        pedido.save(update_fields=["programacion_conf"])
        return pedido

    def _lineas(self, url, pedido):
        resp = self._client().get(url)
        self.assertEqual(resp.status_code, 200, getattr(resp, "data", resp))
        entrada = next(
            (p for p in resp.json()["pedidos"] if p["id"] == pedido.pk), None
        )
        self.assertIsNotNone(entrada)
        return entrada["detalles"]

    # --- reflejante: la forma de ARREGLO, que es la que rompía ---------------

    def test_onboarding_reflejante_con_config_arreglo_responde_200(self):
        pedido = self._pedido_con_config(
            "lleva_reflejante", "reflejante_config", REFLEJANTE_CONFIG_REAL
        )

        lineas = self._lineas(
            "/api/v1/produccion/orden-reflejante/onboarding/", pedido
        )

        self.assertEqual(len(lineas), 1)
        # El arreglo no se traduce a la forma de bordado: para reflejante no
        # existen ubicaciones, foto ni notas (ver ``config_como_dict``).
        self.assertEqual(lineas[0]["ubicaciones"], [])
        self.assertIsNone(lineas[0]["foto"])
        self.assertIsNone(lineas[0]["notas"])
        self.assertIsNone(lineas[0]["posicion_sugerida"])
        # Y lo que sí importa del renglón sigue saliendo bien.
        self.assertEqual(lineas[0]["cantidad_pedido"], 10.0)
        self.assertEqual(lineas[0]["cantidad_pendiente"], 10.0)

    def test_onboarding_reflejante_con_arreglo_de_varias_posiciones(self):
        """Hay filas reales con 2 posiciones; tampoco deben reventar."""
        pedido = self._pedido_con_config(
            "lleva_reflejante", "reflejante_config", REFLEJANTE_CONFIG_REAL_MULTI
        )

        lineas = self._lineas(
            "/api/v1/produccion/orden-reflejante/onboarding/", pedido
        )

        self.assertEqual(len(lineas), 1)
        self.assertEqual(lineas[0]["cantidad_pendiente"], 10.0)

    def test_retrieve_reflejante_con_config_arreglo_responde_200(self):
        """El retrieve ya estaba guardado; queda fijado con config poblado."""
        pedido = self._pedido_con_config(
            "lleva_reflejante", "reflejante_config", REFLEJANTE_CONFIG_REAL
        )
        detalle = PedidoDetalle.objects.get(pedido=pedido)
        orden = OrdenesReflejante.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=pedido,
            folio_reflejante="OR-CFG-ARREGLO",
        )
        OrdenReflejanteDetalle.objects.create(
            orden_r=orden, pedido_detalle=detalle, producto=self.producto,
            cantidad=4, talla=self.talla_ch,
        )

        resp = self._client().get(
            f"/api/v1/produccion/orden-reflejante/{orden.pk}/"
        )

        self.assertEqual(resp.status_code, 200, getattr(resp, "data", resp))
        renglon = resp.json()["detalles"][0]
        # El arreglo CRUDO sí viaja íntegro, sin recortar elementos.
        self.assertEqual(renglon["reflejante_config"], REFLEJANTE_CONFIG_REAL)
        self.assertEqual(renglon["ubicaciones"], [])

    # --- bordado / corte de manga: la forma de OBJETO ------------------------

    def test_onboarding_bordado_con_config_objeto_responde_200(self):
        pedido = self._pedido_con_config(
            "lleva_bordado", "bordado_config", BORDADO_CONFIG_REAL
        )

        lineas = self._lineas(
            "/api/v1/produccion/orden-bordado/onboarding/", pedido
        )

        self.assertEqual(len(lineas), 1)
        # A diferencia de reflejante, aquí el objeto SÍ se lee: es la forma
        # contra la que se escribió el payload.
        self.assertEqual(len(lineas[0]["ubicaciones"]), 1)
        self.assertEqual(lineas[0]["ubicaciones"][0]["codigo"], "A")
        self.assertEqual(lineas[0]["posicion_sugerida"], "A")
        # ``foto`` queda en ``None`` aunque la ubicación traiga ``imagen``: el
        # payload busca ``foto``/``imagen``/``imagen_url``/``foto_url`` sólo en
        # el NIVEL SUPERIOR del config, y en los datos reales de bordado la
        # imagen vive dentro de ``ubicaciones[0]``. Se fija el comportamiento
        # observado, no el deseado — la imagen no se pierde (viaja entera en
        # ``ubicaciones``), pero el atajo ``foto`` nunca se llena. Gap
        # preexistente, reportado aparte; NO se corrige aquí para no mezclarlo
        # con el arreglo del 500.
        self.assertIsNone(lineas[0]["foto"])
        self.assertEqual(
            lineas[0]["ubicaciones"][0]["imagen"],
            BORDADO_CONFIG_REAL["ubicaciones"][0]["imagen"],
        )

    def test_onboarding_corte_manga_con_config_objeto_responde_200(self):
        pedido = self._pedido_con_config(
            "lleva_corte_manga", "corte_manga_config", CORTE_MANGA_CONFIG_REAL
        )

        lineas = self._lineas(
            "/api/v1/produccion/orden-corte-manga/onboarding/", pedido
        )

        self.assertEqual(len(lineas), 1)
        self.assertEqual(lineas[0]["ubicaciones"], [])
        self.assertEqual(lineas[0]["cantidad_pendiente"], 10.0)


#: ``bordado_config`` con la lista de ubicaciones VACÍA. Es la forma más común
#: en producción —45 de 96 filas— y hasta ahora no la tocaba ninguna prueba:
#: los fixtures cubrían 1 y 2 ubicaciones, nunca 0. Produce una combinación
#: propia: ``configuracion`` se guarda (el dict es truthy) mientras los tres
#: escalares quedan en su valor por defecto, porque ``primera_ubicacion`` es
#: ``{}``.
BORDADO_CONFIG_SIN_UBICACIONES = {"notas": "", "ubicaciones": []}

#: Configs con forma de ARREGLO para los campos que hoy son SIEMPRE objeto.
#: No existen en producción (``bordado_config`` 96/96 y ``corte_manga_config``
#: 36/36 son dicts); son el caso que ya reventó tres veces en reflejante y que
#: estas pruebas fijan como tolerado en los otros dos módulos.
BORDADO_CONFIG_FORMA_ARREGLO = [
    {"codigo": "A", "imagen": "https://example.invalid/x.jpg"},
]
CORTE_MANGA_CONFIG_FORMA_ARREGLO = [{"tipo": "1"}]


class BordadoSinUbicacionesTests(TestCase):
    """``bordado_config`` poblado pero con ``ubicaciones: []`` (45/96 filas).

    Es la forma más frecuente en producción y la que ningún fixture cubría.
    """

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        for tipo_documento, serie in SERIES:
            SerieFolio.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal,
                tipo_documento=tipo_documento, serie=serie,
            )
        cls.usuario = Usuario.objects.create(
            username="operador", email="operador@acme.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla_ch = Talla.objects.create(nombre="CH")

    def setUp(self):
        self.pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente,
            moneda=self.moneda, persona_pagos="Pagos",
            correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        self.detalle = PedidoDetalle.objects.create(
            pedido=self.pedido, producto=self.producto
        )
        self.pdt = PedidoDetalleTalla.objects.create(
            pedido_detalle=self.detalle, talla=self.talla_ch, cantidad=6,
            lleva_bordado=True, bordado_config=BORDADO_CONFIG_SIN_UBICACIONES,
        )
        # El onboarding de OB sólo ofrece pedidos ya programados hacia
        # BORDADO (ver ``_payload_pedidos_onboarding``).
        self.pedido.programacion_conf = {
            "programaciones": [{"destino": "BORDADO", "cantidad": 6}]
        }
        self.pedido.save(update_fields=["programacion_conf"])

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    def _crear_ob(self):
        resp = self._client().post(
            "/api/v1/produccion/orden-bordado/onboarding/",
            {"pedido": self.pedido.pk}, format="json",
        )
        self.assertIn(resp.status_code, (200, 201), resp.data)
        return resp.data["id"]

    def test_alta_guarda_el_config_y_deja_los_escalares_en_default(self):
        ob_id = self._crear_ob()

        renglon = OrdenBordadoDetalle.objects.get(ob_id=ob_id)
        # El config SÍ se guarda aunque no traiga ubicaciones: es un dict no
        # vacío, y perderlo borraría las ``notas`` de la línea.
        self.assertEqual(renglon.configuracion, BORDADO_CONFIG_SIN_UBICACIONES)
        # Y los escalares caen a su default, porque no hay ``ubicaciones[0]``.
        self.assertIsNone(renglon.posicion_bordado)
        self.assertEqual(renglon.colores_hilo, 0)
        self.assertEqual(renglon.puntadas, 0)

    def test_retrieve_no_revienta_y_publica_ubicaciones_vacias(self):
        ob_id = self._crear_ob()

        resp = self._client().get(f"/api/v1/produccion/orden-bordado/{ob_id}/")

        self.assertEqual(resp.status_code, 200, getattr(resp, "data", resp))
        renglon = resp.json()["detalles"][0]
        self.assertEqual(renglon["ubicaciones"], [])
        self.assertIsNone(renglon["foto"])
        # ``notas`` viaja vacío en el config, y el extractor descarta lo falsy.
        self.assertIsNone(renglon["notas"])
        self.assertEqual(renglon["bordado_config"], BORDADO_CONFIG_SIN_UBICACIONES)

    def test_onboarding_ofrece_la_linea_sin_ubicaciones(self):
        data = self._client().get(
            "/api/v1/produccion/orden-bordado/onboarding/"
        ).json()

        pedido = next(p for p in data["pedidos"] if p["id"] == self.pedido.pk)
        self.assertEqual(len(pedido["detalles"]), 1)
        self.assertEqual(pedido["detalles"][0]["ubicaciones"], [])
        self.assertIsNone(pedido["detalles"][0]["posicion_sugerida"])
        self.assertEqual(pedido["detalles"][0]["cantidad_pendiente"], 6.0)


class ToleranciaFormaConfigTests(TestCase):
    """Un ``*_config`` con forma de ARREGLO no debe reventar en NINGÚN módulo.

    Estas son las pruebas que habrían atajado los tres 500 históricos de
    reflejante. Se ejercitan los tres sitios que quedaron sin guardia tras
    ``7567f0f``:

    1. ``OrdenBordadoService.save`` — ``.get()`` sobre el config crudo.
    2. ``OrdenBordadoDetalleSerializer`` — ídem en el retrieve.
    3. ``OrdenCorteMangaDetalleSerializer.get_corte_manga_config`` — aquí el
       fallo era ``dict(cfg)``, que lanza ``ValueError: dictionary update
       sequence element #0 has length 1; 2 is required`` en vez de
       ``AttributeError`` (verificado revirtiendo el guardia).

    Ninguno puede dispararse con los datos de hoy (``bordado_config`` 96/96 y
    ``corte_manga_config`` 36/36 son objetos): son pruebas de tolerancia de
    forma, no de un bug activo.
    """

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        for tipo_documento, serie in SERIES:
            SerieFolio.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal,
                tipo_documento=tipo_documento, serie=serie,
            )
        cls.usuario = Usuario.objects.create(
            username="operador", email="operador@acme.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla_ch = Talla.objects.create(nombre="CH")

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    def _pedido(self, flag, campo_config, valor):
        pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente,
            moneda=self.moneda, persona_pagos="Pagos",
            correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        detalle = PedidoDetalle.objects.create(pedido=pedido, producto=self.producto)
        pdt = PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=self.talla_ch, cantidad=5,
            **{flag: True, campo_config: valor},
        )
        _programar(pedido)
        return pedido, pdt

    # --- sitio 1: OrdenBordadoService.save -----------------------------------

    def test_alta_ob_con_config_arreglo_no_revienta(self):
        """Sin el guardia: ``AttributeError: 'list' object has no attribute 'get'``."""
        pedido, _ = self._pedido(
            "lleva_bordado", "bordado_config", BORDADO_CONFIG_FORMA_ARREGLO
        )

        resp = self._client().post(
            "/api/v1/produccion/orden-bordado/onboarding/",
            {"pedido": pedido.pk}, format="json",
        )

        self.assertIn(resp.status_code, (200, 201), resp.data)
        renglon = OrdenBordadoDetalle.objects.get(ob_id=resp.data["id"])
        # Los escalares no se pueden derivar de un arreglo: quedan en default.
        self.assertIsNone(renglon.posicion_bordado)
        # Pero la foto conserva el config ÍNTEGRO en vez de tirarlo.
        self.assertEqual(renglon.configuracion, BORDADO_CONFIG_FORMA_ARREGLO)

    # --- sitio 2: OrdenBordadoDetalleSerializer ------------------------------

    def test_retrieve_ob_con_config_arreglo_no_revienta(self):
        """El pedido se edita DESPUÉS de emitir la orden y cambia de forma.

        Sin el guardia, el retrieve responde 500 en ``get_ubicaciones``.
        """
        pedido, pdt = self._pedido(
            "lleva_bordado", "bordado_config", BORDADO_CONFIG_REAL
        )
        resp = self._client().post(
            "/api/v1/produccion/orden-bordado/onboarding/",
            {"pedido": pedido.pk}, format="json",
        )
        ob_id = resp.data["id"]
        PedidoDetalleTalla.objects.filter(pk=pdt.pk).update(
            bordado_config=BORDADO_CONFIG_FORMA_ARREGLO
        )

        detalle = self._client().get(f"/api/v1/produccion/orden-bordado/{ob_id}/")

        self.assertEqual(detalle.status_code, 200, getattr(detalle, "data", detalle))
        renglon = detalle.json()["detalles"][0]
        self.assertEqual(renglon["ubicaciones"], [])
        self.assertIsNone(renglon["foto"])
        self.assertIsNone(renglon["notas"])
        # El arreglo crudo sí se publica entero, sin recortarlo.
        self.assertEqual(renglon["bordado_config"], BORDADO_CONFIG_FORMA_ARREGLO)

    # --- sitio 3: OrdenCorteMangaDetalleSerializer ---------------------------

    def test_retrieve_ocm_con_config_arreglo_no_revienta(self):
        """La rama de mezcla hacía ``dict(arreglo)`` -> ``ValueError``.

        Se necesita ``OrdenCorteMangaDetalle.configuracion`` con valor para
        entrar a esa rama, que es justo lo que deja el alta desde este módulo.
        """
        pedido, pdt = self._pedido(
            "lleva_corte_manga", "corte_manga_config", CORTE_MANGA_CONFIG_REAL
        )
        resp = self._client().post(
            "/api/v1/produccion/orden-corte-manga/onboarding/",
            {"pedido": pedido.pk}, format="json",
        )
        ocm_id = resp.data["id"]
        self.assertTrue(
            OrdenCorteMangaDetalle.objects.get(ocm_id=ocm_id).configuracion,
            "el alta debe dejar `configuracion` poblado para ejercitar la mezcla",
        )
        PedidoDetalleTalla.objects.filter(pk=pdt.pk).update(
            corte_manga_config=CORTE_MANGA_CONFIG_FORMA_ARREGLO
        )

        detalle = self._client().get(
            f"/api/v1/produccion/orden-corte-manga/{ocm_id}/"
        )

        self.assertEqual(detalle.status_code, 200, getattr(detalle, "data", detalle))
        renglon = detalle.json()["detalles"][0]
        self.assertEqual(renglon["ubicaciones"], [])
        # La mezcla se queda con el campo propio del renglón, que sí es dict.
        self.assertEqual(renglon["corte_manga_config"], CORTE_MANGA_CONFIG_REAL)

    def test_retrieve_ocm_con_config_arreglo_y_sin_configuracion_propia(self):
        """La otra rama: sin ``obj.configuracion`` se devuelve el crudo.

        Ahí el arreglo llega intacto al cliente, y son los tres extractores los
        que deben tolerarlo (``get_ubicaciones``/``get_foto``/``get_notas``).
        """
        pedido, pdt = self._pedido(
            "lleva_corte_manga", "corte_manga_config", CORTE_MANGA_CONFIG_REAL
        )
        resp = self._client().post(
            "/api/v1/produccion/orden-corte-manga/onboarding/",
            {"pedido": pedido.pk}, format="json",
        )
        ocm_id = resp.data["id"]
        OrdenCorteMangaDetalle.objects.filter(ocm_id=ocm_id).update(configuracion=None)
        PedidoDetalleTalla.objects.filter(pk=pdt.pk).update(
            corte_manga_config=CORTE_MANGA_CONFIG_FORMA_ARREGLO
        )

        detalle = self._client().get(
            f"/api/v1/produccion/orden-corte-manga/{ocm_id}/"
        )

        self.assertEqual(detalle.status_code, 200, getattr(detalle, "data", detalle))
        renglon = detalle.json()["detalles"][0]
        self.assertEqual(renglon["ubicaciones"], [])
        self.assertEqual(
            renglon["corte_manga_config"], CORTE_MANGA_CONFIG_FORMA_ARREGLO
        )


#: Claves EXACTAS que emite hoy cada línea del GET de onboarding. Bordado y
#: corte de manga las tienen congeladas: ``_payload_pedidos_onboarding`` es
#: compartido por los tres módulos, así que un cambio pensado para reflejante
#: puede alterarles la respuesta sin que nadie se entere. Reflejante suma
#: ``reflejante_config`` —y sólo él— porque su config es un ARREGLO y los cuatro
#: campos derivados del objeto le salen vacíos.
CLAVES_LINEA_ONBOARDING_BASE = {
    "pedido_detalle_talla_id",
    "pedido_detalle_id",
    "producto_id",
    "producto_nombre",
    "talla_id",
    "talla_nombre",
    "color_id",
    "color_nombre",
    "cantidad_pedido",
    "cantidad_asignada",
    "cantidad_pendiente",
    "posicion_sugerida",
    "ubicaciones",
    "foto",
    "notas",
    "tipos_servicio",
    "tipos_servicio_display",
}


class OnboardingReflejanteConfigCrudoTests(TestCase):
    """El GET de onboarding de OR publica el ``reflejante_config`` ÍNTEGRO.

    Sin esto el endpoint respondía 200 pero sin un solo dato de reflejante: el
    config es un ARREGLO, ``config_como_dict`` lo vuelve ``{}`` y los cuatro
    campos derivados (``posicion_sugerida``/``ubicaciones``/``foto``/``notas``)
    salían vacíos, así que el Paso 2 del alta no tenía nada que pintar.

    La clave y la forma son las MISMAS que ya publica el retrieve
    (``OrdenReflejanteDetalleSerializer.get_reflejante_config``): mismo nombre,
    mismo ``or None``, mismos elementos verbatim.
    """

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        for tipo_documento, serie in SERIES:
            SerieFolio.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal,
                tipo_documento=tipo_documento, serie=serie,
            )
        cls.usuario = Usuario.objects.create(
            username="operador", email="operador@acme.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Chamarra")
        cls.talla_ch = Talla.objects.create(nombre="CH")

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    _DESTINO_POR_FLAG = {
        "lleva_bordado": "BORDADO",
        "lleva_reflejante": "REFLEJANTE",
        "lleva_corte_manga": "CORTE_MANGA",
    }

    def _pedido(self, flag, campo_config, valor):
        pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente,
            moneda=self.moneda, persona_pagos="Pagos",
            correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        detalle = PedidoDetalle.objects.create(pedido=pedido, producto=self.producto)
        PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=self.talla_ch, cantidad=5,
            **{flag: True, campo_config: valor},
        )
        # El onboarding sólo ofrece pedidos ya programados hacia ese destino
        # (ver ``_payload_pedidos_onboarding``).
        pedido.programacion_conf = {
            "programaciones": [{"destino": self._DESTINO_POR_FLAG[flag], "cantidad": 5}]
        }
        pedido.save(update_fields=["programacion_conf"])
        return pedido

    def _lineas(self, url, pedido):
        resp = self._client().get(url)
        self.assertEqual(resp.status_code, 200, getattr(resp, "data", resp))
        entrada = next(
            (p for p in resp.json()["pedidos"] if p["id"] == pedido.pk), None
        )
        self.assertIsNotNone(entrada)
        return entrada["detalles"]

    # --- reflejante: el config crudo debe viajar -----------------------------

    def test_or_onboarding_publica_los_tres_elementos(self):
        """La forma real de P-00027-2026: 3 elementos, DOS materiales."""
        pedido = self._pedido(
            "lleva_reflejante", "reflejante_config", REFLEJANTE_CONFIG_REAL_MULTI_3
        )

        lineas = self._lineas(
            "/api/v1/produccion/orden-reflejante/onboarding/", pedido
        )

        self.assertEqual(len(lineas), 1)
        self.assertIn("reflejante_config", lineas[0])
        # Verbatim: los tres elementos, en orden, con sus tres claves.
        self.assertEqual(
            lineas[0]["reflejante_config"], REFLEJANTE_CONFIG_REAL_MULTI_3
        )
        self.assertEqual(
            [(e["tipo"], e["posicion"]) for e in lineas[0]["reflejante_config"]],
            [("ignifuga-plata-1", "HOMBROS"),
             ("ignifuga-plata-1", "BRAZOS"),
             ("costurable-plata-1", "TIRANTES")],
        )
        # Los cuatro derivados siguen vacíos: el arreglo NO se traduce a la
        # forma de bordado (ver ``config_como_dict``).
        self.assertEqual(lineas[0]["ubicaciones"], [])
        self.assertIsNone(lineas[0]["posicion_sugerida"])

    def test_or_onboarding_con_un_solo_elemento(self):
        """Guardia de regresión: el caso mayoritario (49 de 59 filas)."""
        pedido = self._pedido(
            "lleva_reflejante", "reflejante_config", REFLEJANTE_CONFIG_REAL
        )

        lineas = self._lineas(
            "/api/v1/produccion/orden-reflejante/onboarding/", pedido
        )

        self.assertEqual(lineas[0]["reflejante_config"], REFLEJANTE_CONFIG_REAL)

    def test_or_onboarding_con_config_nulo_o_vacio(self):
        """Sin config no revienta y la clave viaja en ``None``.

        Mismo valor vacío que el retrieve, que hace ``cfg or None``.
        """
        for valor in (None, []):
            with self.subTest(valor=valor):
                pedido = self._pedido(
                    "lleva_reflejante", "reflejante_config", valor
                )

                lineas = self._lineas(
                    "/api/v1/produccion/orden-reflejante/onboarding/", pedido
                )

                self.assertIn("reflejante_config", lineas[0])
                self.assertIsNone(lineas[0]["reflejante_config"])

    def test_or_onboarding_con_config_dict_no_revienta(self):
        """Forma inesperada (objeto en vez de arreglo): no debe romper.

        No existe en datos reales —59/59 son listas— pero el helper es
        compartido y no debe asumir la forma.
        """
        pedido = self._pedido(
            "lleva_reflejante", "reflejante_config", {"tipo": "x", "posicion": "FRENTE"}
        )

        lineas = self._lineas(
            "/api/v1/produccion/orden-reflejante/onboarding/", pedido
        )

        self.assertEqual(
            lineas[0]["reflejante_config"], {"tipo": "x", "posicion": "FRENTE"}
        )
        # Siendo dict, los derivados SÍ se pueden leer.
        self.assertEqual(lineas[0]["posicion_sugerida"], "FRENTE")

    # --- OB y OCM: la respuesta NO cambia ------------------------------------

    def test_ob_onboarding_no_gana_la_clave_del_config(self):
        """Bordado publica su config vía ``ubicaciones``; su shape no cambia."""
        pedido = self._pedido(
            "lleva_bordado", "bordado_config", BORDADO_CONFIG_REAL
        )

        lineas = self._lineas(
            "/api/v1/produccion/orden-bordado/onboarding/", pedido
        )

        self.assertEqual(set(lineas[0].keys()), CLAVES_LINEA_ONBOARDING_BASE)
        self.assertNotIn("bordado_config", lineas[0])
        # Y lo que ya publicaba sigue igual.
        self.assertEqual(len(lineas[0]["ubicaciones"]), 1)
        self.assertEqual(lineas[0]["posicion_sugerida"], "A")

    def test_ocm_onboarding_no_gana_la_clave_del_config(self):
        pedido = self._pedido(
            "lleva_corte_manga", "corte_manga_config", CORTE_MANGA_CONFIG_REAL
        )

        lineas = self._lineas(
            "/api/v1/produccion/orden-corte-manga/onboarding/", pedido
        )

        self.assertEqual(set(lineas[0].keys()), CLAVES_LINEA_ONBOARDING_BASE)
        self.assertNotIn("corte_manga_config", lineas[0])

    def test_or_onboarding_solo_suma_una_clave_sobre_la_base(self):
        """El shape de OR es el de siempre MÁS ``reflejante_config``.

        Fija que la clave nueva es aditiva: no se quitó ni renombró ninguna de
        las 15 que ya emitía el helper.
        """
        pedido = self._pedido(
            "lleva_reflejante", "reflejante_config", REFLEJANTE_CONFIG_REAL
        )

        lineas = self._lineas(
            "/api/v1/produccion/orden-reflejante/onboarding/", pedido
        )

        self.assertEqual(
            set(lineas[0].keys()),
            CLAVES_LINEA_ONBOARDING_BASE | {"reflejante_config"},
        )


class ConsumoOPMovimientoVarianteTests(TestCase):
    """El movimiento formal del consumo de OP conserva la variante consumida.

    El plan consolida por producto componente y ``_discount_existencias`` toma
    existencias del producto o de cualquiera de sus variantes, pero el detalle
    del movimiento es UNO POR EXISTENCIA consumida: cada uno debe llevar la
    variante (o ninguna) de la existencia que realmente se descontó.
    """

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme-cv", razon_social="acme-cv SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="ACV", nombre="acme-cv")
        cls.almacen = Almacen.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, codigo="ALM", nombre="Almacen",
        )
        cls.ubicacion = Ubicacion.objects.create(almacen=cls.almacen, pasillo="1")
        cls.usuario = Usuario.objects.create(
            username="u@acme-cv.test", email="u@acme-cv.test", empresa=cls.empresa,
        )
        cls.op = OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-CV-1",
        )
        color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000")
        cls.componente = Producto.objects.create(empresa=cls.empresa, nombre="Hilo")
        cls.variante = ProductoVariante.objects.create(
            producto=cls.componente, empresa=cls.empresa, color=color, sku="HIL-NEG", precio_base="1",
        )

    def test_detalle_guarda_la_variante_de_cada_existencia_consumida(self):
        ex_sin_var = Existencia.objects.create(
            producto=self.componente, almacen=self.almacen, cantidad=Decimal("5"),
        )
        ex_var = Existencia.objects.create(
            producto=self.componente, producto_variante=self.variante, almacen=self.almacen,
            ubicacion=self.ubicacion, cantidad=Decimal("3"),
        )
        consumos = OrdenProduccionService._discount_existencias(
            [{"producto": self.componente, "cantidad": Decimal("6")}], self.empresa, self.sucursal,
        )
        movimiento = OrdenProduccionService._registrar_movimiento_inventario(self.op, self.usuario, consumos)

        detalles = {
            d.ubicacion_origen_id: d
            for d in MovimientoInventarioDetalle.objects.filter(movimiento_inventario=movimiento)
        }
        self.assertEqual(len(detalles), 2)  # granularidad: uno por existencia
        sin_var = detalles[None]
        self.assertIsNone(sin_var.producto_variante_id)
        self.assertEqual(sin_var.cantidad, Decimal("5"))
        con_var = detalles[self.ubicacion.pk]
        self.assertEqual(con_var.producto_variante_id, self.variante.pk)
        self.assertEqual(con_var.cantidad, Decimal("1"))
        for d in detalles.values():
            self.assertEqual(d.producto_id, self.componente.pk)
            self.assertIsNone(d.ubicacion_destino_id)

        ex_sin_var.refresh_from_db()
        ex_var.refresh_from_db()
        self.assertEqual((ex_sin_var.cantidad, ex_var.cantidad), (Decimal("0"), Decimal("2")))


class PedidoEspecialViewSetTests(TestCase):
    """``GET /pedidos-especiales/``: sólo pedidos con línea especial YA

    clasificados y con fecha de confirmación -- antes de eso mesa de control
    todavía no define un compromiso de entrega real sobre el que producción
    pueda planear el alta del SKU/variante.
    """

    URL = "/api/v1/produccion/pedidos-especiales/"

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme-pe", razon_social="acme-pe SA")
        cls.empresa_b = Empresa.objects.create(codigo="globex-pe", razon_social="globex-pe SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="Matriz")
        cls.sucursal_b = Sucursal.objects.create(empresa=cls.empresa_b, codigo="GDL", nombre="GDL")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.cliente_b = Cliente.objects.create(empresa=cls.empresa_b, nombre="Cliente B")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla = Talla.objects.create(nombre="CH")
        cls.usuario = Usuario.objects.create(
            username="prod", email="prod@acme-pe.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    def _ids_listado(self):
        body = self._client().get(self.URL).json()
        filas = body.get("results", body) if isinstance(body, dict) else body
        return [p["id"] for p in filas]

    def _pedido_especial(self, empresa, sucursal, cliente, clasificacion=None, fecha_confirmacion=None):
        pedido = Pedido.objects.create(
            empresa=empresa, sucursal=sucursal, cliente=cliente, moneda=self.moneda,
            persona_pagos="Pagos", correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
            clasificacion=clasificacion, fecha_confirmacion=fecha_confirmacion,
        )
        detalle = PedidoDetalle.objects.create(
            pedido=pedido, producto_nombre_externo="Muestra especial"
        )
        PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=self.talla, cantidad=5, requiere_produccion=True,
        )
        return pedido

    def test_clasificado_y_confirmado_aparece(self):
        pedido = self._pedido_especial(
            self.empresa, self.sucursal, self.cliente,
            clasificacion="B", fecha_confirmacion=timezone.now(),
        )
        self.assertIn(pedido.pk, self._ids_listado())

    def test_sin_clasificacion_no_aparece(self):
        pedido = self._pedido_especial(
            self.empresa, self.sucursal, self.cliente,
            clasificacion=None, fecha_confirmacion=timezone.now(),
        )
        self.assertNotIn(pedido.pk, self._ids_listado())
        self.assertEqual(self._client().get(f"{self.URL}{pedido.pk}/").status_code, 404)

    def test_sin_fecha_confirmacion_no_aparece(self):
        pedido = self._pedido_especial(
            self.empresa, self.sucursal, self.cliente,
            clasificacion="B", fecha_confirmacion=None,
        )
        self.assertNotIn(pedido.pk, self._ids_listado())

    def test_pedido_sin_linea_especial_no_aparece_aunque_este_confirmado(self):
        pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente, moneda=self.moneda,
            persona_pagos="Pagos", correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
            clasificacion="B", fecha_confirmacion=timezone.now(),
        )
        detalle = PedidoDetalle.objects.create(pedido=pedido, producto=self.producto)
        PedidoDetalleTalla.objects.create(pedido_detalle=detalle, talla=self.talla, cantidad=5)

        self.assertNotIn(pedido.pk, self._ids_listado())

    def test_otra_empresa_no_aparece(self):
        pedido_b = self._pedido_especial(
            self.empresa_b, self.sucursal_b, self.cliente_b,
            clasificacion="B", fecha_confirmacion=timezone.now(),
        )
        self.assertNotIn(pedido_b.pk, self._ids_listado())
        self.assertEqual(self._client().get(f"{self.URL}{pedido_b.pk}/").status_code, 404)


class PedidosEspecialesPorNombreExternoTests(TestCase):
    """``/produccion/pedidos-especiales/`` se deriva de ``producto_nombre_externo``.

    No del flag guardado ``requiere_produccion``: autorizar/aceptar-cambios/
    recomprar lo copian tal cual desde la cotizacion y se desincroniza, asi
    que una muestra con el flag en False desaparecia del endpoint.
    """

    URL = "/api/v1/produccion/pedidos-especiales/"

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Monterrey"
        )
        cls.usuario = Usuario.objects.create(
            username="produccion", email="produccion@acme.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal,
        )
        cls.otra_empresa = Empresa.objects.create(codigo="otra", razon_social="OTRA SA")
        cls.otra_sucursal = Sucursal.objects.create(
            empresa=cls.otra_empresa, codigo="GDL", nombre="Guadalajara"
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.otro_cliente = Cliente.objects.create(empresa=cls.otra_empresa, nombre="Cliente 2")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla_ch = Talla.objects.create(nombre="CH")
        cls.talla_m = Talla.objects.create(nombre="M")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.usuario)

    def _pedido(self, empresa=None, sucursal=None, cliente=None):
        # clasificacion/fecha_confirmacion puestos por default: estos tests
        # ejercitan la derivación por ``producto_nombre_externo``, no el gate
        # de mesa de control (ver ``PedidoEspecialViewSetTests`` para ese otro
        # filtro) -- sin esto, ningún pedido de aquí aparecería nunca.
        return Pedido.objects.create(
            empresa=empresa or self.empresa, sucursal=sucursal or self.sucursal,
            cliente=cliente or self.cliente, moneda=self.moneda,
            persona_pagos="Pagos", correo_facturas="pagos@acme.test",
            telefono_pagos="8100000000", forma_pago="03", metodo_pago="PUE",
            uso_cfdi="G03", clasificacion="B", fecha_confirmacion=timezone.now(),
        )

    def _linea(self, pedido, nombre, requiere_produccion, tallas=None, producto=None):
        detalle = PedidoDetalle.objects.create(
            pedido=pedido, producto=producto, producto_nombre_externo=nombre
        )
        for talla in tallas or [self.talla_ch]:
            PedidoDetalleTalla.objects.create(
                pedido_detalle=detalle, talla=talla, cantidad=5,
                requiere_produccion=requiere_produccion,
            )
        return detalle

    def _ids_listado(self):
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        return {p["id"] for p in resp.json()}

    def test_muestra_con_flag_desincronizado_aparece_con_sus_tallas(self):
        pedido = self._pedido()
        detalle = self._linea(
            pedido, "Muestra cliente", requiere_produccion=False,
            tallas=[self.talla_ch, self.talla_m],
        )

        self.assertIn(pedido.pk, self._ids_listado())

        resp = self.client.get(f"{self.URL}{pedido.pk}/")
        self.assertEqual(resp.status_code, 200)
        detalles = resp.json()["detalles"]
        self.assertEqual([d["id"] for d in detalles], [detalle.pk])
        self.assertEqual(detalles[0]["producto_nombre_externo"], "Muestra cliente")
        self.assertEqual([t["talla_nombre"] for t in detalles[0]["tallas"]], ["CH", "M"])

    def test_flag_en_true_sin_nombre_no_aparece(self):
        sin_nombre = self._pedido()
        self._linea(sin_nombre, None, requiere_produccion=True, producto=self.producto)
        vacio = self._pedido()
        self._linea(vacio, "", requiere_produccion=True, producto=self.producto)

        ids = self._ids_listado()

        self.assertNotIn(sin_nombre.pk, ids)
        self.assertNotIn(vacio.pk, ids)
        self.assertEqual(self.client.get(f"{self.URL}{vacio.pk}/").status_code, 404)

    def test_pedido_mixto_solo_expone_las_lineas_de_muestra(self):
        pedido = self._pedido()
        self._linea(pedido, None, requiere_produccion=False, producto=self.producto)
        self._linea(pedido, "", requiere_produccion=False, producto=self.producto)
        muestra = self._linea(pedido, "Muestra", requiere_produccion=False)

        resp = self.client.get(f"{self.URL}{pedido.pk}/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([d["id"] for d in resp.json()["detalles"]], [muestra.pk])

    def test_muestra_de_otra_empresa_no_se_ve(self):
        ajeno = self._pedido(
            empresa=self.otra_empresa, sucursal=self.otra_sucursal, cliente=self.otro_cliente
        )
        self._linea(ajeno, "Muestra ajena", requiere_produccion=True)

        self.assertNotIn(ajeno.pk, self._ids_listado())
        self.assertEqual(self.client.get(f"{self.URL}{ajeno.pk}/").status_code, 404)


class OrdenProduccionPedidoOpcionalTests(TestCase):
    """``POST /orden-produccion/``: ``pedido`` es opcional, pero si viene debe

    ser un pedido de producción especial (muestra) ya clasificado y con fecha
    de confirmación — mismo criterio que filtra ``GET /pedidos-especiales/``,
    aquí como validación dura del lado servidor (el picker del frontend no es
    suficiente: sin esto, cualquier pedido_id se colaba).
    """

    URL = "/api/v1/produccion/orden-produccion/"

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme-op", razon_social="acme-op SA")
        cls.otra_empresa = Empresa.objects.create(codigo="otra-op", razon_social="otra-op SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="Matriz")
        cls.otra_sucursal = Sucursal.objects.create(
            empresa=cls.otra_empresa, codigo="GDL", nombre="GDL"
        )
        SerieFolio.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal,
            tipo_documento="ORDEN_PRODUCCION", serie="OP",
        )
        cls.usuario = Usuario.objects.create(
            username="op-user", email="op-user@acme-op.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.otro_cliente = Cliente.objects.create(empresa=cls.otra_empresa, nombre="Cliente 2")

        # Producto terminado + BOM + insumo con existencia suficiente, para
        # que la creación de la OP (sin pedido) llegue hasta el final.
        color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.variante = ProductoVariante.objects.create(
            producto=cls.producto, empresa=cls.empresa, color=color, sku="PLA-NEG", precio_base="100",
        )
        cls.unidad = UnidadMedida.objects.create(clave="PZA", nombre="Pieza")
        cls.componente = Producto.objects.create(empresa=cls.empresa, nombre="Tela")
        cls.bom = ListaMaterialBom.objects.create(
            empresa=cls.empresa, producto_variante=cls.variante, activo=True,
        )
        BomDetalle.objects.create(
            bom=cls.bom, componente=cls.componente, cantidad=1, unidad=cls.unidad,
        )
        cls.almacen = Almacen.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, codigo="ALM1", nombre="Principal",
        )
        Existencia.objects.create(almacen=cls.almacen, producto=cls.componente, cantidad=100)

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    def _body(self, pedido=None):
        body = {
            "empresa": self.empresa.pk,
            "sucursal": self.sucursal.pk,
            "orden_produccion_detalle": [
                {"producto_variante_id": self.variante.pk, "cantidad": "3.00", "unidad": self.unidad.pk}
            ],
        }
        if pedido is not None:
            body["pedido"] = pedido.pk
        return body

    def _pedido_muestra(self, empresa=None, sucursal=None, cliente=None, clasificacion="B", fecha_confirmacion="__default__"):
        if fecha_confirmacion == "__default__":
            fecha_confirmacion = timezone.now()
        pedido = Pedido.objects.create(
            empresa=empresa or self.empresa, sucursal=sucursal or self.sucursal,
            cliente=cliente or self.cliente, moneda=self.moneda,
            persona_pagos="Pagos", correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
            clasificacion=clasificacion, fecha_confirmacion=fecha_confirmacion,
        )
        PedidoDetalle.objects.create(pedido=pedido, producto_nombre_externo="Muestra especial")
        return pedido

    def test_sin_pedido_sigue_funcionando(self):
        resp = self._client().post(self.URL, self._body(), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_pedido_de_produccion_especial_confirmado_permite_crear(self):
        pedido = self._pedido_muestra()
        resp = self._client().post(self.URL, self._body(pedido), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        op = OrdenProduccion.objects.get(pk=resp.data["op_id"])
        self.assertEqual(op.pedido_id, pedido.pk)

    def test_pedido_sin_muestra_rechaza(self):
        pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente, moneda=self.moneda,
            persona_pagos="Pagos", correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
            clasificacion="B", fecha_confirmacion=timezone.now(),
        )
        PedidoDetalle.objects.create(pedido=pedido, producto=self.producto)

        resp = self._client().post(self.URL, self._body(pedido), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("pedido", resp.data)
        self.assertFalse(OrdenProduccion.objects.filter(pedido=pedido).exists())

    def test_pedido_sin_clasificar_rechaza(self):
        pedido = self._pedido_muestra(clasificacion=None, fecha_confirmacion=timezone.now())
        resp = self._client().post(self.URL, self._body(pedido), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("pedido", resp.data)

    def test_pedido_sin_fecha_confirmacion_rechaza(self):
        pedido = self._pedido_muestra(clasificacion="B", fecha_confirmacion=None)
        resp = self._client().post(self.URL, self._body(pedido), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("pedido", resp.data)

    def test_pedido_de_otra_empresa_rechaza(self):
        pedido = self._pedido_muestra(
            empresa=self.otra_empresa, sucursal=self.otra_sucursal, cliente=self.otro_cliente,
        )
        resp = self._client().post(self.URL, self._body(pedido), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("pedido", resp.data)

    def test_pedido_ajeno_e_inexistente_responden_igual(self):
        pedido = self._pedido_muestra(
            empresa=self.otra_empresa, sucursal=self.otra_sucursal, cliente=self.otro_cliente,
        )
        ajeno = self._client().post(self.URL, self._body(pedido), format="json")
        body = self._body()
        body["pedido"] = pedido.pk + 999
        inexistente = self._client().post(self.URL, body, format="json")

        self.assertEqual(ajeno.status_code, 400)
        self.assertEqual(
            str(ajeno.data["pedido"][0]).replace(str(pedido.pk), "X"),
            str(inexistente.data["pedido"][0]).replace(str(pedido.pk + 999), "X"),
        )

    def _op_creada(self, pedido=None):
        resp = self._client().post(self.URL, self._body(pedido), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        return OrdenProduccion.objects.get(pk=resp.data["op_id"])

    def test_patch_con_pedido_de_otra_empresa_rechaza(self):
        op = self._op_creada()
        pedido = self._pedido_muestra(
            empresa=self.otra_empresa, sucursal=self.otra_sucursal, cliente=self.otro_cliente,
        )
        resp = self._client().patch(f"{self.URL}{op.pk}/", {"pedido": pedido.pk}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("pedido", resp.data)
        op.refresh_from_db()
        self.assertIsNone(op.pedido_id)

    def test_patch_con_pedido_sin_muestra_rechaza(self):
        op = self._op_creada()
        pedido = self._pedido_muestra()
        PedidoDetalle.objects.filter(pedido=pedido).update(producto_nombre_externo="")

        resp = self._client().patch(f"{self.URL}{op.pk}/", {"pedido": pedido.pk}, format="json")

        self.assertEqual(resp.status_code, 400)
        op.refresh_from_db()
        self.assertIsNone(op.pedido_id)

    def test_patch_con_pedido_valido_lo_liga(self):
        op = self._op_creada()
        pedido = self._pedido_muestra()

        resp = self._client().patch(f"{self.URL}{op.pk}/", {"pedido": pedido.pk}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        op.refresh_from_db()
        self.assertEqual(op.pedido_id, pedido.pk)

    def test_patch_sin_cambiar_pedido_no_revalida_su_estado(self):
        pedido = self._pedido_muestra()
        op = self._op_creada(pedido)
        Pedido.objects.filter(pk=pedido.pk).update(clasificacion=None)

        resp = self._client().patch(
            f"{self.URL}{op.pk}/", {"pedido": pedido.pk, "prioridad": 2}, format="json"
        )

        self.assertEqual(resp.status_code, 200, resp.data)


class OnboardingPostScopeTenantTests(TestCase):
    """POST de onboarding de OB/OR/OCM: un pedido o una talla fuera del alcance
    del usuario se rechaza sin exponer datos del registro ajeno (#275)."""

    URLS = (
        "/api/v1/produccion/orden-bordado/onboarding/",
        "/api/v1/produccion/orden-reflejante/onboarding/",
        "/api/v1/produccion/orden-corte-manga/onboarding/",
    )
    CANTIDAD_AJENA = 37

    @classmethod
    def setUpTestData(cls):
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.talla = Talla.objects.create(nombre="CH")
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="Monterrey")
        cls.sucursal_sin_acceso = Sucursal.objects.create(
            empresa=cls.empresa, codigo="CDMX", nombre="CDMX"
        )
        cls.empresa_b = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        cls.sucursal_b = Sucursal.objects.create(empresa=cls.empresa_b, codigo="GDL", nombre="GDL")
        cls.usuario = Usuario.objects.create(
            username="operador", email="operador@acme.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal,
        )
        cls.pedido, cls.pdt = cls._pedido(cls.empresa, cls.sucursal, 10)
        cls.pedido_b, cls.pdt_b = cls._pedido(cls.empresa_b, cls.sucursal_b, cls.CANTIDAD_AJENA)
        cls.pedido_otra_sucursal, cls.pdt_otra_sucursal = cls._pedido(
            cls.empresa, cls.sucursal_sin_acceso, cls.CANTIDAD_AJENA
        )

    @classmethod
    def _pedido(cls, empresa, sucursal, cantidad):
        cliente = Cliente.objects.create(empresa=empresa, nombre=f"Cliente {empresa.codigo}")
        producto = Producto.objects.create(empresa=empresa, nombre="Playera")
        pedido = Pedido.objects.create(
            empresa=empresa, sucursal=sucursal, cliente=cliente, moneda=cls.moneda,
            persona_pagos="Pagos", correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        detalle = PedidoDetalle.objects.create(pedido=pedido, producto=producto)
        pdt = PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=cls.talla, cantidad=cantidad,
            lleva_bordado=True, lleva_reflejante=True, lleva_corte_manga=True,
        )
        _programar(pedido)
        return pedido, pdt

    def _post(self, url, pedido_id, pdt_id):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client.post(
            url,
            {"pedido": pedido_id, "detalles_override": [{"pedido_detalle_talla_id": pdt_id, "cantidad": 999999}]},
            format="json",
        )

    def test_pedido_de_otra_empresa_responde_como_inexistente(self):
        for url in self.URLS:
            with self.subTest(url=url):
                ajeno = self._post(url, self.pedido_b.pk, self.pdt_b.pk)
                inexistente = self._post(url, 999999, self.pdt_b.pk)

                self.assertEqual(ajeno.status_code, 400)
                self.assertNotIn("detalles_override", ajeno.data)
                self.assertEqual(
                    str(ajeno.data["pedido"][0]).replace(str(self.pedido_b.pk), "X"),
                    str(inexistente.data["pedido"][0]).replace("999999", "X"),
                )

    def test_talla_de_otro_pedido_responde_como_inexistente(self):
        for url in self.URLS:
            with self.subTest(url=url):
                ajena = self._post(url, self.pedido.pk, self.pdt_b.pk)
                inexistente = self._post(url, self.pedido.pk, 999999)

                self.assertEqual(ajena.status_code, 400)
                self.assertNotIn(str(self.CANTIDAD_AJENA), str(ajena.data))
                self.assertEqual(
                    str(ajena.data).replace(str(self.pdt_b.pk), "X"),
                    str(inexistente.data).replace("999999", "X"),
                )

    def test_pedido_de_sucursal_sin_acceso_no_expone_tallas(self):
        for url in self.URLS:
            with self.subTest(url=url):
                resp = self._post(url, self.pedido_otra_sucursal.pk, self.pdt_otra_sucursal.pk)

                self.assertEqual(resp.status_code, 400)
                self.assertIn("sucursal", str(resp.data["pedido"]))
                self.assertNotIn(str(self.CANTIDAD_AJENA), str(resp.data))


class OnboardingPostLineaMuestraTests(TestCase):
    """POST de onboarding con tallas de una línea de muestra (``producto`` null):
    400 accionable en vez del 500 por ``IntegrityError`` (#276)."""

    CASOS = (
        ("/api/v1/produccion/orden-bordado/onboarding/", OrdenesBordado, "ORDEN_BORDADO"),
        ("/api/v1/produccion/orden-reflejante/onboarding/", OrdenesReflejante, "ORDEN_REFLEJANTE"),
        ("/api/v1/produccion/orden-corte-manga/onboarding/", OrdenesCorteManga, "ORDEN_CORTE_MANGA"),
    )

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="Monterrey")
        for tipo_documento, serie in SERIES:
            SerieFolio.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal,
                tipo_documento=tipo_documento, serie=serie,
            )
        cls.usuario = Usuario.objects.create(
            username="operador", email="operador@acme.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal,
        )
        cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.pedido = Pedido.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, cliente=cliente,
            moneda=Moneda.objects.create(codigo_iso="MXN", nombre="Peso"),
            persona_pagos="Pagos", correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        detalle = PedidoDetalle.objects.create(
            pedido=cls.pedido, producto=None, producto_nombre_externo="ZXCZX"
        )
        cls.pdt = PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=Talla.objects.create(nombre="CH"), cantidad=5,
            lleva_bordado=True, lleva_reflejante=True, lleva_corte_manga=True,
        )
        _programar(cls.pedido)

    def test_linea_de_muestra_responde_400_sin_crear_ni_consumir_folio(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        for url, modelo, tipo_documento in self.CASOS:
            with self.subTest(url=url):
                serie = SerieFolio.objects.get(empresa=self.empresa, tipo_documento=tipo_documento)
                folio_previo = serie.folio_actual

                resp = client.post(url, {"pedido": self.pedido.pk}, format="json")

                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertEqual(
                    [str(i) for i in resp.data["pedido_detalle_talla_ids"]], [str(self.pdt.pk)]
                )
                self.assertFalse(modelo.objects.filter(pedido=self.pedido).exists())
                serie.refresh_from_db()
                self.assertEqual(serie.folio_actual, folio_previo)


class OrdenCorteMangaParcialidadesTests(TestCase):
    """OCMs parciales vía ``detalles_override[]``: mismas garantías de cupo,
    override y concurrencia que OB/OR (#277).

        pedido -> pedido_detalle -> talla CH: cantidad 10
                                 -> talla M : cantidad 6
    """

    URL = "/api/v1/produccion/orden-corte-manga/onboarding/"
    CANTIDAD_CH = 10
    CANTIDAD_M = 6

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="Monterrey")
        SerieFolio.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal,
            tipo_documento="ORDEN_CORTE_MANGA", serie="OCM",
        )
        cls.usuario = Usuario.objects.create(
            username="operador", email="operador@acme.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla_ch = Talla.objects.create(nombre="CH")
        cls.talla_m = Talla.objects.create(nombre="M")
        cls.talla_g = Talla.objects.create(nombre="G")

    def setUp(self):
        self.pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente,
            moneda=self.moneda, persona_pagos="Pagos",
            correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        self.detalle = PedidoDetalle.objects.create(pedido=self.pedido, producto=self.producto)
        self.pdt_ch = PedidoDetalleTalla.objects.create(
            pedido_detalle=self.detalle, talla=self.talla_ch,
            cantidad=self.CANTIDAD_CH, lleva_corte_manga=True,
        )
        self.pdt_m = PedidoDetalleTalla.objects.create(
            pedido_detalle=self.detalle, talla=self.talla_m,
            cantidad=self.CANTIDAD_M, lleva_corte_manga=True,
        )
        _programar(self.pedido, ("CORTE_MANGA",))

    def _post(self, overrides):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client.post(
            self.URL,
            {
                "pedido": self.pedido.pk,
                "detalles_override": [
                    {"pedido_detalle_talla_id": pdt.pk, "cantidad": cantidad}
                    for pdt, cantidad in overrides
                ],
            },
            format="json",
        )

    def _save(self, overrides):
        return OrdenCorteMangaService.save(
            {
                "pedido": self.pedido,
                "detalles_override": [
                    {"pedido_detalle_talla_id": pdt.pk, "cantidad": cantidad}
                    for pdt, cantidad in overrides
                ],
            },
            self.usuario,
        )

    def test_dos_parciales_sobre_la_misma_linea_hasta_agotar_el_cupo(self):
        self.assertEqual(self._post([(self.pdt_ch, 4)]).status_code, 201)
        segunda = self._post([(self.pdt_ch, 6)])

        self.assertEqual(segunda.status_code, 201, segunda.data)
        total = sum(
            d.cantidad for d in OrdenCorteMangaDetalle.objects.filter(
                ocm__pedido=self.pedido, ocm__activo=True, talla=self.talla_ch
            )
        )
        self.assertEqual(total, float(self.CANTIDAD_CH))

    def test_parcial_que_excede_el_pendiente_se_rechaza(self):
        self.assertEqual(self._post([(self.pdt_ch, 4)]).status_code, 201)

        resp = self._post([(self.pdt_ch, 7)])

        self.assertEqual(resp.status_code, 400, resp.data)
        exceso = " ".join(str(x) for x in resp.data["detalles_exceso"])
        self.assertIn("disponible_restante=6.0", exceso)
        self.assertEqual(OrdenesCorteManga.objects.filter(pedido=self.pedido).count(), 1)

    def test_override_con_talla_fuera_de_las_elegibles_se_rechaza(self):
        """Antes se descartaba en silencio y se creaba una OCM con menos
        renglones de los pedidos. Una talla en cantidad 0 no entra a
        ``tallas_orden_trabajo_qs``."""
        pdt_g = PedidoDetalleTalla.objects.create(
            pedido_detalle=self.detalle, talla=self.talla_g, cantidad=0, lleva_corte_manga=True,
        )

        with self.assertRaises(DRFValidationError) as ctx:
            self._save([(self.pdt_ch, 5), (pdt_g, 3)])

        self.assertIn(str(pdt_g.pk), str(ctx.exception.detail))
        self.assertFalse(OrdenesCorteManga.objects.filter(pedido=self.pedido).exists())

    def test_renglones_sin_talla_consumen_cupo(self):
        """El renglón tiene 16 piezas (CH 10 + M 6); con 10 ya programadas sin
        talla (p. ej. desde picking) solo quedan 6."""
        orden = OrdenesCorteManga.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=self.pedido,
            folio_ocm="OCM-SIN-TALLA",
        )
        OrdenCorteMangaDetalle.objects.create(
            ocm=orden, pedido_detalle=self.detalle, producto=self.producto,
            cantidad=10, talla=None,
        )

        resp = self._post([(self.pdt_ch, self.CANTIDAD_CH)])

        self.assertEqual(resp.status_code, 400, resp.data)
        exceso = " ".join(str(x) for x in resp.data["detalles_exceso"])
        self.assertIn("total del renglón", exceso)
        self.assertIn("ya_asignado=10.0", exceso)
        self.assertIn("disponible_restante=6.0", exceso)

    def test_cantidad_fraccionaria_se_rechaza(self):
        resp = self._post([(self.pdt_ch, 2.5)])

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("entero", str(resp.data["detalles_override"]))
        self.assertFalse(OrdenesCorteManga.objects.filter(pedido=self.pedido).exists())

    def test_cupo_no_dispara_una_query_por_linea(self):
        def _queries_para(overrides):
            with CaptureQueriesContext(connection) as ctx:
                self._save(overrides)
            return len(ctx)

        con_1 = _queries_para([(self.pdt_ch, 1)])
        con_2 = _queries_para([(self.pdt_ch, 1), (self.pdt_m, 1)])

        self.assertEqual(con_1, con_2)

    def test_bloquea_el_pedido_antes_de_leer_lo_asignado(self):
        """``select_for_update`` es no-op en SQLite; se verifica que se pida."""
        with mock.patch.object(
            Pedido.objects, "select_for_update", wraps=Pedido.objects.select_for_update
        ) as candado:
            self._save([(self.pdt_ch, 1)])

        candado.assert_called_once_with()


class RutaCriticaOPTests(TestCase):
    """``/orden-produccion/{op_id}/ruta-critica/`` (#282, #284, #285, #286)."""

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="Monterrey")
        cls.produccion = Usuario.objects.create(
            username="produccion", email="produccion@acme.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.lector = Usuario.objects.create(
            username="lector", email="lector@acme.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal,
        )

    def setUp(self):
        self.op = OrdenProduccion.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, folio_op=f"OP-{OrdenProduccion.objects.count() + 1}",
        )
        self.url = f"/api/v1/produccion/orden-produccion/{self.op.pk}/ruta-critica/"

    def _client(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.produccion)
        return client

    def _patch(self, body):
        return self._client().patch(self.url, body, format="json")

    # --- #282: el GET no escribe ---------------------------------------------

    def test_get_sin_registro_no_lo_crea(self):
        resp = self._client(self.lector).get(self.url)

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(resp.data["existencia_tela"])
        self.assertIsNone(resp.data["cantidad_real_corte"])
        self.assertFalse(OrdenProduccionRutaCritica.objects.filter(op=self.op).exists())

    def test_primer_patch_crea_el_registro(self):
        resp = self._patch({"corte_recibido": True})

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(OrdenProduccionRutaCritica.objects.get(op=self.op).corte_recibido)
        self.assertTrue(self._client(self.lector).get(self.url).data["corte_recibido"])

    # --- #284: la fecha es "marcado desde" -----------------------------------

    def test_desmarcar_limpia_la_fecha(self):
        marcado = self._patch({"existencia_tela": True})
        self.assertIsNotNone(marcado.data["fecha_existencia_tela"])

        desmarcado = self._patch({"existencia_tela": False})

        self.assertFalse(desmarcado.data["existencia_tela"])
        self.assertIsNone(desmarcado.data["fecha_existencia_tela"])

    def test_reenviar_el_mismo_valor_conserva_la_fecha(self):
        fecha = self._patch({"existencia_avios": True}).data["fecha_existencia_avios"]

        resp = self._patch({"existencia_avios": True, "kit_completo": True})

        self.assertEqual(resp.data["fecha_existencia_avios"], fecha)

    # --- #285: OP cerrada no se edita ----------------------------------------

    def test_patch_en_op_completada_o_cancelada_responde_409(self):
        for estatus in (
            OrdenProduccion.EstatusOrdenProduccion.COMPLETADO,
            OrdenProduccion.EstatusOrdenProduccion.CANCELADO,
        ):
            with self.subTest(estatus=estatus):
                OrdenProduccion.objects.filter(pk=self.op.pk).update(estatus_op=estatus)

                resp = self._patch({"corte_recibido": True})

                self.assertEqual(resp.status_code, 409, resp.data)
                self.assertFalse(OrdenProduccionRutaCritica.objects.filter(op=self.op).exists())
                self.assertEqual(self._client().get(self.url).status_code, 200)

    def test_patch_en_op_detenida_sigue_permitido(self):
        OrdenProduccion.objects.filter(pk=self.op.pk).update(
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.DETENIDO
        )

        self.assertEqual(self._patch({"corte_recibido": True}).status_code, 200)

    # --- #286: cantidad_real_corte -------------------------------------------

    def test_cantidad_real_corte_rechaza_negativos_y_decimales(self):
        for valor in (-3, "-3.5", "12.5"):
            with self.subTest(valor=valor):
                resp = self._patch({"cantidad_real_corte": valor})

                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertIn("cantidad_real_corte", resp.data)

    def test_cantidad_real_corte_acepta_enteros_cero_y_null(self):
        for valor, esperado in ((120, "120.00"), (0, "0.00"), (None, None)):
            with self.subTest(valor=valor):
                resp = self._patch({"cantidad_real_corte": valor})

                self.assertEqual(resp.status_code, 200, resp.data)
                self.assertEqual(resp.data["cantidad_real_corte"], esperado)


class OnboardingPostProgramacionTests(TestCase):
    """#278: el POST de onboarding exige la misma programación de Mesa de
    Control que el GET usa para ofrecer el pedido."""

    CASOS = (
        ("/api/v1/produccion/orden-bordado/onboarding/", OrdenesBordado, "BORDADO"),
        ("/api/v1/produccion/orden-reflejante/onboarding/", OrdenesReflejante, "REFLEJANTE"),
        ("/api/v1/produccion/orden-corte-manga/onboarding/", OrdenesCorteManga, "CORTE_MANGA"),
    )

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="Monterrey")
        for tipo_documento, serie in SERIES:
            SerieFolio.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal, tipo_documento=tipo_documento, serie=serie,
            )
        cls.usuario = Usuario.objects.create(
            username="operador", email="operador@acme.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 1")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.talla = Talla.objects.create(nombre="CH")

    def _pedido(self):
        pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente, moneda=self.moneda,
            persona_pagos="Pagos", correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        detalle = PedidoDetalle.objects.create(pedido=pedido, producto=self.producto)
        PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=self.talla, cantidad=5,
            lleva_bordado=True, lleva_reflejante=True, lleva_corte_manga=True,
        )
        return pedido

    def _post(self, url, pedido):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client.post(url, {"pedido": pedido.pk}, format="json")

    def test_pedido_sin_programar_responde_400(self):
        for url, modelo, destino in self.CASOS:
            with self.subTest(destino=destino):
                pedido = self._pedido()

                resp = self._post(url, pedido)

                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertIn("pedido", resp.data)
                self.assertFalse(modelo.objects.filter(pedido=pedido).exists())

    def test_pedido_programado_hacia_otro_destino_responde_400(self):
        for url, modelo, destino in self.CASOS:
            with self.subTest(destino=destino):
                pedido = self._pedido()
                _programar(pedido, tuple(d for d in DESTINOS if d != destino))

                self.assertEqual(self._post(url, pedido).status_code, 400)

    def test_pedido_programado_hacia_su_destino_se_crea(self):
        for url, modelo, destino in self.CASOS:
            with self.subTest(destino=destino):
                pedido = self._pedido()
                _programar(pedido, (destino,))

                resp = self._post(url, pedido)

                self.assertIn(resp.status_code, (200, 201), resp.data)
                self.assertTrue(modelo.objects.filter(pedido=pedido).exists())


class RutaCriticaBulkTests(TestCase):
    """#283: ``GET /orden-produccion/ruta-critica/?op_id=...``."""

    URL = "/api/v1/produccion/orden-produccion/ruta-critica/"

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.otra = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="MTY")
        cls.sucursal_otra = Sucursal.objects.create(empresa=cls.otra, codigo="GDL", nombre="GDL")
        cls.usuario = Usuario.objects.create(username="u", email="u@acme.test", empresa=cls.empresa)
        cls.op1 = OrdenProduccion.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-1")
        cls.op2 = OrdenProduccion.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-2")
        cls.op_ajena = OrdenProduccion.objects.create(empresa=cls.otra, sucursal=cls.sucursal_otra, folio_op="OP-X")
        OrdenProduccionRutaCritica.objects.create(op=cls.op1, corte_recibido=True)

    def _get(self, query):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client.get(f"{self.URL}{query}")

    def test_devuelve_varias_ops_con_op_id_sin_escribir(self):
        resp = self._get(f"?op_id={self.op1.pk},{self.op2.pk}")

        self.assertEqual(resp.status_code, 200, resp.data)
        filas = {f["op_id"]: f for f in resp.data}
        self.assertEqual(set(filas), {self.op1.pk, self.op2.pk})
        self.assertTrue(filas[self.op1.pk]["corte_recibido"])
        self.assertFalse(filas[self.op2.pk]["corte_recibido"])
        self.assertFalse(OrdenProduccionRutaCritica.objects.filter(op=self.op2).exists())

    def test_omite_ops_de_otra_empresa(self):
        resp = self._get(f"?op_id={self.op1.pk},{self.op_ajena.pk}")

        self.assertEqual([f["op_id"] for f in resp.data], [self.op1.pk])

    def test_parametro_invalido_o_faltante_responde_400(self):
        for query in ("", "?op_id=", "?op_id=1,abc"):
            with self.subTest(query=query):
                self.assertEqual(self._get(query).status_code, 400)

    def test_queries_constantes(self):
        from django.test.utils import CaptureQueriesContext

        def queries(ids):
            with CaptureQueriesContext(connection) as ctx:
                self._get("?op_id=" + ",".join(str(i) for i in ids))
            return len(ctx)

        self.assertEqual(queries([self.op1.pk]), queries([self.op1.pk, self.op2.pk]))

    def test_detalle_tambien_trae_op_id(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)

        resp = client.get(f"/api/v1/produccion/orden-produccion/{self.op2.pk}/ruta-critica/")

        self.assertEqual(resp.data["op_id"], self.op2.pk)


class OrdenProduccionKpisTests(TestCase):
    """``GET /orden-produccion/kpis/`` (EC-412): OTD + atrasadas con datos
    reales; avance_produccion/eficiencia_linea van ``disponible: False`` a
    propósito -- el esquema no registra piezas terminadas ni SAM/operarios."""

    URL = "/api/v1/produccion/orden-produccion/kpis/"

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.otra = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="MTY")
        cls.sucursal_2 = Sucursal.objects.create(empresa=cls.empresa, codigo="CDMX", nombre="CDMX")
        cls.sucursal_otra = Sucursal.objects.create(empresa=cls.otra, codigo="GDL", nombre="GDL")
        # ``sucursal_default=cls.sucursal`` (MTY): con el scope por sucursal
        # (#355), un usuario normal sin sucursal asignada no vería nada: las
        # OPs de este fixture viven todas en MTY salvo ``op_vencida_sucursal_2``.
        cls.usuario = Usuario.objects.create(
            username="u", email="u@acme.test", empresa=cls.empresa, sucursal_default=cls.sucursal,
        )
        cls.sin_empresa = Usuario.objects.create(username="se", email="se@nowhere.test")
        cls.admin_empresa = Usuario.objects.create(
            username="admin", email="admin@acme.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.superuser = Usuario.objects.create(
            username="root", email="root@acme.test", is_superuser=True, is_staff=True,
        )

        hoy = timezone.localdate()
        ahora = timezone.now()

        # A tiempo: terminó antes de su fecha compromiso.
        cls.op_a_tiempo = OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-A",
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.COMPLETADO,
            fecha_entrega_estimada=hoy, fecha_fin=ahora - timedelta(days=1),
        )
        # Tardía: terminó después de su fecha compromiso.
        cls.op_tardia = OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-B",
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.COMPLETADO,
            fecha_entrega_estimada=hoy - timedelta(days=5), fecha_fin=ahora,
        )
        # Vencida y sin terminar: cuenta en ops_atrasadas, no en OTD.
        cls.op_vencida = OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-C",
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.BORDANDO,
            fecha_entrega_estimada=hoy - timedelta(days=2),
        )
        # En tiempo y sin terminar: no debe contar en ningún lado.
        OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-D",
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.PREPARACION,
            fecha_entrega_estimada=hoy + timedelta(days=5),
        )
        # De otra empresa: no debe filtrarse al aislamiento multi-tenant.
        OrdenProduccion.objects.create(
            empresa=cls.otra, sucursal=cls.sucursal_otra, folio_op="OP-X",
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.BORDANDO,
            fecha_entrega_estimada=hoy - timedelta(days=30),
        )

        # #354: Cancelada con fecha_fin capturada -- no debe contar como
        # "terminada" en OTD (antes sí, porque solo miraba fecha_fin).
        cls.op_cancelada_con_fecha_fin = OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-E",
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.CANCELADO,
            fecha_entrega_estimada=hoy - timedelta(days=1), fecha_fin=ahora,
        )
        # #354: Completada sin fecha_fin -- antes no contaba ni como
        # terminada; ahora sí entra al denominador (no al numerador).
        cls.op_completada_sin_fecha_fin = OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-F",
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.COMPLETADO,
            fecha_entrega_estimada=hoy,
        )
        # #354: Completada sin fecha compromiso -- antes contaba como tardía
        # (el exclude la incluía); ahora no es ni a tiempo ni tardía.
        cls.op_completada_sin_compromiso = OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-G",
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.COMPLETADO,
            fecha_fin=ahora,
        )

        # #355: OP vencida en una sucursal a la que ``usuario_mty`` NO tiene acceso.
        cls.op_vencida_sucursal_2 = OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal_2, folio_op="OP-H",
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.BORDANDO,
            fecha_entrega_estimada=hoy - timedelta(days=1),
        )

    def _get(self, user, query=""):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(f"{self.URL}{query}")

    def test_otd_cuenta_solo_lo_de_mi_empresa(self):
        resp = self._get(self.usuario)

        self.assertEqual(resp.status_code, 200, resp.data)
        otd = resp.data["cumplimiento_a_tiempo"]
        self.assertTrue(otd["disponible"])
        # Terminadas = Completado (4): a_tiempo, tardia, sin_fecha_fin, sin_compromiso.
        self.assertEqual(otd["ops_terminadas"], 4)
        self.assertEqual(otd["ops_a_tiempo"], 1)
        self.assertEqual(otd["pct"], 25.0)
        self.assertEqual([d["op_id"] for d in otd["drill_down_tardias"]], [self.op_tardia.pk])
        # #362: totales exactos, no inferidos por resta.
        self.assertEqual(otd["ops_tardias"], 1)
        self.assertEqual(otd["ops_sin_clasificar"], 2)  # sin_fecha_fin + sin_compromiso
        self.assertEqual(
            otd["ops_a_tiempo"] + otd["ops_tardias"] + otd["ops_sin_clasificar"], otd["ops_terminadas"],
        )

    # --- #362: total exacto de tardías/sin-clasificar ---------------------------

    def test_ops_tardias_total_exacto_con_mas_de_20(self):
        # El drill-down se corta en 20; el total no debe.
        hoy = timezone.localdate()
        ahora = timezone.now()
        for i in range(25):
            OrdenProduccion.objects.create(
                empresa=self.empresa, sucursal=self.sucursal, folio_op=f"OP-TARDE-{i}",
                estatus_op=OrdenProduccion.EstatusOrdenProduccion.COMPLETADO,
                fecha_entrega_estimada=hoy - timedelta(days=5), fecha_fin=ahora,
            )

        resp = self._get(self.usuario)

        otd = resp.data["cumplimiento_a_tiempo"]
        self.assertEqual(otd["ops_tardias"], 26)  # 25 nuevas + op_tardia del fixture
        self.assertEqual(len(otd["drill_down_tardias"]), 20)

    # --- #354: "terminada" es por estatus, no por fecha_fin --------------------

    def test_cancelada_con_fecha_fin_no_cuenta_como_terminada(self):
        resp = self._get(self.usuario)

        otd = resp.data["cumplimiento_a_tiempo"]
        atrasadas = resp.data["ops_atrasadas"]
        tardias_ids = [d["op_id"] for d in otd["drill_down_tardias"]]
        atrasadas_ids = [d["op_id"] for d in atrasadas["drill_down"]]
        # Cancelada no debe aparecer en ninguna de las dos tarjetas.
        self.assertNotIn(self.op_cancelada_con_fecha_fin.pk, tardias_ids)
        self.assertNotIn(self.op_cancelada_con_fecha_fin.pk, atrasadas_ids)

    def test_completada_sin_fecha_fin_cuenta_en_total_no_en_a_tiempo(self):
        resp = self._get(self.usuario)

        otd = resp.data["cumplimiento_a_tiempo"]
        tardias_ids = [d["op_id"] for d in otd["drill_down_tardias"]]
        # Entra al denominador (ya se verificó arriba: ops_terminadas=4) pero
        # no puede ser "a tiempo" ni "tardía" sin fecha_fin.
        self.assertNotIn(self.op_completada_sin_fecha_fin.pk, tardias_ids)

    def test_completada_sin_compromiso_no_es_tardia(self):
        resp = self._get(self.usuario)

        tardias_ids = [d["op_id"] for d in resp.data["cumplimiento_a_tiempo"]["drill_down_tardias"]]
        self.assertNotIn(self.op_completada_sin_compromiso.pk, tardias_ids)

    # --- #355: alcance por sucursal ---------------------------------------------

    def test_usuario_limitado_a_su_sucursal_no_ve_otra(self):
        resp = self._get(self.usuario)  # sucursal_default = MTY

        atrasadas_ids = [d["op_id"] for d in resp.data["ops_atrasadas"]["drill_down"]]
        self.assertIn(self.op_vencida.pk, atrasadas_ids)  # MTY: sí
        self.assertNotIn(self.op_vencida_sucursal_2.pk, atrasadas_ids)  # CDMX: no

    def test_admin_empresa_ve_todas_las_sucursales(self):
        resp = self._get(self.admin_empresa)

        atrasadas_ids = [d["op_id"] for d in resp.data["ops_atrasadas"]["drill_down"]]
        self.assertIn(self.op_vencida.pk, atrasadas_ids)
        self.assertIn(self.op_vencida_sucursal_2.pk, atrasadas_ids)

    def test_superuser_sin_empresa_no_ve_nada(self):
        # #361: alcance = SIEMPRE la empresa activa, ni el superusuario se
        # salva -- igual que list/detail de OP, que tampoco le da vista
        # global. Sin empresa no hay "mi empresa" que mostrar.
        resp = self._get(self.superuser)

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["ops_atrasadas"]["disponible"])

    def test_superuser_con_empresa_solo_ve_su_empresa_pero_todas_sus_sucursales(self):
        # #361: con empresa asignada, el superusuario queda acotado a ESA
        # empresa (como list/detail) pero sigue viendo todas sus sucursales
        # (como admin_empresa) -- nunca la de "otra" (globex).
        self.superuser.empresa = self.empresa
        self.superuser.save(update_fields=["empresa"])

        resp = self._get(self.superuser)

        atrasadas = resp.data["ops_atrasadas"]
        self.assertEqual(atrasadas["total"], 2)  # op_vencida (MTY) + op_vencida_sucursal_2 (CDMX)
        atrasadas_ids = [d["op_id"] for d in atrasadas["drill_down"]]
        self.assertIn(self.op_vencida.pk, atrasadas_ids)
        self.assertIn(self.op_vencida_sucursal_2.pk, atrasadas_ids)

    # --- #356: meta_otd no finito o fuera de rango ------------------------------

    def test_meta_otd_no_finito_es_400(self):
        for valor in ("nan", "inf", "-inf", "1e999"):
            with self.subTest(valor=valor):
                resp = self._get(self.usuario, f"?meta_otd={valor}")
                self.assertEqual(resp.status_code, 400)

    def test_meta_otd_fuera_de_rango_es_400(self):
        for valor in ("-1", "101"):
            with self.subTest(valor=valor):
                resp = self._get(self.usuario, f"?meta_otd={valor}")
                self.assertEqual(resp.status_code, 400)

    # --- #358: fecha_fin en el drill-down usa hora local, no UTC ---------------

    def test_fecha_fin_en_drill_down_usa_hora_local(self):
        resp = self._get(self.usuario)

        payload = json.loads(resp.content)
        tardia = next(
            d for d in payload["cumplimiento_a_tiempo"]["drill_down_tardias"]
            if d["op_id"] == self.op_tardia.pk
        )
        # DRF renderiza "+00:00" como "Z"; con hora local (UTC-6) nunca pasa.
        self.assertFalse(tardia["fecha_fin"].endswith("Z"))

    def test_ops_atrasadas_solo_cuenta_vencidas_abiertas(self):
        resp = self._get(self.usuario)

        atrasadas = resp.data["ops_atrasadas"]
        self.assertTrue(atrasadas["disponible"])
        self.assertEqual(atrasadas["total"], 1)
        self.assertEqual(atrasadas["drill_down"][0]["op_id"], self.op_vencida.pk)
        self.assertEqual(atrasadas["drill_down"][0]["dias_vencida"], 2)

    def test_kpis_sin_datos_declaran_no_disponible(self):
        resp = self._get(self.usuario)

        self.assertFalse(resp.data["avance_produccion"]["disponible"])
        self.assertFalse(resp.data["eficiencia_linea"]["disponible"])

    def test_meta_otd_cambia_semaforo(self):
        # pct real = 25.0 (ver test_otd_cuenta_solo_lo_de_mi_empresa).
        verde = self._get(self.usuario, "?meta_otd=20")
        rojo = self._get(self.usuario, "?meta_otd=95")

        self.assertEqual(verde.data["cumplimiento_a_tiempo"]["semaforo"], "verde")
        self.assertEqual(rojo.data["cumplimiento_a_tiempo"]["semaforo"], "rojo")

    def test_meta_otd_invalida_es_400(self):
        resp = self._get(self.usuario, "?meta_otd=abc")
        self.assertEqual(resp.status_code, 400)

    def test_usuario_sin_empresa_no_ve_nada(self):
        resp = self._get(self.sin_empresa)

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["cumplimiento_a_tiempo"]["disponible"])


class _BomTenantBase:
    """Fixture de dos empresas para el aislamiento multi-tenant de BOM y OP.

    ``usuario`` pertenece a ``empresa``; todo lo ``*_ajen*`` es de
    ``otra_empresa`` y nunca debe poder referenciarse desde un request suyo.
    """

    URL_BOM = "/api/v1/produccion/lista-material/"
    URL_BOM_DETALLE = "/api/v1/produccion/bom-detalle/"

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme-ti", razon_social="acme-ti SA")
        cls.otra_empresa = Empresa.objects.create(codigo="otra-ti", razon_social="otra-ti SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="Matriz")
        cls.otra_sucursal = Sucursal.objects.create(empresa=cls.otra_empresa, codigo="GDL", nombre="GDL")
        cls.usuario = Usuario.objects.create(
            username="ti-user", email="ti-user@acme-ti.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal,
        )
        cls.unidad = UnidadMedida.objects.create(clave="PZA", nombre="Pieza")
        cls.color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000")
        cls.talla = Talla.objects.create(nombre="M")

        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.variante = ProductoVariante.objects.create(
            producto=cls.producto, empresa=cls.empresa, color=cls.color, sku="TI-PLA-NEG", precio_base="100",
        )
        cls.variante_2 = ProductoVariante.objects.create(
            producto=cls.producto, empresa=cls.empresa, color=cls.color, talla=cls.talla,
            sku="TI-PLA-NEG-M", precio_base="100",
        )
        cls.componente = Producto.objects.create(empresa=cls.empresa, nombre="Tela")
        cls.componente_2 = Producto.objects.create(empresa=cls.empresa, nombre="Hilo")
        cls.variante_produccion = VarianteProductoProduccion.objects.create(
            empresa=cls.empresa, nombre="Muestra propia", sku="MP-TI-1",
        )

        cls.producto_ajeno = Producto.objects.create(empresa=cls.otra_empresa, nombre="Playera ajena")
        cls.variante_ajena = ProductoVariante.objects.create(
            producto=cls.producto_ajeno, empresa=cls.otra_empresa, color=cls.color,
            sku="TI-AJ-NEG", precio_base="100",
        )
        cls.componente_ajeno = Producto.objects.create(empresa=cls.otra_empresa, nombre="Tela ajena")
        cls.variante_produccion_ajena = VarianteProductoProduccion.objects.create(
            empresa=cls.otra_empresa, nombre="Muestra ajena", sku="MP-TI-AJ",
        )

    def _client(self, usuario=None):
        client = APIClient()
        client.force_authenticate(user=usuario or self.usuario)
        return client

    def _insumo(self, **extra):
        return {
            "componente": self.componente.pk, "cantidad": "2.50", "unidad": self.unidad.pk,
            "desperdicio": "0.00", "obligatorio": True, **extra,
        }

    def _body_bom(self, **extra):
        return {
            "producto_variante": self.variante.pk, "version": 1,
            "materia_prima_detalle": [self._insumo()], **extra,
        }

    def _bom(self, empresa=None, variante=None, componente=None):
        bom = ListaMaterialBom.objects.create(
            empresa=empresa or self.empresa, producto_variante=variante or self.variante,
        )
        BomDetalle.objects.create(
            bom=bom, componente=componente or self.componente, cantidad=1, unidad=self.unidad,
        )
        return bom

    def _bom_ajeno(self):
        return self._bom(
            empresa=self.otra_empresa, variante=self.variante_ajena, componente=self.componente_ajeno
        )


class ListaMaterialEmpresaTenantTests(_BomTenantBase, TestCase):
    """``/lista-material/``: ``empresa`` la fija el servidor (``user.empresa``,
    superusuario incluido) y ``producto_variante``/``variante_produccion`` deben
    ser de esa empresa, en create y en update."""

    CAMPOS_BOM = {
        "bom_id", "empresa", "producto_variante", "variante_produccion", "version",
        "activo", "observaciones", "materia_prima_detalle",
    }

    def test_create_misma_empresa_conserva_el_shape(self):
        resp = self._client().post(self.URL_BOM, self._body_bom(empresa=self.empresa.pk), format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(set(resp.data), self.CAMPOS_BOM)
        self.assertEqual(resp.data["empresa"], self.empresa.pk)
        self.assertEqual(resp.data["producto_variante"], self.variante.pk)
        self.assertEqual(len(resp.data["materia_prima_detalle"]), 1)

    def test_create_sin_empresa_en_el_body_usa_la_del_usuario(self):
        resp = self._client().post(self.URL_BOM, self._body_bom(), format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(ListaMaterialBom.objects.get(pk=resp.data["bom_id"]).empresa_id, self.empresa.pk)

    def test_create_ignora_la_empresa_ajena_del_body(self):
        resp = self._client().post(
            self.URL_BOM, self._body_bom(empresa=self.otra_empresa.pk), format="json"
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["empresa"], self.empresa.pk)
        self.assertFalse(ListaMaterialBom.objects.filter(empresa=self.otra_empresa).exists())

    def test_create_rechaza_producto_variante_de_otra_empresa(self):
        resp = self._client().post(
            self.URL_BOM, self._body_bom(producto_variante=self.variante_ajena.pk), format="json"
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("producto_variante", resp.data)
        self.assertFalse(ListaMaterialBom.objects.exists())

    def test_create_rechaza_variante_produccion_de_otra_empresa(self):
        body = self._body_bom(producto_variante=None, variante_produccion=self.variante_produccion_ajena.pk)
        resp = self._client().post(self.URL_BOM, body, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("variante_produccion", resp.data)
        self.assertFalse(ListaMaterialBom.objects.exists())

    def test_create_acepta_variante_produccion_propia(self):
        body = self._body_bom(producto_variante=None, variante_produccion=self.variante_produccion.pk)
        resp = self._client().post(self.URL_BOM, body, format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["variante_produccion"], self.variante_produccion.pk)

    def test_fk_ajena_e_inexistente_responden_igual(self):
        ajena = self._client().post(
            self.URL_BOM, self._body_bom(producto_variante=self.variante_ajena.pk), format="json"
        )
        inexistente_pk = self.variante_ajena.pk + 999
        inexistente = self._client().post(
            self.URL_BOM, self._body_bom(producto_variante=inexistente_pk), format="json"
        )

        self.assertEqual(ajena.status_code, 400)
        self.assertEqual(
            str(ajena.data["producto_variante"][0]).replace(str(self.variante_ajena.pk), "X"),
            str(inexistente.data["producto_variante"][0]).replace(str(inexistente_pk), "X"),
        )

    def test_patch_no_reasigna_la_empresa(self):
        bom = self._bom()
        resp = self._client().patch(
            f"{self.URL_BOM}{bom.pk}/", {"empresa": self.otra_empresa.pk, "version": 2}, format="json"
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(set(resp.data), self.CAMPOS_BOM)
        bom.refresh_from_db()
        self.assertEqual((bom.empresa_id, bom.version), (self.empresa.pk, 2))

    def test_put_no_reasigna_la_empresa(self):
        bom = self._bom()
        resp = self._client().put(
            f"{self.URL_BOM}{bom.pk}/", self._body_bom(empresa=self.otra_empresa.pk, version=3), format="json"
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        bom.refresh_from_db()
        self.assertEqual((bom.empresa_id, bom.version), (self.empresa.pk, 3))

    def test_patch_rechaza_producto_variante_de_otra_empresa(self):
        bom = self._bom()
        resp = self._client().patch(
            f"{self.URL_BOM}{bom.pk}/", {"producto_variante": self.variante_ajena.pk}, format="json"
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("producto_variante", resp.data)
        bom.refresh_from_db()
        self.assertEqual(bom.producto_variante_id, self.variante.pk)

    def test_patch_rechaza_variante_produccion_de_otra_empresa(self):
        bom = self._bom()
        resp = self._client().patch(
            f"{self.URL_BOM}{bom.pk}/",
            {"variante_produccion": self.variante_produccion_ajena.pk}, format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("variante_produccion", resp.data)
        bom.refresh_from_db()
        self.assertIsNone(bom.variante_produccion_id)

    def test_patch_acepta_variantes_de_la_propia_empresa(self):
        bom = self._bom()
        resp = self._client().patch(
            f"{self.URL_BOM}{bom.pk}/",
            {"producto_variante": self.variante_2.pk, "variante_produccion": self.variante_produccion.pk},
            format="json",
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        bom.refresh_from_db()
        self.assertEqual(
            (bom.producto_variante_id, bom.variante_produccion_id),
            (self.variante_2.pk, self.variante_produccion.pk),
        )

    def test_bom_de_otra_empresa_responde_404_en_lectura_y_escritura(self):
        ajeno = self._bom_ajeno()
        url = f"{self.URL_BOM}{ajeno.pk}/"

        self.assertEqual(self._client().get(url).status_code, 404)
        self.assertEqual(self._client().patch(url, {"version": 9}, format="json").status_code, 404)
        self.assertEqual(self._client().delete(url).status_code, 404)
        ajeno.refresh_from_db()
        self.assertEqual(ajeno.version, 1)

    def test_bulk_solo_devuelve_bom_de_la_propia_empresa(self):
        propio = self._bom()
        self._bom_ajeno()

        resp = self._client().get(
            f"{self.URL_BOM}bulk/",
            {"producto_variante_ids": f"{self.variante.pk},{self.variante_ajena.pk}"},
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual([fila["bom_id"] for fila in resp.data], [propio.pk])

    def test_usuario_sin_empresa_no_puede_crear(self):
        sin_empresa = Usuario.objects.create(username="ti-se", email="ti-se@nowhere.test")
        resp = self._client(sin_empresa).post(self.URL_BOM, self._body_bom(), format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(ListaMaterialBom.objects.exists())

    def test_superusuario_crea_en_su_propia_empresa(self):
        root = Usuario.objects.create(
            username="ti-root", email="ti-root@acme-ti.test", empresa=self.empresa,
            is_superuser=True, is_staff=True,
        )
        resp = self._client(root).post(
            self.URL_BOM, self._body_bom(empresa=self.otra_empresa.pk), format="json"
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["empresa"], self.empresa.pk)

    def test_superusuario_no_referencia_variantes_de_otra_empresa(self):
        root = Usuario.objects.create(
            username="ti-root2", email="ti-root2@acme-ti.test", empresa=self.empresa,
            is_superuser=True, is_staff=True,
        )
        resp = self._client(root).post(
            self.URL_BOM, self._body_bom(producto_variante=self.variante_ajena.pk), format="json"
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("producto_variante", resp.data)


class BomDetalleTenantTests(_BomTenantBase, TestCase):
    """``BomDetalleSerializer``: ``componente`` y ``variante_produccion`` deben
    ser de la empresa del usuario en todos sus consumidores -- anidado en
    ``/lista-material/``, directo en ``/bom-detalle/`` y en el
    ``variante-onboarding`` de pedidos especiales."""

    CAMPOS_DETALLE = {
        "bom_detalle_id", "bom", "variante_produccion", "componente", "cantidad", "unidad",
        "desperdicio", "obligatorio", "observaciones", "activo", "componente_nombre", "unidad_clave",
    }

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = Usuario.objects.create(
            username="ti-admin", email="ti-admin@acme-ti.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente TI")
        cls.pedido = Pedido.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, cliente=cliente, moneda=moneda,
            persona_pagos="Pagos", correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
            clasificacion="B", fecha_confirmacion=timezone.now(),
        )
        cls.linea_muestra = PedidoDetalle.objects.create(
            pedido=cls.pedido, producto_nombre_externo="Muestra especial", color=cls.color,
        )
        PedidoDetalleTalla.objects.create(pedido_detalle=cls.linea_muestra, talla=cls.talla, cantidad=5)
        cls.url_onboarding = (
            f"/api/v1/produccion/pedidos-especiales/{cls.pedido.pk}/variante-onboarding/"
        )

    # --- anidado en /lista-material/ ---

    def test_bom_create_rechaza_componente_de_otra_empresa(self):
        body = self._body_bom(materia_prima_detalle=[self._insumo(componente=self.componente_ajeno.pk)])
        resp = self._client().post(self.URL_BOM, body, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("componente", resp.data["materia_prima_detalle"][0])
        self.assertFalse(ListaMaterialBom.objects.exists())
        self.assertFalse(BomDetalle.objects.exists())

    def test_bom_create_rechaza_variante_produccion_ajena_en_el_insumo(self):
        body = self._body_bom(
            materia_prima_detalle=[self._insumo(variante_produccion=self.variante_produccion_ajena.pk)]
        )
        resp = self._client().post(self.URL_BOM, body, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("variante_produccion", resp.data["materia_prima_detalle"][0])
        self.assertFalse(BomDetalle.objects.exists())

    def test_bom_create_acepta_insumos_de_la_propia_empresa(self):
        body = self._body_bom(
            materia_prima_detalle=[
                self._insumo(variante_produccion=self.variante_produccion.pk),
                self._insumo(componente=self.componente_2.pk),
            ]
        )
        resp = self._client().post(self.URL_BOM, body, format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        detalles = resp.data["materia_prima_detalle"]
        self.assertEqual(set(detalles[0]), self.CAMPOS_DETALLE)
        self.assertEqual(
            {d["componente"] for d in detalles}, {self.componente.pk, self.componente_2.pk}
        )

    def test_bom_create_sobre_bom_existente_rechaza_componente_ajeno(self):
        # Segundo POST de la misma variante: rama de fusión de ``create``.
        bom = self._bom()
        body = self._body_bom(materia_prima_detalle=[self._insumo(componente=self.componente_ajeno.pk)])
        resp = self._client().post(self.URL_BOM, body, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            list(bom.materia_prima_detalle.values_list("componente_id", flat=True)), [self.componente.pk]
        )

    def test_bom_update_rechaza_componente_de_otra_empresa(self):
        bom = self._bom()
        url = f"{self.URL_BOM}{bom.pk}/"
        ajeno = [self._insumo(componente=self.componente_ajeno.pk)]

        patch = self._client().patch(url, {"materia_prima_detalle": ajeno}, format="json")
        put = self._client().put(url, self._body_bom(materia_prima_detalle=ajeno), format="json")

        for resp in (patch, put):
            self.assertEqual(resp.status_code, 400)
            self.assertIn("componente", resp.data["materia_prima_detalle"][0])
        self.assertEqual(
            list(bom.materia_prima_detalle.values_list("componente_id", flat=True)), [self.componente.pk]
        )

    def test_bom_update_acepta_componente_de_la_propia_empresa(self):
        bom = self._bom()
        resp = self._client().patch(
            f"{self.URL_BOM}{bom.pk}/",
            {"materia_prima_detalle": [self._insumo(componente=self.componente_2.pk)]}, format="json",
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(
            list(bom.materia_prima_detalle.values_list("componente_id", flat=True)), [self.componente_2.pk]
        )

    # --- /bom-detalle/ ---

    def test_bom_detalle_create_rechaza_componente_de_otra_empresa(self):
        resp = self._client().post(
            self.URL_BOM_DETALLE, self._insumo(componente=self.componente_ajeno.pk), format="json"
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("componente", resp.data)
        self.assertFalse(BomDetalle.objects.exists())

    def test_bom_detalle_update_rechaza_componente_de_otra_empresa(self):
        detalle = self._bom().materia_prima_detalle.get()
        url = f"{self.URL_BOM_DETALLE}{detalle.pk}/"

        patch = self._client().patch(url, {"componente": self.componente_ajeno.pk}, format="json")
        put = self._client().put(url, self._insumo(componente=self.componente_ajeno.pk), format="json")

        for resp in (patch, put):
            self.assertEqual(resp.status_code, 400)
            self.assertIn("componente", resp.data)
        detalle.refresh_from_db()
        self.assertEqual(detalle.componente_id, self.componente.pk)

    def test_bom_detalle_update_rechaza_variante_produccion_de_otra_empresa(self):
        detalle = self._bom().materia_prima_detalle.get()
        resp = self._client().patch(
            f"{self.URL_BOM_DETALLE}{detalle.pk}/",
            {"variante_produccion": self.variante_produccion_ajena.pk}, format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("variante_produccion", resp.data)
        detalle.refresh_from_db()
        self.assertIsNone(detalle.variante_produccion_id)

    def test_bom_detalle_update_acepta_fks_de_la_propia_empresa(self):
        detalle = self._bom().materia_prima_detalle.get()
        url = f"{self.URL_BOM_DETALLE}{detalle.pk}/"

        patch = self._client().patch(
            url,
            {"componente": self.componente_2.pk, "variante_produccion": self.variante_produccion.pk},
            format="json",
        )
        self.assertEqual(patch.status_code, 200, patch.data)
        self.assertEqual(set(patch.data), self.CAMPOS_DETALLE)
        detalle.refresh_from_db()
        self.assertEqual(
            (detalle.componente_id, detalle.variante_produccion_id),
            (self.componente_2.pk, self.variante_produccion.pk),
        )

        put = self._client().put(url, self._insumo(cantidad="4.00"), format="json")
        self.assertEqual(put.status_code, 200, put.data)
        detalle.refresh_from_db()
        self.assertEqual((detalle.componente_id, detalle.cantidad), (self.componente.pk, Decimal("4.00")))

    def test_bom_detalle_de_otra_empresa_responde_404(self):
        detalle = self._bom_ajeno().materia_prima_detalle.get()
        url = f"{self.URL_BOM_DETALLE}{detalle.pk}/"

        self.assertEqual(self._client().get(url).status_code, 404)
        self.assertEqual(
            self._client().patch(url, {"componente": self.componente.pk}, format="json").status_code, 404
        )
        self.assertEqual(self._client().delete(url).status_code, 404)
        detalle.refresh_from_db()
        self.assertEqual(detalle.componente_id, self.componente_ajeno.pk)
        self.assertEqual(self._client().get(self.URL_BOM_DETALLE).data, [])

    def test_superusuario_tampoco_referencia_componentes_de_otra_empresa(self):
        root = Usuario.objects.create(
            username="ti-root3", email="ti-root3@acme-ti.test", empresa=self.empresa,
            is_superuser=True, is_staff=True,
        )
        detalle = self._bom().materia_prima_detalle.get()
        resp = self._client(root).patch(
            f"{self.URL_BOM_DETALLE}{detalle.pk}/", {"componente": self.componente_ajeno.pk}, format="json"
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("componente", resp.data)

    # --- variante-onboarding de pedidos especiales ---

    def _body_onboarding(self, **insumo):
        return {
            "pedido_detalle_id": self.linea_muestra.pk,
            "materia_prima_detalle": [self._insumo(**insumo)],
        }

    def test_variante_onboarding_rechaza_componente_de_otra_empresa(self):
        resp = self._client(self.admin).post(
            self.url_onboarding, self._body_onboarding(componente=self.componente_ajeno.pk), format="json"
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("componente", resp.data["materia_prima_detalle"][0])
        self.assertFalse(VarianteProductoProduccion.objects.filter(pedido_detalle=self.linea_muestra).exists())
        self.assertFalse(BomDetalle.objects.exists())

    def test_variante_onboarding_rechaza_variante_produccion_ajena_en_el_insumo(self):
        resp = self._client(self.admin).post(
            self.url_onboarding,
            self._body_onboarding(variante_produccion=self.variante_produccion_ajena.pk), format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("variante_produccion", resp.data["materia_prima_detalle"][0])
        self.assertFalse(VarianteProductoProduccion.objects.filter(pedido_detalle=self.linea_muestra).exists())

    def test_variante_onboarding_con_componente_propio_conserva_el_shape(self):
        resp = self._client(self.admin).post(self.url_onboarding, self._body_onboarding(), format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(
            set(resp.data[0]), {"id", "sku", "nombre", "talla_nombre", "aplica_catalogo", "pedido_detalle"}
        )
        variante = VarianteProductoProduccion.objects.get(pk=resp.data[0]["id"])
        self.assertEqual(variante.empresa_id, self.empresa.pk)
        detalle = BomDetalle.objects.get(bom__variante_produccion=variante)
        self.assertEqual(
            (detalle.bom.empresa_id, detalle.componente_id, detalle.variante_produccion_id),
            (self.empresa.pk, self.componente.pk, variante.pk),
        )


class OrdenProduccionDetalleTenantTests(_BomTenantBase, TestCase):
    """Renglones de ``/orden-produccion/``: ``pedido_detalle`` y
    ``producto_variante_id`` deben ser de la empresa del usuario y, si la OP
    tiene ``pedido``, el ``pedido_detalle`` debe ser de ESE pedido. Sin
    ``pedido`` en la OP sólo se exige la empresa."""

    URL = "/api/v1/produccion/orden-produccion/"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        SerieFolio.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal,
            tipo_documento="ORDEN_PRODUCCION", serie="OP",
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente TI")
        cls.cliente_ajeno = Cliente.objects.create(empresa=cls.otra_empresa, nombre="Cliente ajeno")

        cls.bom = ListaMaterialBom.objects.create(empresa=cls.empresa, producto_variante=cls.variante)
        BomDetalle.objects.create(bom=cls.bom, componente=cls.componente, cantidad=1, unidad=cls.unidad)
        # BOM "legado" de la propia empresa colgado de una variante AJENA: es el
        # caso en que el lookup de BOM por empresa NO frena la variante ajena.
        bom_legado = ListaMaterialBom.objects.create(empresa=cls.empresa, producto_variante=cls.variante_ajena)
        BomDetalle.objects.create(bom=bom_legado, componente=cls.componente, cantidad=1, unidad=cls.unidad)
        almacen = Almacen.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, codigo="ALM-TI", nombre="Principal",
        )
        cls.existencia = Existencia.objects.create(almacen=almacen, producto=cls.componente, cantidad=100)

        cls.pedido, cls.linea = cls._pedido_muestra(cls.empresa, cls.sucursal, cls.cliente)
        cls.otro_pedido, cls.linea_otro_pedido = cls._pedido_muestra(cls.empresa, cls.sucursal, cls.cliente)
        cls.pedido_ajeno, cls.linea_ajena = cls._pedido_muestra(
            cls.otra_empresa, cls.otra_sucursal, cls.cliente_ajeno
        )

    @classmethod
    def _pedido_muestra(cls, empresa, sucursal, cliente):
        pedido = Pedido.objects.create(
            empresa=empresa, sucursal=sucursal, cliente=cliente, moneda=cls.moneda,
            persona_pagos="Pagos", correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
            clasificacion="B", fecha_confirmacion=timezone.now(),
        )
        return pedido, PedidoDetalle.objects.create(pedido=pedido, producto_nombre_externo="Muestra")

    def _body(self, pedido=None, **linea):
        body = {
            "orden_produccion_detalle": [
                {
                    "producto_variante_id": self.variante.pk, "cantidad": "3.00",
                    "unidad": self.unidad.pk, **linea,
                }
            ],
        }
        if pedido is not None:
            body["pedido"] = pedido.pk
        return body

    def _error_linea(self, resp, campo):
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn(campo, resp.data["orden_produccion_detalle"][0])
        return resp.data["orden_produccion_detalle"][0][campo]

    def _sin_efectos(self):
        self.assertFalse(OrdenProduccion.objects.exists())
        self.existencia.refresh_from_db()
        self.assertEqual(self.existencia.cantidad, 100)

    def _op(self, pedido=None):
        return OrdenProduccion.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=pedido, folio_op=f"OP-TI-{pedido}",
        )

    # --- create ---

    def test_create_sin_pedido_detalle_conserva_el_shape(self):
        resp = self._client().post(self.URL, self._body(), format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(
            set(resp.data),
            {"msg", "op_id", "folio_op", "consumo_produccion_id", "movimiento_inventario_id", "movimiento_id"},
        )

    def test_create_con_pedido_y_su_propio_renglon(self):
        resp = self._client().post(
            self.URL, self._body(self.pedido, pedido_detalle=self.linea.pk), format="json"
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        detalle = OrdenProduccionDetalle.objects.get(op_id=resp.data["op_id"])
        self.assertEqual(detalle.pedido_detalle_id, self.linea.pk)

    def test_create_sin_pedido_acepta_renglon_de_la_propia_empresa(self):
        # OP sin ``pedido`` + línea con ``pedido_detalle``: sólo se exige la
        # empresa; rechazar o no esa combinación es decisión de negocio pendiente.
        resp = self._client().post(self.URL, self._body(pedido_detalle=self.linea.pk), format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        detalle = OrdenProduccionDetalle.objects.get(op_id=resp.data["op_id"])
        self.assertEqual(detalle.pedido_detalle_id, self.linea.pk)

    def test_create_sin_pedido_rechaza_renglon_de_otra_empresa(self):
        resp = self._client().post(self.URL, self._body(pedido_detalle=self.linea_ajena.pk), format="json")

        self._error_linea(resp, "pedido_detalle")
        self._sin_efectos()

    def test_create_con_pedido_rechaza_renglon_de_otra_empresa(self):
        resp = self._client().post(
            self.URL, self._body(self.pedido, pedido_detalle=self.linea_ajena.pk), format="json"
        )

        self._error_linea(resp, "pedido_detalle")
        self._sin_efectos()

    def test_create_con_pedido_rechaza_renglon_de_otro_pedido(self):
        resp = self._client().post(
            self.URL, self._body(self.pedido, pedido_detalle=self.linea_otro_pedido.pk), format="json"
        )

        self._error_linea(resp, "pedido_detalle")
        self._sin_efectos()

    def test_pedido_detalle_ajeno_e_inexistente_responden_igual(self):
        inexistente_pk = self.linea_ajena.pk + 999
        ajeno = self._client().post(self.URL, self._body(pedido_detalle=self.linea_ajena.pk), format="json")
        inexistente = self._client().post(self.URL, self._body(pedido_detalle=inexistente_pk), format="json")

        self.assertEqual(
            str(self._error_linea(ajeno, "pedido_detalle")[0]).replace(str(self.linea_ajena.pk), "X"),
            str(self._error_linea(inexistente, "pedido_detalle")[0]).replace(str(inexistente_pk), "X"),
        )

    def test_create_rechaza_producto_variante_de_otra_empresa(self):
        # Pasa el lookup de BOM (hay un BOM propio colgado de la variante
        # ajena): la validación explícita es la única barrera.
        resp = self._client().post(
            self.URL, self._body(producto_variante_id=self.variante_ajena.pk), format="json"
        )

        self._error_linea(resp, "producto_variante_id")
        self._sin_efectos()

    def test_onboarding_post_aplica_las_mismas_reglas(self):
        url = f"{self.URL}onboarding/"
        ajeno = self._client().post(url, self._body(pedido_detalle=self.linea_ajena.pk), format="json")
        variante = self._client().post(
            url, self._body(producto_variante_id=self.variante_ajena.pk), format="json"
        )

        self._error_linea(ajeno, "pedido_detalle")
        self._error_linea(variante, "producto_variante_id")
        self._sin_efectos()

    def test_superusuario_tampoco_referencia_fks_de_otra_empresa(self):
        root = Usuario.objects.create(
            username="ti-root4", email="ti-root4@acme-ti.test", empresa=self.empresa,
            sucursal_default=self.sucursal, is_superuser=True, is_staff=True,
        )
        ajeno = self._client(root).post(self.URL, self._body(pedido_detalle=self.linea_ajena.pk), format="json")
        variante = self._client(root).post(
            self.URL, self._body(producto_variante_id=self.variante_ajena.pk), format="json"
        )

        self._error_linea(ajeno, "pedido_detalle")
        self._error_linea(variante, "producto_variante_id")
        self._sin_efectos()

    # --- update ---

    def test_patch_rechaza_renglon_de_otra_empresa(self):
        op = self._op()
        resp = self._client().patch(
            f"{self.URL}{op.pk}/", self._body(pedido_detalle=self.linea_ajena.pk), format="json"
        )

        self._error_linea(resp, "pedido_detalle")
        self.assertFalse(OrdenProduccionDetalle.objects.filter(op=op).exists())

    def test_patch_rechaza_producto_variante_de_otra_empresa(self):
        op = self._op()
        resp = self._client().patch(
            f"{self.URL}{op.pk}/", self._body(producto_variante_id=self.variante_ajena.pk), format="json"
        )

        self._error_linea(resp, "producto_variante_id")
        self.assertFalse(OrdenProduccionDetalle.objects.filter(op=op).exists())

    def test_put_rechaza_renglon_de_otra_empresa(self):
        op = self._op()
        resp = self._client().put(
            f"{self.URL}{op.pk}/", self._body(pedido_detalle=self.linea_ajena.pk), format="json"
        )

        self._error_linea(resp, "pedido_detalle")
        self.assertFalse(OrdenProduccionDetalle.objects.filter(op=op).exists())

    def test_patch_rechaza_renglon_de_otro_pedido_usando_el_pedido_de_la_op(self):
        # El ``pedido`` no viene en el PATCH: se compara contra el de la instancia.
        op = self._op(self.pedido)
        resp = self._client().patch(
            f"{self.URL}{op.pk}/", self._body(pedido_detalle=self.linea_otro_pedido.pk), format="json"
        )

        self._error_linea(resp, "pedido_detalle")
        self.assertFalse(OrdenProduccionDetalle.objects.filter(op=op).exists())

    def test_patch_de_encabezado_en_la_propia_empresa_sigue_funcionando(self):
        op = self._op(self.pedido)
        resp = self._client().patch(f"{self.URL}{op.pk}/", {"prioridad": 4}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual((resp.data["prioridad"], resp.data["pedido"]), (4, self.pedido.pk))
        self.assertEqual(resp.data["empresa"], self.empresa.pk)

    def test_op_de_otra_empresa_responde_404(self):
        ajena = OrdenProduccion.objects.create(
            empresa=self.otra_empresa, sucursal=self.otra_sucursal, folio_op="OP-TI-AJENA",
        )
        url = f"{self.URL}{ajena.pk}/"

        self.assertEqual(self._client().get(url).status_code, 404)
        self.assertEqual(self._client().patch(url, {"prioridad": 9}, format="json").status_code, 404)
        self.assertEqual(self._client().delete(url).status_code, 404)
        ajena.refresh_from_db()
        self.assertEqual(ajena.prioridad, 1)

    # --- cambio de ``pedido`` con renglones ya guardados ---

    def _op_con_renglon(self, pedido=None, pedido_detalle=None):
        op = self._op(pedido)
        OrdenProduccionDetalle.objects.create(
            op=op, bom=self.bom, cantidad=1, unidad=self.unidad,
            producto_variante=self.variante, pedido_detalle=pedido_detalle,
        )
        return op

    def test_patch_de_pedido_rechaza_si_un_renglon_guardado_es_de_otro_pedido(self):
        # Evasión en dos pasos: OP sin pedido con renglón de ``pedido`` y luego
        # PATCH que la liga a ``otro_pedido``.
        op = self._op_con_renglon(pedido_detalle=self.linea)
        resp = self._client().patch(f"{self.URL}{op.pk}/", {"pedido": self.otro_pedido.pk}, format="json")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("pedido", resp.data)
        op.refresh_from_db()
        self.assertIsNone(op.pedido_id)

    def test_patch_que_cambia_de_pedido_rechaza_renglones_del_pedido_anterior(self):
        op = self._op_con_renglon(pedido=self.pedido, pedido_detalle=self.linea)
        resp = self._client().patch(f"{self.URL}{op.pk}/", {"pedido": self.otro_pedido.pk}, format="json")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("pedido", resp.data)
        op.refresh_from_db()
        self.assertEqual(op.pedido_id, self.pedido.pk)

    def test_put_de_pedido_rechaza_si_un_renglon_guardado_es_de_otro_pedido(self):
        op = self._op_con_renglon(pedido_detalle=self.linea)
        resp = self._client().put(f"{self.URL}{op.pk}/", self._body(self.otro_pedido), format="json")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("pedido", resp.data)
        op.refresh_from_db()
        self.assertIsNone(op.pedido_id)

    def test_patch_de_pedido_valido_con_renglones_de_ese_pedido(self):
        op = self._op_con_renglon(pedido_detalle=self.linea)
        resp = self._client().patch(f"{self.URL}{op.pk}/", {"pedido": self.pedido.pk}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        op.refresh_from_db()
        self.assertEqual(op.pedido_id, self.pedido.pk)

    def test_patch_de_pedido_ignora_renglones_sin_pedido_detalle(self):
        op = self._op_con_renglon()
        resp = self._client().patch(f"{self.URL}{op.pk}/", {"pedido": self.otro_pedido.pk}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)

    def test_patch_que_quita_el_pedido_no_exige_nada_a_los_renglones(self):
        op = self._op_con_renglon(pedido=self.pedido, pedido_detalle=self.linea)
        resp = self._client().patch(f"{self.URL}{op.pk}/", {"pedido": None}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        op.refresh_from_db()
        self.assertIsNone(op.pedido_id)

    def test_patch_sin_cambiar_el_pedido_no_revisa_renglones_guardados(self):
        # Sólo se revisa al fijar o cambiar ``pedido``: un PATCH de otro campo
        # no se bloquea por renglones heredados.
        op = self._op_con_renglon(pedido=self.pedido, pedido_detalle=self.linea_otro_pedido)
        resp = self._client().patch(
            f"{self.URL}{op.pk}/", {"pedido": self.pedido.pk, "prioridad": 3}, format="json"
        )

        self.assertEqual(resp.status_code, 200, resp.data)

    # --- ruta_produccion (FK de encabezado con empresa propia) ---

    def test_ruta_produccion_de_otra_empresa_se_rechaza_en_create_y_patch(self):
        ruta_ajena = RutaProduccion.objects.create(empresa=self.otra_empresa, producto=self.producto_ajeno)
        op = self._op()

        create = self._client().post(
            self.URL, {**self._body(), "ruta_produccion": ruta_ajena.pk}, format="json"
        )
        patch = self._client().patch(
            f"{self.URL}{op.pk}/", {"ruta_produccion": ruta_ajena.pk}, format="json"
        )

        for resp in (create, patch):
            self.assertEqual(resp.status_code, 400, resp.data)
            self.assertIn("ruta_produccion", resp.data)
        op.refresh_from_db()
        self.assertIsNone(op.ruta_produccion_id)
        self.assertEqual(OrdenProduccion.objects.count(), 1)

    def test_ruta_produccion_de_la_propia_empresa_se_acepta(self):
        ruta = RutaProduccion.objects.create(empresa=self.empresa, producto=self.producto)
        op = self._op()

        resp = self._client().patch(f"{self.URL}{op.pk}/", {"ruta_produccion": ruta.pk}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        op.refresh_from_db()
        self.assertEqual(op.ruta_produccion_id, ruta.pk)


class _RegistrosDeOPTenantBase(_BomTenantBase):
    """Fixture para los recursos que cuelgan de una OP (``consumo`` y
    ``producto-terminado-entradas``): una OP, un almacén y una ubicación por
    empresa, más una existencia propia para vigilar efectos secundarios."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.op = OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-TI-PROPIA",
        )
        cls.op_ajena = OrdenProduccion.objects.create(
            empresa=cls.otra_empresa, sucursal=cls.otra_sucursal, folio_op="OP-TI-AJENA",
        )
        cls.almacen = Almacen.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, codigo="ALM-TI", nombre="Principal",
        )
        cls.ubicacion = Ubicacion.objects.create(almacen=cls.almacen, pasillo="1")
        cls.almacen_ajeno = Almacen.objects.create(
            empresa=cls.otra_empresa, sucursal=cls.otra_sucursal, codigo="ALM-AJ", nombre="Ajeno",
        )
        cls.ubicacion_ajena = Ubicacion.objects.create(almacen=cls.almacen_ajeno, pasillo="9")
        cls.existencia = Existencia.objects.create(
            almacen=cls.almacen, producto=cls.componente, cantidad=100,
        )
        cls.sin_empresa = Usuario.objects.create(username="ti-se2", email="ti-se2@nowhere.test")
        cls.root = Usuario.objects.create(
            username="ti-root5", email="ti-root5@acme-ti.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_superuser=True, is_staff=True,
        )

    def _sin_efectos_secundarios(self):
        """Un request rechazado no mueve inventario ni toca las OPs."""
        self.existencia.refresh_from_db()
        self.assertEqual(self.existencia.cantidad, 100)
        self.assertFalse(MovimientoInventario.objects.exists())
        for op in (self.op, self.op_ajena):
            antes = (op.estatus_op, op.cerrar_orden, op.activo, op.fecha_fin)
            op.refresh_from_db()
            self.assertEqual((op.estatus_op, op.cerrar_orden, op.activo, op.fecha_fin), antes)

    def _mismo_mensaje_que_inexistente(self, campo, pk_ajeno, body_con):
        inexistente_pk = pk_ajeno + 999
        ajeno = self._client().post(self.URL, body_con(pk_ajeno), format="json")
        inexistente = self._client().post(self.URL, body_con(inexistente_pk), format="json")
        self.assertEqual((ajeno.status_code, inexistente.status_code), (400, 400))
        self.assertEqual(
            str(ajeno.data[campo][0]).replace(str(pk_ajeno), "X"),
            str(inexistente.data[campo][0]).replace(str(inexistente_pk), "X"),
        )


class ConsumoProduccionTenantTests(_RegistrosDeOPTenantBase, TestCase):
    """``/produccion/consumo/`` (sólo GET y POST): ``op`` debe ser de la empresa
    del usuario y todo registro ajeno responde 404, acciones incluidas."""

    URL = "/api/v1/produccion/consumo/"

    def setUp(self):
        self.consumo = ConsumoProduccion.objects.create(op=self.op)
        self.consumo_ajeno = ConsumoProduccion.objects.create(op=self.op_ajena)

    def test_create_con_op_propia_conserva_el_shape(self):
        resp = self._client().post(self.URL, {"op": self.op.pk}, format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(set(resp.data), {"consumo_produccion_id", "op", "detalles"})
        self.assertEqual((resp.data["op"], resp.data["detalles"]), (self.op.pk, []))

    def test_create_rechaza_op_de_otra_empresa(self):
        resp = self._client().post(self.URL, {"op": self.op_ajena.pk}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("op", resp.data)
        self.assertEqual(ConsumoProduccion.objects.count(), 2)
        self._sin_efectos_secundarios()

    def test_op_ajena_e_inexistente_responden_igual(self):
        self._mismo_mensaje_que_inexistente("op", self.op_ajena.pk, lambda pk: {"op": pk})

    def test_list_excluye_registros_de_otra_empresa(self):
        resp = self._client().get(self.URL)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([fila["consumo_produccion_id"] for fila in resp.data], [self.consumo.pk])

    def test_retrieve_propio_200_y_ajeno_404(self):
        self.assertEqual(self._client().get(f"{self.URL}{self.consumo.pk}/").status_code, 200)
        self.assertEqual(self._client().get(f"{self.URL}{self.consumo_ajeno.pk}/").status_code, 404)

    def test_acciones_sobre_registro_ajeno_responden_404(self):
        for accion in ("confirmar", "anular"):
            propio = self._client().post(f"{self.URL}{self.consumo.pk}/{accion}/")
            ajeno = self._client().post(f"{self.URL}{self.consumo_ajeno.pk}/{accion}/")
            self.assertEqual(propio.status_code, 200, accion)
            self.assertEqual(set(propio.data), {"msg"})
            self.assertEqual(ajeno.status_code, 404, accion)

    def test_put_patch_y_delete_no_existen_y_no_modifican(self):
        # ``http_method_names = ['get', 'post']``: no hay ruta de update/delete
        # por la que reasignar ``op``, ni para lo propio ni para lo ajeno.
        for consumo in (self.consumo, self.consumo_ajeno):
            url = f"{self.URL}{consumo.pk}/"
            op_antes = consumo.op_id
            self.assertEqual(self._client().put(url, {"op": self.op_ajena.pk}, format="json").status_code, 405)
            self.assertEqual(self._client().patch(url, {"op": self.op_ajena.pk}, format="json").status_code, 405)
            self.assertEqual(self._client().delete(url).status_code, 405)
            consumo.refresh_from_db()
            self.assertEqual(consumo.op_id, op_antes)

    def test_usuario_sin_empresa_lista_vacia_y_400_al_crear(self):
        client = self._client(self.sin_empresa)

        self.assertEqual(client.get(self.URL).data, [])
        resp = client.post(self.URL, {"op": self.op.pk}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(ConsumoProduccion.objects.count(), 2)

    def test_superusuario_queda_acotado_a_su_empresa(self):
        client = self._client(self.root)

        self.assertEqual(
            [fila["consumo_produccion_id"] for fila in client.get(self.URL).data], [self.consumo.pk]
        )
        self.assertEqual(client.get(f"{self.URL}{self.consumo_ajeno.pk}/").status_code, 404)
        resp = client.post(self.URL, {"op": self.op_ajena.pk}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("op", resp.data)


class ProductoTerminadoEntradasTenantTests(_RegistrosDeOPTenantBase, TestCase):
    """``/produccion/producto-terminado-entradas/`` (sólo GET y POST): el
    queryset se acota por la empresa de la OP y ``op``/``almacen``/``ubicacion``
    deben ser de la empresa del usuario."""

    URL = "/api/v1/produccion/producto-terminado-entradas/"

    def setUp(self):
        self.entrada = ProductoTerminadoEntradas.objects.create(
            op=self.op, almacen=self.almacen, ubicacion=self.ubicacion,
        )
        self.entrada_ajena = ProductoTerminadoEntradas.objects.create(
            op=self.op_ajena, almacen=self.almacen_ajeno, ubicacion=self.ubicacion_ajena,
        )

    def _body(self, **extra):
        return {"op": self.op.pk, "almacen": self.almacen.pk, "ubicacion": self.ubicacion.pk, **extra}

    def test_create_en_la_propia_empresa_conserva_el_shape(self):
        resp = self._client().post(self.URL, self._body(), format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(set(resp.data), {"pt_entrada_id", "op", "almacen", "ubicacion"})
        self.assertEqual(
            (resp.data["op"], resp.data["almacen"], resp.data["ubicacion"]),
            (self.op.pk, self.almacen.pk, self.ubicacion.pk),
        )

    def test_create_rechaza_cada_fk_de_otra_empresa(self):
        ajenas = {
            "op": self.op_ajena.pk, "almacen": self.almacen_ajeno.pk, "ubicacion": self.ubicacion_ajena.pk,
        }
        for campo, pk in ajenas.items():
            with self.subTest(campo=campo):
                resp = self._client().post(self.URL, self._body(**{campo: pk}), format="json")
                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertEqual(set(resp.data), {campo})
        self.assertEqual(ProductoTerminadoEntradas.objects.count(), 2)
        self._sin_efectos_secundarios()

    def test_fk_ajena_e_inexistente_responden_igual(self):
        ajenas = {
            "op": self.op_ajena.pk, "almacen": self.almacen_ajeno.pk, "ubicacion": self.ubicacion_ajena.pk,
        }
        for campo, pk in ajenas.items():
            with self.subTest(campo=campo):
                self._mismo_mensaje_que_inexistente(campo, pk, lambda valor, campo=campo: self._body(**{campo: valor}))

    def test_create_rechaza_ubicacion_de_otro_almacen_de_la_misma_empresa(self):
        otro_almacen = Almacen.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, codigo="ALM-TI2", nombre="Secundario",
        )
        ubicacion_de_otro = Ubicacion.objects.create(almacen=otro_almacen, pasillo="2")

        resp = self._client().post(self.URL, self._body(ubicacion=ubicacion_de_otro.pk), format="json")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(set(resp.data), {"ubicacion"})
        self.assertEqual(ProductoTerminadoEntradas.objects.count(), 2)
        self._sin_efectos_secundarios()

        ok = self._client().post(
            self.URL, self._body(almacen=otro_almacen.pk, ubicacion=ubicacion_de_otro.pk), format="json"
        )
        self.assertEqual(ok.status_code, 201, ok.data)

    def test_create_rechaza_almacen_y_ubicacion_sin_empresa(self):
        # ``Almacen.empresa`` y ``Ubicacion.almacen`` son nullable: sin empresa
        # determinable no se puede afirmar que sean del usuario (falla cerrado).
        almacen_huerfano = Almacen.objects.create(codigo="ALM-H", nombre="Sin empresa")
        ubicacion_huerfana = Ubicacion.objects.create(pasillo="0")

        almacen = self._client().post(self.URL, self._body(almacen=almacen_huerfano.pk), format="json")
        ubicacion = self._client().post(self.URL, self._body(ubicacion=ubicacion_huerfana.pk), format="json")

        self.assertEqual((almacen.status_code, ubicacion.status_code), (400, 400))
        self.assertIn("almacen", almacen.data)
        self.assertIn("ubicacion", ubicacion.data)
        self.assertEqual(ProductoTerminadoEntradas.objects.count(), 2)

    def test_list_excluye_registros_de_otra_empresa(self):
        resp = self._client().get(self.URL)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([fila["pt_entrada_id"] for fila in resp.data], [self.entrada.pk])

    def test_retrieve_propio_200_y_ajeno_404(self):
        self.assertEqual(self._client().get(f"{self.URL}{self.entrada.pk}/").status_code, 200)
        self.assertEqual(self._client().get(f"{self.URL}{self.entrada_ajena.pk}/").status_code, 404)

    def test_acciones_sobre_registro_ajeno_responden_404(self):
        for accion in ("confirmar", "anular"):
            propio = self._client().post(f"{self.URL}{self.entrada.pk}/{accion}/")
            ajeno = self._client().post(f"{self.URL}{self.entrada_ajena.pk}/{accion}/")
            self.assertEqual(propio.status_code, 200, accion)
            self.assertEqual(set(propio.data), {"msg"})
            self.assertEqual(ajeno.status_code, 404, accion)

    def test_put_patch_y_delete_no_existen_y_no_modifican(self):
        # ``http_method_names = ['get', 'post']``: no hay ruta de update/delete.
        for entrada in (self.entrada, self.entrada_ajena):
            url = f"{self.URL}{entrada.pk}/"
            antes = (entrada.op_id, entrada.almacen_id, entrada.ubicacion_id)
            body = {"op": self.op_ajena.pk, "almacen": self.almacen_ajeno.pk, "ubicacion": self.ubicacion_ajena.pk}
            self.assertEqual(self._client().put(url, body, format="json").status_code, 405)
            self.assertEqual(self._client().patch(url, body, format="json").status_code, 405)
            self.assertEqual(self._client().delete(url).status_code, 405)
            entrada.refresh_from_db()
            self.assertEqual((entrada.op_id, entrada.almacen_id, entrada.ubicacion_id), antes)

    def test_usuario_sin_empresa_lista_vacia_y_400_al_crear(self):
        client = self._client(self.sin_empresa)

        self.assertEqual(client.get(self.URL).data, [])
        self.assertEqual(client.get(f"{self.URL}{self.entrada.pk}/").status_code, 404)
        resp = client.post(self.URL, self._body(), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(ProductoTerminadoEntradas.objects.count(), 2)

    def test_superusuario_queda_acotado_a_su_empresa(self):
        client = self._client(self.root)

        self.assertEqual([fila["pt_entrada_id"] for fila in client.get(self.URL).data], [self.entrada.pk])
        self.assertEqual(client.get(f"{self.URL}{self.entrada_ajena.pk}/").status_code, 404)
        resp = client.post(self.URL, self._body(almacen=self.almacen_ajeno.pk), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("almacen", resp.data)
