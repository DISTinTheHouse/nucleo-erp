"""Tests del scope multi-tenant de ``PedidoViewSet``.

Ejecutar SIEMPRE con una BD desechable; el ``.env`` del repo apunta a Supabase
de producción. Ejemplo con un settings de override a SQLite en memoria:

    python manage.py test ventas --settings=sqlite_settings
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TestCase
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APIClient

from auditoria.models import AuditoriaEvento
from catalogo.models import Producto, Talla
from inventarios.models import MovimientoInventario
from nucleo.models import Empresa, Moneda, Sucursal
from terceros.models import Cliente
from usuarios.models import Usuario
from ventas.models import (
    Cotizacion,
    CotizacionServicioExtra,
    CotizacionDetalleTalla,
    Pedido,
    PedidoDetalle,
    PedidoDetalleTalla,
    PedidoServicioExtra,
)
from ventas.servicios_bordado import TipoServicioBordado, validar_tipos_servicio_array
from ventas.utils.helpers import _save_cotizacion_detalle

PEDIDOS_URL = "/api/v1/ventas/pedidos/"
PEDIDO_DETALLE_URL = "/api/v1/ventas/pedido-detalle/"
PEDIDO_DETALLE_TALLA_URL = "/api/v1/ventas/pedido-detalle-talla/"
COTIZACION_ONBOARDING_URL = "/api/v1/ventas/cotizaciones/onboarding/"


def pedido_editar_mesa_control_url(pedido_id):
    return f"/api/v1/ventas/pedidos/{pedido_id}/editar-mesa-control/"


class PedidoViewSetScopeTenantTests(TestCase):
    """``PedidoViewSet.get_queryset()``: quién ve qué pedidos.

    El branch de interés: un usuario **sin empresa** que **no** es superuser no
    entraba en ninguna condición y se llevaba los pedidos de todas las empresas.
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
            folio=f"PED-{codigo}",
            persona_pagos="Pagos",
            correo_facturas=email,
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )
        return {"empresa": empresa, "usuario": usuario, "pedido": pedido}

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
        cls.admin_a = Usuario.objects.create(
            username="admin_a",
            email="admin@acme.test",
            empresa=cls.a["empresa"],
            is_admin_empresa=True,
        )

    def _ids(self, user, url=PEDIDOS_URL):
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
        return [row["id"] for row in resp.json()]

    # --- el branch roto -------------------------------------------------------

    def test_usuario_sin_empresa_y_sin_superuser_no_ve_ningun_pedido(self):
        """Antes caía fuera de toda condición y recibía los pedidos de todas."""
        self.assertEqual(self._ids(self.sin_empresa), [])

    def test_usuario_sin_empresa_tampoco_puede_hacer_retrieve(self):
        client = APIClient()
        client.force_authenticate(user=self.sin_empresa)
        resp = client.get(f"{PEDIDOS_URL}{self.a['pedido'].pk}/")
        self.assertEqual(resp.status_code, 404)

    def test_usuario_sin_empresa_no_esquiva_el_scope_con_el_filtro_q(self):
        """El filtro ``q``/``folio`` no debe reabrir lo que el scope cerró."""
        folio = self.a["pedido"].folio
        self.assertEqual(self._ids(self.sin_empresa, f"{PEDIDOS_URL}?q={folio}"), [])

    # --- no regresión en las ramas que ya eran correctas ----------------------

    def test_usuario_con_empresa_sigue_viendo_solo_la_suya(self):
        self.assertEqual(self._ids(self.a["usuario"]), [self.a["pedido"].pk])
        self.assertEqual(self._ids(self.b["usuario"]), [self.b["pedido"].pk])

    def test_admin_empresa_sigue_viendo_los_de_su_empresa(self):
        self.assertEqual(self._ids(self.admin_a), [self.a["pedido"].pk])

    def test_superuser_sigue_viendo_todas_las_empresas(self):
        self.assertCountEqual(
            self._ids(self.superuser),
            [self.a["pedido"].pk, self.b["pedido"].pk],
        )

    def test_filtro_q_sigue_funcionando_dentro_del_scope(self):
        folio = self.a["pedido"].folio
        self.assertEqual(
            self._ids(self.a["usuario"], f"{PEDIDOS_URL}?q={folio}"),
            [self.a["pedido"].pk],
        )
        self.assertEqual(
            self._ids(self.b["usuario"], f"{PEDIDOS_URL}?q={folio}"), []
        )


