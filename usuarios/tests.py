from django.contrib.admin.sites import site
from django.test import RequestFactory, TestCase
from rest_framework.test import APIClient

from nucleo.models import Empresa, Sucursal
from usuarios.models import Usuario
from wms.models import RfidScan


class BanderasPrivilegioTests(TestCase):
    """#306: un admin de empresa no concede is_admin_empresa / is_staff / is_superuser."""

    URL = "/api/v1/usuarios/"

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="Monterrey")
        cls.admin = Usuario.objects.create(
            username="admin", email="admin@acme.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.superusuario = Usuario.objects.create(
            username="root", email="root@acme.test", empresa=cls.empresa, is_superuser=True,
        )

    def setUp(self):
        self.objetivo = Usuario.objects.create(
            username="operador", email="operador@acme.test", empresa=self.empresa,
            sucursal_default=self.sucursal,
        )

    def _patch(self, body, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.admin)
        return client.patch(f"{self.URL}{self.objetivo.pk}/", body, format="json")

    def test_admin_no_concede_banderas_al_editar(self):
        for bandera in ("is_admin_empresa", "is_staff", "is_superuser"):
            with self.subTest(bandera=bandera):
                resp = self._patch({bandera: True})

                self.assertEqual(resp.status_code, 403, resp.data)
                self.objetivo.refresh_from_db()
                self.assertFalse(getattr(self.objetivo, bandera))

    def test_reenviar_bandera_que_ya_tiene_no_falla(self):
        Usuario.objects.filter(pk=self.objetivo.pk).update(is_admin_empresa=True)

        resp = self._patch({"is_admin_empresa": True, "first_name": "Ana"})

        self.assertEqual(resp.status_code, 200, resp.data)

    def test_admin_no_concede_is_staff_al_crear(self):
        client = APIClient()
        client.force_authenticate(user=self.admin)

        resp = client.post(self.URL, {
            "username": "nuevo", "email": "nuevo@acme.test", "password": "x",
            "empresa": self.empresa.pk, "sucursal_default": self.sucursal.pk, "is_staff": True,
        }, format="json")

        self.assertEqual(resp.status_code, 403, resp.data)
        self.assertFalse(Usuario.objects.filter(username="nuevo").exists())

    def test_superusuario_si_puede_conceder(self):
        resp = self._patch({"is_admin_empresa": True}, user=self.superusuario)

        self.assertEqual(resp.status_code, 200, resp.data)
        self.objetivo.refresh_from_db()
        self.assertTrue(self.objetivo.is_admin_empresa)


class RfidScanAdminTests(TestCase):
    """#306: en el admin de Django, staff no superusuario solo ve lecturas de su empresa."""

    def test_staff_solo_ve_lecturas_de_su_empresa(self):
        empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        otra = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        propia = RfidScan.objects.create(empresa=empresa, epc="EPC-A")
        RfidScan.objects.create(empresa=otra, epc="EPC-B")
        staff = Usuario.objects.create(username="staff", email="s@acme.test", empresa=empresa, is_staff=True)
        request = RequestFactory().get("/admin/wms/rfidscan/")
        request.user = staff

        qs = site._registry[RfidScan].get_queryset(request)

        self.assertEqual(list(qs), [propia])
