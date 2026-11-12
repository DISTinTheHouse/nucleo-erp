"""Tests de Planeación: tablero de rotación y necesidades de materia prima (EC-875).

Correr SOLO con ``--settings=ERP.settings_test`` (Postgres local, ver ``CLAUDE.md``):

    python manage.py test --settings=ERP.settings_test --keepdb planeacion
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework.views import APIView

from catalogo.models import Color, Producto, ProductoVariante, Talla, TipoProducto
from nucleo.models import Empresa, Moneda, Sucursal
from nucleo.permisos import RequiereCodigo
from seguridad.models import Permiso, Rol, RolPermiso, UsuarioPermiso, UsuarioRol
from terceros.models import Cliente
from usuarios.models import Usuario
from ventas.models import Pedido, PedidoDetalle, PedidoDetalleTalla

TABLERO_URL = "/api/v1/planeacion/tablero-rotacion/"
NECESIDADES_URL = "/api/v1/planeacion/necesidades-materia-prima/"
URLS = (TABLERO_URL, NECESIDADES_URL)


def _tipo_producto():
    """Tipo de producto obligatorio (#871); PT tiene pk=3 como en producción."""
    return TipoProducto.objects.get_or_create(codigo="PT", defaults={"pk": 3, "nombre": "Producto terminado"})[0]


def _n(valor):
    """Cantidad del JSON como ``Decimal`` (la respuesta trae ``Decimal`` sin serializer)."""
    return Decimal(str(valor))


def _local(dia, hora=time(12, 0)):
    """``datetime`` aware en la zona local para ese día y hora."""
    return timezone.make_aware(datetime.combine(dia, hora))


class PlaneacionBaseTestCase(TestCase):
    """Dos empresas, cada una con un producto terminado y variantes propias."""

    @classmethod
    def setUpTestData(cls):
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.talla = Talla.objects.create(nombre="M")
        cls.color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000")
        cls.a = cls._tenant("plan-a")
        cls.b = cls._tenant("plan-b")
        cls.superuser = Usuario.objects.create(
            username="root-plan", email="root@plan.test", is_superuser=True,
            empresa=cls.a["empresa"], sucursal_default=cls.a["sucursal"],
        )

    @classmethod
    def _tenant(cls, codigo):
        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        sucursal = Sucursal.objects.create(empresa=empresa, codigo=codigo[-1].upper(), nombre=codigo)
        cliente = Cliente.objects.create(empresa=empresa, nombre=f"Cliente {codigo}")
        admin = Usuario.objects.create(
            username=f"admin-{codigo}", email=f"admin@{codigo}.test", empresa=empresa,
            sucursal_default=sucursal, is_admin_empresa=True,
        )
        producto = Producto.objects.create(tipo=_tipo_producto(), empresa=empresa, nombre=f"Playera {codigo}")
        return {"empresa": empresa, "sucursal": sucursal, "cliente": cliente, "admin": admin, "producto": producto}

    @classmethod
    def _variante(cls, t, sku):
        return ProductoVariante.objects.create(
            producto=t["producto"], empresa=t["empresa"], color=cls.color, talla=cls.talla, sku=sku, precio_base="1",
        )

    @classmethod
    def _venta(cls, t, variante, cantidad, creado, fecha_confirmacion=None):
        """Pedido de venta AUTORIZADO con una talla de ``variante``, dado de alta en ``creado``."""
        pedido = Pedido.objects.create(
            empresa=t["empresa"], sucursal=t["sucursal"], cliente=t["cliente"], moneda=cls.moneda,
            folio=f"P-{Pedido.objects.count() + 1:05d}", persona_pagos="Pagos", correo_facturas="pagos@plan.test",
            telefono_pagos="8100000000", forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
            estatus=Pedido.ESTATUS_AUTORIZADA, fecha_confirmacion=fecha_confirmacion,
        )
        # ``created_at`` es ``auto_now_add``: se fija con ``update``.
        Pedido.objects.filter(pk=pedido.pk).update(created_at=creado)
        detalle = PedidoDetalle.objects.create(pedido=pedido, producto=t["producto"], precio_unitario="10")
        PedidoDetalleTalla.objects.create(pedido_detalle=detalle, talla=cls.talla, cantidad=cantidad, variante=variante)
        return pedido

    @classmethod
    def _rol_con(cls, empresa, codigo, claves):
        rol = Rol.objects.create(empresa=empresa, codigo=codigo, nombre=codigo, estatus=Rol.Estatus.ACTIVO)
        for clave in claves:
            permiso, _ = Permiso.objects.get_or_create(clave=clave, defaults={"nombre": clave})
            RolPermiso.objects.create(rol=rol, permiso=permiso)
        return rol

    def _usuario(self, sufijo, claves=(), empresa_rol=None):
        """Usuario normal de la empresa A con un rol de ``claves`` en ``empresa_rol`` (A por default)."""
        user = Usuario.objects.create(
            username=f"plan-{sufijo}", email=f"plan-{sufijo}@plan.test",
            empresa=self.a["empresa"], sucursal_default=self.a["sucursal"],
        )
        if claves:
            empresa_rol = empresa_rol or self.a["empresa"]
            rol = self._rol_con(empresa_rol, f"rol-{sufijo}", claves)
            UsuarioRol.objects.create(usuario=user, rol=rol, empresa=empresa_rol)
        return user

    def _get(self, user, url=TABLERO_URL, **params):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(url, {"page_size": 500, **params})

    def _fila(self, data, variante):
        filas = [r for r in data["results"] if r["variante_id"] == variante.pk]
        self.assertEqual(len(filas), 1, f"la variante {variante.sku} no vino en el tablero")
        return filas[0]


class PermisoPlaneacionTests(PlaneacionBaseTestCase):
    """Ambos endpoints exigen ``R-PRODUCCION`` con la regla de ``permisos_efectivos``."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls._variante(cls.a, "A-1")

    def _assert_status(self, user, esperado):
        for url in URLS:
            with self.subTest(url=url, usuario=user.username):
                self.assertEqual(self._get(user, url).status_code, esperado)

    def test_sin_codigo_responde_403(self):
        self._assert_status(self._usuario("sin"), 403)

    def test_codigo_de_otro_modulo_no_basta(self):
        self._assert_status(self._usuario("crm", ["R-CRM", "R-PRODUCCION-OP"]), 403)

    def test_con_r_produccion_responde_200(self):
        self._assert_status(self._usuario("prod", ["R-PRODUCCION"]), 200)

    def test_superusuario_pasa(self):
        self._assert_status(self.superuser, 200)

    def test_admin_de_empresa_pasa(self):
        self._assert_status(self.a["admin"], 200)

    def test_override_deny_gana_al_rol(self):
        user = self._usuario("deny", ["R-PRODUCCION"])
        UsuarioPermiso.objects.create(
            usuario=user, permiso=Permiso.objects.get(clave="R-PRODUCCION"), tipo=UsuarioPermiso.TIPO_DENY,
        )
        self._assert_status(user, 403)

    def test_rol_de_otra_empresa_no_cuenta(self):
        self._assert_status(self._usuario("otra", ["R-PRODUCCION"], empresa_rol=self.b["empresa"]), 403)

    def test_anonimo_no_entra(self):
        for url in URLS:
            with self.subTest(url=url):
                self.assertIn(APIClient().get(url).status_code, (401, 403))


class RequiereCodigoTests(SimpleTestCase):
    """``RequiereCodigo`` falla cerrado: sin códigos es error de configuración."""

    def _vista(self, **attrs):
        return type("VistaPrueba", (APIView,), attrs)()

    def test_sin_codigos_declarados_es_error_de_configuracion(self):
        request = APIRequestFactory().get("/x/")
        for attrs in ({}, {"codigos_requeridos": ()}, {"codigos_requeridos": None}):
            with self.subTest(attrs=attrs), self.assertRaises(ImproperlyConfigured):
                RequiereCodigo().has_permission(request, self._vista(**attrs))


class AislamientoPlaneacionTests(PlaneacionBaseTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.va = cls._variante(cls.a, "A-1")
        cls.vb = cls._variante(cls.b, "B-1")
        hace_dias = timezone.localdate() - timedelta(days=3)
        cls._venta(cls.a, cls.va, 4, _local(hace_dias))
        cls._venta(cls.b, cls.vb, 9, _local(hace_dias))

    def test_variante_de_otra_empresa_responde_404(self):
        resp = self._get(self.a["admin"], NECESIDADES_URL, variante=self.vb.pk)
        self.assertEqual(resp.status_code, 404)

    def test_solo_aparecen_variantes_y_ventas_de_la_empresa_activa(self):
        for user in (self.a["admin"], self.superuser):
            with self.subTest(usuario=user.username):
                data = self._get(user).json()
                self.assertEqual([r["variante_id"] for r in data["results"]], [self.va.pk])
                self.assertEqual(_n(data["resumen"]["vendido_periodo"]), 4)

    def test_sucursal_de_otra_empresa_no_trae_ventas(self):
        for url in URLS:
            with self.subTest(url=url):
                resp = self._get(self.a["admin"], url, sucursal=self.b["sucursal"].pk)
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                if url == TABLERO_URL:
                    self.assertEqual(_n(data["resumen"]["vendido_periodo"]), 0)
                    self.assertTrue(all(_n(r["vendido_periodo"]) == 0 for r in data["results"]))
                else:
                    self.assertEqual(data["resumen"]["variantes_a_resurtir"], 0)


class FechaVentaTests(PlaneacionBaseTestCase):
    """La fecha de venta es el alta del pedido, no ``fecha_confirmacion`` (EC-875)."""

    def test_confirmacion_futura_cuenta_por_su_alta(self):
        v = self._variante(self.a, "A-FUT")
        hoy = timezone.localdate()
        self._venta(self.a, v, 7, _local(hoy - timedelta(days=10)), fecha_confirmacion=timezone.now() + timedelta(days=60))
        fila = self._fila(self._get(self.a["admin"]).json(), v)
        self.assertEqual(_n(fila["vendido_periodo"]), 7)
        self.assertEqual(sum(fila["serie_semanal"]), 7)
        self.assertEqual(fila["ultima_venta"], (hoy - timedelta(days=10)).isoformat())
        self.assertEqual(fila["dias_sin_venta"], 10)

    def test_serie_cuadra_con_el_vendido_y_no_hay_dias_negativos(self):
        hoy = timezone.localdate()
        ahora = timezone.now()
        for i, (dias, conf) in enumerate([(0, 30), (5, -2), (20, 90), (40, None), (80, 5)]):
            v = self._variante(self.a, f"A-MIX-{i}")
            creado = min(_local(hoy - timedelta(days=dias), time(0, 30)), ahora)
            self._venta(self.a, v, i + 1, creado, ahora + timedelta(days=conf) if conf is not None else None)
        for r in self._get(self.a["admin"]).json()["results"]:
            with self.subTest(sku=r["sku"]):
                self.assertEqual(sum(r["serie_semanal"]), _n(r["vendido_periodo"]))
                self.assertGreaterEqual(r["dias_sin_venta"], 0)

    def test_dia_frontera_cuenta_igual_en_vendido_y_serie(self):
        """Con ``semanas=1`` el periodo son los 7 días locales hasta hoy: la venta del
        primer día cuenta en ambos y la del día anterior en ninguno (va al periodo anterior)."""
        hoy = timezone.localdate()
        primer_dia = hoy - timedelta(days=6)
        dentro = self._variante(self.a, "A-DENTRO")
        fuera = self._variante(self.a, "A-FUERA")
        self._venta(self.a, dentro, 3, _local(primer_dia, time(0, 1)))
        self._venta(self.a, fuera, 5, _local(primer_dia - timedelta(days=1), time(23, 59)))
        data = self._get(self.a["admin"], semanas=1).json()
        self.assertEqual(data["parametros"]["desde"], primer_dia.isoformat())
        f_dentro, f_fuera = self._fila(data, dentro), self._fila(data, fuera)
        self.assertEqual((_n(f_dentro["vendido_periodo"]), f_dentro["serie_semanal"]), (3, [3]))
        self.assertEqual((_n(f_fuera["vendido_periodo"]), f_fuera["serie_semanal"]), (0, [0]))
        self.assertEqual(_n(f_fuera["vendido_periodo_anterior"]), 5)

    def test_sin_venta_reciente_queda_sin_venta_aunque_la_confirmacion_sea_futura(self):
        v = self._variante(self.a, "A-VIEJO")
        self._venta(self.a, v, 2, _local(timezone.localdate() - timedelta(days=120)),
                    fecha_confirmacion=timezone.now() + timedelta(days=30))
        fila = self._fila(self._get(self.a["admin"]).json(), v)
        self.assertEqual(fila["estado"], "SIN_VENTA")
        self.assertEqual(fila["dias_sin_venta"], 120)
        self.assertTrue(fila["candidato_descontinuar"])

    def test_necesidades_usa_el_mismo_calculo(self):
        """El resurtido de ``necesidades-materia-prima`` sale de la venta por alta: una
        confirmación futura no la saca del periodo ni la duplica."""
        v = self._variante(self.a, "A-NEC")
        self._venta(self.a, v, 80, _local(timezone.localdate() - timedelta(days=2)),
                    fecha_confirmacion=timezone.now() + timedelta(days=40))
        tablero = self._fila(self._get(self.a["admin"]).json(), v)
        necesidades = self._get(self.a["admin"], NECESIDADES_URL).json()
        self.assertGreater(tablero["resurtido_sugerido"], 0)
        self.assertEqual(necesidades["resumen"]["piezas_a_resurtir"], tablero["resurtido_sugerido"])
        self.assertEqual(
            [x["variante_id"] for x in necesidades["variantes_sin_bom"]], [v.pk],
        )


class ConsultasFijasTests(PlaneacionBaseTestCase):
    """El número de consultas no crece con variantes ni pedidos."""

    def _consultas(self, url):
        user = self._usuario(f"q-{Usuario.objects.count()}", ["R-PRODUCCION"])
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self._get(user, url).status_code, 200)
        return len(ctx.captured_queries)

    def test_consultas_constantes(self):
        hoy = timezone.localdate()
        self._venta(self.a, self._variante(self.a, "A-Q0"), 1, _local(hoy - timedelta(days=1)))
        antes = {url: self._consultas(url) for url in URLS}
        for i in range(1, 6):
            v = self._variante(self.a, f"A-Q{i}")
            self._venta(self.a, v, 50 * i, _local(hoy - timedelta(days=i)), fecha_confirmacion=timezone.now() + timedelta(days=i))
            self._venta(self.a, v, i, _local(hoy - timedelta(days=100 + i)))
        for url in URLS:
            with self.subTest(url=url):
                self.assertEqual(self._consultas(url), antes[url])