class PedidoMisPedidosFilterTests(TestCase):
    """``?mis_pedidos=true``: pedidos cuya cotización de origen creó el usuario.

    La cadena es ``pedido.cotizacion.vendedor``; ambos eslabones son NULL-ables,
    así que los pedidos sin cotización (o con cotización sin vendedor) quedan
    fuera por la semántica INNER JOIN del filtro — es el comportamiento buscado.

    Los dos vendedores viven a propósito en la **misma** empresa: así el filtro
    de empresa que ya existía no puede dar un falso verde.
    """

    @classmethod
    def _pedido(cls, folio, cotizacion=None):
        return Pedido.objects.create(
            empresa=cls.empresa,
            sucursal=cls.sucursal,
            cliente=cls.cliente,
            moneda=cls.moneda,
            cotizacion=cotizacion,
            folio=folio,
            persona_pagos="Pagos",
            correo_facturas="pagos@acme.test",
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )

    @classmethod
    def _cotizacion(cls, vendedor):
        return Cotizacion.objects.create(
            empresa=cls.empresa,
            sucursal=cls.sucursal,
            cliente=cls.cliente,
            moneda=cls.moneda,
            vendedor=vendedor,
        )

    @classmethod
    def setUpTestData(cls):
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="MTY"
        )
        cls.cliente = Cliente.objects.create(
            empresa=cls.empresa, nombre="Cliente ACME"
        )

        cls.vendedor_a = Usuario.objects.create(
            username="vendedor_a", email="vendedor.a@acme.test", empresa=cls.empresa
        )
        cls.vendedor_b = Usuario.objects.create(
            username="vendedor_b", email="vendedor.b@acme.test", empresa=cls.empresa
        )

        cls.pedido_a = cls._pedido("PED-A", cotizacion=cls._cotizacion(cls.vendedor_a))
        cls.pedido_b = cls._pedido("PED-B", cotizacion=cls._cotizacion(cls.vendedor_b))
        # Tercer pedido creado directo, sin cotización de origen.
        cls.pedido_sin_cotizacion = cls._pedido("PED-SIN", cotizacion=None)

    def _ids(self, user, url=PEDIDOS_URL):
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
        return [row["id"] for row in resp.json()]

    # --- el filtro nuevo ------------------------------------------------------

    def test_mis_pedidos_true_solo_devuelve_los_del_usuario_autenticado(self):
        self.assertEqual(
            self._ids(self.vendedor_a, f"{PEDIDOS_URL}?mis_pedidos=true"),
            [self.pedido_a.pk],
        )
        self.assertEqual(
            self._ids(self.vendedor_b, f"{PEDIDOS_URL}?mis_pedidos=true"),
            [self.pedido_b.pk],
        )

    def test_mis_pedidos_true_excluye_el_pedido_sin_cotizacion(self):
        """``cotizacion`` es NULL-able: ese pedido no es de nadie."""
        self.assertNotIn(
            self.pedido_sin_cotizacion.pk,
            self._ids(self.vendedor_a, f"{PEDIDOS_URL}?mis_pedidos=true"),
        )

    def test_mis_pedidos_acepta_1_y_mayusculas(self):
        for valor in ("1", "True", "TRUE"):
            with self.subTest(valor=valor):
                self.assertEqual(
                    self._ids(self.vendedor_a, f"{PEDIDOS_URL}?mis_pedidos={valor}"),
                    [self.pedido_a.pk],
                )

    # --- no regresión del comportamiento previo -------------------------------

    def test_sin_el_parametro_se_devuelven_todos_los_de_la_empresa(self):
        self.assertCountEqual(
            self._ids(self.vendedor_a),
            [self.pedido_a.pk, self.pedido_b.pk, self.pedido_sin_cotizacion.pk],
        )

    def test_valores_que_no_activan_el_filtro_se_ignoran(self):
        todos = [self.pedido_a.pk, self.pedido_b.pk, self.pedido_sin_cotizacion.pk]
        for valor in ("false", "foo", "0", ""):
            with self.subTest(valor=valor):
                self.assertCountEqual(
                    self._ids(self.vendedor_a, f"{PEDIDOS_URL}?mis_pedidos={valor}"),
                    todos,
                )

    def test_mis_pedidos_se_combina_con_el_filtro_q(self):
        """Ambos filtros viven en el mismo ``get_queryset``; deben componerse."""
        self.assertEqual(
            self._ids(
                self.vendedor_a, f"{PEDIDOS_URL}?mis_pedidos=true&q={self.pedido_a.folio}"
            ),
            [self.pedido_a.pk],
        )
        # El folio de B existe, pero no es del vendedor A.
        self.assertEqual(
            self._ids(
                self.vendedor_a, f"{PEDIDOS_URL}?mis_pedidos=true&q={self.pedido_b.folio}"
            ),
            [],
        )


