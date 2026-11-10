from types import SimpleNamespace

import pyotp
from auth_kit.mfa.handlers.base import MFAHandlerRegistry
from auth_kit.mfa.models import MFAMethod
from django.conf import settings
from django.contrib.admin.sites import site
from django.db import connection, transaction
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from nucleo.models import Departamento, Empresa, Sucursal
from seguridad.models import Rol, UsuarioRol
from usuarios.api.serializers import UsuarioSerializer
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


# ---------------------------------------------------------------------------
# EC-844: escalada de privilegios y cambio de empresa vía /api/auth/user/
# ---------------------------------------------------------------------------

PASSWORD = "Xq7!vLm2#Rt9"
PASSWORD_NUEVA = "Nv8!Kq2#zLm4"


class _DosEmpresasMixin:
    """Empresa A (la del usuario) y empresa B (ajena), cada una con su sucursal y
    departamento, para probar que nada de B se cuela en un usuario de A."""

    @classmethod
    def _crear_empresas(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="Monterrey")
        cls.departamento = Departamento.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, codigo="VTA", nombre="Ventas",
        )
        cls.ajena = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        cls.sucursal_ajena = Sucursal.objects.create(empresa=cls.ajena, codigo="GDL", nombre="Guadalajara")
        cls.departamento_ajeno = Departamento.objects.create(
            empresa=cls.ajena, sucursal=cls.sucursal_ajena, codigo="VTA", nombre="Ventas",
        )


