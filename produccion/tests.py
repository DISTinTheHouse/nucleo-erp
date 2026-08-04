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

from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APIClient

from catalogo.models import Producto, Talla
from nucleo.models import Empresa, Moneda, SerieFolio, Sucursal
from produccion.models import (
    BordadoAvances,
    BordadoIncidencias,
    OrdenesBordado,
    OrdenesCorteManga,
    OrdenesReflejante,
    ReflejanteAvances,
    ReflejanteIncidencias,
)
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


class OrdenBordadoAntiduplicadoTests(BaseOrdenTrabajoTests, TestCase):
    FLAG = "lleva_bordado"
    SERVICE = OrdenBordadoService
    MODEL = OrdenesBordado
    DUPLICADA = OrdenBordadoDuplicada409
    FOLIO_FIELD = "folio_bordado"
    ESTATUS_FIELD = "estatus_bordado"
    CANCELADO = OrdenesBordado.EstatusBordado.CANCELADO


class OrdenCorteMangaAntiduplicadoTests(BaseOrdenTrabajoTests, TestCase):
    FLAG = "lleva_corte_manga"
    SERVICE = OrdenCorteMangaService
    MODEL = OrdenesCorteManga
    DUPLICADA = OrdenCorteMangaDuplicada409
    FOLIO_FIELD = "folio_ocm"
    ESTATUS_FIELD = "estatus_corte"
    CANCELADO = OrdenesCorteManga.EstatusCorte.CANCELADO


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
    CANCELADO = OrdenesBordado.EstatusBordado.CANCELADO

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
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertIn("usuario", resp.json())
                self.assertEqual(cfg["modelo"].objects.count(), 0)

    def test_create_acepta_usuario_de_la_misma_empresa_distinto_al_solicitante(self):
        """No regresión: supervisor-en-nombre-de-otro-operador sigue funcionando."""
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
                self.assertEqual(creado.usuario_id, self.operador_mismo_tenant.pk)
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