class PedidoDetalleScopeTenantTests(TestCase):
    """``PedidoDetalleViewSet``/``PedidoDetalleTallaViewSet``: aislamiento tenant.

    Sin ``get_queryset`` estos ViewSets servían los renglones de **todas** las
    empresas (list/retrieve/patch). El scope se resuelve por la cadena de FK al
    ``Pedido`` — misma convención empresa-only que ``PedidoViewSet``.
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
            folio=f"PED-{codigo}",
            persona_pagos="Pagos",
            correo_facturas=email,
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )
        producto = Producto.objects.create(empresa=empresa, nombre=f"Prod {codigo}")
        detalle = PedidoDetalle.objects.create(pedido=pedido, producto=producto)
        talla_row = PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=cls.talla, cantidad=5
        )
        return {
            "usuario": usuario,
            "pedido": pedido,
            "producto": producto,
            "detalle": detalle,
            "talla_row": talla_row,
        }

    @classmethod
    def setUpTestData(cls):
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.talla = Talla.objects.create(nombre="M")
        cls.a = cls._tenant("acme", "MTY", "a@acme.test")
        cls.b = cls._tenant("globex", "GDL", "b@globex.test")
        cls.sin_empresa = Usuario.objects.create(
            username="huerfano", email="huerfano@nowhere.test"
        )
        cls.superuser = Usuario.objects.create(
            username="root", email="root@nowhere.test", is_superuser=True
        )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _ids(self, user, url):
        resp = self._client(user).get(url)
        self.assertEqual(resp.status_code, 200)
        return [row["id"] for row in resp.json()]

    # --- PedidoDetalle --------------------------------------------------------

    def test_detalle_list_scoped_por_empresa(self):
        # ``assertCountEqual``: el endpoint no define ``ordering`` (ni el ViewSet ni
        # el modelo), así que el orden de la lista depende de la BD. Comparar por
        # membresía+cardinalidad evita que se vuelva flaky cuando un tenant tenga
        # varios renglones.
        self.assertCountEqual(
            self._ids(self.a["usuario"], PEDIDO_DETALLE_URL), [self.a["detalle"].pk]
        )
        self.assertCountEqual(
            self._ids(self.b["usuario"], PEDIDO_DETALLE_URL), [self.b["detalle"].pk]
        )

    def test_detalle_retrieve_cross_tenant_es_404(self):
        resp = self._client(self.a["usuario"]).get(
            f"{PEDIDO_DETALLE_URL}{self.b['detalle'].pk}/"
        )
        self.assertEqual(resp.status_code, 404)

    def test_detalle_patch_cross_tenant_es_404(self):
        """PATCH cross-tenant no debe poder modificar el renglón de otra empresa."""
        resp = self._client(self.a["usuario"]).patch(
            f"{PEDIDO_DETALLE_URL}{self.b['detalle'].pk}/",
            {"precio_unitario": "999.99"},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    # --- PedidoDetalle: aislamiento del path de ESCRITURA (FK ``pedido``) ------

    def test_detalle_post_pedido_cross_tenant_es_rechazado(self):
        """POST creando un renglón hacia el pedido de OTRA empresa -> 400.

        Vector distinto al retrieve/patch por id: aquí el id del renglón no
        existe todavía; el usuario inyecta la línea vía la FK ``pedido``.
        """
        resp = self._client(self.a["usuario"]).post(
            PEDIDO_DETALLE_URL,
            {"pedido": self.b["pedido"].pk, "producto": self.a["producto"].pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("pedido", resp.json())

    def test_detalle_patch_reasignar_pedido_cross_tenant_es_rechazado(self):
        """PATCH del renglón PROPIO moviéndolo al pedido de otra empresa -> 400.

        El renglón sí es visible (es de A), pero reasignar ``pedido`` a B lo
        sacaría del scope de A; debe rechazarse y no persistir el cambio.
        """
        detalle_a = self.a["detalle"]
        resp = self._client(self.a["usuario"]).patch(
            f"{PEDIDO_DETALLE_URL}{detalle_a.pk}/",
            {"pedido": self.b["pedido"].pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        detalle_a.refresh_from_db()
        self.assertEqual(detalle_a.pedido_id, self.a["pedido"].pk)

    def test_detalle_post_mismo_tenant_ok(self):
        """No es falso positivo: POST hacia el pedido propio -> 201."""
        resp = self._client(self.a["usuario"]).post(
            PEDIDO_DETALLE_URL,
            {"pedido": self.a["pedido"].pk, "producto": self.a["producto"].pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)

    def test_detalle_sin_empresa_no_ve_nada(self):
        self.assertEqual(self._ids(self.sin_empresa, PEDIDO_DETALLE_URL), [])
        resp = self._client(self.sin_empresa).get(
            f"{PEDIDO_DETALLE_URL}{self.a['detalle'].pk}/"
        )
        self.assertEqual(resp.status_code, 404)

    def test_detalle_superuser_ve_todas_las_empresas(self):
        self.assertCountEqual(
            self._ids(self.superuser, PEDIDO_DETALLE_URL),
            [self.a["detalle"].pk, self.b["detalle"].pk],
        )

    # --- PedidoDetalleTalla ---------------------------------------------------

    def test_talla_list_scoped_por_empresa(self):
        # ``assertCountEqual``: sin ``ordering`` el orden de la lista lo decide la
        # BD; comparamos por membresía+cardinalidad. Ver nota en
        # ``test_detalle_list_scoped_por_empresa``.
        self.assertCountEqual(
            self._ids(self.a["usuario"], PEDIDO_DETALLE_TALLA_URL),
            [self.a["talla_row"].pk],
        )
        self.assertCountEqual(
            self._ids(self.b["usuario"], PEDIDO_DETALLE_TALLA_URL),
            [self.b["talla_row"].pk],
        )

    def test_talla_retrieve_cross_tenant_es_404(self):
        resp = self._client(self.a["usuario"]).get(
            f"{PEDIDO_DETALLE_TALLA_URL}{self.b['talla_row'].pk}/"
        )
        self.assertEqual(resp.status_code, 404)

    def test_talla_patch_cross_tenant_es_404(self):
        resp = self._client(self.a["usuario"]).patch(
            f"{PEDIDO_DETALLE_TALLA_URL}{self.b['talla_row'].pk}/",
            {"cantidad": 999},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    # --- PedidoDetalleTalla: aislamiento de ESCRITURA (FK ``pedido_detalle``) --

    def test_talla_post_pedido_detalle_cross_tenant_es_rechazado(self):
        """POST colgando una talla del renglón de OTRA empresa -> 400."""
        resp = self._client(self.a["usuario"]).post(
            PEDIDO_DETALLE_TALLA_URL,
            {
                "pedido_detalle": self.b["detalle"].pk,
                "talla": self.talla.pk,
                "cantidad": 3,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("pedido_detalle", resp.json())

    def test_talla_patch_reasignar_pedido_detalle_cross_tenant_es_rechazado(self):
        """PATCH de la talla propia reasignando ``pedido_detalle`` a B -> 400."""
        talla_a = self.a["talla_row"]
        resp = self._client(self.a["usuario"]).patch(
            f"{PEDIDO_DETALLE_TALLA_URL}{talla_a.pk}/",
            {"pedido_detalle": self.b["detalle"].pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        talla_a.refresh_from_db()
        self.assertEqual(talla_a.pedido_detalle_id, self.a["detalle"].pk)

    def test_talla_post_mismo_tenant_ok(self):
        """No es falso positivo: POST colgando del renglón propio -> 201."""
        resp = self._client(self.a["usuario"]).post(
            PEDIDO_DETALLE_TALLA_URL,
            {
                "pedido_detalle": self.a["detalle"].pk,
                "talla": self.talla.pk,
                "cantidad": 3,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)

    def test_talla_sin_empresa_no_ve_nada(self):
        self.assertEqual(self._ids(self.sin_empresa, PEDIDO_DETALLE_TALLA_URL), [])

    def test_talla_superuser_ve_todas_las_empresas(self):
        self.assertCountEqual(
            self._ids(self.superuser, PEDIDO_DETALLE_TALLA_URL),
            [self.a["talla_row"].pk, self.b["talla_row"].pk],
        )


class TiposServicioBordadoValidacionTests(TestCase):
    """``bordado_config.tipos_servicio``: validación en el alta de cotización.

    El defecto que cubren estos tests: ``validar_tipos_servicio_array`` lanza la
    ``ValidationError`` de **Django**, pero ``ventas/utils/helpers.py`` sólo
    atrapaba la de **DRF**. Al no ser la misma clase, la excepción escapaba del
    ``except`` y salía del view como error no manejado —HTTP 500— en lugar del
    400 que corresponde a un valor inválido enviado por el cliente.
    """

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="MTY"
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente ACME")
        cls.talla = Talla.objects.create(nombre="M")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera")
        cls.vendedor = Usuario.objects.create(
            username="vendedor@acme.test",
            email="vendedor@acme.test",
            empresa=cls.empresa,
            sucursal_default=cls.sucursal,
        )

    def _payload(self, tipos_servicio):
        """Alta mínima de cotización con una talla bordada.

        ``oportunidad`` viaja explícito porque el modelo lo declara ``null=True``
        sin ``blank=True``: DRF lo marca requerido aunque acepte ``None``.
        """
        bordado_config = {"ubicaciones": [], "puntadas": 1000}
        if tipos_servicio is not None:
            bordado_config["tipos_servicio"] = tipos_servicio
        return {
            "cotizacion": {
                "cliente": self.cliente.pk,
                "sucursal": self.sucursal.pk,
                "moneda": self.moneda.pk,
                "oportunidad": None,
                "tipo_pedido": 1,
            },
            "detalle": [
                {
                    "producto": self.producto.pk,
                    "precio_unitario": "10.00",
                    "tallas": [
                        {
                            "talla": self.talla.pk,
                            "cantidad": 2,
                            "lleva_bordado": True,
                            "bordado_config": bordado_config,
                        }
                    ],
                }
            ],
        }

    def _post(self, tipos_servicio):
        client = APIClient()
        client.force_authenticate(user=self.vendedor)
        return client.post(
            COTIZACION_ONBOARDING_URL, self._payload(tipos_servicio), format="json"
        )

    # --- el validador aislado -------------------------------------------------

    def test_validador_acepta_arreglo_valido(self):
        valores = [
            TipoServicioBordado.NUEVO_PONCHADO.value,
            TipoServicioBordado.DTF.value,
        ]
        self.assertEqual(validar_tipos_servicio_array(valores), valores)

    def test_validador_acepta_arreglo_vacio_y_none(self):
        self.assertEqual(validar_tipos_servicio_array([]), [])
        self.assertEqual(validar_tipos_servicio_array(None), [])

    def test_validador_lanza_la_validation_error_de_django(self):
        """La clase que lanza es la de Django, NO la de DRF.

        Es justo lo que hacía inútil el ``except`` del caller. Si algún día se
        cambia a la de DRF, este test avisa para revisar helpers.py.
        """
        with self.assertRaises(DjangoValidationError):
            validar_tipos_servicio_array(["NO_EXISTE"])
        self.assertNotIsInstance(DjangoValidationError("x"), DRFValidationError)

    def test_validador_rechaza_duplicados_y_no_lista(self):
        with self.assertRaises(DjangoValidationError):
            validar_tipos_servicio_array(["DTF", "DTF"])
        with self.assertRaises(DjangoValidationError):
            validar_tipos_servicio_array("DTF")

    # --- el caller: debe relanzar como error de DRF ---------------------------

    def test_save_cotizacion_detalle_relanza_como_validation_error_de_drf(self):
        """Regresión directa del defecto: la excepción que sale es la de DRF.

        Antes salía la de Django y DRF no la convertía en 400.
        """
        cotizacion = Cotizacion.objects.create(
            empresa=self.empresa, vendedor=self.vendedor, estatus=1
        )
        rows = self._payload(["NO_EXISTE"])["detalle"]
        with self.assertRaises(DRFValidationError):
            _save_cotizacion_detalle(cotizacion, rows, self.empresa, self.vendedor)

    # --- extremo a extremo por HTTP -------------------------------------------

    def test_valor_invalido_responde_400_y_no_500(self):
        resp = self._post(["NO_EXISTE"])
        self.assertEqual(resp.status_code, 400)

    def test_el_400_nombra_el_valor_invalido_y_los_aceptados(self):
        cuerpo = str(self._post(["NO_EXISTE"]).json())
        self.assertIn("NO_EXISTE", cuerpo)
        for valor in TipoServicioBordado.values:
            self.assertIn(valor, cuerpo)

    def test_duplicados_responden_400(self):
        self.assertEqual(self._post(["DTF", "DTF"]).status_code, 400)

    def test_arreglo_valido_se_acepta_y_se_persiste(self):
        valores = [
            TipoServicioBordado.SUBLIMADO.value,
            TipoServicioBordado.REVELADO.value,
        ]
        resp = self._post(valores)
        self.assertEqual(resp.status_code, 201)
        fila = CotizacionDetalleTalla.objects.get()
        self.assertEqual(fila.bordado_config["tipos_servicio"], valores)

    def test_arreglo_vacio_se_acepta(self):
        resp = self._post([])
        self.assertEqual(resp.status_code, 201)
        fila = CotizacionDetalleTalla.objects.get()
        self.assertEqual(fila.bordado_config["tipos_servicio"], [])

    def test_bordado_config_sin_la_clave_sigue_siendo_valido(self):
        """La validación sólo aplica si el cliente manda ``tipos_servicio``."""
        resp = self._post(None)
        self.assertEqual(resp.status_code, 201)
        fila = CotizacionDetalleTalla.objects.get()
        self.assertNotIn("tipos_servicio", fila.bordado_config)


class PedidoMesaControlUpdateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.empresa_b = Empresa.objects.create(
            codigo="globex", razon_social="Globex SA"
        )
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTY", nombre="Matriz"
        )
        cls.sucursal_b = Sucursal.objects.create(
            empresa=cls.empresa_b, codigo="GDL", nombre="GDL"
        )
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(
            empresa=cls.empresa,
            nombre="Cliente ACME",
            razon_social="Cliente ACME SA",
        )
        cls.talla_m = Talla.objects.create(nombre="M")
        cls.talla_l = Talla.objects.create(nombre="L")
        cls.producto_a = Producto.objects.create(
            empresa=cls.empresa, nombre="Playera Industrial", codigo="PLAY"
        )
        cls.producto_b = Producto.objects.create(
            empresa=cls.empresa, nombre="Chamarra Softshell", codigo="CHAM"
        )
        cls.admin_mesa = Usuario.objects.create(
            username="mesa_admin",
            email="mesa_admin@acme.test",
            empresa=cls.empresa,
            sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )
        cls.usuario_sin_permiso = Usuario.objects.create(
            username="vendedor",
            email="vendedor@acme.test",
            empresa=cls.empresa,
            sucursal_default=cls.sucursal,
        )
        cls.admin_otra_empresa = Usuario.objects.create(
            username="mesa_globex",
            email="mesa_globex@globex.test",
            empresa=cls.empresa_b,
            sucursal_default=cls.sucursal_b,
            is_admin_empresa=True,
        )

        cls.cotizacion = Cotizacion.objects.create(
            empresa=cls.empresa,
            sucursal=cls.sucursal,
            cliente=cls.cliente,
            moneda=cls.moneda,
            tipo_pedido=1,
            estatus=3,
            persona_pagos="Pagos Iniciales",
            correo_facturas="facturas@acme.test",
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
            subtotal="100.00",
            gran_total="116.00",
        )
        _save_cotizacion_detalle(
            cls.cotizacion,
            [
                {
                    "producto": cls.producto_a.pk,
                    "precio_lista": "100.00",
                    "precio_unitario": "95.00",
                    "costo_unitario": "70.00",
                    "tallas": [{"talla": cls.talla_m.pk, "cantidad": 2}],
                }
            ],
            cls.empresa,
            cls.admin_mesa,
        )
        CotizacionServicioExtra.objects.create(
            cotizacion=cls.cotizacion,
            nombre="Servicio inicial",
            monto="20.00",
            cantidad=1,
            visible_en_factura=True,
        )

        cls.pedido = Pedido.objects.create(
            empresa=cls.empresa,
            sucursal=cls.sucursal,
            cliente=cls.cliente,
            cotizacion=cls.cotizacion,
            moneda=cls.moneda,
            tipo_pedido=1,
            estatus=3,
            folio="P-000001",
            folio_consecutivo=1,
            persona_pagos="Pagos Iniciales",
            correo_facturas="facturas@acme.test",
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
            subtotal="100.00",
            gran_total="116.00",
        )
        pedido_det = PedidoDetalle.objects.create(
            pedido=cls.pedido,
            producto=cls.producto_a,
            precio_lista="100.00",
            precio_unitario="95.00",
            costo_unitario="70.00",
            subtotal_linea="0.00",
        )
        PedidoDetalleTalla.objects.create(
            pedido_detalle=pedido_det,
            talla=cls.talla_m,
            cantidad=2,
            precio_unitario="95.00",
            subtotal_talla="0.00",
        )
        PedidoServicioExtra.objects.create(
            pedido=cls.pedido,
            nombre="Servicio inicial",
            monto="20.00",
            cantidad=1,
            visible_en_factura=True,
        )

        cls.pedido_sin_cotizacion = Pedido.objects.create(
            empresa=cls.empresa,
            sucursal=cls.sucursal,
            cliente=cls.cliente,
            moneda=cls.moneda,
            tipo_pedido=1,
            estatus=3,
            folio="P-000002",
            folio_consecutivo=2,
            persona_pagos="Pagos",
            correo_facturas="pagos@acme.test",
            telefono_pagos="8100000011",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _payload(self):
        return {
            "pedido": {
                "sucursal": self.sucursal.pk,
                "cliente": self.cliente.pk,
                "moneda": self.moneda.pk,
                "tipo_pedido": 2,
                "recompra": True,
                "chat_online": False,
                "pedido_online": True,
                "prospeccion": False,
                "recomendacion": False,
                "amazon": False,
                "google": True,
                "publicidad": False,
                "mercado_libre": False,
                "redes_sociales": True,
                "otro": False,
                "mailing": False,
                "persona_pagos": "Pagos Mesa",
                "correo_facturas": "mesa@acme.test",
                "telefono_pagos": "8111111111",
                "oc": "OC-MESA-01",
                "forma_pago": "03",
                "metodo_pago": "PUE",
                "uso_cfdi": "G03",
                "cliente_razon_social": "Cliente ACME SA",
                "cliente_nombre": "Cliente ACME",
                "cliente_rfc": "XAXX010101000",
                "cliente_regimen_fiscal": None,
                "cliente_direccion_fiscal": "Av. Siempre Viva 123",
                "cliente_colonia": "Centro",
                "cliente_codigo_postal": "64000",
                "cliente_ciudad": "Monterrey",
                "cliente_estado": "Nuevo Leon",
                "cliente_giro_empresarial": "Industria",
                "anticipo_total": False,
                "anticipo_parcial": True,
                "vendedor_autoriza": True,
                "pago_antes_embarque": False,
                "por_confirmar": False,
                "otra_cantidad": False,
                "monto": "25.00",
                "empaque_ecologico": True,
                "embarque_parcial": False,
                "comentarios_parcialidad": "Sin parcialidades",
                "destinatario": "Recibo General",
                "empresa_envio": "Paqueteria Demo",
                "telefono_envio": "8222222222",
                "celular_envio": "8333333333",
                "direccion_envio": "Calle Entrega 456",
                "colonia_envio": "Obrera",
                "codigo_postal": "64100",
                "ciudad_envio": "Monterrey",
                "estado_envio": "Nuevo Leon",
                "referencias": "Puerta 3",
                "envio": "15.00",
                "programa_bordados": "0.00",
                "bordado_pantalones_extras": "0.00",
                "bordado_logotipo": False,
                "serigrafia": "10.00",
                "reflejante": "5.00",
                "observaciones": "Actualizado por mesa",
                "flete": "12.00",
                "seguros": "3.00",
                "anticipo": "0.00",
                "subtotal": "250.00",
                "descuento_global": "0.00",
                "ieps": "0.00",
                "iva": 16,
                "gran_total": "290.00",
            },
            "detalle": [
                {
                    "producto": self.producto_b.pk,
                    "precio_lista": "120.00",
                    "precio_unitario": "110.00",
                    "costo_unitario": "80.00",
                    "tallas": [
                        {
                            "talla": self.talla_m.pk,
                            "cantidad": 3,
                            "lleva_bordado": True,
                            "bordado_config": {"ubicaciones": [{"codigo": "PE"}]},
                        },
                        {
                            "talla": self.talla_l.pk,
                            "cantidad": 4,
                        },
                    ],
                }
            ],
            "servicios_extras": [
                {
                    "nombre": "Urgencia",
                    "monto": "50.00",
                    "cantidad": 2,
                    "visible_en_factura": False,
                }
            ],
        }

    def test_usuario_sin_permiso_recibe_error(self):
        response = self._client(self.usuario_sin_permiso).post(
            pedido_editar_mesa_control_url(self.pedido.pk),
            self._payload(),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("permiso", response.json())

    def test_admin_de_otra_empresa_recibe_404(self):
        response = self._client(self.admin_otra_empresa).post(
            pedido_editar_mesa_control_url(self.pedido.pk),
            self._payload(),
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_falla_si_el_pedido_no_tiene_cotizacion_relacionada(self):
        response = self._client(self.admin_mesa).post(
            pedido_editar_mesa_control_url(self.pedido_sin_cotizacion.pk),
            self._payload(),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("pedido", response.json())

    def test_edicion_mesa_control_actualiza_pedido_y_cotizacion_sin_inventario(self):
        response = self._client(self.admin_mesa).post(
            pedido_editar_mesa_control_url(self.pedido.pk),
            self._payload(),
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["sincronizado"])

        self.pedido.refresh_from_db()
        self.cotizacion.refresh_from_db()

        self.assertEqual(self.pedido.tipo_pedido, 2)
        self.assertEqual(self.pedido.persona_pagos, "Pagos Mesa")
        self.assertEqual(self.pedido.oc, "OC-MESA-01")
        self.assertEqual(str(self.pedido.subtotal), "250.00")
        self.assertEqual(self.pedido.folio, "P-000001")

        self.assertEqual(self.cotizacion.tipo_pedido, 2)
        self.assertEqual(self.cotizacion.persona_pagos, "Pagos Mesa")
        self.assertEqual(self.cotizacion.oc, "OC-MESA-01")
        self.assertEqual(str(self.cotizacion.subtotal), "250.00")
        self.assertEqual(self.cotizacion.estatus, 3)
        self.assertIsNotNone(self.cotizacion.aprobado_snapshot)

        pedido_detalle = self.pedido.detalles.get()
        self.assertEqual(pedido_detalle.producto_id, self.producto_b.pk)
        self.assertEqual(pedido_detalle.tallas.count(), 2)
        self.assertEqual(
            sorted(pedido_detalle.tallas.values_list("cantidad", flat=True)), [3, 4]
        )

        cot_detalle = self.cotizacion.cotizaciondetalle.get()
        self.assertEqual(cot_detalle.producto_id, self.producto_b.pk)
        self.assertEqual(cot_detalle.tallas.count(), 2)
        self.assertEqual(
            sorted(cot_detalle.tallas.values_list("cantidad", flat=True)), [3, 4]
        )
        self.assertTrue(
            cot_detalle.tallas.filter(
                talla=self.talla_m, lleva_bordado=True
            ).exists()
        )

        pedido_servicio = self.pedido.servicios_extras.get()
        self.assertEqual(pedido_servicio.nombre, "Urgencia")
        self.assertEqual(pedido_servicio.cantidad, 2)
        self.assertFalse(pedido_servicio.visible_en_factura)

        cot_servicio = self.cotizacion.servicios_extras.get()
        self.assertEqual(cot_servicio.nombre, "Urgencia")
        self.assertEqual(cot_servicio.cantidad, 2)
        self.assertFalse(cot_servicio.visible_en_factura)

        snapshot = self.cotizacion.aprobado_snapshot or {}
        self.assertEqual(snapshot.get("cotizacion", {}).get("oc"), "OC-MESA-01")
        self.assertEqual(len(snapshot.get("detalles") or []), 1)
        self.assertEqual(len(snapshot.get("servicios_extras") or []), 1)

        self.assertEqual(MovimientoInventario.objects.count(), 0)
        self.assertEqual(
            AuditoriaEvento.objects.filter(modulo="inventarios").count(), 0
        )
