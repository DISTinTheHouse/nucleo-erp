from django.test import TestCase
from rest_framework.test import APIClient
from usuarios.models import Usuario
from nucleo.models import Empresa
from terceros.models import Cliente, Proveedor


class ClientePatchTest(TestCase):
    def setUp(self):
        self.empresa = Empresa.objects.create(codigo="test", razon_social="Test Empresa")
        self.user = Usuario.objects.create(
            username="test",
            email="test@test.com",
            empresa=self.empresa,
            is_admin_empresa=True
        )
        self.client_api = APIClient()
        self.client_api.force_authenticate(user=self.user)
        self.cliente = Cliente.objects.create(
            empresa=self.empresa,
            nombre="Cliente Original",
            razon_social="Razón Social Original",
            correo="original@test.com",
            telefono="1234567890"
        )

    def test_patch_partial_preserves_unmodified_fields(self):
        """PATCH que solo cambia nombre NO debe borrar razon_social, correo, telefono."""
        url = f"/api/v1/terceros/clientes/{self.cliente.id}/"
        payload = {"nombre": "Cliente Modificado"}

        response = self.client_api.patch(url, payload, format="json")

        self.assertEqual(response.status_code, 200)
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.nombre, "Cliente Modificado")
        self.assertEqual(self.cliente.razon_social, "Razón Social Original")
        self.assertEqual(self.cliente.correo, "original@test.com")
        self.assertEqual(self.cliente.telefono, "1234567890")

    def test_patch_partial_can_update_optional_fields(self):
        """PATCH puede actualizar razon_social, correo, telefono explícitamente."""
        url = f"/api/v1/terceros/clientes/{self.cliente.id}/"
        payload = {"razon_social": "Nueva Razón Social"}

        response = self.client_api.patch(url, payload, format="json")

        self.assertEqual(response.status_code, 200)
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.razon_social, "Nueva Razón Social")
        self.assertEqual(self.cliente.correo, "original@test.com")

    def test_patch_partial_can_clear_optional_fields(self):
        """PATCH puede vaciar razon_social, correo, telefono enviándolos como vacío."""
        url = f"/api/v1/terceros/clientes/{self.cliente.id}/"
        payload = {"correo": "", "telefono": ""}

        response = self.client_api.patch(url, payload, format="json")

        self.assertEqual(response.status_code, 200)
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.correo, "")
        self.assertEqual(self.cliente.telefono, "")
        self.assertEqual(self.cliente.razon_social, "Razón Social Original")


class ProveedorEmpresaTests(TestCase):
    """#310: la empresa del proveedor la asigna el servidor. #315: la baja llena fecha_baja."""

    URL = "/api/v1/terceros/proveedores/"

    @classmethod
    def setUpTestData(cls):
        from nucleo.models import Moneda, SatFormaPago, SatMetodoPago, SatRegimenFiscal

        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.otra = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        cls.usuario = Usuario.objects.create(
            username="compras", email="compras@acme.test", empresa=cls.empresa, is_admin_empresa=True,
        )
        cls.regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General")
        cls.forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        cls.metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Una exhibición")
        cls.moneda_global = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.moneda_ajena = Moneda.objects.create(codigo_iso="USD", nombre="Dólar", empresa=cls.otra)

    def setUp(self):
        self.client_api = APIClient()
        self.client_api.force_authenticate(user=self.usuario)

    def _body(self, **extra):
        body = {
            "nombre": "Telas SA", "codigo": "P001", "razon_social": "Telas SA de CV",
            "telefono": "8100000000", "contacto_principal": "Ana", "rfc": "TEL010101AAA",
            "email": "ventas@telas.test", "sat_regimen_fiscal": self.regimen.pk,
            "sat_forma_pago": self.forma.pk, "sat_metodo_pago": self.metodo.pk,
            "moneda": self.moneda_global.pk,
        }
        body.update(extra)
        return body

    def _proveedor(self):
        resp = self.client_api.post(self.URL, self._body(), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        return Proveedor.objects.get(pk=resp.data["id"])

    def test_post_con_empresa_ajena_queda_en_la_del_usuario(self):
        resp = self.client_api.post(self.URL, self._body(empresa=self.otra.pk), format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Proveedor.objects.get(pk=resp.data["id"]).empresa_id, self.empresa.pk)

    def test_post_sin_empresa_queda_visible_para_el_usuario(self):
        proveedor = self._proveedor()

        self.assertEqual(proveedor.empresa_id, self.empresa.pk)
        self.assertEqual(self.client_api.get(f"{self.URL}{proveedor.pk}/").status_code, 200)

    def test_patch_no_cambia_la_empresa(self):
        proveedor = self._proveedor()

        resp = self.client_api.patch(f"{self.URL}{proveedor.pk}/", {"empresa": self.otra.pk}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        proveedor.refresh_from_db()
        self.assertEqual(proveedor.empresa_id, self.empresa.pk)

    def test_moneda_de_otra_empresa_responde_400(self):
        resp = self.client_api.post(self.URL, self._body(moneda=self.moneda_ajena.pk), format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("moneda", resp.data)

    def test_usuario_sin_empresa_no_crea_proveedor(self):
        sin_empresa = Usuario.objects.create(username="libre", email="libre@acme.test")
        self.client_api.force_authenticate(user=sin_empresa)

        resp = self.client_api.post(self.URL, self._body(), format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Proveedor.objects.exists())

    def test_baja_logica_registra_fecha_baja(self):
        proveedor = self._proveedor()

        resp = self.client_api.delete(f"{self.URL}{proveedor.pk}/")

        self.assertEqual(resp.status_code, 204)
        proveedor.refresh_from_db()
        self.assertFalse(proveedor.activo)
        self.assertIsNotNone(proveedor.fecha_baja)


    # --- #315: acumulados de solo lectura -------------------------------------

    ACUMULADOS = {
        "saldo_anterior": "10.00", "saldo_actual": "20.00", "saldo_acumulado": "30.00",
        "plazo_real_dias": 15, "fecha_ultima_compra": "2026-01-01",
        "fecha_ultimo_pago": "2026-01-02", "fecha_baja": "2026-01-03",
    }

    def test_post_y_patch_ignoran_los_acumulados(self):
        resp = self.client_api.post(self.URL, self._body(**self.ACUMULADOS), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        proveedor = Proveedor.objects.get(pk=resp.data["id"])

        self.client_api.patch(f"{self.URL}{proveedor.pk}/", self.ACUMULADOS, format="json")

        proveedor.refresh_from_db()
        self.assertEqual(proveedor.saldo_actual, 0)
        self.assertEqual(proveedor.plazo_real_dias, 0)
        self.assertIsNone(proveedor.fecha_ultima_compra)
        self.assertIsNone(proveedor.fecha_baja)
        self.assertIn("saldo_actual", resp.data)
