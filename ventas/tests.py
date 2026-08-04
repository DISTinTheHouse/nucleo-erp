"""Tests del scope multi-tenant de ``PedidoViewSet``.

Ejecutar SIEMPRE con una BD desechable; el ``.env`` del repo apunta a Supabase
de producción. Ejemplo con un settings de override a SQLite en memoria:

    python manage.py test ventas --settings=sqlite_settings
"""

from django.test import TestCase
from rest_framework.test import APIClient

from nucleo.models import Empresa, Moneda, Sucursal
from terceros.models import Cliente
from usuarios.models import Usuario
from ventas.models import Pedido

PEDIDOS_URL = "/api/v1/ventas/pedidos/"


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