class PerfilAuthUserTests(_DosEmpresasMixin, TestCase):
    """``/api/auth/user/`` (``UserView`` de auth_kit) edita solo el perfil: nombre,
    apellido, teléfono y avatar. Lo demás se ignora y la contraseña se rechaza."""

    URL = "/api/auth/user/"

    # Lo que devolvía el GET antes de EC-844 (sin los write-only ``password`` y
    # ``roles``). NextAuth lee id, username, email, first_name, last_name,
    # permisos, is_admin_empresa e is_superuser; el resto se conserva igual.
    CAMPOS_GET = [
        "id", "username", "email", "first_name", "last_name", "is_active", "estatus",
        "empresa", "sucursal_default", "sucursales", "departamentos", "telefono",
        "avatar_url", "is_admin_empresa", "is_superuser", "is_staff", "empresa_id",
        "nombre_completo", "es_admin", "permisos", "roles_ids", "date_joined", "last_login",
    ]

    @classmethod
    def setUpTestData(cls):
        cls._crear_empresas()
        # Rol de su propia empresa: el que ``validate()`` aceptaría tras la autopromoción.
        cls.rol = Rol.objects.create(empresa=cls.empresa, codigo="ADM", nombre="Admin")

    def setUp(self):
        self.user = Usuario.objects.create(
            username="operador", email="operador@acme.test", empresa=self.empresa,
            sucursal_default=self.sucursal,
        )
        self.user.set_password(PASSWORD)
        self.user.save()
        self.user.sucursales.set([self.sucursal])
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _estado(self):
        u = Usuario.objects.get(pk=self.user.pk)
        return {
            "is_superuser": u.is_superuser, "is_staff": u.is_staff,
            "is_admin_empresa": u.is_admin_empresa, "empresa": u.empresa_id,
            "sucursal_default": u.sucursal_default_id,
            "sucursales": sorted(u.sucursales.values_list("pk", flat=True)),
            "departamentos": sorted(u.departamentos.values_list("pk", flat=True)),
            "estatus": u.estatus, "is_active": u.is_active, "email": u.email,
            "username": u.username, "password": u.password,
            "roles": sorted(UsuarioRol.objects.filter(usuario=u).values_list("rol_id", flat=True)),
        }

    def test_patch_de_campos_protegidos_no_cambia_la_bd(self):
        cambios = {
            "is_superuser": True,
            "is_staff": True,
            "is_admin_empresa": True,
            "empresa": self.ajena.pk,
            "sucursal_default": self.sucursal_ajena.pk,
            "sucursales": [self.sucursal_ajena.pk],
            "departamentos": [self.departamento_ajeno.pk],
            "estatus": "bloqueado",
            "email": "otro@globex.test",
            "username": "otro",
            "roles": [self.rol.pk],
        }
        antes = self._estado()
        for campo, valor in cambios.items():
            # Cada campo sobre el estado original: savepoint que se revierte y un
            # usuario recién leído (``UserView`` edita el mismo objeto autenticado).
            with self.subTest(campo=campo), transaction.atomic():
                self.client.force_authenticate(user=Usuario.objects.get(pk=self.user.pk))
                response = self.client.patch(self.URL, {campo: valor}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(campo, response.data)
                self.assertEqual(self._estado(), antes)
                transaction.set_rollback(True)

    def test_patch_mixto_se_rechaza_entero(self):
        """Un campo editable junto a uno protegido: 400 y no se guarda ninguno."""
        antes = self._estado()
        response = self.client.patch(self.URL, {"first_name": "Ana", "is_superuser": True}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(set(response.data), {"is_superuser"})
        self.assertEqual(self._estado(), antes)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "")

    def test_promocion_en_dos_pasos_no_asigna_roles(self):
        """Antes: un PATCH con ``is_admin_empresa`` y otro con ``roles``."""
        self.client.patch(self.URL, {"is_admin_empresa": True}, format="json")
        self.user.refresh_from_db()
        self.client.force_authenticate(user=self.user)
        self.client.patch(self.URL, {"roles": [self.rol.pk]}, format="json")
        self.assertFalse(UsuarioRol.objects.filter(usuario=self.user).exists())

    def test_put_con_campos_protegidos_se_rechaza(self):
        antes = self._estado()
        response = self.client.put(self.URL, {
            "first_name": "Ana", "email": self.user.email, "is_superuser": True,
            "empresa": self.ajena.pk, "sucursal_default": self.sucursal_ajena.pk,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(set(response.data), {"email", "is_superuser", "empresa", "sucursal_default"})
        self.assertEqual(self._estado(), antes)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "")

    def test_put_solo_con_campos_editables(self):
        response = self.client.put(self.URL, {"first_name": "Ana", "last_name": "López"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name), ("Ana", "López"))

    def test_password_se_rechaza(self):
        antes = Usuario.objects.get(pk=self.user.pk).password
        response = self.client.patch(self.URL, {"password": "1"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Usuario.objects.get(pk=self.user.pk).password, antes)

    def test_los_cuatro_campos_editables_se_guardan(self):
        response = self.client.patch(self.URL, {
            "first_name": "Ana", "last_name": "López", "telefono": "8112345678",
            "avatar_url": "https://example.com/a.png",
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(
            (self.user.first_name, self.user.last_name, self.user.telefono, self.user.avatar_url),
            ("Ana", "López", "8112345678", "https://example.com/a.png"),
        )

    def test_get_conserva_los_campos(self):
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.data), self.CAMPOS_GET)
        self.assertEqual(response.data["empresa"], self.empresa.pk)
        self.assertEqual(response.data["empresa_id"], self.empresa.pk)
        self.assertEqual(response.data["sucursales"], [self.sucursal.pk])
        self.assertIs(response.data["is_superuser"], False)
        self.assertIs(response.data["is_admin_empresa"], False)


class CambioPasswordTests(TestCase):
    """``/api/auth/password/change/`` exige la contraseña actual."""

    URL = "/api/auth/password/change/"

    def setUp(self):
        self.user = Usuario.objects.create(username="operador", email="operador@acme.test")
        self.user.set_password(PASSWORD)
        self.user.save()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post(self, **extra):
        body = {"new_password1": PASSWORD_NUEVA, "new_password2": PASSWORD_NUEVA, **extra}
        return self.client.post(self.URL, body, format="json")

    def test_sin_password_actual_falla(self):
        response = self._post()
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_password_actual_incorrecta_falla(self):
        response = self._post(old_password="no-es-la-actual")
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_con_password_actual_cambia(self):
        response = self._post(old_password=PASSWORD)
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD_NUEVA))


class OrigenesPermitidosTests(TestCase):
    """CORS y ``enforce_origin`` aceptan solo orígenes explícitos: un sitio
    cualquiera en ``*.vercel.app`` no lee respuestas con la cookie ni escribe."""

    PRODUCCION = "https://lazzar-erp.vercel.app"
    DESARROLLO = "http://localhost:3000"
    ATACANTE = "https://sitio-ajeno.vercel.app"
    ESPERADOS = {
        "https://lazzar-erp.vercel.app",
        "http://localhost:3000", "https://localhost:3000",
        "http://localhost:3001", "https://localhost:3001",
        "http://localhost:3002", "https://localhost:3002",
    }

    @classmethod
    def setUpTestData(cls):
        cls.user = Usuario.objects.create(username="operador", email="operador@acme.test")

    def _cabeceras_cors(self, response):
        return [h for h in response.headers if h.lower().startswith("access-control-")]

    def _preflight(self, url, origen):
        return self.client.options(
            url, HTTP_ORIGIN=origen, HTTP_ACCESS_CONTROL_REQUEST_METHOD="PATCH",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type",
        )

    def test_la_lista_es_exactamente_la_confirmada(self):
        self.assertEqual(set(settings.CORS_ALLOWED_ORIGINS), self.ESPERADOS)
        self.assertEqual(list(settings.CORS_ALLOWED_ORIGIN_REGEXES), [])
        self.assertEqual(set(settings.CSRF_TRUSTED_ORIGINS), self.ESPERADOS)

    def test_origen_vercel_ajeno_no_recibe_cabeceras_cors(self):
        for url in ("/api/auth/token/refresh/", "/api/auth/user/"):
            with self.subTest(url=url):
                self.assertFalse(self._cabeceras_cors(self._preflight(url, self.ATACANTE)))

    def test_refresh_con_cookie_desde_origen_ajeno_no_es_legible(self):
        client = APIClient()
        client.cookies["auth-refresh-jwt"] = str(RefreshToken.for_user(self.user))
        response = client.post("/api/auth/token/refresh/", {}, format="json", HTTP_ORIGIN=self.ATACANTE)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_origenes_listados_si_reciben_cabeceras(self):
        for origen in (self.PRODUCCION, self.DESARROLLO):
            for url in ("/api/auth/token/refresh/", "/api/auth/user/"):
                with self.subTest(origen=origen, url=url):
                    response = self._preflight(url, origen)
                    self.assertEqual(response["Access-Control-Allow-Origin"], origen)
                    self.assertEqual(response["Access-Control-Allow-Credentials"], "true")

    def _patch_con_cookie(self, origen):
        client = APIClient()
        client.cookies["auth-jwt"] = str(AccessToken.for_user(self.user))
        return client.patch("/api/auth/user/", {"first_name": "Ana"}, format="json", HTTP_ORIGIN=origen)

    def test_enforce_origin_rechaza_vercel_ajeno(self):
        response = self._patch_con_cookie(self.ATACANTE)
        self.assertEqual(response.status_code, 403)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "")

    def test_enforce_origin_acepta_los_listados(self):
        for origen in (self.PRODUCCION, self.DESARROLLO):
            with self.subTest(origen=origen):
                self.assertEqual(self._patch_con_cookie(origen).status_code, 200)


