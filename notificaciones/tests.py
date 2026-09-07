"""Tests del scope de ``notificaciones``: una notificación es de UN destinatario.

Ejecutar SIEMPRE con una BD desechable; el ``.env`` del repo apunta a Supabase
de producción. Ejemplo con un settings de override a SQLite en memoria:

    python manage.py test notificaciones --settings=sqlite_settings
"""

from django.test import TestCase
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIClient

from notificaciones.models import Notificacion
from notificaciones.services.notificacion_service import marcar_leida
from nucleo.models import Empresa
from usuarios.models import Usuario

NOTIFICACIONES_URL = "/api/v1/notificaciones/"
SIN_LEER_COUNT_URL = "/api/v1/notificaciones/sin-leer/count/"
MARCAR_TODAS_LEIDAS_URL = "/api/v1/notificaciones/marcar-todas-leidas/"


def notificacion_detail_url(pk):
    return f"/api/v1/notificaciones/{pk}/"


def marcar_leida_url(pk):
    return f"/api/v1/notificaciones/{pk}/marcar-leida/"


class NotificacionScopeTests(TestCase):
    """``NotificacionViewSet``: quién ve y quién marca qué notificación.

    El branch de interés: ``_puede_ver_todo`` (``is_superuser`` o
    ``is_admin_empresa``) cortocircuitaba las tres rutas de acceso. En el
    listado devolvía el queryset sin filtrar por ``usuario`` —el admin veía la
    copia de cada destinatario— y sin filtrar siquiera por ``empresa``, que es
    una fuga cross-tenant.
    """

    @classmethod
    def _notificacion(cls, empresa, usuario, titulo, leido=False):
        return Notificacion.objects.create(
            empresa=empresa,
            usuario=usuario,
            titulo=titulo,
            mensaje="Cotización lista para revisión",
            modulo="ventas",
            tipo="cotizacion_en_revision",
            leido=leido,
        )

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme-notif", razon_social="ACME SA")
        cls.empresa_b = Empresa.objects.create(
            codigo="globex-notif", razon_social="Globex SA"
        )

        cls.usuario = Usuario.objects.create(
            username="operativo_notif",
            email="operativo_notif@acme.test",
            empresa=cls.empresa,
        )
        cls.admin_empresa = Usuario.objects.create(
            username="admin_notif",
            email="admin_notif@acme.test",
            empresa=cls.empresa,
            is_admin_empresa=True,
        )
        # ``createsuperuser`` no pide empresa: el caso real es superuser con
        # ``empresa=None`` que sí es destinatario de notificaciones.
        cls.superuser = Usuario.objects.create(
            username="root_notif",
            email="root_notif@acme.test",
            empresa=None,
            is_superuser=True,
        )
        cls.usuario_b = Usuario.objects.create(
            username="operativo_globex",
            email="operativo_globex@globex.test",
            empresa=cls.empresa_b,
        )
        # Multiempresa: su empresa ACTIVA es B, pero tiene acceso a A y recibe
        # notificaciones creadas para A (``crear_notificacion_por_rol`` las
        # asigna por ``rol__empresa``, no por la empresa activa del usuario).
        cls.usuario_multiempresa = Usuario.objects.create(
            username="multi_notif",
            email="multi_notif@acme.test",
            empresa=cls.empresa_b,
        )
        cls.usuario_multiempresa.empresas.add(cls.empresa, cls.empresa_b)

        cls.notif_usuario = cls._notificacion(cls.empresa, cls.usuario, "Para operativo")
        cls.notif_admin = cls._notificacion(cls.empresa, cls.admin_empresa, "Para admin")
        cls.notif_superuser = cls._notificacion(cls.empresa, cls.superuser, "Para root")
        cls.notif_otra_empresa = cls._notificacion(
            cls.empresa_b, cls.usuario_b, "Para globex"
        )
        cls.notif_multi_empresa_a = cls._notificacion(
            cls.empresa, cls.usuario_multiempresa, "Para multi, empresa A"
        )
        cls.notif_multi_empresa_b = cls._notificacion(
            cls.empresa_b, cls.usuario_multiempresa, "Para multi, empresa B"
        )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _ids(self, user):
        resp = self._client(user).get(NOTIFICACIONES_URL)
        self.assertEqual(resp.status_code, 200)
        return {row["id"] for row in resp.json()}

    # --- listado: el branch roto ---------------------------------------------

    def test_admin_empresa_solo_ve_sus_propias_notificaciones(self):
        """Antes veía la copia de cada destinatario de su empresa."""
        self.assertEqual(self._ids(self.admin_empresa), {self.notif_admin.pk})

    def test_superuser_solo_ve_sus_propias_notificaciones(self):
        self.assertEqual(self._ids(self.superuser), {self.notif_superuser.pk})

    def test_admin_empresa_no_ve_notificaciones_de_otra_empresa(self):
        """Fuga cross-tenant: el bypass omitía también el filtro por empresa."""
        self.assertNotIn(self.notif_otra_empresa.pk, self._ids(self.admin_empresa))
        self.assertNotIn(self.notif_otra_empresa.pk, self._ids(self.superuser))

    def test_usuario_no_admin_sigue_viendo_solo_las_suyas(self):
        self.assertEqual(self._ids(self.usuario), {self.notif_usuario.pk})

    # --- el alcance es la FK ``usuario``, nunca la empresa ---------------------

    def test_superuser_sin_empresa_ve_las_suyas(self):
        """Un filtro por empresa le dejaba el listado vacío para siempre."""
        self.assertIsNone(self.superuser.empresa)
        self.assertEqual(self._ids(self.superuser), {self.notif_superuser.pk})

    def test_multiempresa_ve_las_suyas_de_cualquier_empresa(self):
        """Su empresa activa es B; la de la empresa A también es suya."""
        self.assertEqual(
            self._ids(self.usuario_multiempresa),
            {self.notif_multi_empresa_a.pk, self.notif_multi_empresa_b.pk},
        )

    # --- retrieve -------------------------------------------------------------

    def test_admin_no_puede_hacer_retrieve_de_notificacion_ajena(self):
        resp = self._client(self.admin_empresa).get(
            notificacion_detail_url(self.notif_usuario.pk)
        )
        self.assertEqual(resp.status_code, 404)

    def test_admin_no_puede_hacer_retrieve_de_otra_empresa(self):
        resp = self._client(self.admin_empresa).get(
            notificacion_detail_url(self.notif_otra_empresa.pk)
        )
        self.assertEqual(resp.status_code, 404)

    # --- conteo del badge -----------------------------------------------------

    def test_conteo_sin_leer_es_por_usuario(self):
        resp = self._client(self.admin_empresa).get(SIN_LEER_COUNT_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 1)
        self.assertEqual(
            resp.json()["ultima_notificacion_id"], self.notif_admin.pk
        )

    # --- marcar-leida ---------------------------------------------------------

    def test_admin_no_puede_marcar_leida_la_notificacion_de_otro(self):
        """Antes se la desaparecía del pendiente a su verdadero destinatario."""
        resp = self._client(self.admin_empresa).post(
            marcar_leida_url(self.notif_usuario.pk)
        )
        self.assertIn(resp.status_code, (403, 404))
        self.notif_usuario.refresh_from_db()
        self.assertFalse(self.notif_usuario.leido)

    def test_marcar_leida_servicio_rechaza_a_un_admin_ajeno(self):
        with self.assertRaises(PermissionDenied):
            marcar_leida(self.notif_usuario, self.admin_empresa)
        self.notif_usuario.refresh_from_db()
        self.assertFalse(self.notif_usuario.leido)

    def test_cada_quien_puede_marcar_la_suya(self):
        resp = self._client(self.admin_empresa).post(
            marcar_leida_url(self.notif_admin.pk)
        )
        self.assertEqual(resp.status_code, 200)
        self.notif_admin.refresh_from_db()
        self.assertTrue(self.notif_admin.leido)

    # --- no regresión: rutas que ya eran correctas ----------------------------

    def test_marcar_todas_leidas_sigue_siendo_por_usuario(self):
        resp = self._client(self.admin_empresa).post(MARCAR_TODAS_LEIDAS_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["actualizadas"], 1)
        self.notif_usuario.refresh_from_db()
        self.assertFalse(self.notif_usuario.leido)

    def test_lectura_y_marcar_todas_leidas_cubren_el_mismo_conjunto(self):
        """Con un filtro por empresa en la lectura, las dos rutas divergían:
        ``marcar-todas-leidas`` marcaba filas que el listado nunca mostró."""
        client = self._client(self.usuario_multiempresa)
        visibles = self._ids(self.usuario_multiempresa)

        resp = client.post(MARCAR_TODAS_LEIDAS_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["actualizadas"], len(visibles))

        leidas = set(
            Notificacion.objects.filter(
                usuario=self.usuario_multiempresa, leido=True
            ).values_list("id", flat=True)
        )
        self.assertEqual(leidas, visibles)
        self.assertEqual(
            client.get(SIN_LEER_COUNT_URL).json()["count"], 0
        )
