"""Logística: alcance de ``/api/v1/logistica/envios/``."""

from django.test import TestCase
from rest_framework.test import APIClient

from logistica.models import Envio
from nucleo.models import Empresa, Moneda, Sucursal
from terceros.models import Cliente, Transportista
from usuarios.models import Usuario
from ventas.models import Pedido

URL = "/api/v1/logistica/envios/"


class EnvioAlcanceTests(TestCase):
    """Empresa activa para todos, superusuario incluido (EC-816/EC-798); dentro de
    ella, el usuario normal solo ve sus sucursales."""

    @classmethod
    def setUpTestData(cls):
        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.empresa = Empresa.objects.create(codigo="log-a", razon_social="Logistica A")
        cls.otra = Empresa.objects.create(codigo="log-b", razon_social="Logistica B")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="A1", nombre="A1")
        cls.sucursal2 = Sucursal.objects.create(empresa=cls.empresa, codigo="A2", nombre="A2")
        cls.sucursal_otra = Sucursal.objects.create(empresa=cls.otra, codigo="B1", nombre="B1")

        def envio(empresa, sucursal):
            pedido = Pedido.objects.create(
                empresa=empresa, sucursal=sucursal, moneda=moneda,
                cliente=Cliente.objects.create(empresa=empresa, nombre="Cliente"),
            )
            return Envio.objects.create(
                empresa=empresa, sucursal=sucursal, pedido=pedido,
                transportista=Transportista.objects.create(empresa=empresa),
            )

        cls.propio = envio(cls.empresa, cls.sucursal)
        cls.otra_sucursal = envio(cls.empresa, cls.sucursal2)
        cls.ajeno = envio(cls.otra, cls.sucursal_otra)
        cls.superusuario = Usuario.objects.create(
            username="log-su", email="log-su@example.com", empresa=cls.empresa, is_superuser=True,
        )
        cls.usuario = Usuario.objects.create(
            username="log-u", email="log-u@example.com", empresa=cls.empresa, sucursal_default=cls.sucursal,
        )
        cls.sin_empresa = Usuario.objects.create(
            username="log-x", email="log-x@example.com", is_superuser=True,
        )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _ids(self, user):
        resp = self._client(user).get(URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        filas = data["results"] if isinstance(data, dict) else data
        return {f["id"] for f in filas}

    def test_superusuario_solo_ve_su_empresa_activa(self):
        self.assertEqual(self._ids(self.superusuario), {self.propio.pk, self.otra_sucursal.pk})
        self.assertEqual(self._client(self.superusuario).get(f"{URL}{self.ajeno.pk}/").status_code, 404)

    def test_superusuario_sin_empresa_activa_no_ve_nada(self):
        self.assertEqual(self._ids(self.sin_empresa), set())

    def test_usuario_normal_solo_ve_sus_sucursales(self):
        self.assertEqual(self._ids(self.usuario), {self.propio.pk})
        self.assertEqual(self._client(self.usuario).get(f"{URL}{self.otra_sucursal.pk}/").status_code, 404)

    def test_alta_exige_empresa_activa(self):
        propio = Transportista.objects.create(empresa=self.empresa)
        resp = self._client(self.sin_empresa).post(
            URL, {"pedido": self.propio.pedido_id, "transportista": propio.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("empresa", resp.json())

    def test_pedido_o_transportista_ajeno_responde_como_inexistente(self):
        """#510: un id de otra empresa no se distingue de uno que no existe."""
        propio = Transportista.objects.create(empresa=self.empresa)
        ajeno = Transportista.objects.create(empresa=self.otra)
        inexistente = 999999

        def error(pedido_id, transportista_id, campo):
            resp = self._client(self.superusuario).post(
                URL, {"pedido": pedido_id, "transportista": transportista_id}, format="json"
            )
            self.assertEqual(resp.status_code, 400, resp.content)
            self.assertEqual(list(resp.json()), [campo])
            return [m.replace(str(pedido_id if campo == "pedido" else transportista_id), "N")
                    for m in resp.json()[campo]]

        pedido = self.propio.pedido_id
        self.assertEqual(
            error(self.ajeno.pedido_id, propio.pk, "pedido"), error(inexistente, propio.pk, "pedido"),
        )
        self.assertEqual(
            error(pedido, ajeno.pk, "transportista"), error(pedido, inexistente, "transportista"),
        )

    def test_alta_en_la_empresa_activa(self):
        propio = Transportista.objects.create(empresa=self.empresa)
        resp = self._client(self.superusuario).post(
            URL, {"pedido": self.propio.pedido_id, "transportista": propio.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(Envio.objects.get(pk=resp.json()["id"]).empresa_id, self.empresa.pk)
