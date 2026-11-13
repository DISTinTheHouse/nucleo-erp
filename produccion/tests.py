"""Tests de las órdenes de trabajo de producción (Reflejante, Bordado, OCM).

Cubren dos cosas:

* El antiduplicado: el bug de conteo (tallas en cantidad 0 desalineaban
  ``buscar_existente_full_match`` contra la creación del detalle, permitiendo
  esquivar el 409) y la constraint parcial que lo respalda a nivel BD.
* El scope multi-tenant del ``pedido`` en la creación (``_validar_contexto``).

No correr con ``manage.py test``: crearía la BD de test en el Supabase de
producción, y el proyecto ya no usa SQLite. Los cambios se verifican contra
producción en solo lectura o dentro de una transacción con rollback (ver
``CLAUDE.md``, sección Pruebas); estos casos documentan el comportamiento esperado.
"""

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.test import override_settings, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APIClient

from catalogo.models import Color, Producto, ProductoVariante, Talla, TipoProducto, VarianteProductoProduccion
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
    ConsumoProduccionDetalle,
    CorteMangaAvances,
    ListaMaterialBom,
    OrdenBordadoDetalle,
    OrdenesBordado,
    OrdenCorteMangaDetalle,
    OrdenesCorteManga,
    OrdenesReflejante,
    OrdenProduccion,
    OrdenProduccionPedido,
    OrdenProduccionDetalle,
    SalidaMaquila,
    OrdenProduccionRutaCritica,
    OrdenReflejanteDetalle,
    ProductoTerminadoEntradas,
    ReflejanteAvances,
    ReflejanteIncidencias,
    RutaProduccion,
)
from produccion.api.serializers import (
    BomDetalleSerializer,
    OrdenProduccionSerializer,
    ProductoTerminadoEntradasSerializer,
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


def _conceder_permisos(usuario, empresa, claves):
    """Rol activo de ``empresa`` con esas claves, asignado a ``usuario``. El
    catálogo de permisos solo vive en la BD, así que se siembran aquí."""
    from seguridad.models import Permiso, Rol, RolPermiso, UsuarioRol

    # Un rol por llamada: el sufijo no se repite aunque se llame dos veces para
    # el mismo usuario (``uq_rol_empresa_codigo``).
    rol = Rol.objects.create(
        empresa=empresa, codigo=f"rol-{usuario.pk}-{uuid.uuid4().hex[:12]}", nombre="Permisos de prueba",
        estatus=Rol.Estatus.ACTIVO,
    )
    for clave in claves:
        permiso, _ = Permiso.objects.get_or_create(clave=clave, defaults={"nombre": clave})
        RolPermiso.objects.create(rol=rol, permiso=permiso)
    UsuarioRol.objects.create(usuario=usuario, rol=rol, empresa=empresa)
    return rol


def _tipo_producto():
    """Tipo de producto obligatorio (#871); PT tiene pk=3 como en producción."""
    return TipoProducto.objects.get_or_create(codigo="PT", defaults={"pk": 3, "nombre": "Producto terminado"})[0]


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
    #: ¿Una orden cancelada (sin baja) libera el cupo del pedido? Sí en
    #: reflejante y corte de manga desde ddd8342 (#775); no en bordado.
    CANCELADA_LIBERA_CUPO = False

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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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

    def test_orden_cancelada_pero_activa_bloquea_o_libera_segun_el_tipo(self):
        """Cancelar una orden sin darla de baja (soft delete):

        - bordado: el pedido sigue tomado (``cantidades_asignadas`` solo mira
          ``activo``);
        - reflejante y corte de manga: la cancelada (estatus 7) libera el cupo
          (ddd8342, #775), así que se puede volver a generar la orden.
        """
        pedido, detalle = self._crear_pedido()
        self._talla(detalle, self.talla_ch, 10)

        primera = self._save(pedido)
        setattr(primera, self.ESTATUS_FIELD, self.CANCELADO)
        primera.save(update_fields=[self.ESTATUS_FIELD])

        if self.CANCELADA_LIBERA_CUPO:
            segunda = self._save(pedido)
            self.assertNotEqual(segunda.pk, primera.pk)
            self.assertEqual(self.MODEL.objects.filter(pedido=pedido).count(), 2)
            return
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
    CANCELADA_LIBERA_CUPO = True


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
    CANCELADA_LIBERA_CUPO = True


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


#: ViewSets satélite con el contrato CRUD genérico: endpoint, modelo, FK a la orden
#: padre y los campos requeridos / parcheables propios de cada uno.
#: ``reflejante-avances`` no está: desde a352618 es POST/DELETE con
#: ``{orden_r, cantidad}``, sin PATCH y con el gate de Producción; sus garantías
#: viven en ``ReflejanteAvancesContratoTenantTests``.
SATELITES = (
    {
        "nombre": "bordado-avances",
        "url": "/api/v1/produccion/bordado-avances/",
        "modelo": BordadoAvances,
        "fk": "ob",
        "extra": {"cantidad_bordada": 5, "puntadas_por_pieza": 1000},
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
        """EC-880: el nombre se conserva por historia, pero el superusuario ya NO
        ve ambas empresas: sólo la activa (todas sus sucursales); sin empresa,
        nada."""
        self.superuser.empresa = self.a["empresa"]
        self.superuser.save(update_fields=["empresa"])
        client = self._client(self.superuser)
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                ids = [row["id"] for row in client.get(cfg["url"]).json()]
                self.assertEqual(ids, [self.registros[(cfg["nombre"], "a")].pk])

    def test_superuser_sin_empresa_no_ve_nada(self):
        client = self._client(self.superuser)
        for cfg in SATELITES:
            with self.subTest(cfg["nombre"]):
                self.assertEqual(client.get(cfg["url"]).json(), [])

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


class SuperusuarioEmpresaActivaOrdenesTests(TestCase):
    """EC-880: OB/OR/OCM y sus satélites acotan al superusuario a su empresa
    activa, igual que la OP. Antes ``produccion.scope`` y
    ``_OrdenPadreTenantScopedMixin`` le devolvían todo y, como ``get_object``
    pasa por ellos, podía leer, editar, borrar y registrar avances en órdenes
    de otra empresa."""

    ORDENES = (
        ("ob", "/api/v1/produccion/orden-bordado/", {"observaciones": "tocado"}),
        ("orden_r", "/api/v1/produccion/orden-reflejante/", {"observaciones": "tocado"}),
        ("ocm", "/api/v1/produccion/orden-corte-manga/", {"observaciones": "tocado"}),
    )
    #: (url, fk, payload extra de alta, modelo) de los satélites con alta.
    AVANCES = (
        ("/api/v1/produccion/bordado-avances/", "ob", {"cantidad_bordada": 1, "puntadas_por_pieza": 10}, BordadoAvances),
        ("/api/v1/produccion/reflejante-avances/", "orden_r", {"cantidad": 1}, ReflejanteAvances),
        ("/api/v1/produccion/corte-manga-avances/", "ocm", {"cantidad": 1}, CorteMangaAvances),
    )
    SATELITES_LECTURA = (
        ("/api/v1/produccion/bordado-avances/", "ob", BordadoAvances, {"cantidad_bordada": 1}),
        ("/api/v1/produccion/bordado-incidencias/", "ob", BordadoIncidencias, {"tipo_incidencia": 1}),
        ("/api/v1/produccion/reflejante-avances/", "orden_r", ReflejanteAvances, {"cantidad_aplicada": 1}),
        ("/api/v1/produccion/reflejante-incidencias/", "orden_r", ReflejanteIncidencias, {}),
        ("/api/v1/produccion/corte-manga-avances/", "ocm", CorteMangaAvances, {"cantidad": 1}),
    )

    @classmethod
    def _tenant(cls, codigo, email):
        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        sucursal = Sucursal.objects.create(empresa=empresa, codigo=codigo[:3].upper(), nombre=codigo)
        cliente = Cliente.objects.create(empresa=empresa, nombre=f"Cliente {codigo}")
        usuario = Usuario.objects.create(username=email, email=email, empresa=empresa, sucursal_default=sucursal)
        pedido = Pedido.objects.create(
            empresa=empresa, sucursal=sucursal, cliente=cliente, moneda=cls.moneda,
            persona_pagos="Pagos", correo_facturas=email, telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        comun = {"empresa": empresa, "sucursal": sucursal, "pedido": pedido}
        return {
            "empresa": empresa, "usuario": usuario,
            "ob": OrdenesBordado.objects.create(folio_bordado=f"OB-{codigo}", **comun),
            "orden_r": OrdenesReflejante.objects.create(folio_reflejante=f"OR-{codigo}", **comun),
            "ocm": OrdenesCorteManga.objects.create(folio_ocm=f"OCM-{codigo}", **comun),
        }

    @classmethod
    def setUpTestData(cls):
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.a = cls._tenant("acme", "a@acme.test")
        cls.b = cls._tenant("globex", "b@globex.test")
        cls.superuser = Usuario.objects.create(
            username="root", email="root@acme.test", is_superuser=True, is_staff=True,
            empresa=cls.a["empresa"],
        )
        cls.satelites = {}
        for url, fk, modelo, extra in cls.SATELITES_LECTURA:
            for etiqueta, tenant in (("a", cls.a), ("b", cls.b)):
                cls.satelites[(url, etiqueta)] = modelo.objects.create(
                    usuario=tenant["usuario"], **{fk: tenant[fk]}, **extra
                )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.superuser)

    def test_listado_solo_trae_la_empresa_activa(self):
        for fk, url, _ in self.ORDENES:
            with self.subTest(url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                filas = resp.json()
                filas = filas.get("results", filas) if isinstance(filas, dict) else filas
                self.assertEqual([f["id"] for f in filas], [self.a[fk].pk])

    def test_retrieve_de_otra_empresa_es_404(self):
        for fk, url, _ in self.ORDENES:
            with self.subTest(url):
                self.assertEqual(self.client.get(f"{url}{self.b[fk].pk}/").status_code, 404)
                self.assertEqual(self.client.get(f"{url}{self.a[fk].pk}/").status_code, 200)

    def test_patch_de_otra_empresa_es_404_y_no_modifica(self):
        for fk, url, payload in self.ORDENES:
            with self.subTest(url):
                resp = self.client.patch(f"{url}{self.b[fk].pk}/", payload, format="json")
                self.assertEqual(resp.status_code, 404)
                orden = self.b[fk]
                orden.refresh_from_db()
                self.assertNotEqual(orden.observaciones, "tocado")

    def test_delete_de_otra_empresa_es_404_y_no_da_de_baja(self):
        for fk, url, _ in self.ORDENES:
            with self.subTest(url):
                self.assertEqual(self.client.delete(f"{url}{self.b[fk].pk}/").status_code, 404)
                orden = self.b[fk]
                orden.refresh_from_db()
                self.assertTrue(orden.activo)

    def test_no_registra_ni_da_de_baja_avances_de_otra_empresa(self):
        """El alta la rechaza el serializer (400 desde antes de EC-880); lo que
        protege el alcance nuevo es que el avance ajeno ya existente sea 404 en
        retrieve y DELETE por ``_OrdenPadreTenantScopedMixin``: con el atajo del
        superusuario de vuelta, esto devolvería 200/204 y lo daría de baja."""
        for url, fk, extra, modelo in self.AVANCES:
            with self.subTest(url):
                antes = modelo.objects.count()
                resp = self.client.post(url, {fk: self.b[fk].pk, **extra}, format="json")
                self.assertEqual(resp.status_code, 400)
                self.assertIn(fk, resp.data)
                self.assertEqual(modelo.objects.count(), antes)
                ajeno = self.satelites[(url, "b")]
                self.assertEqual(self.client.get(f"{url}{ajeno.pk}/").status_code, 404)
                self.assertEqual(self.client.delete(f"{url}{ajeno.pk}/").status_code, 404)
                ajeno.refresh_from_db()
                self.assertTrue(ajeno.activo)

    def test_registra_avances_en_otra_sucursal_de_su_empresa(self):
        """Escritura alineada con la lectura: el superusuario (sin
        ``is_admin_empresa``) registra en órdenes de cualquier sucursal de su
        empresa activa, no sólo en las de ``sucursales_permitidas()``."""
        empresa = self.a["empresa"]
        self.superuser.sucursal_default = self.a["ob"].sucursal
        self.superuser.save(update_fields=["sucursal_default"])
        gdl = Sucursal.objects.create(empresa=empresa, codigo="GDL2", nombre="Guadalajara")
        comun = {"empresa": empresa, "sucursal": gdl, "pedido": self.a["ob"].pedido}
        ob = OrdenesBordado.objects.create(folio_bordado="OB-acme-gdl", **comun)
        orden_r = OrdenesReflejante.objects.create(folio_reflejante="OR-acme-gdl", **comun)
        for url, cuerpo in (
            ("/api/v1/produccion/bordado-avances/", {"ob": ob.pk, "cantidad_bordada": 1, "puntadas_por_pieza": 10}),
            ("/api/v1/produccion/bordado-incidencias/", {"ob": ob.pk, "tipo_incidencia": 1, "usuario": self.superuser.pk}),
            ("/api/v1/produccion/reflejante-incidencias/", {"orden_r": orden_r.pk, "usuario": self.superuser.pk}),
        ):
            with self.subTest(url):
                resp = self.client.post(url, cuerpo, format="json")
                self.assertEqual(resp.status_code, 201, resp.data)

    def test_satelites_de_otra_empresa_no_se_leen_editan_ni_borran(self):
        for url, fk, modelo, _ in self.SATELITES_LECTURA:
            with self.subTest(url):
                ajeno = self.satelites[(url, "b")]
                ids = [f["id"] for f in self.client.get(url).json()]
                self.assertEqual(ids, [self.satelites[(url, "a")].pk])
                self.assertEqual(self.client.get(f"{url}{ajeno.pk}/").status_code, 404)
                self.assertEqual(
                    self.client.patch(f"{url}{ajeno.pk}/", {"comentario": "x"}, format="json").status_code,
                    404 if "incidencias" in url or "bordado" in url else 405,
                )
                self.assertEqual(self.client.delete(f"{url}{ajeno.pk}/").status_code, 404)
                ajeno.refresh_from_db()
                self.assertTrue(ajeno.activo)


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

    def test_avance_bordado_simple_por_ob(self):
        """Avance por OB: piezas + puntadas por pieza; total calculado en backend."""
        client = self._client()
        url = "/api/v1/produccion/bordado-avances/"
        resp = client.post(
            url,
            {"ob": self.ob.pk, "cantidad_bordada": 20, "puntadas_por_pieza": 5000,
             "puntadas_total": 1},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        av = BordadoAvances.objects.get(pk=resp.data["id"])
        self.assertEqual(av.puntadas_total, 100000)
        self.assertIsNone(av.orden_bordado_detalle_id)
        # Editar piezas recalcula el total.
        resp = client.patch(f"{url}{av.pk}/", {"cantidad_bordada": 10}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        av.refresh_from_db()
        self.assertEqual(av.puntadas_total, 50000)
        # Las puntadas son obligatorias al crear.
        resp = client.post(url, {"ob": self.ob.pk, "cantidad_bordada": 5}, format="json")
        self.assertEqual(resp.status_code, 400)

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


class ReflejanteAvancesContratoTenantTests(TestCase):
    """``reflejante-avances`` con el contrato de a352618: POST/DELETE con
    ``{orden_r, cantidad, comentario?}``, sin PATCH, autor de la sesión y gate de
    Producción. Reúne todas sus garantías multi-tenant (antes repartidas en los
    loops ``SATELITES``), con un alta que sí pasa el tope de piezas: listado y
    detalle acotados a la empresa y sucursal, sin empresa nada, alta contra una
    orden ajena rechazada, autoría de la sesión, baja ajena 404 y sin PATCH.
    """

    URL = "/api/v1/produccion/reflejante-avances/"

    @classmethod
    def _tenant(cls, codigo, email):
        from seguridad.models import Rol, UsuarioRol
        from seguridad.role_identity import CLAVE_DEPARTAMENTO_PRODUCCION

        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        sucursal = Sucursal.objects.create(empresa=empresa, codigo=codigo[:3].upper(), nombre=codigo)
        cliente = Cliente.objects.create(empresa=empresa, nombre=f"Cliente {codigo}")
        usuario = Usuario.objects.create(username=email, email=email, empresa=empresa, sucursal_default=sucursal)
        rol = Rol.objects.create(
            empresa=empresa, codigo=f"PRODUCCION-{codigo}", nombre="Producción",
            clave_departamento=CLAVE_DEPARTAMENTO_PRODUCCION,
        )
        UsuarioRol.objects.create(usuario=usuario, rol=rol, empresa=empresa)
        pedido = Pedido.objects.create(
            empresa=empresa, sucursal=sucursal, cliente=cliente, moneda=cls.moneda,
            persona_pagos="Pagos", correo_facturas=email, telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        orden_r = OrdenesReflejante.objects.create(
            empresa=empresa, sucursal=sucursal, pedido=pedido, folio_reflejante=f"OR-{codigo}",
        )
        # 10 piezas programadas: el alta exige Σ avances ≤ Σ renglones.
        OrdenReflejanteDetalle.objects.create(
            orden_r=orden_r, cantidad=10,
            pedido_detalle=PedidoDetalle.objects.create(pedido=pedido, producto_nombre_externo="Playera"),
            producto=Producto.objects.create(tipo=_tipo_producto(), empresa=empresa, nombre=f"Playera {codigo}"),
        )
        avance = ReflejanteAvances.objects.create(orden_r=orden_r, usuario=usuario, cantidad_aplicada=1)
        return {"empresa": empresa, "sucursal": sucursal, "usuario": usuario, "orden_r": orden_r, "avance": avance}

    @classmethod
    def setUpTestData(cls):
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.a = cls._tenant("acme", "a@acme.test")
        cls.b = cls._tenant("globex", "b@globex.test")
        cls.operador_a = Usuario.objects.create(
            username="operador_a2", email="operador_a2@acme.test", empresa=cls.a["empresa"],
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.a["usuario"])

    def _alta(self, **extra):
        return self.client.post(self.URL, {"orden_r": self.a["orden_r"].pk, "cantidad": 2, **extra}, format="json")

    def _ids(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        return [row["id"] for row in resp.json()]

    # --- lectura -----------------------------------------------------------------

    def test_listado_solo_trae_la_propia_empresa(self):
        self.assertEqual(self._ids(self.a["usuario"]), [self.a["avance"].pk])
        self.assertEqual(self._ids(self.b["usuario"]), [self.b["avance"].pk])

    def test_usuario_sin_acceso_a_la_sucursal_no_ve_el_registro(self):
        otra = Sucursal.objects.create(empresa=self.a["empresa"], codigo="CDMX", nombre="Ciudad de México")
        forastero = Usuario.objects.create(
            username="forastero-ra", email="forastero-ra@acme.test", empresa=self.a["empresa"],
            sucursal_default=otra,
        )
        self.assertEqual(self._ids(forastero), [])
        client = APIClient()
        client.force_authenticate(user=forastero)
        self.assertEqual(client.get(f"{self.URL}{self.a['avance'].pk}/").status_code, 404)

    def test_sin_empresa_no_ve_nada_ni_el_superusuario(self):
        sin_empresa = Usuario.objects.create(username="huerfano-ra", email="huerfano-ra@nowhere.test")
        root = Usuario.objects.create(username="root-ra", email="root-ra@nowhere.test", is_superuser=True)
        for user in (sin_empresa, root):
            with self.subTest(user=user.username):
                self.assertEqual(self._ids(user), [])

    def test_superusuario_ve_solo_su_empresa_activa(self):
        root = Usuario.objects.create(
            username="root-ra-a", email="root-ra-a@acme.test", is_superuser=True, empresa=self.a["empresa"],
        )
        self.assertEqual(self._ids(root), [self.a["avance"].pk])

    def test_alta_legitima_con_autor_de_la_sesion(self):
        # ``activo`` y ``usuario`` del body se ignoran: nace activo y a nombre del solicitante.
        resp = self._alta(usuario=self.operador_a.pk, activo=False)

        self.assertEqual(resp.status_code, 201, resp.data)
        creado = ReflejanteAvances.objects.get(pk=resp.data["id"])
        self.assertEqual(
            (creado.orden_r_id, creado.cantidad_aplicada, creado.activo, creado.usuario_id),
            (self.a["orden_r"].pk, 2, True, self.a["usuario"].pk),
        )

    def test_usuario_de_otra_empresa_en_el_body_se_ignora(self):
        resp = self._alta(usuario=self.b["usuario"].pk)

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(ReflejanteAvances.objects.get(pk=resp.data["id"]).usuario_id, self.a["usuario"].pk)

    def test_alta_con_orden_de_otra_empresa_es_400_sin_crear_nada(self):
        # Con un ``usuario`` ajeno también: un solo error claro, el de la orden.
        antes = ReflejanteAvances.objects.count()
        resp = self.client.post(
            self.URL, {"orden_r": self.b["orden_r"].pk, "cantidad": 1, "usuario": self.b["usuario"].pk}, format="json",
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("orden_r", resp.data)
        self.assertNotIn("usuario", resp.data)
        self.assertEqual(ReflejanteAvances.objects.count(), antes)

    def test_superusuario_sin_empresa_no_da_de_alta(self):
        root = Usuario.objects.create(username="root-ra-se", email="root-ra-se@nowhere.test", is_superuser=True)
        client = APIClient()
        client.force_authenticate(user=root)
        antes = ReflejanteAvances.objects.count()
        resp = client.post(self.URL, {"orden_r": self.a["orden_r"].pk, "cantidad": 1}, format="json")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("orden_r", resp.data)
        self.assertEqual(ReflejanteAvances.objects.count(), antes)

    def test_lectura_y_baja_de_otra_empresa_son_404(self):
        ajeno = self.b["avance"]
        self.assertEqual(self.client.get(f"{self.URL}{self.a['avance'].pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"{self.URL}{ajeno.pk}/").status_code, 404)
        self.assertEqual(self.client.delete(f"{self.URL}{ajeno.pk}/").status_code, 404)
        ajeno.refresh_from_db()
        self.assertTrue(ajeno.activo)

    def test_patch_es_405_y_no_modifica_ni_el_propio_ni_el_ajeno(self):
        for avance in (self.a["avance"], self.b["avance"]):
            with self.subTest(avance=avance.pk):
                # Ni contenido, ni baja por ``activo``, ni reasignar orden o autor.
                orden_original, autor_original = avance.orden_r_id, avance.usuario_id
                resp = self.client.patch(
                    f"{self.URL}{avance.pk}/",
                    {"comentario": "tocado", "cantidad": 9, "activo": False,
                     "orden_r": self.b["orden_r"].pk if avance is self.a["avance"] else self.a["orden_r"].pk,
                     "usuario": self.operador_a.pk},
                    format="json",
                )
                self.assertEqual(resp.status_code, 405)
                avance.refresh_from_db()
                self.assertEqual(
                    (avance.comentario, avance.cantidad_aplicada, avance.activo, avance.orden_r_id, avance.usuario_id),
                    (None, 1, True, orden_original, autor_original),
                )


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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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
            sku_muestra="SKU-TEST",
            pedido_detalle=self.detalle, talla=self.talla_ch,
            cantidad=self.CANTIDAD_CH, lleva_bordado=True,
        )
        self.pdt_m = PedidoDetalleTalla.objects.create(
            sku_muestra="SKU-TEST",
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
                    sku_muestra="SKU-TEST",
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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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
        # #1136: con su estatus.
        self.assertEqual(hermanas[0]["estatus"], 1)
        self.assertEqual(hermanas[0]["estatus_display"], "Pendiente")
        # No se incluye a sí misma.
        self.assertNotIn(primera.data["id"], [h["id"] for h in hermanas])

    def test_hermana_cancelada_no_aparece(self):
        """#1136: una OR cancelada (sin borrar) ya no se lista como hermana."""
        primera = self._post([(self.pdt_ch, self.CANTIDAD_CH)])
        segunda = self._post([(self.pdt_m, self.CANTIDAD_M)])
        OrdenesReflejante.objects.filter(pk=segunda.data["id"]).update(estatus_reflejante=7)
        self.assertEqual(self._detalle(primera.data["id"])["otras_ordenes_del_pedido"], [])

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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Chamarra")
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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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
            sku_muestra="SKU-TEST",
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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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
            sku_muestra="SKU-TEST",
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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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
            sku_muestra="SKU-TEST",
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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Chamarra")
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
            sku_muestra="SKU-TEST",
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

        # Bordado suma sku/sku_proscai/existencias sobre la base.
        self.assertEqual(
            set(lineas[0].keys()),
            CLAVES_LINEA_ONBOARDING_BASE | {"sku", "sku_proscai", "existencias"},
        )
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
        cls.componente = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Hilo")
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

    # --- EC-821: ``detalle.almacen`` con el almacén de cada existencia -------------

    def _consumir(self, cantidad):
        consumos = OrdenProduccionService._discount_existencias(
            [{"producto": self.componente, "cantidad": Decimal(cantidad)}], self.empresa, self.sucursal,
        )
        movimiento = OrdenProduccionService._registrar_movimiento_inventario(self.op, self.usuario, consumos)
        return sorted(
            MovimientoInventarioDetalle.objects.filter(movimiento_inventario=movimiento)
            .values_list("almacen_id", "ubicacion_origen_id", "cantidad")
        , key=lambda f: (f[0] or 0, f[1] or 0))

    def test_llena_el_almacen_con_y_sin_ubicacion(self):
        Existencia.objects.create(producto=self.componente, almacen=self.almacen, cantidad=Decimal("5"))
        Existencia.objects.create(
            producto=self.componente, producto_variante=self.variante, almacen=self.almacen,
            ubicacion=self.ubicacion, cantidad=Decimal("3"),
        )
        self.assertEqual(
            self._consumir("6"),
            sorted([(self.almacen.pk, self.ubicacion.pk, Decimal("1")), (self.almacen.pk, None, Decimal("5"))],
                   key=lambda f: (f[0] or 0, f[1] or 0)),
        )

    def test_consumo_repartido_en_dos_almacenes_un_renglon_por_almacen(self):
        otro = Almacen.objects.create(empresa=self.empresa, sucursal=self.sucursal, codigo="ALM2", nombre="Otro")
        Existencia.objects.create(producto=self.componente, almacen=self.almacen, cantidad=Decimal("4"))
        Existencia.objects.create(producto=self.componente, almacen=otro, cantidad=Decimal("3"))
        self.assertEqual(
            self._consumir("6"),
            sorted([(self.almacen.pk, None, Decimal("4")), (otro.pk, None, Decimal("2"))]),
        )


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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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

    # --- EC-873: ``activo`` expuesto y ``atrasado`` falso si está dado de baja o cancelado ---

    def _pedido_listado(self, horas_desde_creacion, activo=True):
        pedido = self._pedido_especial(
            self.empresa, self.sucursal, self.cliente,
            clasificacion="B", fecha_confirmacion=timezone.now(),
        )
        # ``created_at`` es auto_now_add: se mueve con ``update`` para simular antigüedad.
        Pedido.objects.filter(pk=pedido.pk).update(
            created_at=timezone.now() - timedelta(hours=horas_desde_creacion), activo=activo,
        )
        return pedido

    def _filas_por_id(self):
        body = self._client().get(self.URL).json()
        filas = body.get("results", body) if isinstance(body, dict) else body
        return {p["id"]: p for p in filas}

    def test_inactivo_vencido_aparece_con_activo_false_y_no_atrasado(self):
        pedido = self._pedido_listado(horas_desde_creacion=48, activo=False)
        fila = self._filas_por_id()[pedido.pk]
        self.assertIs(fila["activo"], False)
        self.assertIs(fila["atrasado"], False)

    def test_activo_vencido_sin_avance_esta_atrasado(self):
        pedido = self._pedido_listado(horas_desde_creacion=48)
        fila = self._filas_por_id()[pedido.pk]
        self.assertIs(fila["activo"], True)
        self.assertIs(fila["atrasado"], True)

    def test_activo_en_tiempo_no_esta_atrasado(self):
        pedido = self._pedido_listado(horas_desde_creacion=1)
        fila = self._filas_por_id()[pedido.pk]
        self.assertIs(fila["activo"], True)
        self.assertIs(fila["atrasado"], False)

    def test_exactamente_24_horas_ya_esta_atrasado(self):
        # El umbral es inclusivo (``horas >= HORAS_ALERTA_PEDIDO_ESPECIAL``).
        en_umbral = self._pedido_listado(horas_desde_creacion=24)
        antes = self._pedido_listado(horas_desde_creacion=23)
        filas = self._filas_por_id()
        self.assertIs(filas[en_umbral.pk]["atrasado"], True)
        self.assertIs(filas[antes.pk]["atrasado"], False)

    def test_activo_vencido_con_pre_orden_no_esta_atrasado(self):
        from produccion.models import PreOrdenProduccion

        pedido = self._pedido_listado(horas_desde_creacion=48)
        PreOrdenProduccion.objects.create(empresa=self.empresa, sucursal=self.sucursal, pedido=pedido)
        fila = self._filas_por_id()[pedido.pk]
        self.assertIs(fila["sin_avance"], False)
        self.assertIs(fila["atrasado"], False)

    def test_cancelado_pero_activo_no_esta_atrasado(self):
        pedido = self._pedido_listado(horas_desde_creacion=48)
        Pedido.objects.filter(pk=pedido.pk).update(estatus=Pedido.ESTATUS_CANCELADO)
        fila = self._filas_por_id()[pedido.pk]
        self.assertIs(fila["activo"], True)
        self.assertIs(fila["sin_avance"], True)
        self.assertIs(fila["atrasado"], False)

    def test_detalle_expone_activo(self):
        pedido = self._pedido_listado(horas_desde_creacion=48, activo=False)
        body = self._client().get(f"{self.URL}{pedido.pk}/").json()
        self.assertIs(body["activo"], False)

    #: Consultas del listado ya autenticado: una sola, todo va anotado (EC-873).
    CONSULTAS_LISTADO = 1

    def test_listado_no_crece_en_consultas_con_las_filas(self):
        """``activo``/``estatus`` van en el ``.only()``: leerlos no difiere un query por fila."""
        self._pedido_listado(horas_desde_creacion=48, activo=False)
        client = self._client()
        with self.assertNumQueries(self.CONSULTAS_LISTADO):
            client.get(self.URL)
        self._pedido_listado(horas_desde_creacion=48)
        self._pedido_listado(horas_desde_creacion=1, activo=False)
        with CaptureQueriesContext(connection) as tres:
            filas = self._filas_por_id()
        self.assertEqual(len(filas), 3)
        self.assertEqual(len(tres), self.CONSULTAS_LISTADO)


    # --- #1093: horas_en_produccion y OP que cuentan como avance ---

    def _op_ligada(self, pedido, principal=None, activo=True, folio="OP-PE-1"):
        """OP cuyo principal es ``principal`` (o el propio ``pedido``) con
        ``pedido`` ligado en ``OrdenProduccionPedido`` (como lo hace ``pedidos_ids``)."""
        op = OrdenProduccion.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, folio_op=folio,
            pedido=principal or pedido, activo=activo,
        )
        OrdenProduccionPedido.objects.get_or_create(op=op, pedido=principal or pedido)
        OrdenProduccionPedido.objects.get_or_create(op=op, pedido=pedido)
        return op

    def test_horas_en_produccion_son_horas_completas_desde_la_creacion(self):
        # Se cuenta desde ``created_at`` (no desde la confirmación), truncado.
        pedido = self._pedido_listado(horas_desde_creacion=30)
        Pedido.objects.filter(pk=pedido.pk).update(
            created_at=timezone.now() - timedelta(hours=30, minutes=40),
        )
        self.assertEqual(self._filas_por_id()[pedido.pk]["horas_en_produccion"], 30)

    def test_op_activa_quita_sin_avance_y_atrasado(self):
        pedido = self._pedido_listado(horas_desde_creacion=48)
        op = self._op_ligada(pedido)
        fila = self._filas_por_id()[pedido.pk]
        self.assertIs(fila["sin_avance"], False)
        self.assertIs(fila["atrasado"], False)
        self.assertEqual([o["op_id"] for o in fila["ordenes_produccion"]], [op.pk])

    def test_op_que_lo_liga_como_secundario_tambien_cuenta(self):
        principal = self._pedido_listado(horas_desde_creacion=1)
        secundario = self._pedido_listado(horas_desde_creacion=48)
        self._op_ligada(secundario, principal=principal)
        fila = self._filas_por_id()[secundario.pk]
        self.assertIs(fila["sin_avance"], False)
        self.assertIs(fila["atrasado"], False)

    def test_op_dada_de_baja_no_cuenta_como_avance(self):
        pedido = self._pedido_listado(horas_desde_creacion=48)
        self._op_ligada(pedido, activo=False)
        fila = self._filas_por_id()[pedido.pk]
        self.assertEqual(fila["ordenes_produccion"], [])
        self.assertIs(fila["sin_avance"], True)
        self.assertIs(fila["atrasado"], True)

    def test_listado_con_op_y_pre_orden_sigue_en_una_consulta(self):
        from produccion.models import PreOrdenProduccion

        for i in range(3):
            pedido = self._pedido_listado(horas_desde_creacion=48)
            self._op_ligada(pedido, folio=f"OP-PE-Q{i}")
            PreOrdenProduccion.objects.create(empresa=self.empresa, sucursal=self.sucursal, pedido=pedido)
        client = self._client()
        with self.assertNumQueries(self.CONSULTAS_LISTADO):
            client.get(self.URL)


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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
        cls.variante = ProductoVariante.objects.create(
            producto=cls.producto, empresa=cls.empresa, color=color, sku="PLA-NEG", precio_base="100",
        )
        cls.unidad = UnidadMedida.objects.create(clave="PZA", nombre="Pieza")
        cls.componente = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Tela")
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

    def _segunda_sucursal(self):
        suc = Sucursal.objects.create(empresa=self.empresa, codigo="SLP", nombre="Segunda")
        SerieFolio.objects.create(
            empresa=self.empresa, sucursal=suc, tipo_documento="ORDEN_PRODUCCION", serie="OP",
        )
        alm = Almacen.objects.create(empresa=self.empresa, sucursal=suc, codigo="ALM2", nombre="Segundo")
        Existencia.objects.create(almacen=alm, producto=self.componente, cantidad=100)
        return suc

    def test_la_sucursal_del_body_manda_sobre_la_default(self):
        """#509: antes se ignoraba en silencio y la OP caía en ``sucursal_default``."""
        suc = self._segunda_sucursal()
        body = self._body()
        body["sucursal"] = suc.pk
        resp = self._client().post(self.URL, body, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(OrdenProduccion.objects.get(pk=resp.data["op_id"]).sucursal_id, suc.pk)

    def test_sucursal_de_otra_empresa_o_inexistente_responde_igual_400(self):
        for pk in (self.otra_sucursal.pk, 999999, "x"):
            body = self._body()
            body["sucursal"] = pk
            resp = self._client().post(self.URL, body, format="json")
            self.assertEqual(resp.status_code, 400, resp.data)
            self.assertEqual(str(resp.data["sucursal"]), "Sucursal no válida.")

    def test_usuario_no_admin_no_puede_crear_en_sucursal_fuera_de_su_alcance(self):
        suc = self._segunda_sucursal()
        limitado = Usuario.objects.create(
            username="op-lim", email="lim@acme-op.test", empresa=self.empresa,
            sucursal_default=self.sucursal,
        )
        client = APIClient()
        client.force_authenticate(user=limitado)
        body = self._body()
        body["sucursal"] = suc.pk
        resp = client.post(self.URL, body, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(str(resp.data["sucursal"]), "Sucursal no válida.")

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
    #: Cantidad de las tallas ajenas: no debe aparecer en ninguna respuesta. Cerca
    #: del máximo de ``PositiveIntegerField`` para que ningún id (ni el 999999 del
    #: body) la contenga; con 37 la prueba fallaba al azar cuando un id era 137.
    CANTIDAD_AJENA = 2147483646

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
        producto = Producto.objects.create(tipo=_tipo_producto(), empresa=empresa, nombre="Playera")
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

    # Bordado ya no está aquí: acepta muestras con SKU y ve ``OrdenBordadoMuestraTests``.
    CASOS = (
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


class OrdenBordadoMuestraTests(OnboardingPostLineaMuestraTests):
    """#516/#518: GET y POST de OB aplican el mismo criterio sobre las muestras."""

    URL = "/api/v1/produccion/orden-bordado/onboarding/"
    CASOS = ()

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client

    def _linea_en_get(self):
        data = self._client().get(self.URL).json()
        pedido = next((p for p in data["pedidos"] if p["id"] == self.pedido.pk), None)
        return None if pedido is None else {l["talla_id"]: l for l in pedido["detalles"]}

    def test_muestra_sin_sku_no_se_ofrece_ni_se_acepta(self):
        serie = SerieFolio.objects.get(empresa=self.empresa, tipo_documento="ORDEN_BORDADO")
        self.assertIsNone(self._linea_en_get())

        resp = self._client().post(self.URL, {"pedido": self.pedido.pk}, format="json")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(OrdenesBordado.objects.filter(pedido=self.pedido).exists())
        serie.refresh_from_db()
        self.assertEqual(serie.folio_actual, 0)

    def test_muestra_con_sku_se_ofrece_y_genera_ob_sin_producto(self):
        PedidoDetalleTalla.objects.filter(pk=self.pdt.pk).update(sku_muestra="MUE-001")
        self.assertIn(self.pdt.talla_id, self._linea_en_get())

        resp = self._client().post(self.URL, {"pedido": self.pedido.pk}, format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        detalle = OrdenesBordado.objects.get(pedido=self.pedido).detalles.get()
        self.assertIsNone(detalle.producto_id)
        self.assertEqual(detalle.cantidad, 5)
        # La OB queda visible por API aunque el detalle no tenga producto.
        self.assertEqual(
            self._client().get(f"/api/v1/produccion/orden-bordado/{resp.data['id']}/").status_code, 200
        )

    def test_linea_de_catalogo_sin_variante_no_se_oculta(self):
        """#518: el filtro es solo para muestras."""
        PedidoDetalle.objects.filter(pk=self.pdt.pedido_detalle_id).update(
            producto=Producto.objects.create(tipo=_tipo_producto(), empresa=self.empresa, nombre="Playera"),
            producto_nombre_externo=None,
        )
        self.assertIn(self.pdt.talla_id, self._linea_en_get())
        resp = self._client().post(self.URL, {"pedido": self.pedido.pk}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_override_de_talla_oculta_se_rechaza(self):
        resp = self._client().post(
            self.URL,
            {"pedido": self.pedido.pk, "detalles_override": [
                {"pedido_detalle_talla_id": self.pdt.pk, "cantidad": 1}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)


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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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


class EstatusOPTests(TestCase):
    """``/orden-produccion/{op_id}/estatus/``: opciones y cambio de estatus."""

    E = OrdenProduccion.EstatusOrdenProduccion

    @classmethod
    def setUpTestData(cls):
        from auditoria.models import AuditoriaEvento  # noqa: F401
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.otra = Empresa.objects.create(codigo="otra", razon_social="OTRA SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="Monterrey")
        cls.admin = Usuario.objects.create(
            username="adm", email="adm@acme.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.lector = Usuario.objects.create(
            username="lec", email="lec@acme.test", empresa=cls.empresa, sucursal_default=cls.sucursal,
        )
        cls.ajeno = Usuario.objects.create(
            username="ajeno", email="ajeno@otra.test", empresa=cls.otra, is_admin_empresa=True,
        )

    def setUp(self):
        self.op = OrdenProduccion.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, folio_op="OP-EST-1",
        )
        self.url = f"/api/v1/produccion/orden-produccion/{self.op.pk}/estatus/"

    def _client(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.admin)
        return client

    def _post(self, body, user=None):
        return self._client(user).post(self.url, body, format="json")

    def _set(self, estatus):
        OrdenProduccion.objects.filter(pk=self.op.pk).update(estatus_op=estatus)

    def _liberar_ruta(self):
        OrdenProduccionRutaCritica.objects.create(
            op=self.op, kit_completo=True,
            fecha_liberacion_paquete_tecnico=timezone.localdate(),
            estatus_paquete_tecnico="nueva",
        )

    def test_get_lista_opciones_con_pocas_queries(self):
        client = self._client()
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(self.url)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertLessEqual(len(ctx), 3, [q["sql"] for q in ctx])
        self.assertEqual(resp.data["estatus_op"], 1)
        self.assertEqual(
            [o["estatus_op"] for o in resp.data["opciones"]],
            [int(self.E.PREPARACION), int(self.E.DETENIDO), int(self.E.CANCELADO)],
        )

    def test_cambio_feliz_deja_auditoria_con_usuario(self):
        from auditoria.models import AuditoriaEvento
        resp = self._post({"estatus_op": int(self.E.PREPARACION)})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.op.refresh_from_db()
        self.assertEqual(self.op.estatus_op, self.E.PREPARACION)
        ev = AuditoriaEvento.objects.get(modulo="produccion", accion="CAMBIO_ESTATUS")
        self.assertEqual(ev.usuario_id, self.admin.pk)
        self.assertEqual(ev.id_registro, str(self.op.pk))
        self.assertEqual(ev.antes_json["estatus_op"], 1)
        self.assertEqual(ev.despues_json["estatus_op"], 2)

    def test_cambio_con_pocas_queries(self):
        client = self._client()
        with CaptureQueriesContext(connection) as ctx:
            resp = client.post(self.url, {"estatus_op": int(self.E.PREPARACION)}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertLessEqual(len(ctx), 9, [q["sql"] for q in ctx])

    def test_transicion_invalida_409(self):
        resp = self._post({"estatus_op": int(self.E.COMPLETADO)})
        self.assertEqual(resp.status_code, 409, resp.data)
        self.op.refresh_from_db()
        self.assertEqual(self.op.estatus_op, self.E.PENDIENTE)

    def test_estatus_terminales_no_tienen_salida(self):
        for terminal in (self.E.COMPLETADO, self.E.CANCELADO):
            self._set(terminal)
            self.assertEqual(self._client().get(self.url).data["opciones"], [])
            self.assertEqual(self._post({"estatus_op": int(self.E.PREPARACION)}).status_code, 409)

    def test_usuario_no_admin_cambia_estatus_de_op_de_otra_sucursal_de_su_empresa(self):
        """#507: mismo alcance que list/detail (empresa), sin filtrar por sucursal."""
        otra_suc = Sucursal.objects.create(empresa=self.empresa, codigo="GDL", nombre="Guadalajara")
        OrdenProduccion.objects.filter(pk=self.op.pk).update(sucursal=otra_suc)
        self.assertEqual(self._client(self.lector).get(self.url).status_code, 200)

    def test_motivo_que_no_es_texto_responde_400(self):
        resp = self._post({"estatus_op": int(self.E.DETENIDO), "motivo": 5})
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("motivo", resp.data)

    def test_ip_de_auditoria_con_cadena_de_proxies_no_tumba_el_cambio(self):
        """#507: ``X-Forwarded-For`` encadenado o inválido no debe dar 500."""
        from auditoria.models import AuditoriaEvento

        with override_settings(ENVIRONMENT="production"):
            resp = self._client().post(
                self.url, {"estatus_op": int(self.E.PREPARACION)}, format="json",
                HTTP_X_FORWARDED_FOR="1.2.3.4, 10.0.0.1",
            )
            self.assertEqual(resp.status_code, 200, resp.data)
            self.assertEqual(AuditoriaEvento.objects.get(accion="CAMBIO_ESTATUS").ip, "1.2.3.4")

            self._set(self.E.PENDIENTE)
            resp = self._client().post(
                self.url, {"estatus_op": int(self.E.PREPARACION)}, format="json",
                HTTP_X_FORWARDED_FOR="no-es-ip",
            )
            self.assertEqual(resp.status_code, 200, resp.data)

    def test_detener_y_cancelar_exigen_motivo(self):
        for destino in (self.E.DETENIDO, self.E.CANCELADO):
            resp = self._post({"estatus_op": int(destino)})
            self.assertEqual(resp.status_code, 400, resp.data)
            self.assertIn("motivo", resp.data)
        ok = self._post({"estatus_op": int(self.E.DETENIDO), "motivo": "Falta de tela"})
        self.assertEqual(ok.status_code, 200, ok.data)

    def test_en_produccion_exige_ruta_critica(self):
        self._set(self.E.PREPARACION)
        resp = self._post({"estatus_op": int(self.E.BORDANDO)})
        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertEqual(len(resp.data["bloqueos"]), 2)
        opcion = next(o for o in self._client().get(self.url).data["opciones"] if o["estatus_op"] == 3)
        self.assertFalse(opcion["permitido"])
        self._liberar_ruta()
        self.assertEqual(self._post({"estatus_op": int(self.E.BORDANDO)}).status_code, 200)

    def test_espera_muestra_bloquea_el_paquete_tecnico(self):
        self._set(self.E.PREPARACION)
        self._liberar_ruta()
        OrdenProduccionRutaCritica.objects.filter(op=self.op).update(estatus_paquete_tecnico="espera_muestra")
        self.assertEqual(self._post({"estatus_op": int(self.E.BORDANDO)}).status_code, 409)

    def test_forzar_solo_admin_y_queda_en_auditoria(self):
        from auditoria.models import AuditoriaEvento
        self._set(self.E.PREPARACION)
        resp = self._post({"estatus_op": int(self.E.BORDANDO), "forzar": True})
        self.assertEqual(resp.status_code, 200, resp.data)
        ev = AuditoriaEvento.objects.get(accion="CAMBIO_ESTATUS")
        self.assertTrue(ev.despues_json["forzado"])
        self.assertEqual(len(ev.despues_json["bloqueos_omitidos"]), 2)

    def test_completar_sella_fecha_fin(self):
        self._set(self.E.REVISION)
        resp = self._post({"estatus_op": int(self.E.COMPLETADO)})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.op.refresh_from_db()
        self.assertIsNotNone(self.op.fecha_fin)

    def test_sin_departamento_produccion_no_escribe_pero_si_lee(self):
        self.assertEqual(self._client(self.lector).get(self.url).status_code, 200)
        resp = self._post({"estatus_op": int(self.E.PREPARACION)}, user=self.lector)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("permiso", resp.data)

    def test_otra_empresa_recibe_404(self):
        self.assertEqual(self._client(self.ajeno).get(self.url).status_code, 404)
        self.assertEqual(self._post({"estatus_op": int(self.E.PREPARACION)}, user=self.ajeno).status_code, 404)

    def test_estatus_invalido_o_mal_tipado(self):
        for valor in ("2", None, True, 99):
            with self.subTest(valor=valor):
                self.assertEqual(self._post({"estatus_op": valor}).status_code, 400)

    def test_patch_generico_ya_no_cambia_el_estatus(self):
        resp = self._client().patch(
            f"/api/v1/produccion/orden-produccion/{self.op.pk}/",
            {"estatus_op": int(self.E.COMPLETADO)}, format="json",
        )
        self.op.refresh_from_db()
        self.assertEqual(self.op.estatus_op, self.E.PENDIENTE, resp.data)


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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
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

    # --- #357: parámetros soportados / no soportados ---------------------------

    def test_parametro_no_soportado_responde_400(self):
        resp = self._get(self.usuario, "?cliente=1&linea=3")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("cliente", str(resp.data["parametros"]))
        self.assertIn("linea", str(resp.data["parametros"]))

    def test_parametros_invalidos_responden_400(self):
        for query in (
            "?sucursal=abc", "?fecha_desde=2026-13-40", "?fecha_desde=2026-02-01&fecha_hasta=2026-01-01",
            "?sucursal=1_0", "?sucursal=%D9%A3",  # EC-829: solo dígitos ASCII
        ):
            with self.subTest(query=query):
                self.assertEqual(self._get(self.usuario, query).status_code, 400)

    def test_filtro_por_sucursal_cambia_el_resultado_y_se_refleja(self):
        resp = self._get(self.admin_empresa, f"?sucursal={self.sucursal_2.pk}")

        self.assertEqual(resp.data["filtros"]["sucursal"], self.sucursal_2.pk)
        self.assertEqual([d["op_id"] for d in resp.data["ops_atrasadas"]["drill_down"]], [self.op_vencida_sucursal_2.pk])
        self.assertEqual(resp.data["cumplimiento_a_tiempo"]["ops_terminadas"], 0)

    def test_sucursal_inexistente_o_ajena_da_vacio_no_datos_sin_filtrar(self):
        resp = self._get(self.admin_empresa, f"?sucursal={self.sucursal_otra.pk}")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["ops_atrasadas"]["total"], 0)
        self.assertEqual(resp.data["cumplimiento_a_tiempo"]["ops_terminadas"], 0)

    def test_filtro_por_fecha_compromiso(self):
        hoy = timezone.localdate()
        desde = (hoy - timedelta(days=3)).isoformat()

        resp = self._get(self.admin_empresa, f"?fecha_desde={desde}&fecha_hasta={hoy.isoformat()}")

        self.assertEqual(resp.data["filtros"]["fecha_desde"].isoformat(), desde)
        # Compromiso en la ventana: OP-A (hoy), OP-C (hace 2), OP-F (hoy), OP-H (hace 1), OP-E cancelada (hace 1).
        self.assertEqual(resp.data["cumplimiento_a_tiempo"]["ops_terminadas"], 2)  # OP-A y OP-F
        self.assertEqual(resp.data["ops_atrasadas"]["total"], 2)  # OP-C y OP-H

    # --- #358: drill-down completo --------------------------------------------

    def test_drill_down_marca_truncado_y_orden_estable(self):
        hoy = timezone.localdate()
        ahora = timezone.now()
        for i in range(25):
            OrdenProduccion.objects.create(
                empresa=self.empresa, sucursal=self.sucursal, folio_op=f"OP-ATR-{i}",
                estatus_op=OrdenProduccion.EstatusOrdenProduccion.BORDANDO,
                fecha_entrega_estimada=hoy - timedelta(days=10),
            )
            OrdenProduccion.objects.create(
                empresa=self.empresa, sucursal=self.sucursal, folio_op=f"OP-TAR-{i}",
                estatus_op=OrdenProduccion.EstatusOrdenProduccion.COMPLETADO,
                fecha_entrega_estimada=hoy - timedelta(days=5), fecha_fin=ahora,
            )

        data = self._get(self.usuario).data

        atrasadas = data["ops_atrasadas"]
        self.assertEqual(atrasadas["total"], 26)
        self.assertEqual(atrasadas["drill_down_total"], 26)
        self.assertTrue(atrasadas["drill_down_truncado"])
        self.assertEqual(len(atrasadas["drill_down"]), 20)
        tardias = data["cumplimiento_a_tiempo"]
        self.assertEqual(tardias["drill_down_tardias_total"], 26)
        self.assertTrue(tardias["drill_down_tardias_truncado"])
        # Misma fecha_fin en 25 OPs: el desempate por op_id descendente fija el orden.
        ids = [d["op_id"] for d in tardias["drill_down_tardias"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(len(set(ids)), 20)

    LISTADO = "/api/v1/produccion/orden-produccion/"

    def _listado(self, user, query=""):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(f"{self.LISTADO}{query}")

    def test_listado_reproduce_cada_conjunto_del_kpi(self):
        kpis = self._get(self.usuario).data
        tardias = self._listado(self.usuario, "?kpi=tardias").data
        atrasadas = self._listado(self.usuario, "?kpi=atrasadas").data
        terminadas = self._listado(self.usuario, "?kpi=terminadas").data
        a_tiempo = self._listado(self.usuario, "?kpi=a_tiempo").data
        sin_clasificar = self._listado(self.usuario, "?kpi=sin_clasificar").data

        otd = kpis["cumplimiento_a_tiempo"]
        self.assertEqual(len(tardias), otd["ops_tardias"])
        self.assertEqual([o["op_id"] for o in tardias], [self.op_tardia.pk])
        self.assertEqual(len(atrasadas), kpis["ops_atrasadas"]["total"])
        self.assertEqual(len(terminadas), otd["ops_terminadas"])
        self.assertEqual(len(a_tiempo), otd["ops_a_tiempo"])
        self.assertEqual(len(sin_clasificar), otd["ops_sin_clasificar"])

    def test_listado_estatus_negativo_da_vacio(self):
        # EC-829: ``estatus_op`` admite signo; uno negativo no existe y da 200 vacío.
        resp = self._listado(self.admin_empresa, "?estatus_op=-1")
        self.assertEqual((resp.status_code, resp.data), (200, []))
        self.assertEqual(self._listado(self.admin_empresa, "?estatus_op=1_0").status_code, 400)

    def test_listado_kpi_desconocido_es_400(self):
        self.assertEqual(self._listado(self.usuario, "?kpi=otro").status_code, 400)

    def test_listado_filtra_por_estatus_sucursal_y_fechas(self):
        completadas = self._listado(self.admin_empresa, f"?estatus_op={OrdenProduccion.EstatusOrdenProduccion.COMPLETADO}").data
        self.assertEqual(len(completadas), 4)
        cdmx = self._listado(self.admin_empresa, f"?sucursal={self.sucursal_2.pk}").data
        self.assertEqual([o["op_id"] for o in cdmx], [self.op_vencida_sucursal_2.pk])
        hoy = timezone.localdate().isoformat()
        hoy_ops = self._listado(self.admin_empresa, f"?fecha_desde={hoy}&fecha_hasta={hoy}").data
        self.assertEqual({o["folio_op"] for o in hoy_ops}, {"OP-A", "OP-F"})

    def test_listado_excluye_inactivas_y_expone_fecha_entrega_estimada(self):
        OrdenProduccion.objects.filter(pk=self.op_vencida.pk).update(activo=False)

        data = self._listado(self.usuario).data

        self.assertNotIn(self.op_vencida.pk, [o["op_id"] for o in data])
        self.assertTrue(all("fecha_entrega_estimada" in o for o in data))
        # Los KPIs ya las excluían: ahora cuadran.
        self.assertEqual(self._get(self.usuario).data["ops_atrasadas"]["total"], 0)

    def test_listado_solo_pagina_si_se_pide(self):
        completo = self._listado(self.usuario).data
        self.assertIsInstance(completo, list)

        pagina = self._listado(self.usuario, "?page_size=2").data

        self.assertEqual(pagina["count"], len(completo))
        self.assertEqual(len(pagina["results"]), 2)
        self.assertIsNotNone(pagina["next"])

    def test_fecha_fin_tiene_el_mismo_formato_en_listado_y_drill_down(self):
        drill = json.loads(self._get(self.usuario).content)["cumplimiento_a_tiempo"]["drill_down_tardias"][0]
        listado = next(
            o for o in json.loads(self._listado(self.usuario).content) if o["op_id"] == drill["op_id"]
        )

        self.assertEqual(drill["fecha_fin"], listado["fecha_fin"])
        self.assertFalse(drill["fecha_fin"].endswith("Z"))


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

        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
        cls.variante = ProductoVariante.objects.create(
            producto=cls.producto, empresa=cls.empresa, color=cls.color, sku="TI-PLA-NEG", precio_base="100",
        )
        cls.variante_2 = ProductoVariante.objects.create(
            producto=cls.producto, empresa=cls.empresa, color=cls.color, talla=cls.talla,
            sku="TI-PLA-NEG-M", precio_base="100",
        )
        cls.componente = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Tela")
        cls.componente_2 = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Hilo")
        cls.variante_produccion = VarianteProductoProduccion.objects.create(
            empresa=cls.empresa, nombre="Muestra propia", sku="MP-TI-1",
        )

        cls.producto_ajeno = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.otra_empresa, nombre="Playera ajena")
        cls.variante_ajena = ProductoVariante.objects.create(
            producto=cls.producto_ajeno, empresa=cls.otra_empresa, color=cls.color,
            sku="TI-AJ-NEG", precio_base="100",
        )
        cls.componente_ajeno = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.otra_empresa, nombre="Tela ajena")
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

    # --- sin ``request`` en el contexto ---

    def test_sin_request_en_el_contexto_rechaza_toda_fk_de_empresa(self):
        # Es la garantía en la que se apoya ``variante-onboarding``: si un
        # llamador olvida pasar el contexto, el serializer falla cerrado en vez
        # de aceptar la FK sin validarla. Los datos son de la PROPIA empresa.
        con_request = {"request": SimpleNamespace(user=self.usuario)}
        casos = {
            "componente": self._insumo(),
            "variante_produccion": self._insumo(
                componente=None, variante_produccion=self.variante_produccion.pk
            ),
        }
        for campo, data in casos.items():
            with self.subTest(campo=campo):
                self.assertTrue(BomDetalleSerializer(data=data, context=con_request).is_valid())
                for contexto in ({}, {"request": None}):
                    serializer = BomDetalleSerializer(data=data, context=contexto)
                    self.assertFalse(serializer.is_valid())
                    self.assertEqual(set(serializer.errors), {campo})

    def test_sin_request_en_el_contexto_rechaza_tambien_el_update(self):
        detalle = self._bom().materia_prima_detalle.get()
        serializer = BomDetalleSerializer(
            detalle, data={"componente": self.componente_2.pk}, partial=True
        )

        self.assertFalse(serializer.is_valid())
        self.assertEqual(set(serializer.errors), {"componente"})

    # --- variante-onboarding de pedidos especiales ---

    def _body_onboarding(self, **insumo):
        return {
            "pedido_detalle_id": self.linea_muestra.pk,
            "materia_prima_detalle": [self._insumo(**insumo)],
        }

    def _con_categoria(self):
        from catalogo.models import CategoriaProducto
        from ventas.models import Cotizacion, CotizacionDetalle
        categoria = CategoriaProducto.objects.create(empresa=self.empresa, nombre="Chamarra", codigo="CHA")
        # 766eb3c (#927): una categoría sin tallas configuradas no acepta ninguna.
        categoria.tallas.add(self.talla)
        cotizacion = Cotizacion.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.pedido.cliente, estatus=3,
        )
        CotizacionDetalle.objects.create(
            cotizacion=cotizacion, producto_nombre_externo="Muestra especial", categoria_producto=categoria,
        )
        Pedido.objects.filter(pk=self.pedido.pk).update(cotizacion=cotizacion)
        return categoria

    def test_detalle_trae_categoria_y_prefijo_de_sku(self):
        self._con_categoria()
        resp = self._client(self.admin).get(f"/api/v1/produccion/pedidos-especiales/{self.pedido.pk}/")
        (linea,) = resp.data["detalles"]
        self.assertEqual(linea["categoria_producto"]["nombre"], "Chamarra")
        self.assertEqual(linea["sku_prefijo"], f"MP{self.linea_muestra.pk}")
        self.assertTrue(linea["tallas"][0]["sku_siguiente"].startswith(f"MP{self.linea_muestra.pk}-"))

    def test_variante_onboarding_alta_catalogo_crea_producto_y_variantes(self):
        categoria = self._con_categoria()
        body = {**self._body_onboarding(), "alta_catalogo": True}
        resp = self._client(self.admin).post(self.url_onboarding, body, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        producto = Producto.objects.get(categoria_producto=categoria)
        self.assertEqual(producto.codigo, "CHA00")
        variante = ProductoVariante.objects.get(producto=producto)
        self.assertEqual(resp.data[0]["sku_catalogo"], variante.sku)
        self.assertTrue(resp.data[0]["aplica_catalogo"])

    def test_variante_onboarding_alta_catalogo_liga_el_bom_al_sku_de_catalogo(self):
        self._con_categoria()
        body = {**self._body_onboarding(), "alta_catalogo": True}
        resp = self._client(self.admin).post(self.url_onboarding, body, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        for vpp in VarianteProductoProduccion.objects.filter(pedido_detalle=self.linea_muestra):
            bom = ListaMaterialBom.objects.get(variante_produccion=vpp)
            self.assertEqual(bom.producto_variante.sku, f"CHA00-{vpp.color.codigo}-{vpp.talla.nombre}".upper())

    def test_alta_catalogo_de_sku_ya_generados(self):
        categoria = self._con_categoria()
        resp = self._client(self.admin).post(self.url_onboarding, self._body_onboarding(), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        url = f"/api/v1/produccion/pedidos-especiales/{self.pedido.pk}/alta-catalogo/"
        resp = self._client(self.admin).post(url, {"pedido_detalle_id": self.linea_muestra.pk}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(all(v["aplica_catalogo"] and v["sku_catalogo"] for v in resp.data))
        self.assertEqual(Producto.objects.filter(categoria_producto=categoria).count(), 1)
        self.assertFalse(ListaMaterialBom.objects.filter(
            variante_produccion__pedido_detalle=self.linea_muestra, producto_variante__isnull=True
        ).exists())
        # El detalle ya lo dice por talla (#767).
        detalle = self._client(self.admin).get(f"/api/v1/produccion/pedidos-especiales/{self.pedido.pk}/")
        talla = detalle.data["detalles"][0]["tallas"][0]
        self.assertTrue(talla["aplica_catalogo"])
        self.assertEqual(talla["sku_catalogo"], resp.data[0]["sku_catalogo"])
        # Sin pendientes: 400.
        resp = self._client(self.admin).post(url, {"pedido_detalle_id": self.linea_muestra.pk}, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_alta_catalogo_sin_categoria_es_400(self):
        self._client(self.admin).post(self.url_onboarding, self._body_onboarding(), format="json")
        url = f"/api/v1/produccion/pedidos-especiales/{self.pedido.pk}/alta-catalogo/"
        resp = self._client(self.admin).post(url, {"pedido_detalle_id": self.linea_muestra.pk}, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("alta_catalogo", resp.data)
        self.assertFalse(Producto.objects.filter(variantes__isnull=False, categoria_producto__isnull=False).exists())

    def _alta_catalogo(self):
        url = f"/api/v1/produccion/pedidos-especiales/{self.pedido.pk}/alta-catalogo/"
        return self._client(self.admin).post(url, {"pedido_detalle_id": self.linea_muestra.pk}, format="json")

    def test_alta_catalogo_pedido_cancelado_es_400(self):
        self._con_categoria()
        self._client(self.admin).post(self.url_onboarding, self._body_onboarding(), format="json")
        Pedido.objects.filter(pk=self.pedido.pk).update(estatus=Pedido.ESTATUS_CANCELADO)
        resp = self._alta_catalogo()
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("pedido", resp.data)
        detalle = self._client(self.admin).get(f"/api/v1/produccion/pedidos-especiales/{self.pedido.pk}/")
        self.assertEqual(detalle.data["estatus"], Pedido.ESTATUS_CANCELADO)

    def test_alta_catalogo_ignora_variantes_inactivas(self):
        self._con_categoria()
        self._client(self.admin).post(self.url_onboarding, self._body_onboarding(), format="json")
        VarianteProductoProduccion.objects.filter(pedido_detalle=self.linea_muestra).update(activo=False)
        resp = self._alta_catalogo()
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("pedido_detalle_id", resp.data)

    def test_detalle_no_ofrece_talla_con_variante_inactiva(self):
        # ``variante-onboarding`` no la regenera (#788): el detalle no la marca pendiente.
        self._client(self.admin).post(self.url_onboarding, self._body_onboarding(), format="json")
        VarianteProductoProduccion.objects.filter(pedido_detalle=self.linea_muestra).update(activo=False)
        resp = self._client(self.admin).get(f"/api/v1/produccion/pedidos-especiales/{self.pedido.pk}/")
        tallas = resp.data["detalles"][0]["tallas"]
        self.assertTrue(all(t["sku_siguiente"] is None for t in tallas if t["cantidad"]))

    def test_alta_catalogo_linea_sin_categoria_la_toma_del_body(self):
        from catalogo.models import CategoriaProducto
        categoria = CategoriaProducto.objects.create(empresa=self.empresa, nombre="Camisola", codigo="CMS")
        categoria.tallas.add(self.talla)
        self._client(self.admin).post(self.url_onboarding, self._body_onboarding(), format="json")
        url = f"/api/v1/produccion/pedidos-especiales/{self.pedido.pk}/alta-catalogo/"
        body = {"pedido_detalle_id": self.linea_muestra.pk, "categoria_producto": categoria.pk}
        resp = self._client(self.admin).post(url, body, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.linea_muestra.refresh_from_db()
        self.assertEqual(self.linea_muestra.categoria_producto_id, categoria.pk)
        self.assertTrue(Producto.objects.filter(categoria_producto=categoria).exists())

    def test_alta_catalogo_categoria_inactiva_es_400(self):
        categoria = self._con_categoria()
        categoria.activo = False
        categoria.save(update_fields=["activo"])
        self._client(self.admin).post(self.url_onboarding, self._body_onboarding(), format="json")
        resp = self._alta_catalogo()
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("inactiva", str(resp.data["alta_catalogo"]))

    def test_alta_catalogo_sku_ganado_por_otra_alta_es_400(self):
        self._con_categoria()
        self._client(self.admin).post(self.url_onboarding, self._body_onboarding(), format="json")
        vpp = VarianteProductoProduccion.objects.filter(pedido_detalle=self.linea_muestra).first()
        ProductoVariante.objects.create(
            producto=self.producto, empresa=self.empresa, color=vpp.color, talla=vpp.talla,
            sku=f"CHA00-{vpp.color.codigo}-{vpp.talla.nombre}".upper(), precio_base="1",
        )
        # El ``exists`` previo no la ve (la otra alta todavía no confirmaba): manda el INSERT.
        # Solo se finge esa verificación (``filter(empresa=, sku=)``): el resto de
        # consultas a ``ProductoVariante`` (p. ej. los contadores de tallas del
        # ``get_queryset`` de pedidos especiales, de6a730) siguen siendo reales.
        sin_choque = mock.Mock(exists=mock.Mock(return_value=False))
        filtro_real = ProductoVariante.objects.filter

        def filtro(*args, **kwargs):
            if not args and set(kwargs) == {"empresa", "sku"}:
                return sin_choque
            return filtro_real(*args, **kwargs)

        with mock.patch.object(ProductoVariante.objects, "filter", side_effect=filtro):
            resp = self._alta_catalogo()
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("ya existe", str(resp.data["alta_catalogo"]))

    def test_variante_onboarding_alta_catalogo_sin_categoria_es_400(self):
        body = {**self._body_onboarding(), "alta_catalogo": True}
        resp = self._client(self.admin).post(self.url_onboarding, body, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("alta_catalogo", resp.data)
        self.assertFalse(VarianteProductoProduccion.objects.filter(pedido_detalle=self.linea_muestra).exists())

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
            set(resp.data[0]), {"id", "sku", "nombre", "talla_nombre", "aplica_catalogo", "sku_catalogo", "pedido_detalle"}
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
        # EC-870 (b70ee1a): editar y dar de baja la OP exige estos permisos; el
        # aislamiento por empresa se prueba con un usuario que sí los tiene.
        _conceder_permisos(cls.usuario, cls.empresa, ["E-PRODUCCION-OP", "D-PRODUCCION-OP"])

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

    def test_sin_permisos_de_op_no_edita_ni_da_de_baja(self):
        # EC-870: sin ``E-/D-PRODUCCION-OP`` no se edita ni se da de baja una OP
        # de la propia empresa (403). La OP de otra empresa con permisos es 404
        # (``test_op_de_otra_empresa_responde_404``).
        sin_permisos = Usuario.objects.create(
            username="ti-sin-op", email="ti-sin-op@acme-ti.test", empresa=self.empresa,
            sucursal_default=self.sucursal,
        )
        propia = self._op(self.pedido)
        cliente = self._client(sin_permisos)

        self.assertEqual(cliente.patch(f"{self.URL}{propia.pk}/", {"prioridad": 4}, format="json").status_code, 403)
        self.assertEqual(cliente.delete(f"{self.URL}{propia.pk}/").status_code, 403)
        propia.refresh_from_db()
        self.assertEqual(propia.prioridad, 1)
        self.assertNotEqual(propia.estatus_op, OrdenProduccion.EstatusOrdenProduccion.CANCELADO)

    def test_validar_renglones_no_consulta_el_pedido_por_renglon(self):
        # Por renglón: variante, unidad y ``pedido_detalle`` (con su pedido en
        # el mismo JOIN). Antes eran 4: la empresa del pedido salía de una
        # consulta perezosa adicional por renglón.
        self.usuario.empresa  # ya cargada, como en un request real
        renglon = self._body(pedido_detalle=self.linea.pk)["orden_produccion_detalle"][0]

        def consultas(n):
            serializer = OrdenProduccionSerializer(
                data={"orden_produccion_detalle": [renglon] * n},
                context={"request": SimpleNamespace(user=self.usuario)},
            )
            with CaptureQueriesContext(connection) as ctx:
                self.assertTrue(serializer.is_valid(), serializer.errors)
            return len(ctx)

        self.assertEqual(consultas(4) - consultas(1), 3 * 3)

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

    def test_patch_sin_pedido_en_el_body_no_revisa_renglones_guardados(self):
        op = self._op_con_renglon(pedido=self.pedido, pedido_detalle=self.linea_otro_pedido)
        resp = self._client().patch(f"{self.URL}{op.pk}/", {"prioridad": 3}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        op.refresh_from_db()
        self.assertEqual(op.prioridad, 3)

    # --- varios pedidos: cambiar el conjunto ``pedidos_ids`` también revisa ---

    def _op_con_pedidos(self, pedidos, pedido_detalle):
        from produccion.models import OrdenProduccionPedido

        op = self._op_con_renglon(pedido=pedidos[0], pedido_detalle=pedido_detalle)
        for pedido in pedidos:
            OrdenProduccionPedido.objects.create(op=op, pedido=pedido)
        return op

    def _pedidos_ligados(self, op):
        return set(op.op_pedidos.values_list("pedido_id", flat=True))

    def test_reducir_pedidos_ids_rechaza_renglones_del_pedido_que_se_quita(self):
        op = self._op_con_pedidos([self.pedido, self.otro_pedido], self.linea_otro_pedido)
        resp = self._client().patch(f"{self.URL}{op.pk}/", {"pedidos_ids": [self.pedido.pk]}, format="json")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("pedido", resp.data)
        self.assertEqual(self._pedidos_ligados(op), {self.pedido.pk, self.otro_pedido.pk})

    def test_cambiar_pedidos_ids_rechaza_renglones_del_pedido_que_sale(self):
        tercero, _ = self._pedido_muestra(self.empresa, self.sucursal, self.cliente)
        op = self._op_con_pedidos([self.pedido, self.otro_pedido], self.linea_otro_pedido)
        resp = self._client().patch(
            f"{self.URL}{op.pk}/", {"pedidos_ids": [self.pedido.pk, tercero.pk]}, format="json"
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("pedido", resp.data)
        self.assertEqual(self._pedidos_ligados(op), {self.pedido.pk, self.otro_pedido.pk})

    def test_mismos_pedidos_ids_no_revisa_renglones_guardados(self):
        # Renglón heredado de un pedido que la OP no tiene: reenviar el mismo
        # conjunto no es un cambio y no bloquea la edición del encabezado.
        op = self._op_con_pedidos([self.pedido], self.linea_otro_pedido)
        resp = self._client().patch(
            f"{self.URL}{op.pk}/", {"pedidos_ids": [self.pedido.pk], "prioridad": 2}, format="json"
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._pedidos_ligados(op), {self.pedido.pk})

    def test_ampliar_pedidos_ids_acepta_renglones_de_los_pedidos_ligados(self):
        op = self._op_con_pedidos([self.pedido], self.linea)
        resp = self._client().patch(
            f"{self.URL}{op.pk}/", {"pedidos_ids": [self.pedido.pk, self.otro_pedido.pk]}, format="json"
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._pedidos_ligados(op), {self.pedido.pk, self.otro_pedido.pk})

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

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # EC-880: el alta pasa por ``_require_produccion``; un admin de la
        # empresa lo cumple sin depender del catálogo de roles.
        cls.admin = Usuario.objects.create(
            username="ti-admin-consumo", email="ti-admin-consumo@acme-ti.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )

    def setUp(self):
        self.consumo = ConsumoProduccion.objects.create(op=self.op)
        self.consumo_ajeno = ConsumoProduccion.objects.create(op=self.op_ajena)

    def test_create_sin_permiso_de_produccion_se_rechaza(self):
        """EC-880: un usuario de la empresa sin el departamento Producción (ni
        admin ni superusuario) ya no crea encabezados de consumo."""
        resp = self._client().post(self.URL, {"op": self.op.pk}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("permiso", resp.data)
        self.assertEqual(ConsumoProduccion.objects.count(), 2)

    def test_create_con_departamento_produccion_se_permite(self):
        """EC-880: el uso real del gate es un operador sin admin ni superusuario
        con un rol del departamento Producción en su empresa."""
        from seguridad.models import Rol, UsuarioRol
        from seguridad.role_identity import CLAVE_DEPARTAMENTO_PRODUCCION
        operador = Usuario.objects.create(
            username="ti-prod-consumo", email="ti-prod-consumo@acme-ti.test", empresa=self.empresa,
            sucursal_default=self.sucursal,
        )
        rol = Rol.objects.create(
            empresa=self.empresa, codigo="PRODUCCION-TI", nombre="Producción",
            clave_departamento=CLAVE_DEPARTAMENTO_PRODUCCION,
        )
        UsuarioRol.objects.create(usuario=operador, rol=rol, empresa=self.empresa)

        resp = self._client(operador).post(self.URL, {"op": self.op.pk}, format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(ConsumoProduccion.objects.count(), 3)

    def test_create_con_op_propia_conserva_el_shape(self):
        resp = self._client(self.admin).post(self.URL, {"op": self.op.pk}, format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(set(resp.data), {"consumo_produccion_id", "op", "detalles"})
        self.assertEqual((resp.data["op"], resp.data["detalles"]), (self.op.pk, []))

    def test_create_rechaza_op_de_otra_empresa(self):
        resp = self._client(self.admin).post(self.URL, {"op": self.op_ajena.pk}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("op", resp.data)
        self.assertEqual(ConsumoProduccion.objects.count(), 2)
        self._sin_efectos_secundarios()

    def test_op_ajena_e_inexistente_responden_igual(self):
        inexistente_pk = self.op_ajena.pk + 999
        client = self._client(self.admin)
        ajeno = client.post(self.URL, {"op": self.op_ajena.pk}, format="json")
        inexistente = client.post(self.URL, {"op": inexistente_pk}, format="json")
        self.assertEqual((ajeno.status_code, inexistente.status_code), (400, 400))
        self.assertEqual(
            str(ajeno.data["op"][0]).replace(str(self.op_ajena.pk), "X"),
            str(inexistente.data["op"][0]).replace(str(inexistente_pk), "X"),
        )

    def test_list_excluye_registros_de_otra_empresa(self):
        resp = self._client().get(self.URL)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([fila["consumo_produccion_id"] for fila in resp.data], [self.consumo.pk])

    def test_retrieve_propio_200_y_ajeno_404(self):
        self.assertEqual(self._client().get(f"{self.URL}{self.consumo.pk}/").status_code, 200)
        self.assertEqual(self._client().get(f"{self.URL}{self.consumo_ajeno.pk}/").status_code, 404)

    def test_confirmar_y_anular_ya_no_existen(self):
        # #1124 (271508f): eran stubs que respondían 200 sin hacer nada y se
        # quitaron. No hay ruta ni para lo propio ni para lo ajeno, y no cambia
        # nada; el detalle ajeno sigue en 404 (la única acción de detalle que queda).
        for registro in (self.consumo, self.consumo_ajeno):
            antes = ConsumoProduccion.objects.filter(pk=registro.pk).values().get()
            for accion in ("confirmar", "anular"):
                with self.subTest(registro=registro.pk, accion=accion):
                    resp = self._client().post(f"{self.URL}{registro.pk}/{accion}/")
                    self.assertEqual(resp.status_code, 404, accion)
            self.assertEqual(ConsumoProduccion.objects.filter(pk=registro.pk).values().get(), antes)
        self.assertEqual(self._client().get(f"{self.URL}{self.consumo_ajeno.pk}/").status_code, 404)

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

    def test_validar_no_consulta_el_almacen_de_la_ubicacion_aparte(self):
        # Una consulta por FK (op, almacén y ubicación con su almacén en JOIN).
        self.usuario.empresa
        serializer = ProductoTerminadoEntradasSerializer(
            data=self._body(), context={"request": SimpleNamespace(user=self.usuario)}
        )
        with self.assertNumQueries(3):
            self.assertTrue(serializer.is_valid(), serializer.errors)

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

    def test_confirmar_y_anular_ya_no_existen(self):
        # #1124 (271508f): eran stubs que respondían 200 sin hacer nada y se
        # quitaron. No hay ruta ni para lo propio ni para lo ajeno, y no cambia
        # nada; el detalle ajeno sigue en 404 (la única acción de detalle que queda).
        for registro in (self.entrada, self.entrada_ajena):
            antes = ProductoTerminadoEntradas.objects.filter(pk=registro.pk).values().get()
            for accion in ("confirmar", "anular"):
                with self.subTest(registro=registro.pk, accion=accion):
                    resp = self._client().post(f"{self.URL}{registro.pk}/{accion}/")
                    self.assertEqual(resp.status_code, 404, accion)
            self.assertEqual(ProductoTerminadoEntradas.objects.filter(pk=registro.pk).values().get(), antes)
        self.assertEqual(self._client().get(f"{self.URL}{self.entrada_ajena.pk}/").status_code, 404)

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


class VarianteOnboardingColorTests(_BomTenantBase, TestCase):
    """``variante-onboarding`` acepta ``color`` en el body: obligatorio si la
    línea de muestra no tiene color (se guarda en la línea, en la misma
    transacción que variantes y BOM) y, si ya lo tiene, sólo puede coincidir."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = Usuario.objects.create(
            username="vc-admin", email="vc-admin@acme-ti.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.otro_color = Color.objects.create(nombre="Rojo", codigo="ROJ", codigo_hex="#ff0000")
        cls.color_inactivo = Color.objects.create(
            nombre="Viejo", codigo="VIE", codigo_hex="#111111", activo=False
        )
        cls.color_sin_codigo = Color.objects.create(nombre="Sin código", codigo="", codigo_hex="#222222")
        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente VC")
        cls.pedido = Pedido.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, cliente=cliente, moneda=moneda,
            persona_pagos="Pagos", correo_facturas="pagos@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
            clasificacion="B", fecha_confirmacion=timezone.now(),
        )
        cls.url = f"/api/v1/produccion/pedidos-especiales/{cls.pedido.pk}/variante-onboarding/"

    def _linea(self, color=None):
        linea = PedidoDetalle.objects.create(
            pedido=self.pedido, producto_nombre_externo="Muestra especial", color=color,
        )
        PedidoDetalleTalla.objects.create(pedido_detalle=linea, talla=self.talla, cantidad=5)
        return linea

    def _post(self, linea, **extra):
        body = {"pedido_detalle_id": linea.pk, "materia_prima_detalle": [self._insumo()], **extra}
        return self._client(self.admin).post(self.url, body, format="json")

    def _nada_creado(self, linea, color_id=None):
        linea.refresh_from_db()
        self.assertEqual(linea.color_id, color_id)
        self.assertFalse(VarianteProductoProduccion.objects.filter(pedido_detalle=linea).exists())
        self.assertFalse(ListaMaterialBom.objects.exists())
        self.assertFalse(BomDetalle.objects.exists())

    def test_linea_sin_color_con_color_en_el_body(self):
        linea = self._linea()
        resp = self._post(linea, color=self.otro_color.pk)

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(
            set(resp.data[0]), {"id", "sku", "nombre", "talla_nombre", "aplica_catalogo", "sku_catalogo", "pedido_detalle"}
        )
        self.assertEqual(resp.data[0]["sku"], f"MP{linea.pk}-ROJ-M")
        linea.refresh_from_db()
        self.assertEqual(linea.color_id, self.otro_color.pk)
        variante = VarianteProductoProduccion.objects.get(pedido_detalle=linea)
        self.assertEqual(variante.color_id, self.otro_color.pk)
        self.assertEqual(BomDetalle.objects.filter(bom__variante_produccion=variante).count(), 1)

    def test_linea_sin_color_y_sin_color_en_el_body(self):
        linea = self._linea()
        for extra in ({}, {"color": None}):
            resp = self._post(linea, **extra)
            self.assertEqual(resp.status_code, 400, resp.data)
            self.assertEqual(set(resp.data), {"color"})
        self._nada_creado(linea)

    def test_linea_con_color_omitiendo_el_del_body(self):
        linea = self._linea(self.color)
        resp = self._post(linea)

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data[0]["sku"], f"MP{linea.pk}-NEG-M")
        linea.refresh_from_db()
        self.assertEqual(linea.color_id, self.color.pk)

    def test_linea_con_color_y_el_mismo_en_el_body(self):
        linea = self._linea(self.color)
        resp = self._post(linea, color=self.color.pk)

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data[0]["sku"], f"MP{linea.pk}-NEG-M")

    def test_linea_con_color_y_otro_distinto_en_el_body(self):
        linea = self._linea(self.color)
        resp = self._post(linea, color=self.otro_color.pk)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(set(resp.data), {"color"})
        self._nada_creado(linea, self.color.pk)

    def test_color_inexistente_inactivo_o_sin_codigo(self):
        linea = self._linea()
        inexistente = Color.objects.order_by("-pk").first().pk + 999
        for color in (inexistente, self.color_inactivo.pk, self.color_sin_codigo.pk, "abc"):
            with self.subTest(color=color):
                resp = self._post(linea, color=color)
                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertEqual(set(resp.data), {"color"})
        self._nada_creado(linea)

    def test_componente_invalido_no_guarda_el_color(self):
        linea = self._linea()
        body = {
            "pedido_detalle_id": linea.pk, "color": self.otro_color.pk,
            "materia_prima_detalle": [self._insumo(componente=self.componente_ajeno.pk)],
        }
        resp = self._client(self.admin).post(self.url, body, format="json")

        self.assertEqual(resp.status_code, 400, resp.data)
        self._nada_creado(linea)

    def test_fallo_a_media_transaccion_revierte_el_color(self):
        # Falla DESPUÉS de validar y de guardar el color (dentro del atomic).
        linea = self._linea()
        client = self._client(self.admin)
        client.raise_request_exception = False
        with mock.patch(
            "produccion.api.views.BomDetalle.objects.bulk_create", side_effect=RuntimeError("boom")
        ):
            resp = client.post(
                self.url,
                {"pedido_detalle_id": linea.pk, "color": self.otro_color.pk,
                 "materia_prima_detalle": [self._insumo()]},
                format="json",
            )

        self.assertEqual(resp.status_code, 500)
        self._nada_creado(linea)

    def test_linea_que_ya_tiene_sku_no_cambia_de_color(self):
        linea = self._linea()
        self.assertEqual(self._post(linea, color=self.otro_color.pk).status_code, 201)
        resp = self._post(linea, color=self.otro_color.pk)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(VarianteProductoProduccion.objects.filter(pedido_detalle=linea).count(), 1)

    # --- largo de ``VarianteProductoProduccion.nombre`` ---
    # SQLite no aplica el max_length; Postgres sí (DataError -> 500). Por eso
    # los largos se afirman explícitamente.

    MAX_NOMBRE = VarianteProductoProduccion._meta.get_field("nombre").max_length

    def _onboarding_con_nombre(self, nombre):
        linea = self._linea(self.color)
        PedidoDetalleTalla.objects.create(
            pedido_detalle=linea, talla=Talla.objects.create(nombre="G"), cantidad=2
        )
        PedidoDetalle.objects.filter(pk=linea.pk).update(producto_nombre_externo=nombre)
        resp = self._post(linea)
        self.assertEqual(resp.status_code, 201, resp.data)
        variantes = list(VarianteProductoProduccion.objects.filter(pedido_detalle=linea).order_by("pk"))
        self.assertEqual(len(variantes), 2)
        self.assertEqual([v["nombre"] for v in resp.data], [v.nombre for v in variantes])
        return linea, variantes

    def test_nombre_externo_mas_largo_que_el_campo_se_trunca(self):
        nombre = "A" * (self.MAX_NOMBRE + 80)
        linea, variantes = self._onboarding_con_nombre(nombre)

        for variante in variantes:
            self.assertEqual(len(variante.nombre), self.MAX_NOMBRE)
            self.assertEqual(variante.nombre, nombre[: self.MAX_NOMBRE])
        # El nombre no lleva sufijo de talla: lo que distingue a cada variante
        # es su SKU, que no se toca.
        self.assertEqual({v.sku for v in variantes}, {f"MP{linea.pk}-NEG-M", f"MP{linea.pk}-NEG-G"})
        linea.refresh_from_db()
        self.assertEqual(linea.producto_nombre_externo, nombre)

    def test_truncado_no_deja_espacios_ni_separadores_colgando(self):
        base = "B" * (self.MAX_NOMBRE - 3)
        _, variantes = self._onboarding_con_nombre(base + " - CON REFLEJANTE PLATA")

        for variante in variantes:
            self.assertEqual(variante.nombre, base)

    def test_nombre_externo_en_el_limite_o_corto_no_cambia(self):
        for nombre in ("C" * self.MAX_NOMBRE, "Gorra bordada muestra"):
            with self.subTest(largo=len(nombre)):
                _, variantes = self._onboarding_con_nombre(nombre)
                for variante in variantes:
                    self.assertEqual(variante.nombre, nombre)

    def test_nombre_corto_que_termina_en_separador_se_copia_igual(self):
        # El recorte de separadores sólo aplica cuando hubo truncado.
        for nombre in ("Playera modelo A.", "Pantalón 40/", "Gorra - ", " - "):
            with self.subTest(nombre=nombre):
                _, variantes = self._onboarding_con_nombre(nombre)
                for variante in variantes:
                    self.assertEqual(variante.nombre, nombre)

    # --- orden de validaciones y carrera entre dos altas ---

    def test_reintento_con_otro_color_responde_primero_que_ya_tiene_sku(self):
        linea = self._linea()
        self.assertEqual(self._post(linea, color=self.otro_color.pk).status_code, 201)
        resp = self._post(linea, color=self.color.pk)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(set(resp.data), {"pedido_detalle_id"})
        self.assertIn("ya tiene SKU", str(resp.data["pedido_detalle_id"]))

    def test_reintento_genera_sku_solo_de_las_tallas_que_faltan(self):
        linea = self._linea()
        self.assertEqual(self._post(linea, color=self.color.pk).status_code, 201)
        nueva = Talla.objects.create(nombre="XG-inc")
        PedidoDetalleTalla.objects.create(pedido_detalle=linea, talla=nueva, cantidad=3)

        resp = self._post(linea)

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(
            VarianteProductoProduccion.objects.filter(pedido_detalle=linea).count(), 2
        )
        self.assertEqual(self._post(linea).status_code, 400)

    def _post_con_integrity_error(self, linea, mensaje):
        client = self._client(self.admin)
        client.raise_request_exception = False
        with mock.patch(
            "produccion.api.views.VarianteProductoProduccion.objects.create",
            side_effect=IntegrityError(mensaje),
        ):
            return client.post(
                self.url,
                {"pedido_detalle_id": linea.pk, "color": self.otro_color.pk,
                 "materia_prima_detalle": [self._insumo()]},
                format="json",
            )

    def test_choque_de_unicidad_de_la_variante_responde_400_como_ya_tiene_sku(self):
        # Lo que vería la alta que pierde una carrera si el candado de fila no
        # bastara: el índice único (pedido_detalle, talla) rechaza su INSERT.
        linea = self._linea()
        resp = self._post_con_integrity_error(
            linea,
            'duplicate key value violates unique constraint "uq_variante_produccion_pedido_detalle_talla"',
        )

        self.assertEqual(resp.status_code, 400, getattr(resp, "data", None))
        self.assertEqual(set(resp.data), {"pedido_detalle_id"})
        self.assertIn("ya tiene SKU", str(resp.data["pedido_detalle_id"]))
        self._nada_creado(linea)

    def test_otro_error_de_integridad_no_se_disfraza_de_400(self):
        linea = self._linea()
        resp = self._post_con_integrity_error(linea, 'null value in column "empresa_id"')

        self.assertEqual(resp.status_code, 500)
        self._nada_creado(linea)

    # --- reglas de color: inactivo y sin ``codigo`` ---

    def test_mismo_color_inactivo_de_la_linea_se_acepta(self):
        # El color de la línea se desactivó después de capturar el pedido:
        # reenviarlo no puede fallar si omitirlo funciona.
        linea = self._linea(self.color_inactivo)
        resp = self._post(linea, color=self.color_inactivo.pk)

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data[0]["sku"], f"MP{linea.pk}-VIE-M")

    def test_color_inactivo_distinto_al_de_la_linea_se_rechaza(self):
        con_color = self._linea(self.color)
        sin_color = self._linea()
        for linea, color_id in ((con_color, self.color.pk), (sin_color, None)):
            with self.subTest(color_de_la_linea=color_id):
                resp = self._post(linea, color=self.color_inactivo.pk)
                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertEqual(set(resp.data), {"color"})
                self._nada_creado(linea, color_id)

    def test_color_del_body_sin_codigo_responde_en_color(self):
        linea = self._linea()
        resp = self._post(linea, color=self.color_sin_codigo.pk)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(set(resp.data), {"color"})
        self.assertIn("color seleccionado no tiene código", str(resp.data["color"]))
        self._nada_creado(linea)

    def test_color_de_la_linea_sin_codigo_responde_en_pedido_detalle_id(self):
        linea = self._linea(self.color_sin_codigo)
        for extra in ({}, {"color": self.color_sin_codigo.pk}):
            with self.subTest(body=extra):
                resp = self._post(linea, **extra)
                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertEqual(set(resp.data), {"pedido_detalle_id"})
                self.assertIn("catálogo de colores", str(resp.data["pedido_detalle_id"]))
        self._nada_creado(linea, self.color_sin_codigo.pk)

    def test_otro_color_no_reemplaza_al_de_la_linea_aunque_no_tenga_codigo(self):
        # Decisión: el color que capturó ventas no se sobrescribe desde aquí.
        linea = self._linea(self.color_sin_codigo)
        resp = self._post(linea, color=self.otro_color.pk)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(set(resp.data), {"color"})
        self._nada_creado(linea, self.color_sin_codigo.pk)

    # --- pedido cancelado ---

    def test_pedido_cancelado_rechaza_el_alta_sin_escribir_nada(self):
        linea = self._linea()
        Pedido.objects.filter(pk=self.pedido.pk).update(estatus=Pedido.ESTATUS_CANCELADO)
        resp = self._post(linea, color=self.otro_color.pk)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(set(resp.data), {"pedido"})
        self._nada_creado(linea)
        # El GET no cambia: el pedido cancelado sigue visible para producción.
        detalle = self._client(self.admin).get(
            f"/api/v1/produccion/pedidos-especiales/{self.pedido.pk}/"
        )
        self.assertEqual(detalle.status_code, 200)

    def test_pedido_en_cualquier_otro_estatus_permite_el_alta(self):
        for estatus, _ in Pedido.CHOICES_ESTATUS:
            if estatus == Pedido.ESTATUS_CANCELADO:
                continue
            with self.subTest(estatus=estatus):
                Pedido.objects.filter(pk=self.pedido.pk).update(estatus=estatus)
                resp = self._post(self._linea(), color=self.otro_color.pk)
                self.assertEqual(resp.status_code, 201, resp.data)


class RecalculoConsumoTelaTests(TestCase):
    """``POST /ordenes-produccion/{id}/recalcular-consumos/``: ajusta los
    consumos a las piezas realmente cortadas, solo por diferencia y sin
    pólizas."""

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme-rc", razon_social="acme-rc SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="ARC", nombre="acme-rc")
        cls.almacen = Almacen.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, codigo="ALM", nombre="Almacen",
        )
        cls.usuario = Usuario.objects.create(
            username="a@acme-rc.test", email="a@acme-rc.test", empresa=cls.empresa, is_admin_empresa=True,
        )
        cls.op = OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-RC-1",
        )
        um = UnidadMedida.objects.create(clave="MTR", nombre="Metro")
        bom = ListaMaterialBom.objects.create(empresa=cls.empresa)
        OrdenProduccionDetalle.objects.create(op=cls.op, bom=bom, cantidad=Decimal("90"), unidad=um)
        cls.tela = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Algodon")
        cls.hilo = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Hilo")

    def setUp(self):
        self.existencia = Existencia.objects.create(
            producto=self.tela, almacen=self.almacen, cantidad=Decimal("500"),
        )
        self.ex_hilo = Existencia.objects.create(producto=self.hilo, almacen=self.almacen, cantidad=Decimal("50"))
        consumos = OrdenProduccionService._discount_existencias(
            [
                {"producto": self.tela, "cantidad": Decimal("90")},
                {"producto": self.hilo, "cantidad": Decimal("9")},
            ],
            self.empresa, self.sucursal,
        )
        OrdenProduccionService._registrar_consumo(self.op, consumos)
        OrdenProduccionService._registrar_movimiento_inventario(self.op, self.usuario, consumos)
        self.client = APIClient()
        self.client.force_authenticate(self.usuario)

    def _post(self, cantidad=None, productos=None, op=None):
        body = {}
        if cantidad is not None:
            body["cantidad_real_corte"] = cantidad
        body["productos"] = [p.pk for p in (productos if productos is not None else [self.tela])]
        return self.client.post(
            f"/api/v1/produccion/orden-produccion/{(op or self.op).pk}/recalcular-consumos/",
            body, format="json",
        )

    def _total(self, producto):
        from django.db.models import Sum
        from produccion.models import ConsumoProduccionDetalle
        return ConsumoProduccionDetalle.objects.filter(
            consumo_produccion__op=self.op, producto=producto
        ).aggregate(t=Sum("cantidad"))["t"]

    def test_mas_piezas_escala_la_tela_y_descuenta_la_diferencia(self):
        resp = self._post(100)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.existencia.refresh_from_db()
        self.ex_hilo.refresh_from_db()
        self.assertEqual(self.existencia.cantidad, Decimal("400"))
        self.assertEqual(self._total(self.tela), Decimal("100"))
        # El hilo no es tela: no se toca.
        self.assertEqual(self._total(self.hilo), Decimal("9"))
        self.assertEqual(self.ex_hilo.cantidad, Decimal("41"))
        mov = MovimientoInventario.objects.filter(op=self.op).order_by("-id").first()
        self.assertEqual(mov.tipo_movimiento, "SALIDA")
        self.assertEqual(list(mov.detalles.values_list("cantidad", flat=True)), [Decimal("10")])
        self.assertEqual(
            OrdenProduccionRutaCritica.objects.get(op=self.op).cantidad_real_corte, Decimal("100")
        )

    # --- EC-821: ``detalle.almacen`` con el almacén de la existencia ---------------

    def _ultimo_detalle(self):
        mov = MovimientoInventario.objects.filter(op=self.op).order_by("-id").first()
        return mov.tipo_movimiento, list(mov.detalles.values_list("almacen_id", "ubicacion_origen_id", "ubicacion_destino_id"))

    def test_recalculo_sin_ubicacion_llena_el_almacen_en_salida_y_entrada(self):
        self.assertEqual(self._post(100).status_code, 200)
        self.assertEqual(self._ultimo_detalle(), ("SALIDA", [(self.almacen.pk, None, None)]))
        self.assertEqual(self._post(95).status_code, 200)
        self.assertEqual(self._ultimo_detalle(), ("ENTRADA", [(self.almacen.pk, None, None)]))

    def test_recalculo_con_ubicacion_tambien_llena_el_almacen(self):
        ubicacion = Ubicacion.objects.create(almacen=self.almacen, pasillo="1")
        Existencia.objects.filter(pk=self.existencia.pk).update(ubicacion=ubicacion)
        self.assertEqual(self._post(100).status_code, 200)
        self.assertEqual(self._ultimo_detalle(), ("SALIDA", [(self.almacen.pk, ubicacion.pk, None)]))
        self.assertEqual(self._post(95).status_code, 200)
        self.assertEqual(self._ultimo_detalle(), ("ENTRADA", [(self.almacen.pk, None, ubicacion.pk)]))

    def test_sin_telas_es_400(self):
        resp = self.client.post(
            f"/api/v1/produccion/orden-produccion/{self.op.pk}/recalcular-consumos/",
            {"cantidad_real_corte": 100}, format="json",
        )
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(
            f"/api/v1/produccion/orden-produccion/{self.op.pk}/recalcular-consumos/",
            {"cantidad_real_corte": 100, "productos": []}, format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_varias_telas(self):
        self.assertEqual(self._post(100, productos=[self.tela, self.hilo]).status_code, 200)
        self.assertEqual(self._total(self.tela), Decimal("100"))
        self.assertEqual(self._total(self.hilo), Decimal("10"))

    def test_planeado_es_el_consumo_inicial_aun_con_encabezado_vacio_y_recalculos(self):
        """EC-880: la regla compartida (``consumo_resumen``) da lo mismo que la
        anterior para los materiales del consumo inicial: escala sobre lo
        planeado (90), no sobre lo acumulado, y un encabezado vacío no cuenta."""
        ConsumoProduccion.objects.create(op=self.op)  # alta vacía vía POST /consumo/
        self.assertEqual(self._post(100).status_code, 200)
        self.assertEqual(self._total(self.tela), Decimal("100"))
        self.assertEqual(self._post(110).status_code, 200)
        self.assertEqual(self._total(self.tela), Decimal("110"))
        self.assertEqual(self._post(100).status_code, 200)
        self.assertEqual(self._total(self.tela), Decimal("100"))
        self.assertEqual(self._total(self.hilo), Decimal("9"))

    def test_material_sin_planeado_se_rechaza_sin_mover_inventario(self):
        """EC-880: un material que sólo está en un ajuste (planeado 0) daría
        objetivo 0 y devolvería todo lo consumido; se rechaza con 400 y su nombre."""
        from produccion.models import ConsumoProduccionDetalle
        boton = Producto.objects.create(tipo=_tipo_producto(), empresa=self.empresa, nombre="Boton")
        ajuste = ConsumoProduccion.objects.create(op=self.op)
        ConsumoProduccionDetalle.objects.create(consumo_produccion=ajuste, producto=boton, cantidad=Decimal("3"))
        movimientos = MovimientoInventario.objects.count()

        resp = self._post(100, productos=[self.tela, boton])

        self.assertEqual(resp.status_code, 400)
        self.assertIn("Boton", str(resp.data["productos"]))
        self.assertEqual(MovimientoInventario.objects.count(), movimientos)
        self.assertEqual(self._total(boton), Decimal("3"))
        self.assertEqual(self._total(self.tela), Decimal("90"))

    def test_producto_fuera_del_consumo_se_rechaza(self):
        otro = Producto.objects.create(tipo=_tipo_producto(), empresa=self.empresa, nombre="Boton")
        self.assertEqual(self._post(100, productos=[otro]).status_code, 400)

    def test_sin_cantidad_usa_la_de_ruta_critica(self):
        OrdenProduccionRutaCritica.objects.create(op=self.op, cantidad_real_corte=Decimal("100"))
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(self._total(self.tela), Decimal("100"))

    def test_sin_cantidad_en_ningun_lado_es_400(self):
        self.assertEqual(self._post().status_code, 400)

    def test_repetir_con_el_mismo_valor_no_hace_nada(self):
        self._post(100)
        n = MovimientoInventario.objects.filter(op=self.op).count()
        self.assertEqual(self._post(100).status_code, 200)
        self.assertEqual(MovimientoInventario.objects.filter(op=self.op).count(), n)

    def test_segundo_recalculo_hacia_abajo_regresa_a_inventario(self):
        self._post(100)
        self.assertEqual(self._post(95).status_code, 200)
        self.existencia.refresh_from_db()
        self.assertEqual(self.existencia.cantidad, Decimal("405"))
        self.assertEqual(self._total(self.tela), Decimal("95"))

    def test_menos_de_lo_planeado_se_rechaza(self):
        self.assertEqual(self._post(80).status_code, 400)
        self.existencia.refresh_from_db()
        self.assertEqual(self.existencia.cantidad, Decimal("410"))

    def test_sin_existencia_suficiente_no_cambia_nada(self):
        self.assertEqual(self._post(1000).status_code, 400)
        self.assertEqual(self._total(self.tela), Decimal("90"))
        self.assertFalse(OrdenProduccionRutaCritica.objects.filter(op=self.op).exists())

    def test_op_cerrada_se_rechaza(self):
        OrdenProduccion.objects.filter(pk=self.op.pk).update(
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.COMPLETADO
        )
        self.assertEqual(self._post(100).status_code, 400)

    def test_op_de_otra_empresa_es_404(self):
        otra = Empresa.objects.create(codigo="otra-rc", razon_social="otra SA")
        suc = Sucursal.objects.create(empresa=otra, codigo="ORC", nombre="otra")
        op = OrdenProduccion.objects.create(empresa=otra, sucursal=suc, folio_op="OP-RC-X")
        self.assertEqual(self._post(100, op=op).status_code, 404)

    def test_no_genera_polizas(self):
        from finanzas.models import Poliza
        antes = Poliza.objects.count()
        self._post(100)
        self.assertEqual(Poliza.objects.count(), antes)


class OrdenBordadoOnboardingSkuTests(OrdenBordadoParcialidadesTests):
    """GET onboarding de OB: oculta muestras sin SKU y publica SKU/Proscai/existencias."""

    def test_oculta_muestra_sin_sku_y_publica_sku_proscai_existencias(self):
        from catalogo.models import Color, ProductoVariante
        from inventarios.models import Almacen, Existencia

        color = Color.objects.create(nombre="Marino")
        variante = ProductoVariante.objects.create(
            producto=self.producto, empresa=self.empresa, color=color,
            talla=self.talla_ch, sku="PLY-CH-MAR", cod_proscai="PRO-1", precio_base=1,
        )
        almacen = Almacen.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, codigo="A1", nombre="Alm", tipo_almacen="PT",
        )
        Existencia.objects.create(almacen=almacen, producto_variante=variante, cantidad=7)
        Existencia.objects.create(almacen=almacen, producto_variante=variante, cantidad=3)
        PedidoDetalleTalla.objects.filter(pk=self.pdt_ch.pk).update(
            variante=variante, sku_muestra=None
        )
        # La talla M pasa a una línea de MUESTRA real (sin producto) y sin SKU.
        muestra = PedidoDetalle.objects.create(
            pedido=self.pedido, producto=None, producto_nombre_externo="Muestra"
        )
        PedidoDetalleTalla.objects.filter(pk=self.pdt_m.pk).update(
            pedido_detalle=muestra, sku_muestra=None
        )

        lineas = self._lineas_del_get()

        self.assertEqual(list(lineas), [self.talla_ch.pk])  # la muestra sin SKU no sale
        linea = lineas[self.talla_ch.pk]
        self.assertEqual(linea["sku"], "PLY-CH-MAR")
        self.assertEqual(linea["sku_proscai"], "PRO-1")
        self.assertEqual(linea["existencias"], 10.0)


    def test_existencias_solo_cuentan_almacenes_pt_activos_de_las_sucursales_del_usuario(self):
        """#518: MP, inactivos y otras sucursales no suman."""
        from catalogo.models import Color, ProductoVariante
        from inventarios.models import Almacen, Existencia

        variante = ProductoVariante.objects.create(
            producto=self.producto, empresa=self.empresa, color=Color.objects.create(nombre="Gris"),
            talla=self.talla_ch, sku="PLY-CH-GRI", precio_base=1,
        )
        PedidoDetalleTalla.objects.filter(pk=self.pdt_ch.pk).update(variante=variante)
        otra_suc = Sucursal.objects.create(empresa=self.empresa, codigo="OTR", nombre="Otra")

        def alm(codigo, **kw):
            kw.setdefault("sucursal", self.sucursal)
            kw.setdefault("tipo_almacen", "PT")
            a = Almacen.objects.create(empresa=self.empresa, codigo=codigo, nombre=codigo, **kw)
            Existencia.objects.create(almacen=a, producto_variante=variante, cantidad=5)
            return a

        alm("OK")                                   # cuenta
        alm("MP", tipo_almacen="MP")                # no: materia prima
        alm("CUA", tipo_almacen="CUARENTENA")       # no: cuarentena
        alm("INA", estatus="INACTIVO")              # no: inactivo
        alm("OTR", sucursal=otra_suc)               # no: sucursal fuera de alcance

        linea = self._lineas_del_get()[self.talla_ch.pk]

        self.assertEqual(linea["existencias"], 5.0)

    def test_sku_proscai_sale_del_producto_cuando_la_variante_no_lo_tiene(self):
        """#517: el dato real vive en ``Producto.cod_proscai``."""
        from catalogo.models import Color, ProductoVariante

        Producto.objects.filter(pk=self.producto.pk).update(cod_proscai="PROD-PRO-9")
        variante = ProductoVariante.objects.create(
            producto=self.producto, empresa=self.empresa, color=Color.objects.create(nombre="Rojo"),
            talla=self.talla_ch, sku="PLY-CH-ROJ", precio_base=1,
        )
        PedidoDetalleTalla.objects.filter(pk=self.pdt_ch.pk).update(variante=variante)

        linea = self._lineas_del_get()[self.talla_ch.pk]

        self.assertEqual(linea["sku_proscai"], "PROD-PRO-9")


class SkuProduccionVacioTests(TestCase):
    """#440: producción y ventas coinciden en "sku vacío = sin SKU" y en qué es muestra."""

    def test_sku_vacio_sale_como_none_en_pedidos_especiales(self):
        from types import SimpleNamespace
        from produccion.api.serializers import PedidoDetalleTallaEspecialSerializer

        serializer = PedidoDetalleTallaEspecialSerializer()
        self.assertIsNone(serializer.get_sku_produccion(SimpleNamespace(variante_produccion=SimpleNamespace(sku=""))))
        self.assertEqual(
            serializer.get_sku_produccion(SimpleNamespace(variante_produccion=SimpleNamespace(sku="MP1"))), "MP1"
        )

    def test_una_sola_definicion_de_muestra(self):
        from ventas.services.sku_produccion_service import nombre_es_muestra

        self.assertFalse(nombre_es_muestra(None))
        self.assertFalse(nombre_es_muestra(""))
        self.assertTrue(nombre_es_muestra("Gorra"))
        self.assertTrue(nombre_es_muestra("  "))


class RutaCriticaFechasSelladasTests(TestCase):
    """#425: la fecha acompaña al checkbox en el modelo, no solo en el serializer."""

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme-rcf", razon_social="acme-rcf SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="RCF", nombre="acme-rcf")
        cls.usuario = Usuario.objects.create(
            username="a@acme-rcf.test", email="a@acme-rcf.test", empresa=cls.empresa, is_admin_empresa=True,
        )
        cls.op = OrdenProduccion.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-RCF-1")

    def _patch(self, **datos):
        client = APIClient()
        client.force_authenticate(self.usuario)
        return client.patch(
            f"/api/v1/produccion/orden-produccion/{self.op.pk}/ruta-critica/", datos, format="json"
        )

    def test_kit_completo_sella_limpia_y_no_cambia_al_reenviar(self):
        resp = self._patch(kit_completo=True)
        self.assertEqual(resp.status_code, 200, resp.data)
        sellada = OrdenProduccionRutaCritica.objects.get(op=self.op).fecha_kit_completo
        self.assertIsNotNone(sellada)

        self._patch(kit_completo=True)
        self.assertEqual(OrdenProduccionRutaCritica.objects.get(op=self.op).fecha_kit_completo, sellada)

        self._patch(kit_completo=False)
        self.assertIsNone(OrdenProduccionRutaCritica.objects.get(op=self.op).fecha_kit_completo)

    def test_fecha_enviada_por_el_cliente_se_ignora(self):
        self._patch(fecha_kit_completo="2020-01-01T00:00:00Z")
        self.assertIsNone(OrdenProduccionRutaCritica.objects.get(op=self.op).fecha_kit_completo)

    def test_fecha_llegada_centro_confeccion_se_lee_escribe_y_limpia(self):
        resp = self._patch(fecha_llegada_centro_confeccion="2026-10-20")
        self.assertEqual(resp.data["fecha_llegada_centro_confeccion"], "2026-10-20")
        resp = self._patch(fecha_llegada_centro_confeccion=None)
        self.assertIsNone(resp.data["fecha_llegada_centro_confeccion"])

    def test_escritura_por_orm_tambien_sella(self):
        ruta = OrdenProduccionRutaCritica.objects.create(op=self.op, kit_completo=True)
        self.assertIsNotNone(ruta.fecha_kit_completo)

    def test_renglon_anterior_sin_fecha_no_se_sella_en_silencio(self):
        OrdenProduccionRutaCritica.objects.bulk_create([OrdenProduccionRutaCritica(op=self.op, kit_completo=True)])
        ruta = OrdenProduccionRutaCritica.objects.get(op=self.op)
        ruta.fecha_trazo = date(2026, 10, 1)
        ruta.save()
        self.assertIsNone(OrdenProduccionRutaCritica.objects.get(op=self.op).fecha_kit_completo)

    def test_op_cerrada_responde_409_con_mensaje_neutro(self):
        OrdenProduccion.objects.filter(pk=self.op.pk).update(estatus_op=OrdenProduccion.EstatusOrdenProduccion.COMPLETADO)
        resp = self._patch(kit_completo=True)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("estatus «Completado»", resp.data["msg"])


class BordadoAvancesRegresionesTests(TestCase):
    """Regresiones de #521 (sucursal), #522 (filtro ?ob=) y #523 (PATCH legacy)."""

    URL = "/api/v1/produccion/bordado-avances/"

    @classmethod
    def setUpTestData(cls):
        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.mty = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="MTY")
        cls.gdl = Sucursal.objects.create(empresa=cls.empresa, codigo="GDL", nombre="GDL")
        cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente")
        cls.op = Usuario.objects.create(
            username="op@acme.test", email="op@acme.test",
            empresa=cls.empresa, sucursal_default=cls.mty,
        )
        cls.admin = Usuario.objects.create(
            username="adm@acme.test", email="adm@acme.test",
            empresa=cls.empresa, sucursal_default=cls.mty, is_admin_empresa=True,
        )

        def _ob(sucursal, folio):
            pedido = Pedido.objects.create(
                empresa=cls.empresa, sucursal=sucursal, cliente=cliente, moneda=moneda,
                persona_pagos="Pagos", correo_facturas="a@acme.test",
                telefono_pagos="8100000000", forma_pago="03", metodo_pago="PUE",
                uso_cfdi="G03",
            )
            return OrdenesBordado.objects.create(
                empresa=cls.empresa, sucursal=sucursal, pedido=pedido, folio_bordado=folio
            )

        cls.ob1 = _ob(cls.mty, "OB-1")
        cls.ob2 = _ob(cls.mty, "OB-2")
        cls.ob_gdl = _ob(cls.gdl, "OB-GDL")

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_no_admin_no_crea_avance_en_ob_de_otra_sucursal(self):
        resp = self._client(self.op).post(
            self.URL,
            {"ob": self.ob_gdl.pk, "cantidad_bordada": 1, "puntadas_por_pieza": 1},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("ob", resp.data)
        # El admin de empresa sí puede.
        resp = self._client(self.admin).post(
            self.URL,
            {"ob": self.ob_gdl.pk, "cantidad_bordada": 1, "puntadas_por_pieza": 1},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_filtro_ob_devuelve_solo_esa_ob(self):
        for ob in (self.ob1, self.ob2):
            BordadoAvances.objects.create(
                ob=ob, usuario=self.op, cantidad_bordada=1, puntadas_por_pieza=1
            )
        resp = self._client(self.op).get(self.URL, {"ob": self.ob1.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual({r["ob"] for r in resp.json()}, {self.ob1.pk})

    def test_patch_no_borra_puntadas_total_de_avance_legacy(self):
        av = BordadoAvances.objects.create(
            ob=self.ob1, usuario=self.op, cantidad_bordada=10,
            puntadas_por_pieza=0, puntadas_total=80000,
        )
        resp = self._client(self.op).patch(
            f"{self.URL}{av.pk}/", {"comentario": "x"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        av.refresh_from_db()
        self.assertEqual(av.puntadas_total, 80000)
        # Cambiar solo las piezas de un legacy tampoco lo borra.
        self._client(self.op).patch(
            f"{self.URL}{av.pk}/", {"cantidad_bordada": 5}, format="json"
        )
        av.refresh_from_db()
        self.assertEqual(av.puntadas_total, 80000)

    def test_patch_de_comentario_no_recalcula_avance_normal(self):
        av = BordadoAvances.objects.create(
            ob=self.ob1, usuario=self.op, cantidad_bordada=10,
            puntadas_por_pieza=100, puntadas_total=1000,
        )
        self._client(self.op).patch(f"{self.URL}{av.pk}/", {"comentario": "x"}, format="json")
        av.refresh_from_db()
        self.assertEqual(av.puntadas_total, 1000)


class MatrizPrendaProcesoOrdenesTests(TestCase):
    """EC-575: cada orden de trabajo revisa la matriz solo para su propio proceso.

    «Playera» está configurada solo con corte de manga y reflejante en HOMBROS; la
    talla además lleva bordado, que la categoría no admite.
    """

    @classmethod
    def setUpTestData(cls):
        from catalogo.models import CategoriaProducto, CategoriaProductoProceso

        cls.empresa = Empresa.objects.create(codigo="ec575p", razon_social="EC575P SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="M75", nombre="M75")
        for tipo_documento, serie in SERIES:
            SerieFolio.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, tipo_documento=tipo_documento, serie=serie)
        cls.usuario = Usuario.objects.create(
            username="ec575p", email="ec575p@x.test", empresa=cls.empresa, sucursal_default=cls.sucursal,
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente")
        categoria = CategoriaProducto.objects.create(empresa=cls.empresa, nombre="Playera", codigo="PLA", descripcion="x")
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera", categoria_producto=categoria)
        cls.talla = Talla.objects.create(nombre="M575p")
        for proceso, ubicacion in (("corte_manga", ""), ("reflejante", "HOMBROS")):
            CategoriaProductoProceso.objects.create(
                empresa=cls.empresa, categoria_producto=categoria, proceso=proceso, ubicacion=ubicacion
            )

    def _pedido(self, **talla):
        pedido = Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente, moneda=self.moneda,
            persona_pagos="P", correo_facturas="p@x.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        detalle = PedidoDetalle.objects.create(pedido=pedido, producto=self.producto)
        PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=self.talla, cantidad=4,
            lleva_bordado=True, bordado_config={"ubicaciones": [{"codigo": "A"}]}, **talla,
        )
        return pedido

    def test_corte_de_manga_se_crea_aunque_el_bordado_no_se_admita(self):
        pedido = self._pedido(lleva_corte_manga=True, corte_manga_config={"tipo": "1"})
        orden = OrdenCorteMangaService.save({"pedido": pedido}, self.usuario)
        self.assertTrue(OrdenesCorteManga.objects.filter(pk=orden.pk, pedido=pedido).exists())

    def test_reflejante_revisa_su_propio_proceso(self):
        pedido = self._pedido(lleva_reflejante=True, reflejante_config=[{"posicion": "ESPALDA"}])
        with self.assertRaises(DRFValidationError) as ctx:
            OrdenReflejanteService.save({"pedido": pedido}, self.usuario)
        self.assertEqual(
            ctx.exception.detail,
            {"compatibilidad_proceso": ["La categoría «Playera» no admite Reflejante en «ESPALDA»."]},
        )
        self.assertFalse(OrdenesReflejante.objects.filter(pedido=pedido).exists())


class EditarOrdenServicioTests(TestCase):
    """PATCH/DELETE del detalle de reflejante y corte de manga (#755, #774-#778)."""

    FLAG = "lleva_reflejante"
    SERVICE = OrdenReflejanteService
    _crear_pedido = BaseOrdenTrabajoTests._crear_pedido
    _talla = BaseOrdenTrabajoTests._talla
    _save = BaseOrdenTrabajoTests._save

    @classmethod
    def setUpTestData(cls):
        BaseOrdenTrabajoTests.setUpTestData.__func__(cls)
        cls.admin = Usuario.objects.create(
            username="admin", email="admin@acme.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )

    def setUp(self):
        self.pedido, detalle = self._crear_pedido()
        self._talla(detalle, self.talla_ch, 10)
        self.orden = self._save(self.pedido)
        self.url = f"/api/v1/produccion/orden-reflejante/{self.orden.pk}/"

    def _patch(self, body, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.admin)
        return client.patch(self.url, body, format="json")

    def test_completar_sin_avances_rechaza(self):
        """#1138: Completado exige avances que cubran lo programado."""
        resp = self._patch({"estatus_reflejante": 5})
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("0 de 10", str(resp.data))

    def test_fecha_fin_no_se_escribe_por_patch(self):
        resp = self._patch({"fecha_fin": "2020-01-01T00:00:00Z", "prioridad": 2})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIsNone(resp.data["fecha_fin"])

    def test_completar_sella_fecha_fin_y_reabrir_la_limpia(self):
        ReflejanteAvances.objects.create(orden_r=self.orden, cantidad_aplicada=10, usuario=self.admin)
        resp = self._patch({"estatus_reflejante": 5})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIsNotNone(resp.data["fecha_fin"])
        resp = self._patch({"estatus_reflejante": 3})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIsNone(resp.data["fecha_fin"])

    def test_cancelada_no_se_edita_y_libera_cupo(self):
        self.assertTrue(OrdenReflejanteService.cantidades_asignadas_por_pedidos([self.pedido.pk])[0])
        self.assertEqual(self._patch({"estatus_reflejante": 7}).status_code, 200)
        por_linea, sin_talla = OrdenReflejanteService.cantidades_asignadas_por_pedidos([self.pedido.pk])
        self.assertEqual((por_linea, sin_talla), ({}, {}))
        resp = self._patch({"estatus_reflejante": 3})
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_prioridad_solo_1_2_o_3(self):
        self.assertEqual(self._patch({"prioridad": 2}).status_code, 200)
        self.assertEqual(self._patch({"prioridad": 999}).status_code, 400)

    def test_campos_no_editables_se_ignoran_y_put_es_405(self):
        folio = self.orden.folio_reflejante
        resp = self._patch({"folio_reflejante": "OTRO", "pedido": 0, "observaciones": "x"})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.folio_reflejante, self.orden.pedido_id), (folio, self.pedido.pk))
        client = APIClient()
        client.force_authenticate(user=self.admin)
        self.assertEqual(client.put(self.url, {}, format="json").status_code, 405)

    def test_sin_departamento_produccion_no_edita_ni_borra(self):
        resp = self._patch({"estatus_reflejante": 3}, user=self.usuario)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("permiso", resp.data)
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        self.assertEqual(client.delete(self.url).status_code, 400)
        self.orden.refresh_from_db()
        self.assertTrue(self.orden.activo)


class AvancesPiezasYSkuContratoTests(TestCase):
    """Documentación del contrato (no se ejecuta en CI; ver CLAUDE.md).

    - ``detalles[].sku`` en el retrieve de OB/OR/OCM: SKU de la variante
      producto+color+talla (``PedidoDetalleTalla.variante.sku``, o
      ``sku_muestra``), ``null`` si no hay. Sale de la misma query del config.
    - ``POST /reflejante-avances/`` ``{orden_r, cantidad, comentario?}`` y
      ``POST /corte-manga-avances/`` ``{ocm, cantidad, comentario?}``: usuario
      de la sesión; cantidad entera > 0; Σ avances ≤ Σ renglones de la orden
      (400 ``cantidad``); orden COMPLETADO/CANCELADO/inactiva → 400; gate
      ``_require_produccion``. ``DELETE /{id}/`` es borrado lógico con las
      mismas reglas. Sin PUT/PATCH.
    - Retrieve de OR/OCM: ``avances`` (``-fecha``) y ``resumen_avance``
      ``{cantidad_programada, cantidad_realizada_total, porcentaje_avance}``.
    - Retrieve de OCM: ``cantidad_pedido``/``cantidad_asignada``/
      ``cantidad_pendiente`` por renglón; ``cantidad_cubierta``/
      ``cantidad_contratada``/``cobertura_completa``/``otras_ordenes_del_pedido``.
    - Medido en producción (33 renglones): OB 7→7 queries, OR 6→7, OCM 3→7;
      constantes respecto al número de renglones.
    """

    def test_contrato_documentado(self):
        from produccion.api.serializers import resumen_avance_piezas
        det = [SimpleNamespace(cantidad=10), SimpleNamespace(cantidad=30)]
        av = [SimpleNamespace(cantidad=4), SimpleNamespace(cantidad=6)]
        self.assertEqual(
            resumen_avance_piezas(det, av),
            {"cantidad_programada": 40.0, "cantidad_realizada_total": 10.0, "porcentaje_avance": 25.0},
        )


class PedidosEspecialesAvanceContratoTests(TestCase):
    """Documentación del contrato (no se ejecuta en CI; ver CLAUDE.md).

    - Listado ``/pedidos-especiales/``: ``tallas_total``/``tallas_con_sku``/
      ``tallas_en_catalogo`` (tallas con cantidad > 0 de líneas de muestra),
      anotados con subconsultas: sigue en 1 query.
    - Detalle: ``detalles[].tallas[].avance`` =
      ``{surtido, bordado, reflejante, corte_manga}``; cada servicio
      ``{programado, realizado}`` o ``null`` si la talla no lo lleva. 2 queries
      constantes (picking + UNION ALL de renglones y avances). Avances por
      orden (reflejante, corte de manga, bordado sin talla) se reparten en
      proporción a lo programado por renglón.
    """

    def test_reparto_proporcional(self):
        from produccion.api.views import _repartir
        self.assertEqual(_repartir(10, [("a", 30), ("b", 20)]), {"a": 6, "b": 4})
        self.assertEqual(_repartir(99, [("a", 3), ("b", 2)]), {"a": 3, "b": 2})
        self.assertEqual(_repartir(0, [("a", 3)]), {})


class ContratoProduccionListadosYOnboardingMuestraTests(TestCase):
    """Documentación ejecutable (no es gate de CI).

    - ``pedidos-especiales`` (listado): ``fecha_compromiso`` (= ``fecha_entrega_max``
      de la clasificación), ``prendas_total``, ``piezas_total``, ``servicios`` y
      ``ordenes_produccion`` (OP activas), en la misma consulta.
    - ``orden-produccion`` (listado): ``pedido_folio`` vía ``select_related``.
    - Onboarding de OP: cada renglón lleva ``producto_variante_id`` o
      ``variante_produccion_id`` (uno de los dos); con variante de producción el
      BOM sale de ella y ``pedido_detalle`` se toma de su línea de muestra.
    - ``variantes-produccion``: id, sku, nombre, talla/color, pedido_folio y
      ``tiene_bom``.
    """

    def test_contrato_documentado(self):
        from produccion.api.serializers import (
            OrdenProduccionListSerializer, PedidoEspecialListSerializer, VarianteProduccionListSerializer,
        )
        self.assertIn("pedido_folio", OrdenProduccionListSerializer.Meta.fields)
        for campo in ("fecha_compromiso", "prendas_total", "piezas_total", "servicios", "ordenes_produccion"):
            self.assertIn(campo, PedidoEspecialListSerializer.Meta.fields)
        self.assertIn("tiene_bom", VarianteProduccionListSerializer.Meta.fields)


# --- EC-880: datos para el PDF de OP, OB, OR y OCM ----------------------------


class _DetallesOrdenesEC880Base(TestCase):
    """Una empresa con dos pedidos del mismo cliente: uno con la foto del cliente
    llena (``cliente_*`` del pedido) y otro sin foto (respaldo al ``Cliente``
    vivo). OB, OR y OCM sobre cada pedido y una OP que cubre ambos."""

    URL_OP = "/api/v1/produccion/orden-produccion/onboarding/"

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="ec880", razon_social="EC880 SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="E88", nombre="Matriz")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.usuario = Usuario.objects.create(
            username="ec880@acme.test", email="ec880@acme.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.cliente = Cliente.objects.create(
            empresa=cls.empresa, nombre="Cliente Vivo", razon_social="CLIENTE VIVO SA",
            rfc="CVI010101AA1",
        )
        cls.pedido_foto = cls._pedido(
            cliente_nombre="Foto Nombre", cliente_razon_social="FOTO SA", cliente_rfc="FOT010101AA1",
        )
        cls.pedido_vivo = cls._pedido()
        comun = {"empresa": cls.empresa, "sucursal": cls.sucursal}
        cls.ordenes = {}
        for etiqueta, pedido in (("foto", cls.pedido_foto), ("vivo", cls.pedido_vivo)):
            cls.ordenes[("ob", etiqueta)] = OrdenesBordado.objects.create(
                folio_bordado=f"OB-880-{etiqueta}", pedido=pedido, **comun)
            cls.ordenes[("or", etiqueta)] = OrdenesReflejante.objects.create(
                folio_reflejante=f"OR-880-{etiqueta}", pedido=pedido, **comun)
            cls.ordenes[("ocm", etiqueta)] = OrdenesCorteManga.objects.create(
                folio_ocm=f"OCM-880-{etiqueta}", pedido=pedido, **comun)
        cls.op = OrdenProduccion.objects.create(folio_op="OP-880-1", pedido=cls.pedido_foto, **comun)
        from produccion.api.serializers import ligar_pedidos_op
        ligar_pedidos_op(cls.op, [cls.pedido_foto, cls.pedido_vivo])

    @classmethod
    def _pedido(cls, **extra):
        return Pedido.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, cliente=cls.cliente, moneda=cls.moneda,
            persona_pagos="Pagos", correo_facturas="f@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03", **extra,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.usuario)

    def _detalle_op(self, op=None):
        resp = self.client.get(self.URL_OP, {"op_id": (op or self.op).pk})
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return resp.json()


class ClienteEnDetallesDeOrdenesTests(_DetallesOrdenesEC880Base):
    """``pedido_vinculado.cliente`` en OB/OR/OCM (y OP) y ``pedidos[].cliente``
    en el detalle de la OP: la foto del pedido manda; dato vacío -> cliente vivo."""

    URLS = {
        "ob": "/api/v1/produccion/orden-bordado/",
        "or": "/api/v1/produccion/orden-reflejante/",
        "ocm": "/api/v1/produccion/orden-corte-manga/",
    }

    def _esperado(self, etiqueta):
        if etiqueta == "foto":
            return {"id": self.cliente.pk, "nombre": "Foto Nombre", "razon_social": "FOTO SA",
                    "rfc": "FOT010101AA1"}
        return {"id": self.cliente.pk, "nombre": "Cliente Vivo", "razon_social": "CLIENTE VIVO SA",
                "rfc": "CVI010101AA1"}

    def test_ob_or_ocm_traen_el_cliente_en_pedido_vinculado(self):
        for tipo, url in self.URLS.items():
            for etiqueta in ("foto", "vivo"):
                with self.subTest(tipo=tipo, etiqueta=etiqueta):
                    orden = self.ordenes[(tipo, etiqueta)]
                    resp = self.client.get(f"{url}{orden.pk}/")
                    self.assertEqual(resp.status_code, 200)
                    vinculado = resp.json()["pedido_vinculado"]
                    self.assertEqual(vinculado["id"], orden.pedido_id)
                    self.assertIn("folio", vinculado)
                    self.assertEqual(vinculado["cliente"], self._esperado(etiqueta))

    def test_foto_parcial_no_se_mezcla_con_el_cliente_vivo(self):
        """La foto se usa en bloque: un dato vacío queda ``None``, no se toma del
        cliente vivo (podría ser otro cliente)."""
        Pedido.objects.filter(pk=self.pedido_foto.pk).update(cliente_rfc="")
        orden = self.ordenes[("ob", "foto")]
        cliente = self.client.get(f"{self.URLS['ob']}{orden.pk}/").json()["pedido_vinculado"]["cliente"]
        self.assertEqual(
            cliente,
            {"id": self.cliente.pk, "nombre": "Foto Nombre", "razon_social": "FOTO SA", "rfc": None},
        )

    def test_detalle_op_trae_cliente_por_pedido_principal_primero(self):
        data = self._detalle_op()
        self.assertEqual(
            data["pedidos"],
            [
                {"id": self.pedido_foto.pk, "folio": self.pedido_foto.folio, "cliente": self._esperado("foto")},
                {"id": self.pedido_vivo.pk, "folio": self.pedido_vivo.folio, "cliente": self._esperado("vivo")},
            ],
        )
        self.assertEqual(data["pedido_vinculado"]["cliente"], self._esperado("foto"))

    def test_retrieve_op_trae_cliente_por_pedido(self):
        """``/orden-produccion/{id}/`` lleva el mismo ``pedidos[]`` que el detalle
        ``onboarding/?op_id=``."""
        resp = self.client.get(f"/api/v1/produccion/orden-produccion/{self.op.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pedidos"], self._detalle_op()["pedidos"])
        self.assertEqual(
            [p["cliente"] for p in resp.json()["pedidos"]], [self._esperado("foto"), self._esperado("vivo")])

    def test_op_sin_pedido_no_trae_pedido_vinculado(self):
        op = OrdenProduccion.objects.create(empresa=self.empresa, sucursal=self.sucursal, folio_op="OP-880-SP")
        data = self._detalle_op(op)
        self.assertIsNone(data["pedido_vinculado"])
        self.assertEqual(data["pedidos"], [])

    def test_listado_de_op_no_cambia_su_forma_de_pedidos(self):
        filas = self.client.get("/api/v1/produccion/orden-produccion/").json()
        fila = next(f for f in filas if f["op_id"] == self.op.pk)
        self.assertEqual(
            fila["pedidos"],
            [{"id": self.pedido_foto.pk, "folio": self.pedido_foto.folio},
             {"id": self.pedido_vivo.pk, "folio": self.pedido_vivo.folio}],
        )


class FechasEnDetallesOcmYOpTests(_DetallesOrdenesEC880Base):
    """``fecha_pedido``/``fecha_vence`` en el detalle de la OCM y de la OP, con
    los mismos valores que sus listados."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        confirmacion = timezone.make_aware(timezone.datetime(2026, 10, 1, 12, 0))
        Pedido.objects.filter(pk=cls.pedido_foto.pk).update(clasificacion="B", fecha_confirmacion=confirmacion)

    def _fechas(self, data):
        return data["fecha_pedido"], data["fecha_vence"]

    def test_ocm_detalle_igual_al_listado(self):
        orden = self.ordenes[("ocm", "foto")]
        detalle = self.client.get(f"/api/v1/produccion/orden-corte-manga/{orden.pk}/").json()
        fila = next(f for f in self.client.get("/api/v1/produccion/orden-corte-manga/").json() if f["id"] == orden.pk)
        self.assertEqual(self._fechas(detalle), self._fechas(fila))
        # Clasificación B: 8 días máximos desde la confirmación.
        self.assertEqual(detalle["fecha_vence"], "2026-10-09")
        self.assertEqual(detalle["fecha_pedido"], str(timezone.localdate(self.pedido_foto.created_at)))

    def test_ocm_sin_clasificacion_vence_null(self):
        orden = self.ordenes[("ocm", "vivo")]
        detalle = self.client.get(f"/api/v1/produccion/orden-corte-manga/{orden.pk}/").json()
        self.assertIsNone(detalle["fecha_vence"])
        self.assertIsNotNone(detalle["fecha_pedido"])

    def test_op_detalle_igual_al_listado(self):
        detalle = self._detalle_op()
        fila = next(f for f in self.client.get("/api/v1/produccion/orden-produccion/").json() if f["op_id"] == self.op.pk)
        self.assertEqual(self._fechas(detalle), self._fechas(fila))
        self.assertEqual(detalle["fecha_vence"], "2026-10-09")

    def test_op_sin_pedido_fechas_null(self):
        op = OrdenProduccion.objects.create(empresa=self.empresa, sucursal=self.sucursal, folio_op="OP-880-SF")
        self.assertEqual(self._fechas(self._detalle_op(op)), (None, None))


class ConsumosResumenDetalleOpTests(_DetallesOrdenesEC880Base):
    """``consumos_resumen`` del detalle de la OP: planeado = cantidad del
    material en el consumo inicial (primer encabezado con renglones), 0 si no
    está; actual = suma; ajustes = actual - planeado. ``consumos`` no cambia."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.mtr = UnidadMedida.objects.create(clave="mtr", nombre="Metro")
        cls.tela = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Tela",
                                           unidad_medida=cls.mtr)
        cls.hilo = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Hilo")
        cls.boton = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Boton")

    def _consumo(self, *renglones):
        consumo = ConsumoProduccion.objects.create(op=self.op)
        for producto, cantidad in renglones:
            ConsumoProduccionDetalle.objects.create(
                consumo_produccion=consumo, producto=producto, cantidad=Decimal(cantidad))
        return consumo

    def test_sin_consumos_lista_vacia(self):
        data = self._detalle_op()
        self.assertEqual(data["consumos_resumen"], [])
        self.assertEqual(data["consumos"], [])

    def test_encabezado_vacio_previo_no_es_el_inicial(self):
        self._consumo()  # POST /consumo/ sin renglones
        self._consumo((self.tela, "120"), (self.hilo, "9"))
        resumen = {r["producto"]: r for r in self._detalle_op()["consumos_resumen"]}
        self.assertEqual(resumen[self.tela.pk]["planeado"], "120.0000")
        self.assertEqual(resumen[self.hilo.pk]["planeado"], "9.0000")

    def test_ajustes_con_signo_y_material_solo_en_ajuste(self):
        self._consumo((self.tela, "120"), (self.hilo, "9"))
        self._consumo((self.tela, "10"))
        self._consumo((self.tela, "-4"), (self.boton, "3"))
        self._consumo()  # vacío al final: tampoco cuenta
        data = self._detalle_op()
        self.assertEqual(data["consumos_resumen"], [
            {"producto": self.tela.pk, "producto_nombre": "Tela", "unidad": "MTR",
             "planeado": "120.0000", "ajustes": "6.0000", "actual": "126.0000"},
            {"producto": self.hilo.pk, "producto_nombre": "Hilo", "unidad": None,
             "planeado": "9.0000", "ajustes": "0.0000", "actual": "9.0000"},
            {"producto": self.boton.pk, "producto_nombre": "Boton", "unidad": None,
             "planeado": "0.0000", "ajustes": "3.0000", "actual": "3.0000"},
        ])
        # ``consumos`` (historial) conserva su forma: un elemento por encabezado.
        self.assertEqual(len(data["consumos"]), 4)
        self.assertEqual(set(data["consumos"][0]), {"consumo_produccion_id", "detalles"})
        self.assertEqual(
            set(data["consumos"][0]["detalles"][0]), {"producto", "producto_nombre", "cantidad"})

    def test_resumen_no_crece_en_consultas_con_los_renglones(self):
        self._consumo((self.tela, "120"), (self.hilo, "9"))
        with CaptureQueriesContext(connection) as uno:
            self._detalle_op()
        for _ in range(3):
            self._consumo((self.tela, "1"), (self.boton, "1"))
        with CaptureQueriesContext(connection) as varios:
            self._detalle_op()
        self.assertEqual(len(varios), len(uno))


class _OpRealEC880Base(_DetallesOrdenesEC880Base):
    """OP creada con ``save_orden_produccion`` (consumo real con desperdicio):
    dos variantes con BOM (uno con un insumo inactivo) y existencias de sobra."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.pza = UnidadMedida.objects.create(clave="pza", nombre="Pieza")
        cls.mtr = UnidadMedida.objects.create(clave="mtr", nombre="Metro")
        color = Color.objects.create(nombre="Azul", codigo="AZU", codigo_hex="#0000FF")
        cls.talla_m = Talla.objects.create(nombre="M")
        cls.talla_g = Talla.objects.create(nombre="G")
        playera = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera")
        cls.var_m = ProductoVariante.objects.create(
            producto=playera, empresa=cls.empresa, color=color, talla=cls.talla_m, sku="EC880-M", precio_base="1")
        cls.var_g = ProductoVariante.objects.create(
            producto=playera, empresa=cls.empresa, color=color, talla=cls.talla_g, sku="EC880-G", precio_base="1")
        cls.tela = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Tela",
                                           cod_proscai="TEL-1", unidad_medida=cls.mtr)
        cls.hilo = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Hilo")
        cls.boton = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Boton")
        cls.boms = {}
        for var, tela in ((cls.var_m, "1.25"), (cls.var_g, "1.37")):
            bom = ListaMaterialBom.objects.create(empresa=cls.empresa, producto_variante=var)
            BomDetalle.objects.create(bom=bom, componente=cls.tela, cantidad=Decimal(tela), unidad=cls.mtr,
                                      desperdicio=Decimal("7.5"))
            BomDetalle.objects.create(bom=bom, componente=cls.hilo, cantidad=Decimal("0.33"), unidad=cls.pza)
            BomDetalle.objects.create(bom=bom, componente=cls.boton, cantidad=Decimal("2"), unidad=cls.pza,
                                      desperdicio=Decimal("10"), activo=False)
            cls.boms[var.pk] = bom
        almacen = Almacen.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, codigo="A880", nombre="A")
        for producto in (cls.tela, cls.hilo, cls.boton):
            Existencia.objects.create(almacen=almacen, producto=producto, cantidad=Decimal("100000"))
        SerieFolio.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, tipo_documento="ORDEN_PRODUCCION",
                                  serie="OP")
        resultado = OrdenProduccionService.save_orden_produccion(
            {
                "empresa": cls.empresa, "sucursal": cls.sucursal, "pedido": cls.pedido_foto,
                "orden_produccion_detalle": [
                    {"producto_variante": cls.var_m, "bom": cls.boms[cls.var_m.pk], "cantidad": Decimal("13"),
                     "unidad": cls.pza},
                    {"producto_variante": cls.var_g, "bom": cls.boms[cls.var_g.pk], "cantidad": Decimal("7"),
                     "unidad": cls.pza},
                ],
            },
            cls.usuario,
        )
        cls.op_real = resultado["op"]


class RenglonesInactivosOpTests(_OpRealEC880Base):
    """Un renglón de la OP dado de baja (``activo=False``) ya no cuenta en el
    detalle (onboarding), en el retrieve ni en el costo por pieza."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.inactivo = OrdenProduccionDetalle.objects.create(
            op=cls.op_real, bom=cls.boms[cls.var_m.pk], producto_variante=cls.var_m,
            cantidad=Decimal("500"), unidad=cls.pza, activo=False,
        )

    def test_detalle_onboarding_no_cuenta_el_renglon_inactivo(self):
        productos = self._detalle_op(self.op_real)["productos"]
        self.assertEqual(len(productos), 1)
        self.assertEqual(productos[0]["cantidades"]["total"], 20)
        tallas = {t["talla"]: t["cantidad"] for t in productos[0]["cantidades"]["tallas"]}
        self.assertEqual(tallas, {"M": 13, "G": 7})
        hilo = next(h for h in productos[0]["habilitacion"] if h["descripcion"] == "Hilo")
        self.assertEqual(hilo["total"], 6.6)  # 20 piezas × 0.33, sin las 500 inactivas

    def test_retrieve_no_devuelve_el_renglon_inactivo(self):
        resp = self.client.get(f"/api/v1/produccion/orden-produccion/{self.op_real.pk}/")
        self.assertEqual(resp.status_code, 200)
        ids = {r["op_detalle_id"] for r in resp.json()["orden_produccion_detalle"]}
        self.assertNotIn(self.inactivo.pk, ids)
        self.assertEqual(len(ids), 2)

    def test_costo_por_pieza_divide_solo_entre_piezas_activas(self):
        from produccion.services.costo_produccion import costo_unitario_produccion
        mov = MovimientoInventario.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, tipo_movimiento="SALIDA", usuario=self.usuario,
            op=OrdenProduccion.objects.create(empresa=self.empresa, sucursal=self.sucursal, folio_op="OP-880-C"),
        )
        MovimientoInventarioDetalle.objects.create(
            movimiento_inventario=mov, producto=self.tela, cantidad=Decimal("10"), costo_unitario=Decimal("20"))
        op = mov.op
        OrdenProduccionDetalle.objects.create(op=op, bom=self.boms[self.var_m.pk], producto_variante=self.var_m,
                                              cantidad=Decimal("4"), unidad=self.pza)
        OrdenProduccionDetalle.objects.create(op=op, bom=self.boms[self.var_m.pk], producto_variante=self.var_m,
                                              cantidad=Decimal("96"), unidad=self.pza, activo=False)
        # 200 MXN de materiales entre 4 piezas activas (antes: entre 100).
        self.assertEqual(costo_unitario_produccion(op), Decimal("50"))


class HabilitacionConDesperdicioTests(_OpRealEC880Base):
    """``productos[].habilitacion_con_desperdicio`` aplica el desperdicio con la
    fórmula del consumo real: por material, su suma es el planeado del consumo
    inicial. ``habilitacion`` conserva su valor (sin desperdicio)."""

    def test_suma_por_material_igual_al_consumo_real(self):
        data = self._detalle_op(self.op_real)
        planeado = {r["producto_nombre"]: Decimal(r["planeado"]) for r in data["consumos_resumen"]}
        suma = {}
        for producto in data["productos"]:
            for fila in producto["habilitacion_con_desperdicio"]:
                suma[fila["descripcion"]] = suma.get(fila["descripcion"], Decimal("0")) + Decimal(fila["total"])
        self.assertEqual(suma, planeado)
        # 13 × 1.25 × 1.075 + 7 × 1.37 × 1.075 = 17.4688 + 10.3093
        self.assertEqual(suma["Tela"], Decimal("27.7781"))
        self.assertNotIn("Boton", suma)  # insumo inactivo del BOM: ni consumo ni habilitación

    def test_habilitacion_original_sin_cambios(self):
        producto = self._detalle_op(self.op_real)["productos"][0]
        tela = next(h for h in producto["habilitacion"] if h["descripcion"] == "Tela")
        self.assertEqual(tela, {"codigo": "TEL-1", "descripcion": "Tela", "unidad": "MTR", "total": 25.84})
        tela_d = next(h for h in producto["habilitacion_con_desperdicio"] if h["descripcion"] == "Tela")
        self.assertEqual(tela_d, {"codigo": "TEL-1", "descripcion": "Tela", "unidad": "MTR", "total": "27.7781"})


class ConsultasDetalleYListadoOpEC880Tests(_OpRealEC880Base):
    """El detalle de la OP (onboarding/?op_id=) no crece en consultas con el
    número de pedidos, renglones o consumos, y el listado de OP no cambia."""

    #: Consultas del detalle: 1 de alcance (``exists``) + OP con sus FK de
    #: encabezado + renglones + BOM + consumos + pedidos ligados.
    CONSULTAS_DETALLE = 7
    #: Listado: OPs + ``op_pedidos`` precargados (sin cambios en EC-880).
    CONSULTAS_LISTADO = 2

    def _contar(self, url, params=None):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url, params or {})
        self.assertEqual(resp.status_code, 200)
        return len(ctx.captured_queries)

    def _otro_pedido(self, n):
        return self._pedido(cliente_nombre=f"Otro {n}", cliente_razon_social=f"OTRO {n} SA", cliente_rfc="")

    def test_detalle_constante_con_mas_pedidos_y_consumos(self):
        base = self._contar(self.URL_OP, {"op_id": self.op_real.pk})
        from produccion.api.serializers import ligar_pedidos_op
        ligar_pedidos_op(self.op_real, [self.pedido_foto] + [self._otro_pedido(i) for i in range(4)])
        ConsumoProduccion.objects.create(op=self.op_real)
        con_mas = self._contar(self.URL_OP, {"op_id": self.op_real.pk})
        self.assertEqual(con_mas, base)
        self.assertEqual(base, self.CONSULTAS_DETALLE)

    def test_retrieve_constante_con_mas_pedidos(self):
        url = f"/api/v1/produccion/orden-produccion/{self.op_real.pk}/"
        base = self._contar(url)
        from produccion.api.serializers import ligar_pedidos_op
        ligar_pedidos_op(self.op_real, [self.pedido_foto] + [self._otro_pedido(i) for i in range(4)])
        self.assertEqual(self._contar(url), base)
        self.assertEqual(len(self.client.get(url).json()["pedidos"]), 5)

    def test_listado_no_crece_con_las_ops(self):
        base = self._contar("/api/v1/produccion/orden-produccion/")
        for i in range(3):
            op = OrdenProduccion.objects.create(
                empresa=self.empresa, sucursal=self.sucursal, folio_op=f"OP-880-L{i}", pedido=self._otro_pedido(i))
            from produccion.api.serializers import ligar_pedidos_op
            ligar_pedidos_op(op)
        self.assertEqual(self._contar("/api/v1/produccion/orden-produccion/"), base)
        self.assertEqual(base, self.CONSULTAS_LISTADO)



class OrdenProduccionPedidosLigadosTests(TestCase):
    """#1089: ``pedidos_ids`` (y ``pedido``) solo ligan pedidos activos, no
    cancelados, de la sucursal de la OP y dentro del alcance del usuario.
    Reutiliza el escenario de ``OrdenProduccionPedidoOpcionalTests``."""

    URL = OrdenProduccionPedidoOpcionalTests.URL
    setUpTestData = OrdenProduccionPedidoOpcionalTests.setUpTestData
    _client = OrdenProduccionPedidoOpcionalTests._client
    _body = OrdenProduccionPedidoOpcionalTests._body
    _pedido_muestra = OrdenProduccionPedidoOpcionalTests._pedido_muestra
    _segunda_sucursal = OrdenProduccionPedidoOpcionalTests._segunda_sucursal

    def _crear(self, *pedidos, client=None):
        body = self._body()
        body["pedidos_ids"] = [p.pk for p in pedidos]
        return (client or self._client()).post(self.URL, body, format="json")

    def test_dos_pedidos_de_la_sucursal_se_ligan(self):
        resp = self._crear(self._pedido_muestra(), self._pedido_muestra())
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_pedido_inactivo_rechaza(self):
        inactivo = self._pedido_muestra()
        Pedido.objects.filter(pk=inactivo.pk).update(activo=False)
        resp = self._crear(self._pedido_muestra(), inactivo)
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_pedido_cancelado_rechaza(self):
        cancelado = self._pedido_muestra()
        Pedido.objects.filter(pk=cancelado.pk).update(estatus=Pedido.ESTATUS_CANCELADO)
        resp = self._crear(self._pedido_muestra(), cancelado)
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_pedido_de_otra_sucursal_rechaza_aunque_el_usuario_la_vea(self):
        suc = self._segunda_sucursal()
        resp = self._crear(self._pedido_muestra(), self._pedido_muestra(sucursal=suc))
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("otra sucursal", str(resp.data))

    def test_no_admin_no_liga_pedido_fuera_de_su_alcance(self):
        suc = self._segunda_sucursal()
        limitado = Usuario.objects.create(
            username="op-lim2", email="lim2@acme-op.test", empresa=self.empresa, sucursal_default=suc,
        )
        limitado.sucursales.add(suc)
        client = APIClient()
        client.force_authenticate(user=limitado)
        body = self._body()
        body["sucursal"] = suc.pk
        body["pedidos_ids"] = [self._pedido_muestra().pk]  # de la sucursal 1, que no ve
        resp = client.post(self.URL, body, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertNotIn("otra sucursal", str(resp.data))  # mismo mensaje que un pk inexistente


class SalidaMaquilaReglasTests(TestCase):
    """#1088: tope total de piezas por OP y precio > 0. #1091: serie SM por
    sucursal, dada de alta al primer uso."""

    URL = "/api/v1/produccion/salidas-maquila/"

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme-sm", razon_social="acme-sm SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="Matriz")
        cls.sucursal2 = Sucursal.objects.create(empresa=cls.empresa, codigo="SLP", nombre="Segunda")
        SerieFolio.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, tipo_documento="Salida a Maquila", serie="SM",
            relleno_ceros=5, separador="-",
        )
        cls.usuario = Usuario.objects.create(
            username="sm-user", email="sm@acme-sm.test", empresa=cls.empresa, is_admin_empresa=True,
        )
        from terceros.models import Proveedor
        cls.maquilero = Proveedor.objects.create(empresa=cls.empresa, nombre="Maquila SA", es_maquilero=True)
        unidad = UnidadMedida.objects.create(clave="PZA", nombre="Pieza")
        cls.op = OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-SM-1",
            centro_confeccion=cls.maquilero, costo_maquila_unitario=Decimal("10"),
        )
        cls.det = OrdenProduccionDetalle.objects.create(op=cls.op, cantidad=Decimal("5"), unidad=unidad)

    def _post(self, **body):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        return client.post(self.URL, {"op": self.op.pk, **body}, format="json")

    def test_tope_total_por_op(self):
        self.assertEqual(self._post(detalles=[{"op_detalle": self.det.pk, "cantidad": "3"}]).status_code, 201)
        resp = self._post(detalles=[{"cantidad": "3", "descripcion": "libre"}])
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("quedan 2", str(resp.data))

    def test_canceladas_no_cuentan(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 201, resp.data)
        SalidaMaquila.objects.filter(pk=resp.data["id"]).update(estatus=SalidaMaquila.Estatus.CANCELADA)
        self.assertEqual(self._post().status_code, 201)

    def test_precio_cero_rechaza(self):
        resp = self._post(detalles=[{"op_detalle": self.det.pk, "cantidad": "1", "precio_unitario": "0"}])
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_op_sin_costo_y_sin_precio_rechaza(self):
        OrdenProduccion.objects.filter(pk=self.op.pk).update(costo_maquila_unitario=None)
        resp = self._post()
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("precio_unitario", resp.data)

    def test_sucursal_sin_serie_la_da_de_alta(self):
        OrdenProduccion.objects.filter(pk=self.op.pk).update(sucursal=self.sucursal2)
        resp = self._post()
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(resp.data["folio"].startswith(f"SM{self.sucursal2.pk}-"))
        self.assertTrue(
            SerieFolio.objects.filter(sucursal=self.sucursal2, tipo_documento="Salida a Maquila").exists()
        )


class PermisosProduccionTests(TestCase):
    """#1133: leer OP/OB/OR/OCM (y sus avances/incidencias) exige ``R-PRODUCCION``
    o el código de la sección; editar/borrar una OB, sus avances o incidencias
    exige ``E-``/``D-PRODUCCION-OB``. #1135: el costo de maquila de la OP solo
    viaja (y se cambia) con ``R-PRODUCCION-COSTOS``."""

    @classmethod
    def setUpTestData(cls):
        from seguridad.models import Permiso, Rol, RolPermiso, UsuarioRol

        cls.empresa = Empresa.objects.create(codigo="acme-perm", razon_social="acme-perm SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="Matriz")

        def usuario(nombre, *claves):
            u = Usuario.objects.create(
                username=nombre, email=f"{nombre}@acme-perm.test", empresa=cls.empresa,
                sucursal_default=cls.sucursal,
            )
            u.sucursales.add(cls.sucursal)
            if claves:
                rol = Rol.objects.create(empresa=cls.empresa, codigo=f"rol-{nombre}", nombre=f"rol-{nombre}")
                for clave in claves:
                    permiso = Permiso.objects.get_or_create(clave=clave, defaults={"nombre": clave})[0]
                    RolPermiso.objects.create(rol=rol, permiso=permiso)
                UsuarioRol.objects.create(usuario=u, rol=rol, empresa=cls.empresa)
            return u

        cls.modulo = usuario("modulo", "R-PRODUCCION")
        cls.seccion_ob = usuario("seccionob", "R-PRODUCCION-OB")
        cls.crm = usuario("crm", "R-CRM")
        cls.costos = usuario("costos", "R-PRODUCCION", "R-PRODUCCION-COSTOS")
        cls.op = OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-PERM-1",
            costo_maquila_unitario=Decimal("12"),
        )

    def _get(self, user, url):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(url)

    def test_lectura_con_modulo_o_seccion(self):
        urls = [
            "/api/v1/produccion/orden-produccion/", "/api/v1/produccion/orden-bordado/",
            "/api/v1/produccion/orden-reflejante/", "/api/v1/produccion/orden-corte-manga/",
            "/api/v1/produccion/bordado-avances/", "/api/v1/produccion/orden-produccion/kpis/",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self._get(self.modulo, url).status_code, 200)
                self.assertEqual(self._get(self.crm, url).status_code, 403)
        self.assertEqual(self._get(self.seccion_ob, "/api/v1/produccion/orden-bordado/").status_code, 200)
        self.assertEqual(self._get(self.seccion_ob, "/api/v1/produccion/orden-reflejante/").status_code, 403)

    def test_editar_y_borrar_ob_exigen_codigos(self):
        client = APIClient()
        client.force_authenticate(user=self.modulo)
        # La OB no existe: el 403 sale antes de buscarla.
        self.assertEqual(client.patch("/api/v1/produccion/orden-bordado/999999/", {}, format="json").status_code, 403)
        self.assertEqual(client.delete("/api/v1/produccion/orden-bordado/999999/").status_code, 403)
        self.assertEqual(client.patch("/api/v1/produccion/bordado-avances/999999/", {}, format="json").status_code, 403)

    def test_costo_de_maquila_solo_con_permiso_de_costos(self):
        url = f"/api/v1/produccion/orden-produccion/{self.op.pk}/"
        self.assertNotIn("costo_maquila_unitario", self._get(self.modulo, url).json())
        self.assertEqual(self._get(self.costos, url).json()["costo_maquila_unitario"], "12.0000")

    def test_sin_permiso_de_costos_no_cambia_el_costo(self):
        client = APIClient()
        client.force_authenticate(user=self.modulo)
        with mock.patch.object(Usuario, "tiene_permiso", return_value=True):  # E-PRODUCCION-OP
            resp = client.patch(
                f"/api/v1/produccion/orden-produccion/{self.op.pk}/",
                {"costo_maquila_unitario": "99"}, format="json",
            )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.op.refresh_from_db()
        self.assertEqual(self.op.costo_maquila_unitario, Decimal("12"))


class BomUnidadPorcentajeTests(TestCase):
    """#1140: ``%`` no es unidad de consumo en un renglón de BOM."""

    def test_rechaza_unidad_porcentaje(self):
        empresa = Empresa.objects.create(codigo="acme-pct", razon_social="acme-pct SA")
        usuario = Usuario.objects.create(username="pct", email="pct@acme-pct.test", empresa=empresa)
        pct = UnidadMedida.objects.create(clave="%", nombre="Porcentaje")
        componente = Producto.objects.create(tipo=_tipo_producto(), empresa=empresa, nombre="Tela")
        bom = ListaMaterialBom.objects.create(empresa=empresa, activo=True)
        client = APIClient()
        client.force_authenticate(user=usuario)
        resp = client.post(
            "/api/v1/produccion/bom-detalle/",
            {"bom": bom.pk, "componente": componente.pk, "cantidad": "1", "unidad": pct.pk}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("unidad", resp.data)


class RenglonSinColorTests(TestCase):
    """#1141: ``color_nombre``/``talla_nombre`` vienen como ``null`` (no desaparecen)."""

    def test_color_y_talla_nulos(self):
        from produccion.api.serializers import OrdenReflejanteDetalleListSerializer

        data = OrdenReflejanteDetalleListSerializer(OrdenReflejanteDetalle()).data
        self.assertIn("color_nombre", data)
        self.assertIsNone(data["color_nombre"])
        self.assertIsNone(data["talla_nombre"])
