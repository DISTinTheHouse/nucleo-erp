"""Tests del scope multi-tenant de ``PedidoViewSet``.

Ejecutar SIEMPRE con una BD desechable; el ``.env`` del repo apunta a Supabase
de producción. Ejemplo con un settings de override a SQLite en memoria:

    python manage.py test ventas --settings=sqlite_settings
"""

from django.test import TestCase
from rest_framework.test import APIClient

from catalogo.models import Producto, Talla
from nucleo.models import Empresa, Moneda, Sucursal
from terceros.models import Cliente
from usuarios.models import Usuario
from ventas.models import Pedido, PedidoDetalle, PedidoDetalleTalla

PEDIDOS_URL = "/api/v1/ventas/pedidos/"
PEDIDO_DETALLE_URL = "/api/v1/ventas/pedido-detalle/"
PEDIDO_DETALLE_TALLA_URL = "/api/v1/ventas/pedido-detalle-talla/"


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