class IaCrearUsuarioTests(_DosEmpresasMixin, TestCase):
    """Herramienta ``create_usuario`` del asistente: un admin de empresa no concede
    ``is_staff`` ni asigna sucursales o departamentos de otra empresa."""

    @classmethod
    def setUpTestData(cls):
        cls._crear_empresas()
        cls.admin = Usuario.objects.create(
            username="admin", email="admin@acme.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )

    def _crear(self, **extra):
        from ia.api.views import AIAssistantAPIView

        args = {
            "username": "nuevo", "email": "nuevo@acme.test", "password": PASSWORD,
            "sucursal_default": self.sucursal.pk, **extra,
        }
        return AIAssistantAPIView()._tool_create_usuario(SimpleNamespace(user=self.admin), args)

    def _no_se_creo(self):
        self.assertFalse(Usuario.objects.filter(email="nuevo@acme.test").exists())

    def test_admin_no_concede_is_staff(self):
        self.assertFalse(self._crear(is_staff=True)["ok"])
        self._no_se_creo()

    def test_admin_no_concede_ninguna_bandera_de_la_lista_compartida(self):
        """Misma lista que ``UsuarioViewSet``: vive en ``UsuarioSerializer``."""
        self.assertEqual(
            UsuarioSerializer.BANDERAS_PRIVILEGIO, ("is_superuser", "is_admin_empresa", "is_staff"),
        )
        for bandera in UsuarioSerializer.BANDERAS_PRIVILEGIO:
            with self.subTest(bandera=bandera):
                resultado = self._crear(**{bandera: True})
                self.assertFalse(resultado["ok"], resultado)
                self.assertIn(f"`{bandera}`", resultado["error"])
                self._no_se_creo()

    def test_admin_no_asigna_sucursales_ajenas(self):
        self.assertFalse(self._crear(sucursales=[self.sucursal_ajena.pk])["ok"])
        self._no_se_creo()

    def test_admin_no_asigna_departamentos_ajenos(self):
        self.assertFalse(self._crear(departamentos=[self.departamento_ajeno.pk])["ok"])
        self._no_se_creo()

    def test_alta_valida_dentro_de_la_empresa(self):
        resultado = self._crear(sucursales=[self.sucursal.pk], departamentos=[self.departamento.pk])
        self.assertTrue(resultado["ok"], resultado)
        nuevo = Usuario.objects.get(email="nuevo@acme.test")
        self.assertEqual(nuevo.empresa_id, self.empresa.pk)
        self.assertFalse(nuevo.is_staff)


class UsuarioViewSetAlcanceTests(_DosEmpresasMixin, TestCase):
    """``/api/v1/usuarios/``: sucursales, departamentos y sucursal por defecto
    deben ser de la empresa del usuario editado, aunque llegue uno solo."""

    URL = "/api/v1/usuarios/"

    @classmethod
    def setUpTestData(cls):
        cls._crear_empresas()
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

    def _patch(self, body, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.patch(f"{self.URL}{self.objetivo.pk}/", body, format="json")

    def _alcance(self):
        u = Usuario.objects.get(pk=self.objetivo.pk)
        return (
            u.empresa_id, u.sucursal_default_id,
            sorted(u.sucursales.values_list("pk", flat=True)),
            sorted(u.departamentos.values_list("pk", flat=True)),
        )

    def test_rechaza_cada_campo_ajeno_aunque_llegue_solo(self):
        cuerpos = {
            "sucursales": {"sucursales": [self.sucursal_ajena.pk]},
            "departamentos": {"departamentos": [self.departamento_ajeno.pk]},
            "sucursal_default": {"sucursal_default": self.sucursal_ajena.pk},
        }
        antes = self._alcance()
        for quien in (self.admin, self.superusuario):
            for campo, body in cuerpos.items():
                with self.subTest(usuario=quien.username, campo=campo):
                    response = self._patch(body, quien)
                    self.assertEqual(response.status_code, 400, getattr(response, "data", None))
                    self.assertIn(campo, response.data)
                    self.assertEqual(self._alcance(), antes)

    def test_campos_de_la_empresa_se_guardan(self):
        response = self._patch({
            "sucursales": [self.sucursal.pk], "departamentos": [self.departamento.pk],
            "sucursal_default": self.sucursal.pk,
        }, self.admin)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            self._alcance(),
            (self.empresa.pk, self.sucursal.pk, [self.sucursal.pk], [self.departamento.pk]),
        )

    def test_superusuario_mueve_de_empresa_con_su_alcance(self):
        response = self._patch({
            "empresa": self.ajena.pk, "sucursal_default": self.sucursal_ajena.pk,
            "sucursales": [self.sucursal_ajena.pk], "departamentos": [self.departamento_ajeno.pk],
        }, self.superusuario)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            self._alcance(),
            (self.ajena.pk, self.sucursal_ajena.pk, [self.sucursal_ajena.pk], [self.departamento_ajeno.pk]),
        )

    def test_cambiar_empresa_sin_alcance_valida_el_que_ya_tiene(self):
        """Mover al usuario de empresa sin mandar alcance falla si el que ya
        tiene es de la empresa anterior: no se queda con sucursales ajenas."""
        self.objetivo.sucursales.set([self.sucursal])
        self.objetivo.departamentos.set([self.departamento])
        antes = self._alcance()
        response = self._patch({"empresa": self.ajena.pk}, self.superusuario)
        self.assertEqual(response.status_code, 400, getattr(response, "data", None))
        self.assertEqual(
            set(response.data), {"sucursal_default", "sucursales", "departamentos"}
        )
        self.assertEqual(self._alcance(), antes)

    def test_cambiar_empresa_reemplazando_parte_del_alcance(self):
        """Lo que llega se valida tal cual; lo que no llega, con su valor actual."""
        self.objetivo.sucursales.set([self.sucursal])
        response = self._patch({
            "empresa": self.ajena.pk, "sucursal_default": self.sucursal_ajena.pk,
        }, self.superusuario)
        self.assertEqual(response.status_code, 400, getattr(response, "data", None))
        self.assertEqual(set(response.data), {"sucursales"})

    def test_empresa_nula_con_sucursales_falla(self):
        """``empresa: null`` deja al usuario sin empresa: ninguna sucursal es
        suya, aunque fuera de la empresa que tenía."""
        antes = self._alcance()
        response = self._patch({"empresa": None, "sucursales": [self.sucursal.pk]}, self.superusuario)
        self.assertEqual(response.status_code, 400, getattr(response, "data", None))
        self.assertIn("sucursales", response.data)
        self.assertEqual(self._alcance(), antes)

    def test_patch_sin_alcance_no_consulta_empresa(self):
        """La empresa destino se compara por id: un PATCH sin campos de alcance
        no lee la fila de ``empresas`` del usuario editado."""
        client = APIClient()
        client.force_authenticate(user=self.superusuario)
        objetivo = Usuario.objects.get(pk=self.objetivo.pk)  # sin la empresa en caché
        with CaptureQueriesContext(connection) as ctx:
            response = client.patch(f"{self.URL}{objetivo.pk}/", {"first_name": "Ana"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(
            [q["sql"] for q in ctx.captured_queries if 'FROM "empresas"' in q["sql"]]
        )

    def test_alta_de_admin_con_sucursal_ajena_falla(self):
        client = APIClient()
        client.force_authenticate(user=self.admin)
        response = client.post(self.URL, {
            "username": "nuevo", "email": "nuevo@acme.test", "password": PASSWORD,
            "empresa": self.empresa.pk, "sucursal_default": self.sucursal.pk,
            "sucursales": [self.sucursal_ajena.pk],
        }, format="json")
        self.assertEqual(response.status_code, 400, getattr(response, "data", None))
        self.assertFalse(Usuario.objects.filter(email="nuevo@acme.test").exists())


class SuperusuarioEmpresaActivaTests(_DosEmpresasMixin, TestCase):
    """EC-843: en ``/api/v1/usuarios/`` el superusuario solo ve y edita usuarios
    de su empresa activa, como en el resto del ERP (EC-798). Puede mover a uno de
    ellos a otra empresa, pero su propia empresa solo cambia por
    ``/api/v1/nucleo/cambiar-empresa/``."""

    URL = "/api/v1/usuarios/"

    @classmethod
    def setUpTestData(cls):
        cls._crear_empresas()
        cls.ajeno = Usuario.objects.create(
            username="ajeno", email="ajeno@globex.test", empresa=cls.ajena,
            sucursal_default=cls.sucursal_ajena,
        )

    def setUp(self):
        self.root = Usuario.objects.create(
            username="root", email="root@acme.test", empresa=self.empresa,
            sucursal_default=self.sucursal, is_superuser=True,
        )
        self.objetivo = Usuario.objects.create(
            username="operador", email="operador@acme.test", empresa=self.empresa,
            sucursal_default=self.sucursal,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.root)

    def _alcance(self, user):
        u = Usuario.objects.get(pk=user.pk)
        return (
            u.empresa_id, u.sucursal_default_id,
            sorted(u.sucursales.values_list("pk", flat=True)),
            sorted(u.departamentos.values_list("pk", flat=True)),
        )

    def test_lista_solo_su_empresa_activa(self):
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200)
        ids = {u["id"] for u in response.data}
        self.assertEqual(ids, {self.root.pk, self.objetivo.pk})

    def test_usuario_de_otra_empresa_da_404(self):
        url = f"{self.URL}{self.ajeno.pk}/"
        antes = self._alcance(self.ajeno)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, {"first_name": "X"}, format="json").status_code, 404)
        self.assertEqual(self.client.put(url, {"first_name": "X"}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertTrue(Usuario.objects.filter(pk=self.ajeno.pk, first_name="").exists())
        self.assertEqual(self._alcance(self.ajeno), antes)

    def test_sin_empresa_activa_no_ve_a_nadie(self):
        Usuario.objects.filter(pk=self.root.pk).update(empresa=None, sucursal_default=None)
        self.root.refresh_from_db()
        self.client.force_authenticate(user=self.root)
        self.assertEqual(self.client.get(self.URL).data, [])
        self.assertEqual(self.client.get(f"{self.URL}{self.objetivo.pk}/").status_code, 404)

    def test_mueve_a_un_usuario_de_su_empresa_y_deja_de_verlo(self):
        response = self.client.patch(f"{self.URL}{self.objetivo.pk}/", {
            "empresa": self.ajena.pk, "sucursal_default": self.sucursal_ajena.pk,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self._alcance(self.objetivo), (self.ajena.pk, self.sucursal_ajena.pk, [], []))
        self.assertEqual(self.client.get(f"{self.URL}{self.objetivo.pk}/").status_code, 404)

    def test_no_cambia_su_propia_empresa_aqui(self):
        """Ni con el alcance de la empresa destino ni dejándose sin empresa: 403
        y nada cambia. Las reglas de alcance (400) se revisan antes, como el
        resto de validaciones de ``UsuarioSerializer``."""
        antes = self._alcance(self.root)
        cuerpos = (
            {"empresa": self.ajena.pk, "sucursal_default": self.sucursal_ajena.pk},
            {"empresa": None, "sucursal_default": None},
        )
        for body in cuerpos:
            with self.subTest(body=body):
                response = self.client.patch(f"{self.URL}{self.root.pk}/", body, format="json")
                self.assertEqual(response.status_code, 403, response.data)
                self.assertIn("cambiar-empresa", str(response.data["detail"]))
                self.assertEqual(self._alcance(self.root), antes)

    def test_editarse_sin_cambiar_de_empresa_sigue_funcionando(self):
        """El front edita con PUT/PATCH reenviando la empresa que ya tiene."""
        response = self.client.patch(f"{self.URL}{self.root.pk}/", {
            "empresa": self.empresa.pk, "first_name": "Root",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.root.refresh_from_db()
        self.assertEqual((self.root.empresa_id, self.root.first_name), (self.empresa.pk, "Root"))


class MfaCodigoTests(TestCase):
    """Quitar el método MFA activo o cambiar el primario exige un código válido."""

    @classmethod
    def setUpTestData(cls):
        cls.user = Usuario.objects.create(username="operador", email="operador@acme.test")

    def setUp(self):
        self.app = MFAMethod.objects.create(
            user=self.user, name="app", secret=pyotp.random_base32(), is_primary=True, is_active=True,
        )
        self.email = MFAMethod.objects.create(
            user=self.user, name="email", secret=pyotp.random_base32(), is_primary=False, is_active=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _codigo(self, metodo):
        return MFAHandlerRegistry.get_handler(metodo).get_otp_code()

    def test_borrar_metodo_activo_sin_codigo_falla(self):
        response = self.client.post("/api/auth/mfa/delete/", {"method": "app"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(MFAMethod.objects.filter(pk=self.app.pk).exists())

    def test_borrar_metodo_activo_con_codigo_incorrecto_falla(self):
        response = self.client.post("/api/auth/mfa/delete/", {"method": "app", "code": "000000"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(MFAMethod.objects.filter(pk=self.app.pk).exists())

    def test_borrar_metodo_activo_con_codigo_valido(self):
        response = self.client.post(
            "/api/auth/mfa/delete/", {"method": "email", "code": self._codigo(self.email)}, format="json",
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(MFAMethod.objects.filter(pk=self.email.pk).exists())

    def test_cambiar_primario_sin_codigo_falla(self):
        response = self.client.post("/api/auth/mfa/primary/", {"method": "email"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.app.refresh_from_db()
        self.assertTrue(self.app.is_primary)

    def test_cambiar_primario_con_codigo_del_primario(self):
        response = self.client.post(
            "/api/auth/mfa/primary/", {"method": "email", "primary_code": self._codigo(self.app)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.email.refresh_from_db()
        self.assertTrue(self.email.is_primary)
