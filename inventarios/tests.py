"""Tests del scope multi-tenant de ``MovimientoOperacionViewSet``.

``GET /api/v1/inventarios/movimientos/`` NO sirve ``inventarios.Movimiento
Inventario``: sirve ``auditoria.AuditoriaEvento`` filtrado por
``modulo="inventarios", tabla="existencias"`` (es la forma documentada en
``DOCUMENTACION_API.md``, e incluye los eventos que WMS registra al crear una
transferencia). Esos eventos cargan ``antes_json``/``despues_json`` con el
detalle de existencias, así que el aislamiento por empresa es obligatorio.

No correr con ``manage.py test``: crearía la BD de test en el Supabase de
producción, y el proyecto ya no usa SQLite. Los cambios se verifican contra
producción en solo lectura o dentro de una transacción con rollback (ver
``CLAUDE.md``, sección Pruebas); estos casos documentan el comportamiento esperado.
"""

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.db import DatabaseError, connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from auditoria.models import AuditoriaEvento
from catalogo.models import Color, Producto, ProductoVariante, Talla, TipoProducto, VarianteProductoProduccion
from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle
from inventarios.api import auxiliar as auxiliar_mod
from inventarios.api.serializers import ExistenciaSerializer
from inventarios.models import (
    AjusteInventario,
    Almacen,
    Existencia,
    MovimientoInventario,
    MovimientoInventarioDetalle,
    TipoAlmacen,
    Ubicacion,
)
from nucleo.models import (
    Empresa,
    Moneda,
    SatFormaPago,
    SatMetodoPago,
    SatRegimenFiscal,
    Sucursal,
    UnidadMedida,
)
from produccion.models import ListaMaterialBom, OrdenProduccion, OrdenProduccionDetalle
from terceros.models import Proveedor
from usuarios.models import Usuario
from wms.models import Transferencia


def _tipo_producto():
    """Tipo de producto obligatorio (#871); PT tiene pk=3 como en producción."""
    return TipoProducto.objects.get_or_create(codigo="PT", defaults={"pk": 3, "nombre": "Producto terminado"})[0]


MOVIMIENTOS_URL = "/api/v1/inventarios/movimientos/"


class MovimientoOperacionViewSetScopeTenantTests(TestCase):
    """``MovimientoOperacionViewSet.get_queryset()``: quién ve qué eventos.

    El branch roto: la empresa salía de un query param OPCIONAL en vez del
    usuario autenticado, así que omitirlo devolvía los eventos de auditoría de
    inventario de TODAS las empresas, ``antes_json``/``despues_json`` incluidos.
    """

    @classmethod
    def _tenant(cls, codigo, email):
        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        sucursal = Sucursal.objects.create(
            empresa=empresa, codigo=codigo[:3].upper(), nombre=codigo
        )
        usuario = Usuario.objects.create(
            username=email, email=email, empresa=empresa, sucursal_default=sucursal
        )
        evento = AuditoriaEvento.objects.create(
            empresa=empresa,
            usuario=usuario,
            modulo="inventarios",
            accion="ENTRADA",
            tabla="existencias",
            id_registro="1",
            antes_json={"items": []},
            despues_json={"items": [{"producto_id": 1, "delta": "5.0000"}]},
        )
        return {"empresa": empresa, "usuario": usuario, "evento": evento}

    @classmethod
    def setUpTestData(cls):
        cls.a = cls._tenant("acme-inv", "a@acme-inv.test")
        cls.b = cls._tenant("globex-inv", "b@globex-inv.test")

        cls.sin_empresa = Usuario.objects.create(
            username="huerfano-inv", email="huerfano@nowhere-inv.test"
        )
        cls.superuser = Usuario.objects.create(
            username="root-inv", email="root@nowhere-inv.test", is_superuser=True
        )
        # Superusuario CON empresa asignada: debe seguir viendo todo (misma
        # política que Pedido/Empleado y que la recepción de compras).
        cls.superuser_con_empresa = Usuario.objects.create(
            username="root-inv-a",
            email="root-a@acme-inv.test",
            empresa=cls.a["empresa"],
            is_superuser=True,
        )
        # Acceso a una segunda empresa por el M2M ``empresas``, que es el scope
        # de acceso documentado y el que ya usan el resto de ViewSets de
        # ``inventarios`` (incluidos los reportes de esta misma clase).
        cls.multi_empresa = Usuario.objects.create(
            username="multi-inv",
            email="multi@acme-inv.test",
            empresa=cls.a["empresa"],
        )
        cls.multi_empresa.empresas.add(cls.b["empresa"])

    def _ids(self, user, url=MOVIMIENTOS_URL):
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
        return [row["id"] for row in resp.json()]

    # --- el branch roto -------------------------------------------------------

    def test_usuario_de_empresa_a_no_ve_eventos_de_empresa_b(self):
        """Sin ningún query param: antes devolvía los eventos de todas."""
        self.assertEqual(self._ids(self.a["usuario"]), [self.a["evento"].pk])

    def test_empresa_id_de_otra_empresa_no_abre_el_scope(self):
        """El param no es la fuente del tenant: no puede escapar de él."""
        url = f"{MOVIMIENTOS_URL}?empresa_id={self.b['empresa'].pk}"
        self.assertEqual(self._ids(self.a["usuario"], url), [])

    def test_empresa_alias_del_param_tampoco_abre_el_scope(self):
        """``?empresa=`` es el alias que acepta el mismo filtro."""
        url = f"{MOVIMIENTOS_URL}?empresa={self.b['empresa'].pk}"
        self.assertEqual(self._ids(self.a["usuario"], url), [])

    def test_usuario_sin_empresa_no_ve_ningun_evento(self):
        self.assertEqual(self._ids(self.sin_empresa), [])

    def test_retrieve_de_otra_empresa_devuelve_404(self):
        client = APIClient()
        client.force_authenticate(user=self.a["usuario"])
        resp = client.get(f"{MOVIMIENTOS_URL}{self.b['evento'].pk}/")
        self.assertEqual(resp.status_code, 404)

    def test_detalles_de_otra_empresa_no_expone_los_payloads(self):
        """``detalles`` sale de ``get_queryset()``: no debe filtrar json ajeno."""
        client = APIClient()
        client.force_authenticate(user=self.a["usuario"])
        resp = client.get(f"{MOVIMIENTOS_URL}{self.b['evento'].pk}/detalles/")
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("despues_json", resp.json())

    # --- el param sigue filtrando DENTRO del scope ----------------------------

    def test_empresa_id_propia_sigue_filtrando(self):
        url = f"{MOVIMIENTOS_URL}?empresa_id={self.a['empresa'].pk}"
        self.assertEqual(self._ids(self.a["usuario"], url), [self.a["evento"].pk])

    def test_multi_empresa_no_acota_a_una_que_no_es_la_activa(self):
        """El M2M ``empresas`` no da alcance (#538): pedir B con A activa no regresa nada."""
        url = f"{MOVIMIENTOS_URL}?empresa_id={self.b['empresa'].pk}"
        self.assertEqual(self._ids(self.multi_empresa, url), [])

    def test_filtro_accion_sigue_vivo(self):
        AuditoriaEvento.objects.create(
            empresa=self.a["empresa"],
            usuario=self.a["usuario"],
            modulo="inventarios",
            accion="SALIDA",
            tabla="existencias",
        )
        url = f"{MOVIMIENTOS_URL}?accion=ENTRADA"
        self.assertEqual(self._ids(self.a["usuario"], url), [self.a["evento"].pk])

    # --- no regresión en las ramas que ya eran correctas ----------------------

    def test_multi_empresa_solo_ve_su_empresa_activa(self):
        """Solo la empresa activa (#538), como ventas y producción."""
        self.assertEqual(self._ids(self.multi_empresa), [self.a["evento"].pk])

    # La BD es la de producción (sin BD de pruebas vacía, EC-780): el
    # superusuario ve también filas reales, así que se exige que incluya las
    # de ambas empresas, no que sean las únicas.
    def test_superuser_ve_todo(self):
        self.assertTrue(
            {self.a["evento"].pk, self.b["evento"].pk} <= set(self._ids(self.superuser)),
        )

    def test_superuser_con_empresa_asignada_sigue_viendo_todo(self):
        self.assertTrue(
            {self.a["evento"].pk, self.b["evento"].pk} <= set(self._ids(self.superuser_con_empresa)),
        )

    def test_superuser_puede_acotar_con_el_param(self):
        url = f"{MOVIMIENTOS_URL}?empresa_id={self.b['empresa'].pk}"
        self.assertEqual(self._ids(self.superuser, url), [self.b["evento"].pk])

    def test_solo_devuelve_eventos_de_inventarios_sobre_existencias(self):
        """El filtro base del módulo/tabla no se pierde con el aislamiento."""
        AuditoriaEvento.objects.create(
            empresa=self.a["empresa"],
            usuario=self.a["usuario"],
            modulo="ventas",
            accion="CREATE",
            tabla="pedidos",
        )
        self.assertEqual(self._ids(self.a["usuario"]), [self.a["evento"].pk])


EXISTENCIAS_URL = "/api/v1/inventarios/existencias/"


class ExistenciaViewSetScopeTenantTests(TestCase):
    """``ExistenciaViewSet``: aislamiento por empresa/sucursal del almacén.

    ``Existencia`` no tiene ``empresa`` propia: la hereda de ``almacen.empresa``.
    El alcance es el mismo que el resto de ``inventarios`` (almacenes,
    ubicaciones y los reportes de esta misma clase): empresas del usuario
    (``empresa`` + M2M ``empresas``) Y sus ``sucursales``.

    Nota: las rutas de detalle (retrieve/PUT/PATCH/DELETE) hoy responden 404 a
    todos porque ``get_queryset`` devuelve un queryset recortado por ``limit``;
    se dejan así a propósito, las pruebas de detalle sólo fijan que lo ajeno
    siga siendo 404.
    """

    @classmethod
    def _tenant(cls, codigo):
        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        sucursal = Sucursal.objects.create(empresa=empresa, codigo=codigo[:3].upper(), nombre=codigo)
        almacen = Almacen.objects.create(
            empresa=empresa, sucursal=sucursal, codigo=f"{codigo}-1", nombre=f"{codigo} 1",
        )
        ubicacion = Ubicacion.objects.create(almacen=almacen, pasillo="1")
        producto = Producto.objects.create(tipo=_tipo_producto(), empresa=empresa, nombre=f"Producto {codigo}")
        variante = ProductoVariante.objects.create(
            producto=producto, empresa=empresa, color=cls.color, sku=f"{codigo}-SKU", precio_base="1",
        )
        existencia = Existencia.objects.create(producto=producto, almacen=almacen, cantidad="10")
        regular = Usuario.objects.create(
            username=f"r@{codigo}.test", email=f"r@{codigo}.test", empresa=empresa,
        )
        admin = Usuario.objects.create(
            username=f"a@{codigo}.test", email=f"a@{codigo}.test", empresa=empresa, is_admin_empresa=True,
        )
        for usuario in (regular, admin):
            usuario.sucursales.add(sucursal)
        return {
            "empresa": empresa, "sucursal": sucursal, "almacen": almacen, "ubicacion": ubicacion,
            "producto": producto, "variante": variante, "existencia": existencia,
            "regular": regular, "admin": admin,
        }

    @classmethod
    def setUpTestData(cls):
        cls.color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000")
        cls.a = cls._tenant("acme-ex")
        cls.b = cls._tenant("globex-ex")

        cls.sin_empresa = Usuario.objects.create(
            username="huerfano-ex", email="huerfano@nowhere-ex.test", is_admin_empresa=True,
        )
        # Sin empresa pero CON la sucursal de B: la sucursal sola no abre el alcance.
        cls.sin_empresa.sucursales.add(cls.b["sucursal"])
        cls.superuser = Usuario.objects.create(
            username="root-ex", email="root@nowhere-ex.test", is_superuser=True,
        )
        cls.multi_empresa = Usuario.objects.create(
            username="multi-ex", email="multi@acme-ex.test", empresa=cls.a["empresa"],
        )
        cls.multi_empresa.empresas.add(cls.b["empresa"])
        cls.multi_empresa.sucursales.add(cls.a["sucursal"], cls.b["sucursal"])

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _ids(self, user, params=None):
        resp = self._client(user).get(EXISTENCIAS_URL, params or {})
        self.assertEqual(resp.status_code, 200, resp.content)
        return sorted(row["id"] for row in resp.json())

    # --- lectura --------------------------------------------------------------

    def test_list_solo_devuelve_existencias_del_alcance_del_usuario(self):
        self.assertEqual(self._ids(self.a["regular"]), [self.a["existencia"].pk])
        self.assertEqual(self._ids(self.a["admin"]), [self.a["existencia"].pk])

    def test_filtros_de_almacen_o_empresa_ajenos_no_abren_el_alcance(self):
        for params in (
            {"almacen_id": self.b["almacen"].pk},
            {"empresa_id": self.b["empresa"].pk},
            {"producto_id": self.b["producto"].pk},
        ):
            self.assertEqual(self._ids(self.a["regular"], params), [], params)

    def test_usuario_sin_empresa_no_ve_nada(self):
        self.assertEqual(self._ids(self.sin_empresa), [])
        self.assertEqual(self._ids(self.sin_empresa, {"almacen_id": self.b["almacen"].pk}), [])

    def test_usuario_sin_sucursales_no_ve_nada(self):
        sin_sucursal = Usuario.objects.create(
            username="sinsuc-ex", email="sinsuc@acme-ex.test", empresa=self.a["empresa"],
        )
        self.assertEqual(self._ids(sin_sucursal), [])

    def test_multi_empresa_ve_las_empresas_de_su_alcance(self):
        self.assertEqual(
            self._ids(self.multi_empresa),
            sorted([self.a["existencia"].pk, self.b["existencia"].pk]),
        )

    def test_superusuario_ve_todo_y_puede_filtrar(self):
        self.assertTrue(
            {self.a["existencia"].pk, self.b["existencia"].pk} <= set(self._ids(self.superuser)),
        )
        self.assertEqual(
            self._ids(self.superuser, {"almacen_id": self.b["almacen"].pk}), [self.b["existencia"].pk],
        )

    def test_detalle_update_y_delete_de_otra_empresa_son_404_y_no_tocan_la_fila(self):
        ajena = self.b["existencia"]
        for user in (self.a["regular"], self.a["admin"]):
            client = self._client(user)
            url = f"{EXISTENCIAS_URL}{ajena.pk}/"
            self.assertEqual(client.get(url).status_code, 404, user.email)
            if user.is_admin_empresa:
                self.assertEqual(client.patch(url, {"cantidad": "99"}, format="json").status_code, 404)
                self.assertEqual(
                    client.put(
                        url, {"almacen": self.b["almacen"].pk, "cantidad": "99"}, format="json",
                    ).status_code,
                    404,
                )
                self.assertEqual(client.delete(url).status_code, 404)
        ajena.refresh_from_db()
        self.assertEqual(str(ajena.cantidad), "10.0000")

    def test_detalle_propio_es_200_y_ordering_no_revienta(self):
        """#835: el 404 de la ajena sale del alcance, no del ``limit`` recortado
        en ``get_queryset``; el detalle propio y ``?ordering=`` funcionan."""
        propia = self.a["existencia"]
        for user in (self.a["admin"], self.superuser):
            with self.subTest(user=user.email):
                client = self._client(user)
                self.assertEqual(client.get(f"{EXISTENCIAS_URL}{propia.pk}/").status_code, 200)
                resp = client.get(EXISTENCIAS_URL, {"ordering": "cantidad"})
                self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self._ids(self.superuser, {"limit": "1"})), 1)

    # --- acceso al almacén en create (antes del save) ---------------------------

    def _crear(self, user, **payload):
        return self._client(user).post(EXISTENCIAS_URL, {"cantidad": "1", **payload}, format="json")

    def test_admin_no_crea_en_almacen_de_otra_empresa_ni_deja_la_fila(self):
        resp = self._crear(self.a["admin"], almacen=self.b["almacen"].pk, producto=self.b["producto"].pk)
        self.assertEqual(resp.status_code, 403, resp.content)
        # Antes el save() corría primero y la fila se quedaba pese al 403.
        self.assertEqual(Existencia.objects.filter(almacen=self.b["almacen"]).count(), 1)

    def test_admin_sin_empresa_no_crea_aunque_tenga_la_sucursal(self):
        # Antes la guarda era ``user.empresa_id and ...``: sin empresa se saltaba.
        resp = self._crear(self.sin_empresa, almacen=self.b["almacen"].pk, producto=self.b["producto"].pk)
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(Existencia.objects.filter(almacen=self.b["almacen"]).count(), 1)

    def test_create_legitimo_y_superusuario_siguen_funcionando(self):
        propia = self._crear(self.a["admin"], almacen=self.a["almacen"].pk, producto=self.a["producto"].pk)
        self.assertEqual(propia.status_code, 201, propia.content)
        self.assertEqual(propia.json()["almacen"], self.a["almacen"].pk)
        root = self._crear(self.superuser, almacen=self.b["almacen"].pk, producto=self.b["producto"].pk)
        self.assertEqual(root.status_code, 201, root.content)

    # --- relacionados == empresa del almacén (todos, superusuario incluido) -----

    def test_create_con_relacionados_de_otra_empresa_es_rechazado(self):
        propio = {"almacen": self.a["almacen"].pk}
        for user in (self.a["admin"], self.superuser):
            for campo, ajeno in (
                ("producto", self.b["producto"]),
                ("producto_variante", self.b["variante"]),
                ("ubicacion", self.b["ubicacion"]),
            ):
                resp = self._crear(user, **propio, **{campo: ajeno.pk})
                self.assertEqual(resp.status_code, 400, (user.email, campo, resp.content))
                self.assertIn(campo, resp.json())
        self.assertEqual(Existencia.objects.filter(almacen=self.a["almacen"]).count(), 1)

    def test_update_con_relacionados_de_otra_empresa_es_rechazado(self):
        # Las rutas de detalle hoy son 404 para todos (ver docstring), así que
        # la regla del serializer se fija directo sobre él, en PATCH y PUT.
        propia = self.a["existencia"]
        request = APIRequestFactory().patch("/")
        request.user = self.superuser
        for data, partial in (
            ({"producto": self.b["producto"].pk}, True),
            ({"producto_variante": self.b["variante"].pk}, True),
            ({"almacen": self.b["almacen"].pk}, True),  # la otra mitad: el almacén cambia
            ({"almacen": self.a["almacen"].pk, "producto": self.b["producto"].pk, "cantidad": "1"}, False),
        ):
            serializer = ExistenciaSerializer(
                propia, data=data, partial=partial, context={"request": request},
            )
            self.assertFalse(serializer.is_valid(), data)
        ok = ExistenciaSerializer(
            propia, data={"producto_variante": self.a["variante"].pk}, partial=True,
            context={"request": request},
        )
        self.assertTrue(ok.is_valid(), ok.errors)

    def test_create_con_relacionados_de_la_misma_empresa_sigue_funcionando(self):
        resp = self._crear(
            self.a["admin"],
            almacen=self.a["almacen"].pk,
            producto_variante=self.a["variante"].pk,
            ubicacion=self.a["ubicacion"].pk,
        )
        self.assertEqual(resp.status_code, 201, resp.content)


OPERACIONES_URL = "/api/v1/inventarios/operaciones/"


class OperacionInventarioScopeTenantTests(TestCase):
    """``OperacionInventarioViewSet`` (entrada/salida/ajuste): aislamiento.

    Mismo alcance que ``ExistenciaViewSet`` (``almacenes_en_alcance``). El
    almacén fuera de alcance se trata como no encontrado (400 con el mensaje
    existente, igual que un pedido de otra empresa en ``_get_pedido``).
    """

    @classmethod
    def _tenant(cls, codigo):
        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        sucursal = Sucursal.objects.create(empresa=empresa, codigo=codigo[:3].upper(), nombre=codigo)
        almacen = Almacen.objects.create(
            empresa=empresa, sucursal=sucursal, codigo=f"{codigo}-1", nombre=f"{codigo} 1",
        )
        ubicacion = Ubicacion.objects.create(almacen=almacen, pasillo="1")
        producto = Producto.objects.create(tipo=_tipo_producto(), empresa=empresa, nombre=f"Producto {codigo}")
        variante = ProductoVariante.objects.create(
            producto=producto, empresa=empresa, color=cls.color, sku=f"{codigo}-SKU", precio_base="1",
        )
        existencia = Existencia.objects.create(producto=producto, almacen=almacen, cantidad="10")
        admin = Usuario.objects.create(
            username=f"a@{codigo}.test", email=f"a@{codigo}.test", empresa=empresa, is_admin_empresa=True,
        )
        admin.sucursales.add(sucursal)
        return {
            "empresa": empresa, "sucursal": sucursal, "almacen": almacen, "ubicacion": ubicacion,
            "producto": producto, "variante": variante, "existencia": existencia, "admin": admin,
        }

    @classmethod
    def setUpTestData(cls):
        cls.color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000")
        cls.a = cls._tenant("acme-op")
        cls.b = cls._tenant("globex-op")
        cls.superuser = Usuario.objects.create(
            username="root-op", email="root@nowhere-op.test", is_superuser=True,
        )

    def _post(self, user, tipo, almacen, items, **extra):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.post(
            f"{OPERACIONES_URL}{tipo}/", {"almacen": almacen.pk, "items": items, **extra}, format="json",
        )

    def _huella(self):
        """Todo lo que una operación escribe: debe quedar intacto si se rechaza."""
        return (
            sorted(Existencia.objects.values_list("id", "cantidad")),
            MovimientoInventario.objects.count(),
            MovimientoInventarioDetalle.objects.count(),
            AjusteInventario.objects.count(),
            AuditoriaEvento.objects.filter(modulo="inventarios").count(),
        )

    def _assert_rechazo_sin_escrituras(self, resp, campo, status=400):
        self.assertEqual(resp.status_code, status, resp.content)
        self.assertIn(campo, resp.json())

    # --- alcance del almacén ----------------------------------------------------

    def test_almacen_de_otra_empresa_es_rechazado_en_las_tres_operaciones(self):
        antes = self._huella()
        for tipo, cantidad in (("entrada", "1"), ("salida", "1"), ("ajuste", "0")):
            resp = self._post(
                self.a["admin"], tipo, self.b["almacen"],
                [{"producto": self.b["producto"].pk, "cantidad": cantidad}],
            )
            self._assert_rechazo_sin_escrituras(resp, "almacen")
            self.assertEqual(resp.json(), {"almacen": "Almacén no encontrado."})
        self.assertEqual(self._huella(), antes)

    def test_usuario_sin_alcance_es_rechazado(self):
        sin_empresa = Usuario.objects.create(
            username="huerfano-op", email="huerfano@nowhere-op.test", is_admin_empresa=True,
        )
        sin_empresa.sucursales.add(self.b["sucursal"])  # la sucursal sola no abre el alcance
        sin_sucursal = Usuario.objects.create(
            username="sinsuc-op", email="sinsuc@acme-op.test",
            empresa=self.a["empresa"], is_admin_empresa=True,
        )
        antes = self._huella()
        for user, almacen, producto in (
            (sin_empresa, self.b["almacen"], self.b["producto"]),
            (sin_sucursal, self.a["almacen"], self.a["producto"]),
        ):
            resp = self._post(user, "entrada", almacen, [{"producto": producto.pk, "cantidad": "1"}])
            self._assert_rechazo_sin_escrituras(resp, "almacen")
        self.assertEqual(self._huella(), antes)

    # --- producto / variante / ubicación == empresa del almacén (todos) ----------

    def test_relacionados_de_otra_empresa_son_rechazados_incluso_al_superusuario(self):
        antes = self._huella()
        for user in (self.a["admin"], self.superuser):
            for item, campo in (
                ({"producto": self.b["producto"].pk}, "items"),
                ({"producto_variante": self.b["variante"].pk}, "items"),
                ({"producto": self.a["producto"].pk, "ubicacion": self.b["ubicacion"].pk}, "ubicacion"),
            ):
                for tipo in ("entrada", "ajuste"):
                    resp = self._post(user, tipo, self.a["almacen"], [{**item, "cantidad": "1"}])
                    self._assert_rechazo_sin_escrituras(resp, campo)
        self.assertEqual(self._huella(), antes)

    def test_linea_ajena_despues_de_una_valida_no_deja_escrituras(self):
        antes = self._huella()
        resp = self._post(
            self.a["admin"], "entrada", self.a["almacen"],
            [
                {"producto": self.a["producto"].pk, "cantidad": "5"},
                {"producto": self.b["producto"].pk, "cantidad": "5"},
            ],
        )
        self._assert_rechazo_sin_escrituras(resp, "items")
        self.assertIn("Item #2", resp.json()["items"])
        self.assertEqual(self._huella(), antes)

    # --- empresa/sucursal salen del almacén; el body no las puede contradecir ----

    def test_empresa_o_sucursal_del_body_en_conflicto_se_rechaza(self):
        antes = self._huella()
        item = [{"producto": self.a["producto"].pk, "cantidad": "1"}]
        for extra, campo in (
            ({"empresa": self.b["empresa"].pk}, "empresa"),
            ({"empresa_id": self.b["empresa"].pk}, "empresa"),
            ({"sucursal": self.b["sucursal"].pk}, "sucursal"),
            ({"sucursal_id": self.b["sucursal"].pk}, "sucursal"),
        ):
            for user in (self.a["admin"], self.superuser):
                resp = self._post(user, "ajuste", self.a["almacen"], item, **extra)
                self._assert_rechazo_sin_escrituras(resp, campo)
        self.assertEqual(self._huella(), antes)

    def test_empresa_y_sucursal_del_body_coincidentes_se_aceptan(self):
        resp = self._post(
            self.a["admin"], "entrada", self.a["almacen"],
            [{"producto": self.a["producto"].pk, "cantidad": "1"}],
            empresa=self.a["empresa"].pk, sucursal=self.a["sucursal"].pk,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        movimiento = MovimientoInventario.objects.get(pk=resp.json()["movimiento_inventario_id"])
        self.assertEqual((movimiento.empresa_id, movimiento.sucursal_id), (self.a["empresa"].pk, self.a["sucursal"].pk))

    # --- atomicidad -----------------------------------------------------------

    def test_ajuste_que_falla_a_mitad_no_deja_el_ajuste_huerfano(self):
        # Cualquier fallo dentro de la escritura (p. ej. un desbordamiento
        # numérico en Postgres) debe revertir TODO, incluido el AjusteInventario
        # que antes se creaba fuera del ``transaction.atomic``.
        antes = self._huella()
        with mock.patch(
            "inventarios.api.views.AuditoriaEvento.objects.create", side_effect=DatabaseError("falla simulada"),
        ):
            with self.assertRaises(DatabaseError):
                self._post(
                    self.a["admin"], "ajuste", self.a["almacen"],
                    [{"producto": self.a["producto"].pk, "cantidad": "3"}],
                )
        self.assertEqual(self._huella(), antes)

    # --- movimiento formal: la variante de cada línea se conserva --------------

    def test_movimiento_formal_guarda_la_variante_de_cada_linea(self):
        variante, ubicacion = self.a["variante"], self.a["ubicacion"]
        # (tipo, cantidad enviada, cantidad del detalle, stock final, origen, destino)
        pasos = (
            ("entrada", "5", "5", "5.0000", None, ubicacion.pk),
            ("salida", "2", "2", "3.0000", ubicacion.pk, None),
            ("ajuste", "7", "7", "7.0000", ubicacion.pk, ubicacion.pk),
        )
        for tipo, enviada, en_detalle, stock, origen, destino in pasos:
            resp = self._post(
                self.a["admin"], tipo, self.a["almacen"],
                [{"producto_variante": variante.pk, "ubicacion": ubicacion.pk, "cantidad": enviada}],
            )
            self.assertEqual(resp.status_code, 200, (tipo, resp.content))
            detalle = MovimientoInventarioDetalle.objects.get(
                movimiento_inventario_id=resp.json()["movimiento_inventario_id"],
            )
            self.assertEqual(detalle.producto_variante_id, variante.pk, tipo)
            # Lo que ya se guardaba no cambia.
            self.assertEqual(detalle.producto_id, variante.producto_id, tipo)
            self.assertEqual(detalle.cantidad, Decimal(en_detalle), tipo)
            self.assertEqual((detalle.ubicacion_origen_id, detalle.ubicacion_destino_id), (origen, destino), tipo)
            existencia = Existencia.objects.get(producto_variante=variante, ubicacion=ubicacion)
            self.assertEqual(str(existencia.cantidad), stock, tipo)
            self.assertEqual(existencia.almacen_id, self.a["almacen"].pk, tipo)

    def test_linea_sin_variante_sigue_sin_variante_en_el_movimiento(self):
        resp = self._post(
            self.a["admin"], "entrada", self.a["almacen"], [{"producto": self.a["producto"].pk, "cantidad": "1"}],
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        detalle = MovimientoInventarioDetalle.objects.get(
            movimiento_inventario_id=resp.json()["movimiento_inventario_id"],
        )
        self.assertEqual((detalle.producto_id, detalle.producto_variante_id), (self.a["producto"].pk, None))

    def test_superusuario_opera_en_cualquier_almacen(self):
        resp = self._post(
            self.superuser, "entrada", self.b["almacen"], [{"producto": self.b["producto"].pk, "cantidad": "4"}],
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.b["existencia"].refresh_from_db()
        self.assertEqual(str(self.b["existencia"].cantidad), "14.0000")

    def test_entrada_salida_y_ajuste_legitimos_actualizan_el_stock(self):
        ex = self.a["existencia"]
        pasos = (("entrada", "5", "15.0000"), ("salida", "3", "12.0000"), ("ajuste", "7", "7.0000"))
        for tipo, cantidad, esperado in pasos:
            resp = self._post(
                self.a["admin"], tipo, self.a["almacen"], [{"producto": self.a["producto"].pk, "cantidad": cantidad}],
            )
            self.assertEqual(resp.status_code, 200, (tipo, resp.content))
            self.assertEqual(resp.json()["result"][0]["id"], ex.pk)
            ex.refresh_from_db()
            self.assertEqual(str(ex.cantidad), esperado, tipo)
        self.assertEqual(MovimientoInventario.objects.filter(empresa=self.a["empresa"]).count(), 3)
        self.assertEqual(AjusteInventario.objects.filter(almacen=self.a["almacen"]).count(), 1)
        # Variante + ubicación del mismo almacén: crea su propia existencia.
        resp = self._post(
            self.a["admin"], "entrada", self.a["almacen"],
            [{"producto_variante": self.a["variante"].pk, "ubicacion": self.a["ubicacion"].pk, "cantidad": "2"}],
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            str(Existencia.objects.get(producto_variante=self.a["variante"], ubicacion=self.a["ubicacion"]).cantidad),
            "2.0000",
        )


REPORTE_EXISTENCIAS_PRODUCCION_COMPRAS_URL = "/api/v1/inventarios/existencias/reporte-existencias-produccion-compras/"


class ReporteExistenciasProduccionComprasTests(TestCase):
    """``GET .../existencias/reporte-existencias-produccion-compras/``:
    disponible (almacenes de Producto Terminado) + en producción (detalle de
    OP activas) + pendiente de compra (OC no recibidas), agrupado por
    producto con desglose por talla en disponible/producción — mesa de
    control lo usa para decidir resurtido sin cruzar pantallas de producción
    y compras.

    Mismo alcance de almacenes que ``reporte-existencias-periodo``
    (``_build_report_almacenes_queryset``): empresas del usuario Y sus
    sucursales.
    """

    @classmethod
    def _catalogos_compartidos(cls):
        # Catálogos SAT y moneda no son propios de un tenant: se comparten
        # entre A y B para no duplicar setup irrelevante a lo que se prueba.
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        sat_regimen = SatRegimenFiscal.objects.create(codigo="601-RS", descripcion="General de Ley")
        sat_forma_pago = SatFormaPago.objects.create(codigo="99-RS", descripcion="Por definir")
        sat_metodo_pago = SatMetodoPago.objects.create(codigo="PUE-RS", descripcion="Pago en una exhibición")
        cls.proveedor = Proveedor.objects.create(
            nombre="Proveedor Tela", codigo="PROV-RS", razon_social="Proveedor Tela SA",
            telefono="8180000000", contacto_principal="Juan Perez", rfc="XAXX010101000",
            email="proveedor@resurtido.test", moneda=cls.moneda, sat_regimen_fiscal=sat_regimen,
            sat_forma_pago=sat_forma_pago, sat_metodo_pago=sat_metodo_pago,
        )

    @classmethod
    def _tenant(cls, codigo):
        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        sucursal = Sucursal.objects.create(empresa=empresa, codigo=codigo[:3].upper(), nombre=codigo)
        almacen_pt = Almacen.objects.create(
            empresa=empresa, sucursal=sucursal, codigo=f"{codigo}-00", nombre="Producto Terminado",
            tipo_almacen=TipoAlmacen.PRODUCTO_TERMINADO,
        )
        almacen_mp = Almacen.objects.create(
            empresa=empresa, sucursal=sucursal, codigo=f"{codigo}-MP", nombre="Materia Prima",
            tipo_almacen=TipoAlmacen.MATERIA_PRIMA,
        )
        regular = Usuario.objects.create(
            username=f"mesa@{codigo}.test", email=f"mesa@{codigo}.test", empresa=empresa,
        )
        regular.sucursales.add(sucursal)

        producto = Producto.objects.create(tipo=_tipo_producto(), empresa=empresa, nombre=f"Playera {codigo}", codigo=codigo[:5])  # varchar(5) (EC-780)
        var_ch = ProductoVariante.objects.create(
            producto=producto, empresa=empresa, color=cls.color, talla=cls.talla_ch,
            sku=f"{codigo}-CH", precio_base="100",
        )
        var_m = ProductoVariante.objects.create(
            producto=producto, empresa=empresa, color=cls.color, talla=cls.talla_m,
            sku=f"{codigo}-M", precio_base="100",
        )
        Existencia.objects.create(almacen=almacen_pt, producto_variante=var_ch, cantidad="100")
        Existencia.objects.create(almacen=almacen_pt, producto_variante=var_m, cantidad="50")
        # No debe contar: existencia en almacén de MATERIA_PRIMA, no de PT.
        Existencia.objects.create(almacen=almacen_mp, producto_variante=var_ch, cantidad="999")

        bom = ListaMaterialBom.objects.create(empresa=empresa, producto_variante=var_ch, activo=True)

        op_activa = OrdenProduccion.objects.create(
            empresa=empresa, sucursal=sucursal, folio_op=f"OP-{codigo}-PEND",
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.PENDIENTE,
            fecha_entrega_estimada="2026-11-01", observaciones="Resurtido",
        )
        OrdenProduccionDetalle.objects.create(op=op_activa, bom=bom, cantidad="20", unidad=cls.unidad, producto_variante=var_ch)
        OrdenProduccionDetalle.objects.create(op=op_activa, bom=bom, cantidad="10", unidad=cls.unidad, producto_variante=var_m)

        # No deben contar como "en producción": completada y cancelada.
        op_completa = OrdenProduccion.objects.create(
            empresa=empresa, sucursal=sucursal, folio_op=f"OP-{codigo}-COMP",
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.COMPLETADO,
        )
        OrdenProduccionDetalle.objects.create(op=op_completa, bom=bom, cantidad="500", unidad=cls.unidad, producto_variante=var_ch)

        op_cancelada = OrdenProduccion.objects.create(
            empresa=empresa, sucursal=sucursal, folio_op=f"OP-{codigo}-CANC",
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.CANCELADO,
        )
        OrdenProduccionDetalle.objects.create(op=op_cancelada, bom=bom, cantidad="700", unidad=cls.unidad, producto_variante=var_ch)

        def _oc(sufijo, estatus, cantidad, recibido=None):
            oc = OrdenCompra.objects.create(
                empresa=empresa, sucursal=sucursal, proveedor=cls.proveedor, moneda=cls.moneda, usuario=regular,
                folio=f"OC-{codigo}-{sufijo}", fecha_oc="2026-09-01", fecha_entrega_estimada="2026-11-15",
                estatus=estatus, observaciones=f"Insumo {sufijo}",
            )
            det = OrdenCompraDetalle.objects.create(
                orden_compra=oc, producto=producto, sucursal=sucursal, cantidad=cantidad, piezas=cantidad,
            )
            if recibido:
                recepcion = Recepcion.objects.create(
                    tipo_origen=Recepcion.TipoOrigen.ORDEN_COMPRA, orden_compra=oc, empresa=empresa,
                    sucursal=sucursal, proveedor=cls.proveedor, almacen=almacen_pt, usuario=regular,
                    fecha_recepcion=timezone.now(), folio=f"REC-{codigo}-{sufijo}",
                )
                RecepcionDetalle.objects.create(
                    recepcion=recepcion, orden_compra_detalle=det, producto=producto, cantidad_recibida=recibido,
                )
            return oc

        # Única que debe contar como "pendiente": 40 - 15 = 25.
        oc_parcial = _oc("PARCIAL", OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA, 40, 15)
        _oc("RECIBIDA", OrdenCompra.EstatusOrdenCompra.RECIBIDA, 40, 40)
        _oc("BORRADOR", OrdenCompra.EstatusOrdenCompra.BORRADOR, 999)
        _oc("CANCELADA", OrdenCompra.EstatusOrdenCompra.CANCELADA, 999)

        return {
            "empresa": empresa, "sucursal": sucursal, "almacen_pt": almacen_pt, "almacen_mp": almacen_mp,
            "producto": producto, "var_ch": var_ch, "var_m": var_m,
            "op_activa": op_activa, "op_completa": op_completa, "op_cancelada": op_cancelada,
            "oc_parcial": oc_parcial, "regular": regular,
        }

    @classmethod
    def setUpTestData(cls):
        cls.color = Color.objects.create(nombre="Negro", codigo="NRS", codigo_hex="#000000")
        cls.talla_ch = Talla.objects.create(nombre="CH-RS")
        cls.talla_m = Talla.objects.create(nombre="M-RS")
        cls.unidad = UnidadMedida.objects.create(clave="PZA-RS", nombre="Pieza")
        cls._catalogos_compartidos()
        cls.a = cls._tenant("acme-rs")
        cls.b = cls._tenant("globex-rs")
        cls.superuser = Usuario.objects.create(username="root-rs", email="root@nowhere-rs.test", is_superuser=True)

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _get(self, user, params=None):
        resp = self._client(user).get(REPORTE_EXISTENCIAS_PRODUCCION_COMPRAS_URL, params or {})
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["resultados"]

    def _resultado(self, user, producto, params=None):
        for r in self._get(user, params):
            if r["producto_id"] == producto.pk:
                return r
        return None

    # --- disponible / producción --------------------------------------------

    def test_disponible_agrega_solo_almacenes_de_producto_terminado_por_talla(self):
        r = self._resultado(self.a["regular"], self.a["producto"])
        self.assertIsNotNone(r)
        self.assertEqual(r["disponible_por_talla"], {"CH-RS": "100.0000", "M-RS": "50.0000"})
        self.assertEqual(r["disponible_total"], "150.0000")

    def test_produccion_excluye_ordenes_completadas_y_canceladas(self):
        r = self._resultado(self.a["regular"], self.a["producto"])
        self.assertEqual(r["produccion_por_talla"], {"CH-RS": "20.0000", "M-RS": "10.0000"})
        self.assertEqual(r["produccion_total"], "30.0000")
        folios = {o["folio"] for o in r["ordenes_produccion"]}
        self.assertEqual(folios, {f"OP-acme-rs-PEND"})

    def test_total_por_talla_suma_disponible_mas_produccion(self):
        r = self._resultado(self.a["regular"], self.a["producto"])
        self.assertEqual(r["total_por_talla"], {"CH-RS": "120.0000", "M-RS": "60.0000"})

    def test_ordenes_produccion_incluyen_fecha_entrega_y_comentarios(self):
        r = self._resultado(self.a["regular"], self.a["producto"])
        entry = next(o for o in r["ordenes_produccion"] if o["folio"] == "OP-acme-rs-PEND")
        self.assertEqual(entry["estatus_display"], "Pendiente")
        self.assertEqual(entry["fecha_entrega_estimada"], "2026-11-01")
        self.assertEqual(entry["comentarios"], "Resurtido")

    # --- compras --------------------------------------------------------------

    def test_compras_pendiente_solo_cuenta_lo_no_recibido_de_ordenes_activas(self):
        r = self._resultado(self.a["regular"], self.a["producto"])
        self.assertEqual(r["compras_pendiente_cantidad"], "25.0000")
        folios = {o["folio"] for o in r["ordenes_compra"]}
        self.assertEqual(folios, {"OC-acme-rs-PARCIAL"})

    def test_orden_compra_incluye_fecha_entrega_y_comentarios(self):
        r = self._resultado(self.a["regular"], self.a["producto"])
        entry = next(o for o in r["ordenes_compra"] if o["folio"] == "OC-acme-rs-PARCIAL")
        self.assertEqual(entry["estatus_display"], "Parcialmente recibida")
        self.assertEqual(entry["fecha_entrega_estimada"], "2026-11-15")
        self.assertEqual(entry["comentarios"], "Insumo PARCIAL")

    def test_compras_por_talla_solo_de_renglones_con_variante(self):
        oc = OrdenCompra.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"], proveedor=self.proveedor, moneda=self.moneda,
            usuario=self.a["regular"], folio="O-acme-rs-PT", fecha_oc="2026-09-01",
            estatus=OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
        )
        OrdenCompraDetalle.objects.create(
            orden_compra=oc, producto=self.a["producto"], producto_variante=self.a["var_m"],
            sucursal=self.a["sucursal"], cantidad=7, piezas=7,
        )
        r = self._resultado(self.a["regular"], self.a["producto"])
        # 25 del renglón legado sin variante + 7 de la talla M.
        self.assertEqual(r["compras_pendiente_cantidad"], "32.0000")
        self.assertEqual(r["compras_por_talla"], {"M-RS": "7.0000"})
        # El SKU de la variante también encuentra lo que viene en la O.
        r = self._resultado(self.a["regular"], self.a["producto"], {"sku": "acme-rs-M"})
        self.assertEqual(r["compras_por_talla"], {"M-RS": "7.0000"})

    # --- alcance / filtros ------------------------------------------------

    def test_sin_almacenes_en_alcance_devuelve_vacio(self):
        sin_sucursal = Usuario.objects.create(
            username="sinsuc-rs", email="sinsuc@acme-rs.test", empresa=self.a["empresa"],
        )
        self.assertEqual(self._get(sin_sucursal), [])

    def test_filtro_por_producto_id_y_por_sku(self):
        otro_producto = Producto.objects.create(tipo=_tipo_producto(), empresa=self.a["empresa"], nombre="Otro", codigo="OTRO-RS")
        otra_var = ProductoVariante.objects.create(
            producto=otro_producto, empresa=self.a["empresa"], color=self.color, talla=self.talla_ch,
            sku="OTRO-RS-CH", precio_base="50",
        )
        Existencia.objects.create(almacen=self.a["almacen_pt"], producto_variante=otra_var, cantidad="5")

        resultados = self._get(self.a["regular"], {"producto_id": self.a["producto"].pk})
        self.assertEqual([r["producto_id"] for r in resultados], [self.a["producto"].pk])

        resultados = self._get(self.a["regular"], {"sku": "OTRO-RS"})
        self.assertEqual([r["producto_id"] for r in resultados], [otro_producto.pk])

    def test_aislamiento_multi_tenant_entre_empresas(self):
        self.assertEqual(
            [r["producto_id"] for r in self._get(self.a["regular"])], [self.a["producto"].pk],
        )
        self.assertEqual(
            [r["producto_id"] for r in self._get(self.b["regular"])], [self.b["producto"].pk],
        )

    def test_superusuario_ve_ambas_empresas(self):
        ids = {r["producto_id"] for r in self._get(self.superuser)}
        self.assertEqual(ids, {self.a["producto"].pk, self.b["producto"].pk})


class DetallesMovimientoFormalTests(TestCase):
    """``movimientos/formal/{id}/detalles/``: el id que da el reporte por periodo."""

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="det-for", razon_social="Det SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="DET", nombre="Det"
        )
        cls.otra = Empresa.objects.create(codigo="det-otra", razon_social="Otra SA")
        cls.otra_suc = Sucursal.objects.create(
            empresa=cls.otra, codigo="OTR", nombre="Otra"
        )
        cls.user = Usuario.objects.create(
            username="det@x.test", email="det@x.test",
            empresa=cls.empresa, sucursal_default=cls.sucursal,
        )
        cls.user.sucursales.add(cls.sucursal)
        cls.mov = MovimientoInventario.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, usuario=cls.user,
            tipo_movimiento="ENTRADA",
        )
        MovimientoInventarioDetalle.objects.create(
            movimiento_inventario=cls.mov, cantidad=Decimal("3"),
        )
        cls.ajeno = MovimientoInventario.objects.create(
            empresa=cls.otra, sucursal=cls.otra_suc, usuario=cls.user,
            tipo_movimiento="ENTRADA",
        )

    def _get(self, pk):
        client = APIClient()
        client.force_authenticate(user=self.user)
        return client.get(f"{MOVIMIENTOS_URL}formal/{pk}/detalles/")

    def test_devuelve_el_detalle(self):
        resp = self._get(self.mov.pk)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["detalle_count"], 1)
        self.assertEqual(resp.json()["detalle"][0]["cantidad"], "3.00000000")

    def test_movimiento_de_otra_empresa_es_404(self):
        self.assertEqual(self._get(self.ajeno.pk).status_code, 404)


class AlmacenPermiteEntradaTests(TestCase):
    """EC-607: ``permite_entrada`` se lee en ``/inventarios/almacenes/`` pero no se
    edita por esta API (el selector del traslado de picking filtra con ella)."""

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="ec607-alm", razon_social="EC607 Almacenes SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTZ", nombre="Matriz")
        cls.usuario = Usuario.objects.create(
            username="ec607-alm", email="ec607-alm@example.com", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,  # escribir almacenes lo exige
        )
        cls.usuario.sucursales.add(cls.sucursal)
        cls.con_entrada = Almacen.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, codigo="APA", nombre="Apartados", permite_entrada=True
        )
        cls.sin_entrada = Almacen.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, codigo="PT", nombre="Terminado"
        )

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.usuario)

    def test_listado_y_detalle_exponen_permite_entrada(self):
        resp = self.api.get("/api/v1/inventarios/almacenes/")
        self.assertEqual(resp.status_code, 200, resp.content)
        filas = resp.json()
        filas = filas.get("results", filas) if isinstance(filas, dict) else filas
        self.assertEqual(
            {f["id_almacen"]: f["permite_entrada"] for f in filas},
            {self.con_entrada.pk: True, self.sin_entrada.pk: False},
        )
        resp = self.api.get(f"/api/v1/inventarios/almacenes/{self.sin_entrada.pk}/")
        self.assertIs(resp.json()["permite_entrada"], False)

    def test_patch_no_cambia_permite_entrada(self):
        resp = self.api.patch(
            f"/api/v1/inventarios/almacenes/{self.sin_entrada.pk}/",
            {"permite_entrada": True, "nombre": "Terminado 2"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIs(resp.json()["permite_entrada"], False)
        self.sin_entrada.refresh_from_db()
        self.assertEqual((self.sin_entrada.nombre, self.sin_entrada.permite_entrada), ("Terminado 2", False))

    def test_alta_ignora_permite_entrada(self):
        resp = self.api.post(
            "/api/v1/inventarios/almacenes/",
            {"sucursal": self.sucursal.pk, "codigo": "NUEVO", "nombre": "Nuevo", "permite_entrada": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIs(Almacen.objects.get(pk=resp.json()["id_almacen"]).permite_entrada, False)


class AuxiliarMovimientosTests(TestCase):
    """``GET movimientos/auxiliar/``: kardex de una variante."""

    URL = f"{MOVIMIENTOS_URL}auxiliar/"

    @classmethod
    def setUpTestData(cls):
        color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000")
        cls.empresa = Empresa.objects.create(codigo="acme-aux", razon_social="acme SA")
        sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="AUX", nombre="aux")
        cls.alm1 = Almacen.objects.create(empresa=cls.empresa, sucursal=sucursal, codigo="A1", nombre="Uno")
        cls.alm2 = Almacen.objects.create(empresa=cls.empresa, sucursal=sucursal, codigo="A2", nombre="Dos")
        cls.ub1 = Ubicacion.objects.create(almacen=cls.alm1, pasillo="1")
        cls.ub2 = Ubicacion.objects.create(almacen=cls.alm2, pasillo="1")
        producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Camisa azul")
        cls.variante = ProductoVariante.objects.create(
            producto=producto, empresa=cls.empresa, color=color, sku="CAM-01", precio_base="1",
        )
        ProductoVariante.objects.create(
            producto=producto, empresa=cls.empresa, color=color, sku="CAM-02", precio_base="1",
        )
        cls.user = Usuario.objects.create(
            username="u@aux.test", email="u@aux.test", empresa=cls.empresa, is_admin_empresa=True,
            sucursal_default=sucursal,
        )
        cls.otra_sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="AUX2", nombre="aux 2")
        cls.alm3 = Almacen.objects.create(empresa=cls.empresa, sucursal=cls.otra_sucursal, codigo="A3", nombre="Tres")
        otra = Empresa.objects.create(codigo="globex-aux", razon_social="globex SA")
        cls.ajena = ProductoVariante.objects.create(
            producto=Producto.objects.create(tipo=_tipo_producto(), empresa=otra, nombre="Camisa ajena"),
            empresa=otra, color=color, sku="CAM-99", precio_base="1",
        )

        def mov(tipo, origen=None, destino=None, cantidad="0", costo="0", **extra):
            m = MovimientoInventario.objects.create(
                empresa=cls.empresa, sucursal=sucursal, tipo_movimiento=tipo, usuario=cls.user, **extra,
            )
            MovimientoInventarioDetalle.objects.create(
                movimiento_inventario=m, producto=producto, producto_variante=cls.variante,
                ubicacion_origen=origen, ubicacion_destino=destino, cantidad=cantidad, costo_unitario=costo,
            )

        mov("ENTRADA", destino=cls.ub1, cantidad="10", costo="5")
        mov("SALIDA", origen=cls.ub1, cantidad="3")
        mov("TRANSFERENCIA", origen=cls.ub1, destino=cls.ub2, cantidad="2")
        mov("AJUSTE", origen=cls.ub2, destino=cls.ub2, cantidad="5")  # cantidad final, no diferencia
        # Transferencia sin ubicaciones (picking) hacia un almacén de otra
        # sucursal: los almacenes salen de la transferencia (#801).
        transferencia = Transferencia.objects.create(
            empresa=cls.empresa, sucursal=sucursal, almacen_origen=cls.alm1, almacen_destino=cls.alm3,
            folio="TR-AUX", usuario=cls.user,
        )
        mov("TRANSFERENCIA", cantidad="1", transferencia=transferencia)
        Existencia.objects.create(producto=producto, producto_variante=cls.variante, almacen=cls.alm1, ubicacion=cls.ub1, cantidad="4")
        Existencia.objects.create(producto=producto, producto_variante=cls.variante, almacen=cls.alm2, ubicacion=cls.ub2, cantidad="5")
        Existencia.objects.create(producto=producto, producto_variante=cls.variante, almacen=cls.alm3, cantidad="1")

    def _get(self, user=None, **params):
        client = APIClient()
        client.force_authenticate(user=user or self.user)
        return client.get(self.URL, params)

    def test_busqueda_con_varias_coincidencias_regresa_opciones(self):
        data = self._get(q="camisa").json()
        self.assertIsNone(data["variante"])
        self.assertEqual({o["sku"] for o in data["opciones"]}, {"CAM-01", "CAM-02"})

    def test_sku_exacto_regresa_kardex(self):
        data = self._get(q="cam-01").json()
        self.assertEqual(data["variante"]["sku"], "CAM-01")
        filas = data["results"]
        self.assertEqual(
            [(f["tm"], f["almacen_codigo"], f["entradas"], f["salidas"], f["saldo"]) for f in filas],
            [
                ("ENTRADA", "A1", "10.0000", "0.0000", "10.0000"),
                ("SALIDA", "A1", "0.0000", "3.0000", "7.0000"),
                ("TRANSFERENCIA", "A1", "0.0000", "2.0000", "5.0000"),
                ("TRANSFERENCIA", "A2", "2.0000", "0.0000", "7.0000"),
                ("AJUSTE", "A2", "3.0000", "0.0000", "10.0000"),
                # La entrada a A3 es de otra sucursal: no se ve (#800).
                ("TRANSFERENCIA", "A1", "0.0000", "1.0000", "9.0000"),
            ],
        )
        self.assertEqual(filas[0]["costo_promedio"], "5.00")
        self.assertEqual(data["resumen"]["saldo_final"], data["resumen"]["existencia_actual"])

    def test_filtro_por_almacen(self):
        filas = self._get(producto_variante=self.variante.pk, almacen=self.alm2.pk).json()["results"]
        self.assertEqual([f["saldo"] for f in filas], ["2.0000", "5.0000"])

    def test_variante_de_otra_empresa(self):
        self.assertEqual(self._get(producto_variante=self.ajena.pk).status_code, 404)
        self.assertEqual(self._get(q="CAM-99").json()["opciones"], [])

    def test_transferencia_sin_ubicaciones_usa_almacenes_de_la_transferencia(self):
        superuser = Usuario.objects.create(username="su@aux.test", email="su@aux.test", is_superuser=True)
        filas = self._get(user=superuser, producto_variante=self.variante.pk).json()["results"]
        self.assertEqual(
            [(f["almacen_codigo"], f["efecto"]) for f in filas if f["folio_referencia"] == "TR-AUX"],
            [("A1", "SALIDA"), ("A3", "ENTRADA")],
        )

    def test_almacen_de_otra_sucursal_no_regresa_datos(self):
        data = self._get(producto_variante=self.variante.pk, almacen=self.alm3.pk).json()
        self.assertEqual(data["results"], [])
        self.assertEqual(data["resumen"]["existencia_actual"], "0.0000")

    def test_id_no_numerico_es_400(self):
        self.assertIn("almacen", self._get(producto_variante=self.variante.pk, almacen="A1").json())
        self.assertIn("producto_variante", self._get(producto_variante="CAM-01").json())

    def test_limite_recorta_a_los_mas_recientes(self):
        # #807: el saldo sale de toda la historia; la respuesta, solo los últimos N.
        completo = self._get(producto_variante=self.variante.pk).json()
        data = self._get(producto_variante=self.variante.pk, limite=2).json()
        self.assertTrue(data["results_truncado"])
        self.assertEqual(data["results"], completo["results"][-2:])
        self.assertFalse(completo["results_truncado"])

    def test_documento_factura_antes_que_pedido(self):
        # #804: el movimiento de una factura lleva su folio, no el del pedido.
        m = {k: None for k in (
            "movimiento_inventario__recepcion_id", "movimiento_inventario__transferencia_id",
            "movimiento_inventario__op_id", "movimiento_inventario__ajuste_inventario_id",
        )}
        m.update({
            "movimiento_inventario__factura_id": 1, "movimiento_inventario__factura__folio": "F-1",
            "movimiento_inventario__pedido_id": 2, "movimiento_inventario__pedido__folio": "P-2",
        })
        self.assertEqual(auxiliar_mod._documento(m), ("FACTURA", "F-1"))

    def test_id_cero_es_400(self):
        # #815: ``almacen=0`` quitaba el filtro y ``producto_variante=0`` caía a ``q``.
        resp = self._get(producto_variante=self.variante.pk, almacen=0)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("almacen", resp.json())
        resp = self._get(producto_variante=0, q="camisa")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("producto_variante", resp.json())

    def test_renglon_sin_ubicacion_usa_el_almacen_del_detalle(self):
        # #803: movimiento por pedido sobre una existencia sin ubicación.
        m = MovimientoInventario.objects.create(
            empresa=self.empresa, sucursal=self.alm2.sucursal, tipo_movimiento="SALIDA", usuario=self.user,
        )
        MovimientoInventarioDetalle.objects.create(
            movimiento_inventario=m, producto=self.variante.producto, producto_variante=self.variante,
            almacen=self.alm2, cantidad="1",
        )
        filas = self._get(producto_variante=self.variante.pk, almacen=self.alm2.pk).json()["results"]
        self.assertEqual((filas[-1]["almacen_id"], filas[-1]["salidas"]), (self.alm2.pk, "1.0000"))

    def test_opciones_recortadas_avisan_hay_mas(self):
        with mock.patch.object(auxiliar_mod, "LIMITE_OPCIONES", 1):
            data = self._get(q="camisa").json()
        self.assertEqual((len(data["opciones"]), data["hay_mas"]), (1, True))

    def test_ajuste_usa_la_diferencia_de_la_auditoria(self):
        # El saldo corrido de ub1 es 5, pero la existencia era 4 (stock sin
        # movimiento): la auditoría manda (#802).
        ajuste = AjusteInventario.objects.create(
            empresa=self.empresa, sucursal=self.alm1.sucursal, almacen=self.alm1, usuario=self.user, motivo="Conteo",
        )
        m = MovimientoInventario.objects.create(
            empresa=self.empresa, sucursal=self.alm1.sucursal, tipo_movimiento="AJUSTE",
            usuario=self.user, ajuste_inventario=ajuste,
        )
        MovimientoInventarioDetalle.objects.create(
            movimiento_inventario=m, producto_variante=self.variante,
            ubicacion_origen=self.ub1, ubicacion_destino=self.ub1, cantidad="0",
        )
        AuditoriaEvento.objects.create(
            empresa=self.empresa, usuario=self.user, modulo="inventarios", accion="AJUSTE",
            tabla="existencias", id_registro=str(self.alm1.pk),
            despues_json={"ajuste_id": ajuste.pk, "items": [
                {"producto_variante_id": self.variante.pk, "ubicacion_id": self.ub1.pk, "delta": "-4.0000"},
            ]},
        )
        fila = self._get(producto_variante=self.variante.pk).json()["results"][-1]
        self.assertEqual((fila["tm"], fila["salidas"]), ("AJUSTE", "4.0000"))

POR_ALMACEN_URL = f"{EXISTENCIAS_URL}por-almacen/"
POR_ALMACEN_RESUMEN_URL = f"{POR_ALMACEN_URL}resumen/"


class ExistenciasPorAlmacenTests(TestCase):
    """``GET existencias/por-almacen/`` y ``por-almacen/resumen/`` (EC-774):
    la pantalla de stock paginada en servidor. Replica lo que el front hace
    hoy en memoria (``StockView``/``StockColumns``/``StockFilter``).

    Almacén principal con ``max_stock`` = 100 y un renglón en cada borde de
    estado (> 80 % full, > 40 % ok, > 15 % low, resto critical).
    """

    @classmethod
    def setUpTestData(cls):
        negro = Color.objects.create(nombre="Negro EC774", codigo="N74", codigo_hex="#000000")
        rojo = Color.objects.create(nombre="Rojo EC774", codigo="R74", codigo_hex="#ff0000")
        pt = _tipo_producto()
        mp = TipoProducto.objects.get_or_create(codigo="MP", defaults={"nombre": "Materia prima"})[0]

        cls.empresa = Empresa.objects.create(codigo="ec774-a", razon_social="ec774 A SA")
        sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="E7A", nombre="ec774 a")
        cls.alm = Almacen.objects.create(empresa=cls.empresa, sucursal=sucursal, codigo="E7-1", nombre="Alm EC774")
        cls.alm_otro = Almacen.objects.create(empresa=cls.empresa, sucursal=sucursal, codigo="E7-2", nombre="Otro EC774")
        cls.alm_ceros = Almacen.objects.create(empresa=cls.empresa, sucursal=sucursal, codigo="E7-3", nombre="Ceros EC774")
        cls.u1 = Ubicacion.objects.create(almacen=cls.alm, pasillo="A", rack="1", nivel="2", posicion="3")
        cls.u2 = Ubicacion.objects.create(almacen=cls.alm, pasillo="B", rack="1", nivel="1", posicion="1")
        # Ubicación de OTRO almacén: el nombre completo lleva el almacén de la ubicación.
        cls.u_otro = Ubicacion.objects.create(almacen=cls.alm_otro, pasillo="Z", rack="9", nivel="9", posicion="9")

        playera = Producto.objects.create(tipo=pt, empresa=cls.empresa, nombre="Playera azul")
        chamarra = Producto.objects.create(tipo=mp, empresa=cls.empresa, nombre="Chamarra")
        tela = Producto.objects.create(tipo=pt, empresa=cls.empresa, nombre="Tela sin variante")
        v_playera = ProductoVariante.objects.create(
            producto=playera, empresa=cls.empresa, color=negro, sku="ZZEC774-PLA-01", precio_base="1",
        )
        v_chamarra = ProductoVariante.objects.create(
            producto=chamarra, empresa=cls.empresa, color=rojo, sku="ZZEC774-CHA-01", precio_base="1",
        )
        muestra = VarianteProductoProduccion.objects.create(
            empresa=cls.empresa, nombre="Muestra bordada", sku="ZZEC774-MUE-01", color=rojo,
        )

        def ex(stock, ubicacion=None, almacen=None, **fuente):
            return Existencia.objects.create(
                almacen=almacen or cls.alm, ubicacion=ubicacion, stock=stock, cantidad=f"{stock}.5", **fuente,
            )

        cls.e = {
            "full100": ex(100, cls.u1, producto=playera, producto_variante=v_playera),
            "ok80": ex(80, cls.u2, producto_variante=v_chamarra),        # 80 % exacto → ok
            "full81": ex(81, cls.u1, producto=tela),                     # sin variante: sku/color null
            "low40": ex(40, cls.u2, variante_produccion=muestra),        # 40 % exacto → low
            "ok41": ex(41, cls.u1, producto=playera, producto_variante=v_playera),
            "crit15": ex(15, cls.u2, producto_variante=v_chamarra),      # 15 % exacto → critical
            "low16": ex(16, cls.u1, variante_produccion=muestra),
            "crit0": ex(0, None, producto=playera, producto_variante=v_playera),
            "otra_ubic5": ex(5, cls.u_otro, producto_variante=v_chamarra),
        }
        # Otro almacén del mismo tenant: su stock no cuenta para el máximo.
        ex(1000, almacen=cls.alm_otro, producto=tela)
        cls.ceros = [ex(0, almacen=cls.alm_ceros, producto=tela), ex(0, almacen=cls.alm_ceros, producto=playera)]

        mx = timezone.get_default_timezone()
        fechas = {
            "full100": datetime(2026, 10, 15, 12, 0, tzinfo=mx),
            # 1-nov 05:00 UTC = 31-oct 23:00 en México: cuenta para octubre.
            "ok80": datetime(2026, 11, 1, 5, 0, tzinfo=dt_timezone.utc),
            "full81": datetime(2026, 11, 2, 9, 0, tzinfo=mx),
            "low40": datetime(2026, 9, 1, 0, 30, tzinfo=mx),
        }
        for clave, fecha in fechas.items():
            Existencia.objects.filter(pk=cls.e[clave].pk).update(fecha_actualizacion=fecha)
        # El resto queda sin fecha (``auto_now`` la llenó al crear): se limpia.
        Existencia.objects.filter(almacen=cls.alm).exclude(
            pk__in=[cls.e[c].pk for c in fechas]
        ).update(fecha_actualizacion=None)

        cls.user = Usuario.objects.create(username="u@ec774.test", email="u@ec774.test", empresa=cls.empresa)
        cls.user.sucursales.add(sucursal)

        otra = Empresa.objects.create(codigo="ec774-b", razon_social="ec774 B SA")
        suc_b = Sucursal.objects.create(empresa=otra, codigo="E7B", nombre="ec774 b")
        cls.alm_b = Almacen.objects.create(empresa=otra, sucursal=suc_b, codigo="E7B-1", nombre="Alm B EC774")
        prod_b = Producto.objects.create(tipo=pt, empresa=otra, nombre="Producto B")
        cls.e_b = Existencia.objects.create(almacen=cls.alm_b, producto=prod_b, stock=7, cantidad="7")
        cls.superuser = Usuario.objects.create(username="root-ec774", email="root@ec774.test", is_superuser=True)

    def _get(self, params, user=None, url=POR_ALMACEN_URL):
        client = APIClient()
        client.force_authenticate(user=user or self.user)
        return client.get(url, params)

    def _ids(self, **params):
        resp = self._get({"almacen_id": self.alm.pk, **params})
        self.assertEqual(resp.status_code, 200, resp.content)
        return [fila["id"] for fila in resp.json()]

    def _set(self, *claves):
        return sorted(self.e[c].pk for c in claves)

    # --- almacen_id y alcance ---------------------------------------------------

    def test_almacen_id_obligatorio_y_entero_positivo(self):
        for url in (POR_ALMACEN_URL, POR_ALMACEN_RESUMEN_URL):
            for params in ({}, {"almacen_id": ""}, {"almacen_id": "x"}, {"almacen_id": "0"}, {"almacen_id": "-3"}):
                resp = self._get(params, url=url)
                self.assertEqual(resp.status_code, 400, (url, params))
                self.assertIn("almacen_id", resp.json())

    def test_almacen_de_otra_empresa_da_vacio(self):
        params = {"almacen_id": self.alm_b.pk}
        self.assertEqual(self._get(params).json(), [])
        self.assertEqual(self._get({**params, "limit": 5}).json()["count"], 0)
        resumen = self._get(params, url=POR_ALMACEN_RESUMEN_URL).json()
        self.assertEqual(resumen["max_stock"], 0)
        self.assertEqual(
            resumen["kpis"],
            {"total_registros": 0, "skus_en_existencia": 0, "requiere_stock": 0, "total_stock": 0},
        )
        self.assertEqual(resumen["opciones"], {"nombre": [], "tipo": [], "color": [], "ubicacion": [], "mes": []})

    def test_almacen_inexistente_da_vacio(self):
        self.assertEqual(self._get({"almacen_id": 999999999}).json(), [])

    def test_superusuario_consulta_cualquier_almacen(self):
        filas = self._get({"almacen_id": self.alm_b.pk}, user=self.superuser).json()
        self.assertEqual([f["id"] for f in filas], [self.e_b.pk])
        resumen = self._get({"almacen_id": self.alm_b.pk}, user=self.superuser, url=POR_ALMACEN_RESUMEN_URL).json()
        self.assertEqual(resumen["kpis"]["total_registros"], 1)

    def test_solo_get(self):
        client = APIClient()
        client.force_authenticate(user=self.superuser)
        for url in (POR_ALMACEN_URL, POR_ALMACEN_RESUMEN_URL):
            self.assertEqual(client.post(f"{url}?almacen_id={self.alm.pk}", {}).status_code, 405)

    # --- paginación y forma -----------------------------------------------------

    def test_sin_limit_lista_completa_y_con_limit_sobre_drf(self):
        self.assertEqual(sorted(self._ids()), sorted(e.pk for e in self.e.values()))
        data = self._get({"almacen_id": self.alm.pk, "limit": 4, "offset": 4}).json()
        self.assertEqual(set(data), {"count", "next", "previous", "results"})
        self.assertEqual(data["count"], 9)
        self.assertEqual(len(data["results"]), 4)
        self.assertIsNotNone(data["next"])
        self.assertIsNotNone(data["previous"])

    def test_limit_se_topa_en_500(self):
        """``PaginacionOpcionalLimitOffset.max_limit``: con más de 500 filas,
        ``limit=9999`` trae una página de 500, no el almacén entero."""
        tela = Producto.objects.get(empresa=self.empresa, nombre="Tela sin variante")
        Existencia.objects.bulk_create(
            Existencia(almacen=self.alm_ceros, producto=tela, stock=1, cantidad="1") for _ in range(510)
        )
        data = self._get({"almacen_id": self.alm_ceros.pk, "limit": 9999}).json()
        self.assertEqual(data["count"], 512)
        self.assertEqual(len(data["results"]), 500)
        self.assertIsNotNone(data["next"])

    def test_forma_del_renglon(self):
        filas = {f["id"]: f for f in self._get({"almacen_id": self.alm.pk}).json()}
        self.assertEqual(
            filas[self.e["full100"].pk],
            {
                "id": self.e["full100"].pk,
                "producto_info": {"nombre": "Playera azul", "sku": "ZZEC774-PLA-01", "tipo": "PT", "color": "Negro EC774"},
                "ubicacion_info": {"id_ubicacion": self.u1.pk, "nombre_completo": "Alm EC774 - A-1-2-3"},
                "stock": 100,
                "cantidad": "100.5000",
                "fecha_actualizacion": "2026-10-15T12:00:00-06:00",
                "estado": "full",
            },
        )
        # Solo variante: el producto sale de la variante.
        self.assertEqual(
            filas[self.e["ok80"].pk]["producto_info"],
            {"nombre": "Chamarra", "sku": "ZZEC774-CHA-01", "tipo": "MP", "color": "Rojo EC774"},
        )
        # Producto sin variante: sin SKU ni color, como ``ExistenciaSerializer``.
        self.assertEqual(
            filas[self.e["full81"].pk]["producto_info"],
            {"nombre": "Tela sin variante", "sku": None, "tipo": "PT", "color": None},
        )
        # SKU de muestra: sin tipo.
        self.assertEqual(
            filas[self.e["low40"].pk]["producto_info"],
            {"nombre": "Muestra bordada", "sku": "ZZEC774-MUE-01", "tipo": None, "color": "Rojo EC774"},
        )
        self.assertIsNone(filas[self.e["crit0"].pk]["ubicacion_info"])
        self.assertIsNone(filas[self.e["crit0"].pk]["fecha_actualizacion"])
        self.assertEqual(
            filas[self.e["otra_ubic5"].pk]["ubicacion_info"]["nombre_completo"], "Otro EC774 - Z-9-9-9",
        )

    def test_coincide_con_el_listado_clasico(self):
        """Mismos valores que ``GET existencias/?limit=all`` para cada campo que lee la pantalla."""
        client = APIClient()
        client.force_authenticate(user=self.user)
        viejo = {
            f["id"]: f for f in client.get(EXISTENCIAS_URL, {"almacen_id": self.alm.pk, "limit": "all"}).json()
        }
        for fila in self._get({"almacen_id": self.alm.pk}).json():
            o = viejo[fila["id"]]
            self.assertEqual(
                fila["producto_info"], {k: o["producto_info"][k] for k in ("nombre", "sku", "tipo", "color")},
            )
            self.assertEqual(
                fila["ubicacion_info"] and fila["ubicacion_info"]["nombre_completo"],
                o["ubicacion_info"] and o["ubicacion_info"]["nombre_completo"],
            )
            for campo in ("stock", "cantidad", "fecha_actualizacion"):
                self.assertEqual(fila[campo], o[campo], campo)

    # --- búsqueda -----------------------------------------------------------------

    def test_busqueda_por_nombre_y_sku_de_las_tres_fuentes(self):
        casos = {
            "PLAYERA": ("full100", "ok41", "crit0"),        # nombre del producto de la fila
            "chamar": ("ok80", "crit15", "otra_ubic5"),     # nombre del producto de la variante
            "sin variante": ("full81",),                    # producto sin variante
            "bordada": ("low40", "low16"),                  # nombre de la muestra
            "zzec774-pla": ("full100", "ok41", "crit0"),    # SKU de variante
            "MUE-01": ("low40", "low16"),                   # SKU de muestra
        }
        for texto, esperadas in casos.items():
            self.assertEqual(sorted(self._ids(search=texto)), self._set(*esperadas), texto)
        # Ni color ni ubicación entran a la búsqueda.
        self.assertEqual(self._ids(search="Negro EC774"), [])

    # --- filtros ------------------------------------------------------------------

    def test_filtros_exactos_de_nombre_tipo_y_color(self):
        self.assertEqual(sorted(self._ids(nombre="Playera azul")), self._set("full100", "ok41", "crit0"))
        self.assertEqual(self._ids(nombre="playera azul"), [])  # exacto, como ``exactFilter``
        self.assertEqual(sorted(self._ids(nombre="Muestra bordada")), self._set("low40", "low16"))
        self.assertEqual(sorted(self._ids(tipo="MP")), self._set("ok80", "crit15", "otra_ubic5"))
        self.assertEqual(
            sorted(self._ids(color="Rojo EC774")), self._set("ok80", "crit15", "otra_ubic5", "low40", "low16"),
        )

    def test_filtro_de_ubicacion(self):
        self.assertEqual(sorted(self._ids(ubicacion=self.u2.pk)), self._set("ok80", "low40", "crit15"))
        for invalido in ("x", "0"):
            self.assertEqual(self._get({"almacen_id": self.alm.pk, "ubicacion": invalido}).status_code, 400)

    def test_filtro_de_rango_de_existencias(self):
        casos = {
            "cero": ("crit0",),
            "1-10": ("otra_ubic5",),
            "11-100": ("full100", "ok80", "full81", "low40", "ok41", "crit15", "low16"),
            "mas-100": (),
        }
        for rango, esperadas in casos.items():
            self.assertEqual(sorted(self._ids(existencias=rango)), self._set(*esperadas), rango)
        self.assertEqual(self._get({"almacen_id": self.alm.pk, "existencias": "10-20"}).status_code, 400)

    def test_filtro_de_mes_en_hora_de_mexico(self):
        self.assertEqual(sorted(self._ids(mes="2026-10")), self._set("full100", "ok80"))
        self.assertEqual(self._ids(mes="2026-11"), [self.e["full81"].pk])
        for invalido in ("2026-13", "2026-1", "oct"):
            self.assertEqual(self._get({"almacen_id": self.alm.pk, "mes": invalido}).status_code, 400, invalido)

    def test_estado_y_su_filtro_en_los_bordes(self):
        casos = {
            "full": ("full100", "full81"),
            "ok": ("ok80", "ok41"),
            "low": ("low40", "low16"),
            "critical": ("crit15", "crit0", "otra_ubic5"),
        }
        estados = {f["id"]: f["estado"] for f in self._get({"almacen_id": self.alm.pk}).json()}
        for estado, esperadas in casos.items():
            self.assertEqual(sorted(self._ids(estado=estado)), self._set(*esperadas), estado)
            for clave in esperadas:
                self.assertEqual(estados[self.e[clave].pk], estado, clave)
        self.assertEqual(self._get({"almacen_id": self.alm.pk, "estado": "Óptimo"}).status_code, 400)

    def test_max_stock_cero_todo_es_critico(self):
        filas = self._get({"almacen_id": self.alm_ceros.pk}).json()
        self.assertEqual({f["estado"] for f in filas}, {"critical"})
        resumen = self._get({"almacen_id": self.alm_ceros.pk}, url=POR_ALMACEN_RESUMEN_URL).json()
        self.assertEqual(resumen["max_stock"], 0)
        self.assertEqual(resumen["kpis"]["requiere_stock"], 2)
        self.assertEqual(resumen["kpis"]["skus_en_existencia"], 0)

    def test_filtros_se_combinan_con_busqueda(self):
        self.assertEqual(self._ids(search="chamarra", estado="ok"), [self.e["ok80"].pk])

    # --- ordenamiento -------------------------------------------------------------

    def test_orden_por_defecto_fecha_desc_nulos_al_final_y_desempate_id(self):
        con_fecha = [self.e[c].pk for c in ("full81", "ok80", "full100", "low40")]
        sin_fecha = sorted(
            (self.e[c].pk for c in ("ok41", "crit15", "low16", "crit0", "otra_ubic5")), reverse=True,
        )
        self.assertEqual(self._ids(), con_fecha + sin_fecha)

    def test_ordering_lista_blanca_y_desempate(self):
        # ``-stock``; sin empates en stock aquí, el desempate se prueba con tipo.
        self.assertEqual(
            self._ids(ordering="-stock"),
            self._ids_por(lambda e: -e.stock),
        )
        # ``tipo`` con empates: el desempate por ``id`` sigue la dirección del primero
        # y los NULL (muestras) van al final.
        orden = self._ids(ordering="tipo")
        tipos = {"MP": ("ok80", "crit15", "otra_ubic5"), "PT": ("full100", "full81", "ok41", "crit0")}
        esperado = self._set(*tipos["MP"]) + self._set(*tipos["PT"]) + self._set("low40", "low16")
        self.assertEqual(orden, esperado)
        desc = self._ids(ordering="-tipo")
        self.assertEqual(
            desc,
            sorted(self._set(*tipos["PT"]), reverse=True)
            + sorted(self._set(*tipos["MP"]), reverse=True)
            + sorted(self._set("low40", "low16"), reverse=True),
        )
        # sku y nombre por las anotaciones; SKU null (producto sin variante) al final.
        por_sku = self._ids(ordering="sku")
        self.assertEqual(por_sku[-1], self.e["full81"].pk)
        self.assertEqual(self._ids(ordering="nombre")[:3], self._set("ok80", "crit15", "otra_ubic5"))
        self.assertEqual(self._ids(ordering="ubicacion_nombre")[-1], self.e["crit0"].pk)
        # Fuera de la lista blanca: se ignora y queda el orden por defecto (sin 500).
        self.assertEqual(self._ids(ordering="producto__empresa__razon_social"), self._ids())
        self.assertEqual(self._ids(ordering="estado"), self._ids())

    def _ids_por(self, clave):
        return [e.pk for e in sorted(self.e.values(), key=lambda e: (clave(e), e.pk))]

    # --- resumen ------------------------------------------------------------------

    def test_resumen_kpis_y_opciones(self):
        resumen = self._get({"almacen_id": self.alm.pk}, url=POR_ALMACEN_RESUMEN_URL).json()
        stocks = [e.stock for e in self.e.values()]
        self.assertEqual(resumen["almacen_id"], self.alm.pk)
        self.assertEqual(resumen["max_stock"], 100)
        self.assertEqual(
            resumen["kpis"],
            {
                "total_registros": 9,
                "skus_en_existencia": 8,           # stock > 0
                "requiere_stock": 5,               # low + critical
                "total_stock": sum(stocks),
            },
        )
        opciones = resumen["opciones"]
        self.assertEqual(
            [o["value"] for o in opciones["nombre"]],
            ["Chamarra", "Muestra bordada", "Playera azul", "Tela sin variante"],
        )
        self.assertEqual([o["value"] for o in opciones["tipo"]], ["MP", "PT"])
        self.assertEqual([o["value"] for o in opciones["color"]], ["Negro EC774", "Rojo EC774"])
        self.assertEqual(
            opciones["ubicacion"],
            [
                {"value": self.u1.pk, "label": "Alm EC774 - A-1-2-3"},
                {"value": self.u2.pk, "label": "Alm EC774 - B-1-1-1"},
                {"value": self.u_otro.pk, "label": "Otro EC774 - Z-9-9-9"},
            ],
        )
        self.assertEqual(
            opciones["mes"],
            [
                {"value": "2026-11", "label": "Noviembre 2026"},
                {"value": "2026-10", "label": "Octubre 2026"},
                {"value": "2026-09", "label": "Septiembre 2026"},
            ],
        )

    def test_resumen_es_del_almacen_completo_e_ignora_filtros(self):
        url = POR_ALMACEN_RESUMEN_URL
        completo = self._get({"almacen_id": self.alm.pk}, url=url).json()
        filtrado = self._get({"almacen_id": self.alm.pk, "search": "playera", "estado": "full"}, url=url).json()
        self.assertEqual(completo, filtrado)

    # --- consultas ----------------------------------------------------------------

    def test_numero_de_consultas_constante(self):
        params = {"almacen_id": self.alm.pk, "limit": 3, "search": "a", "ordering": "nombre"}

        # Cada request: 2 del alcance del usuario (sus empresas M2M y sus
        # sucursales, ``almacenes_en_alcance``, igual que el listado) + 1 del
        # ``max_stock`` del almacén, que entra como constante a las demás.
        def medir():
            with self.assertNumQueries(2 + 1 + 2):   # count + página
                self._get(params)
            with self.assertNumQueries(2 + 1 + 1):   # sin limit: la lista
                self._get({"almacen_id": self.alm.pk})
            with self.assertNumQueries(2 + 1 + 1):   # todo el resumen
                self._get({"almacen_id": self.alm.pk}, url=POR_ALMACEN_RESUMEN_URL)

        medir()
        tela = Producto.objects.get(empresa=self.empresa, nombre="Tela sin variante")
        for stock in range(20):
            Existencia.objects.create(almacen=self.alm, ubicacion=self.u1, producto=tela, stock=stock, cantidad="1")
        medir()


# --- Reportes por periodo: existencias y movimientos (EC-814) -----------------

REPORTE_EXISTENCIAS_PERIODO_URL = f"{EXISTENCIAS_URL}reporte-existencias-periodo/"
REPORTE_MOVIMIENTOS_PERIODO_URL = f"{MOVIMIENTOS_URL}reporte-movimientos-periodo/"
PERIODO_REPORTE = {"fecha_inicio": "2026-01-01", "fecha_final": "2026-06-30"}


def _fecha_reporte(anio, mes, dia):
    return datetime(anio, mes, dia, 18, 0, tzinfo=dt_timezone.utc)


class ReportesPeriodoBase(TestCase):
    """Dos empresas, cada una con un almacén en su sucursal matriz (donde vive el
    usuario regular) y otro en una sucursal norte a la que el usuario NO tiene
    acceso. Una existencia de 10 en matriz y de 7 en norte; la auditoría suma +3
    dentro del periodo y -1 después; una entrada costeada a 5 dentro del periodo.
    """

    @classmethod
    def _tenant(cls, codigo):
        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        matriz = Sucursal.objects.create(empresa=empresa, codigo=f"{codigo[:2].upper()}M", nombre=f"Matriz {codigo}")
        norte = Sucursal.objects.create(empresa=empresa, codigo=f"{codigo[:2].upper()}N", nombre=f"Norte {codigo}")
        almacen = Almacen.objects.create(
            empresa=empresa, sucursal=matriz, codigo=f"{codigo}-PT", nombre=f"PRODUCTO TERMINADO {codigo}",
        )
        almacen_norte = Almacen.objects.create(
            empresa=empresa, sucursal=norte, codigo=f"{codigo}-NT", nombre=f"NORTE {codigo}",
        )
        ubicacion = Ubicacion.objects.create(almacen=almacen, pasillo="1", rack="A", nivel="1", posicion="1")
        usuario = Usuario.objects.create(
            username=f"u@{codigo}.test", email=f"u@{codigo}.test", empresa=empresa,
            first_name="Ana", last_name=codigo,
        )
        usuario.sucursales.add(matriz)
        producto = Producto.objects.create(tipo=_tipo_producto(), empresa=empresa, nombre=f"Playera {codigo}")
        variante = ProductoVariante.objects.create(
            producto=producto, empresa=empresa, color=cls.color, talla=cls.talla,
            sku=f"{codigo}-AZ-M", precio_base="1",
        )
        Existencia.objects.create(
            almacen=almacen, ubicacion=ubicacion, producto=producto, producto_variante=variante, cantidad="10",
        )
        Existencia.objects.create(almacen=almacen_norte, producto=producto, producto_variante=variante, cantidad="7")

        mov = MovimientoInventario.objects.create(
            empresa=empresa, sucursal=matriz, tipo_movimiento="ENTRADA", usuario=usuario,
        )
        MovimientoInventarioDetalle.objects.create(
            movimiento_inventario=mov, producto=producto, producto_variante=variante,
            ubicacion_destino=ubicacion, cantidad="10", costo_unitario="5",
        )
        MovimientoInventario.objects.filter(pk=mov.pk).update(fecha_movimiento=_fecha_reporte(2026, 2, 1))

        for fecha, delta in ((_fecha_reporte(2026, 3, 1), "3"), (_fecha_reporte(2026, 8, 1), "-1")):
            evento = AuditoriaEvento.objects.create(
                empresa=empresa, usuario=usuario, modulo="inventarios", accion="ENTRADA",
                tabla="existencias", id_registro=str(almacen.pk),
                despues_json={"almacen_id": almacen.pk, "items": [
                    {"producto_id": producto.pk, "producto_variante_id": variante.pk, "delta": delta},
                ]},
            )
            AuditoriaEvento.objects.filter(pk=evento.pk).update(created_at=fecha)

        return {
            "empresa": empresa, "matriz": matriz, "norte": norte, "almacen": almacen,
            "almacen_norte": almacen_norte, "ubicacion": ubicacion, "usuario": usuario,
            "producto": producto, "variante": variante,
        }

    @classmethod
    def setUpTestData(cls):
        cls.color = Color.objects.create(nombre="Azul RP", codigo="AZR", codigo_hex="#0000ff")
        cls.talla = Talla.objects.create(nombre="M-RP")
        cls.a = cls._tenant("acme-rp")
        cls.b = cls._tenant("globex-rp")
        cls.superuser = Usuario.objects.create(username="root-rp", email="root@rp.test", is_superuser=True)

    def _get(self, user, url, params=None):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(url, {**PERIODO_REPORTE, **(params or {})})

    def _movimiento(self, tenant, tipo="ENTRADA", fecha=None, **kwargs):
        """Movimiento de una línea sin ubicación (el almacén sale del documento)."""
        mov = MovimientoInventario.objects.create(
            empresa=tenant["empresa"], sucursal=tenant["matriz"], tipo_movimiento=tipo,
            usuario=tenant["usuario"], **kwargs,
        )
        MovimientoInventarioDetalle.objects.create(
            movimiento_inventario=mov, producto=tenant["producto"], producto_variante=tenant["variante"],
            cantidad="2", costo_unitario="4",
        )
        MovimientoInventario.objects.filter(pk=mov.pk).update(fecha_movimiento=fecha or _fecha_reporte(2026, 4, 1))
        return mov

    def _entrada_por_recepcion(self, tenant, folio):
        recepcion = Recepcion.objects.create(
            empresa=tenant["empresa"], sucursal=tenant["matriz"], almacen=tenant["almacen"],
            usuario=tenant["usuario"], folio=folio, fecha_recepcion=_fecha_reporte(2026, 4, 1),
        )
        return self._movimiento(tenant, recepcion=recepcion)

    def _entrada_por_ajuste(self, tenant):
        ajuste = AjusteInventario.objects.create(
            empresa=tenant["empresa"], sucursal=tenant["matriz"], almacen=tenant["almacen"],
            usuario=tenant["usuario"], motivo="Conteo",
        )
        return self._movimiento(tenant, ajuste_inventario=ajuste)


class ReporteExistenciasPeriodoTests(ReportesPeriodoBase):
    """``GET existencias/reporte-existencias-periodo/`` (cálculo en ``reportes_periodo``)."""

    def _filas(self, data):
        return [
            (
                f["almacen_codigo"], f["sku"], f["color"], f["talla"], f["existencia_inicial"], f["entradas"],
                f["salidas"], f["existencia_final"], f["costo_unitario_final"], f["costo_existencia_final"],
            )
            for f in data["results"]
        ]

    def test_usuario_ve_solo_los_almacenes_de_sus_sucursales(self):
        resp = self._get(self.a["usuario"], REPORTE_EXISTENCIAS_PERIODO_URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(
            self._filas(data),
            [("acme-rp-PT", "acme-rp-AZ-M", "Azul RP", "M-RP", "8.0000", "3.0000", "0.0000", "11.0000", "5.00", "55.00")],
        )
        self.assertEqual(data["resumen"], {
            "existencia_inicial": "8.0000", "entradas": "3.0000", "salidas": "0.0000",
            "existencia_final": "11.0000", "costo_total_existencia_final": "55.00",
        })
        self.assertEqual([r["almacen_codigo"] for r in data["resumen_por_almacen"]], ["acme-rp-PT"])
        self.assertEqual((data["fecha_inicio"], data["fecha_final"]), ("2026-01-01", "2026-06-30"))

    def test_superusuario_ve_todos_los_almacenes(self):
        data = self._get(self.superuser, REPORTE_EXISTENCIAS_PERIODO_URL).json()
        self.assertEqual(data["count"], 4)
        norte = [f for f in self._filas(data) if f[0] == "acme-rp-NT"]
        self.assertEqual(norte, [("acme-rp-NT", "acme-rp-AZ-M", "Azul RP", "M-RP", "7.0000", "0.0000", "0.0000", "7.0000", "0.00", "0.00")])

    def test_almacen_o_empresa_ajenos_no_abren_el_alcance(self):
        for params in ({"almacen_id": self.b["almacen"].pk}, {"empresa": self.b["empresa"].pk}, {"almacen": self.a["almacen_norte"].pk}):
            data = self._get(self.a["usuario"], REPORTE_EXISTENCIAS_PERIODO_URL, params).json()
            self.assertEqual((data["count"], data["results"]), (0, []), params)
            self.assertEqual(data["resumen"]["existencia_final"], "0.0000")

    def test_fechas_requeridas_y_en_orden(self):
        resp = self._get(self.a["usuario"], REPORTE_EXISTENCIAS_PERIODO_URL, {"fecha_inicio": ""})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("fecha_inicio", resp.json())
        resp = self._get(self.a["usuario"], REPORTE_EXISTENCIAS_PERIODO_URL, {"fecha_final": "2025-12-31"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("fecha_final", resp.json())

    def test_paginacion_recorta_el_reporte_completo(self):
        data = self._get(self.superuser, REPORTE_EXISTENCIAS_PERIODO_URL, {"page_size": 1, "page": 2}).json()
        self.assertEqual((data["count"], len(data["results"])), (4, 1))
        self.assertIsNotNone(data["next"])
        self.assertEqual(data["resumen"]["existencia_final"], "36.0000")  # 11 + 7 por empresa

    def test_costo_unitario_del_ultimo_movimiento_costeado_hasta_fecha_final(self):
        # Entrada por ajuste (sin ubicación: el almacén sale del ajuste) más
        # reciente que la de 5 → gana 4. Una de 9 después del periodo no cuenta,
        # y un renglón sin costo tampoco.
        self._entrada_por_ajuste(self.a)
        tardio = self._entrada_por_ajuste(self.a)
        tardio.detalles.update(costo_unitario="9")
        MovimientoInventario.objects.filter(pk=tardio.pk).update(fecha_movimiento=_fecha_reporte(2026, 7, 15))
        sin_costo = self._entrada_por_ajuste(self.a)
        sin_costo.detalles.update(costo_unitario="0")
        MovimientoInventario.objects.filter(pk=sin_costo.pk).update(fecha_movimiento=_fecha_reporte(2026, 6, 1))

        data = self._get(self.a["usuario"], REPORTE_EXISTENCIAS_PERIODO_URL).json()
        self.assertEqual(
            [(f["costo_unitario_final"], f["costo_existencia_final"]) for f in data["results"]],
            [("4.00", "44.00")],
        )

    def test_filtros_almacen_id_es_el_filtro_pedido(self):
        # Antes del EC-814 traía el almacén de la última fila cuando no se
        # filtraba por almacén; ahora ``null``.
        data = self._get(self.superuser, REPORTE_EXISTENCIAS_PERIODO_URL, {"page_size": 100}).json()
        self.assertEqual(data["count"], 4)
        self.assertEqual(data["filtros"], {"almacen_id": None})
        data = self._get(self.a["usuario"], REPORTE_EXISTENCIAS_PERIODO_URL, {"almacen_id": self.a["almacen"].pk}).json()
        self.assertEqual(data["filtros"], {"almacen_id": self.a["almacen"].pk})


class ReporteMovimientosPeriodoTests(ReportesPeriodoBase):
    """``GET movimientos/reporte-movimientos-periodo/``."""

    def test_entrada_toma_el_almacen_del_documento_cuando_no_hay_ubicacion(self):
        self._entrada_por_recepcion(self.a, "REC-RP-1")
        self._entrada_por_ajuste(self.a)
        data = self._get(self.a["usuario"], REPORTE_MOVIMIENTOS_PERIODO_URL, {"tipo_movimiento": "entrada"}).json()
        self.assertEqual(data["tipo_movimiento"], "ENTRADA")
        self.assertEqual(data["count"], 3)
        self.assertEqual({f["almacen_codigo"] for f in data["results"]}, {"acme-rp-PT"})
        self.assertEqual(data["resumen"], {"total_movimientos": 3, "total_registros": 3, "total_cantidad": "14.0000"})
        self.assertEqual(data["results"][0]["usuario_nombre"], "Ana acme-rp")

    def test_entrada_sin_n_mas_1_por_almacen_del_documento(self):
        def consultas():
            client = APIClient()
            client.force_authenticate(user=self.a["usuario"])
            with CaptureQueriesContext(connection) as ctx:
                resp = client.get(REPORTE_MOVIMIENTOS_PERIODO_URL, {**PERIODO_REPORTE, "tipo_movimiento": "ENTRADA"})
            self.assertEqual(resp.status_code, 200)
            return resp.json()["count"], len(ctx.captured_queries)

        self._entrada_por_recepcion(self.a, "REC-RP-N0")
        self._entrada_por_ajuste(self.a)
        filas_antes, antes = consultas()
        for i in range(1, 5):
            self._entrada_por_recepcion(self.a, f"REC-RP-N{i}")
            self._entrada_por_ajuste(self.a)
        filas_despues, despues = consultas()
        self.assertEqual((filas_antes, filas_despues), (3, 11))
        self.assertEqual(despues, antes)

    def test_usuario_de_otra_empresa_no_ve_los_movimientos(self):
        self._entrada_por_ajuste(self.a)
        data = self._get(self.b["usuario"], REPORTE_MOVIMIENTOS_PERIODO_URL, {"tipo_movimiento": "ENTRADA"}).json()
        self.assertEqual({f["almacen_codigo"] for f in data["results"]}, {"globex-rp-PT"})
        data = self._get(
            self.b["usuario"], REPORTE_MOVIMIENTOS_PERIODO_URL,
            {"tipo_movimiento": "ENTRADA", "almacen": self.a["almacen"].pk},
        ).json()
        self.assertEqual((data["count"], data["results"]), (0, []))
        self.assertEqual(data["filtros"], {"almacen_id": self.a["almacen"].pk})

    def test_tipo_y_fechas_requeridos(self):
        resp = self._get(self.a["usuario"], REPORTE_MOVIMIENTOS_PERIODO_URL)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("tipo_movimiento", resp.json())
        resp = self._get(self.a["usuario"], REPORTE_MOVIMIENTOS_PERIODO_URL, {"tipo_movimiento": "SALIDA", "fecha_inicio": ""})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("fecha_inicio", resp.json())


REPORTE_EXISTENCIAS_EXPORTAR_URL = f"{REPORTE_EXISTENCIAS_PERIODO_URL}exportar/"
REPORTE_MOVIMIENTOS_EXPORTAR_URL = f"{REPORTE_MOVIMIENTOS_PERIODO_URL}exportar/"
CONTENT_TYPE_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ExportacionReportesPeriodoTests(ReportesPeriodoBase):
    """``.../exportar/?formato=pdf|xlsx`` de ambos reportes (EC-814): mismo
    cálculo, alcance y validación que el JSON."""

    def _texto_pdf(self, contenido):
        import io
        from pypdf import PdfReader
        return "\n".join(pagina.extract_text() for pagina in PdfReader(io.BytesIO(contenido)).pages)

    def _hoja(self, contenido):
        import io
        from openpyxl import load_workbook
        return load_workbook(io.BytesIO(contenido), read_only=True).active

    def _tabla_xlsx(self, contenido, primer_encabezado):
        filas = [list(f) for f in self._hoja(contenido).iter_rows(values_only=True)]
        inicio = next(i for i, f in enumerate(filas) if f and f[0] == primer_encabezado)
        return filas[inicio], [f for f in filas[inicio + 1:] if any(v is not None for v in f)]

    def test_existencias_pdf(self):
        resp = self._get(self.a["usuario"], REPORTE_EXISTENCIAS_EXPORTAR_URL, {"almacen": self.a["almacen"].pk})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertEqual(
            resp["Content-Disposition"],
            'attachment; filename="reporte-existencias-acme-rp-pt-2026-01-01-a-2026-06-30.pdf"',
        )
        self.assertTrue(resp.content.startswith(b"%PDF"))
        texto = self._texto_pdf(resp.content)
        for esperado in (
            "Reporte de Inventario", "PRODUCTO TERMINADO acme-rp (acme-rp-PT)", "1/1/2026 — 30/6/2026",
            "RESUMEN DEL PERIODO", "Exist. Final", "acme-rp-AZ-M", "55.00", "Página 1 de 1",
        ):
            self.assertIn(esperado, texto)

    def test_existencias_xlsx_con_numeros_reales(self):
        resp = self._get(
            self.a["usuario"], REPORTE_EXISTENCIAS_EXPORTAR_URL, {"formato": "xlsx", "almacen_id": self.a["almacen"].pk},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], CONTENT_TYPE_XLSX)
        self.assertIn("reporte-existencias-acme-rp-pt-2026-01-01-a-2026-06-30.xlsx", resp["Content-Disposition"])
        encabezado, filas = self._tabla_xlsx(resp.content, "SKU")
        self.assertEqual(encabezado[:6], ["SKU", "Producto", "Color", "Talla", "Almacén", "Exist. Inicial"])
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0][0], "acme-rp-AZ-M")
        self.assertEqual(filas[0][5:], [8, 3, 0, 11, 5, 55])
        self.assertIsInstance(filas[0][8], (int, float))

    def test_movimientos_pdf_y_xlsx(self):
        self._entrada_por_ajuste(self.a)
        params = {"tipo_movimiento": "ENTRADA", "almacen": self.a["almacen"].pk}
        resp = self._get(self.a["usuario"], REPORTE_MOVIMIENTOS_EXPORTAR_URL, params)
        self.assertEqual((resp.status_code, resp["Content-Type"]), (200, "application/pdf"))
        self.assertIn(
            'filename="reporte-movimientos-entrada-acme-rp-pt-2026-01-01-a-2026-06-30.pdf"', resp["Content-Disposition"],
        )
        texto = self._texto_pdf(resp.content)
        for esperado in ("Reporte de Movimientos de Inventario", "Entrada · PRODUCTO TERMINADO acme-rp", "Ana acme-rp", "01 feb 2026"):
            self.assertIn(esperado, texto)

        resp = self._get(self.a["usuario"], REPORTE_MOVIMIENTOS_EXPORTAR_URL, {**params, "formato": "XLSX"})
        self.assertEqual((resp.status_code, resp["Content-Type"]), (200, CONTENT_TYPE_XLSX))
        encabezado, filas = self._tabla_xlsx(resp.content, "Fecha")
        self.assertEqual(encabezado, ["Fecha", "Tipo", "Producto", "Almacén", "Ubicación", "Cantidad", "Costo Total", "Usuario"])
        self.assertEqual(len(filas), 2)
        self.assertEqual([f[5] for f in filas], [2, 10])
        # Hora de México, sin zona (Excel no la guarda): 18:00 UTC = 12:00.
        self.assertEqual(filas[-1][0], datetime(2026, 2, 1, 12, 0))

    def test_validacion_igual_que_el_json(self):
        casos = (
            (REPORTE_EXISTENCIAS_EXPORTAR_URL, REPORTE_EXISTENCIAS_PERIODO_URL, {"fecha_inicio": ""}),
            (REPORTE_EXISTENCIAS_EXPORTAR_URL, REPORTE_EXISTENCIAS_PERIODO_URL, {"fecha_final": "2025-01-01"}),
            (REPORTE_MOVIMIENTOS_EXPORTAR_URL, REPORTE_MOVIMIENTOS_PERIODO_URL, {}),
            (REPORTE_MOVIMIENTOS_EXPORTAR_URL, REPORTE_MOVIMIENTOS_PERIODO_URL, {"tipo_movimiento": "SALIDA", "fecha_final": ""}),
        )
        for exportar, json_url, params in casos:
            for formato in ("pdf", "xlsx"):
                resp = self._get(self.a["usuario"], exportar, {**params, "formato": formato})
                esperado = self._get(self.a["usuario"], json_url, params)
                self.assertEqual(resp.status_code, 400, (exportar, params))
                self.assertEqual(resp.json(), esperado.json(), (exportar, params))

    def test_existencias_sin_almacen_es_400(self):
        # Solo la exportación: el JSON sigue aceptando todos los almacenes.
        for params in ({}, {"almacen": ""}, {"almacen_id": "abc"}, {"formato": "xlsx"}):
            resp = self._get(self.a["usuario"], REPORTE_EXISTENCIAS_EXPORTAR_URL, params)
            self.assertEqual(resp.status_code, 400, params)
            self.assertEqual(list(resp.json()), ["almacen"], params)
        self.assertEqual(self._get(self.a["usuario"], REPORTE_EXISTENCIAS_PERIODO_URL).status_code, 200)
        # Las fechas se validan antes que el almacén, igual que en el JSON.
        resp = self._get(self.a["usuario"], REPORTE_EXISTENCIAS_EXPORTAR_URL, {"fecha_inicio": ""})
        self.assertEqual(list(resp.json()), ["fecha_inicio"])

    def test_formato_invalido_es_400(self):
        resp = self._get(
            self.a["usuario"], REPORTE_EXISTENCIAS_EXPORTAR_URL, {"formato": "csv", "almacen": self.a["almacen"].pk},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("formato", resp.json())

    def test_aislamiento_por_empresa_y_sucursal(self):
        self._entrada_por_ajuste(self.a)
        # Empresa ajena: el almacén de A no abre el alcance de B.
        resp = self._get(self.b["usuario"], REPORTE_EXISTENCIAS_EXPORTAR_URL, {"formato": "xlsx", "almacen": self.a["almacen"].pk})
        _, filas = self._tabla_xlsx(resp.content, "SKU")
        self.assertEqual(filas, [])
        texto = self._texto_pdf(self._get(self.b["usuario"], REPORTE_EXISTENCIAS_EXPORTAR_URL, {"almacen": self.a["almacen"].pk}).content)
        self.assertIn("Sin existencias en el periodo.", texto)
        self.assertNotIn("acme-rp", texto.replace(f"Almacén #{self.a['almacen'].pk}", ""))
        # Misma empresa, sucursal sin acceso: el almacén norte no sale.
        resp = self._get(
            self.a["usuario"], REPORTE_EXISTENCIAS_EXPORTAR_URL, {"formato": "xlsx", "almacen": self.a["almacen_norte"].pk},
        )
        _, filas = self._tabla_xlsx(resp.content, "SKU")
        self.assertEqual(filas, [])
        # Movimientos de otra empresa.
        resp = self._get(
            self.b["usuario"], REPORTE_MOVIMIENTOS_EXPORTAR_URL,
            {"formato": "xlsx", "tipo_movimiento": "ENTRADA", "almacen": self.a["almacen"].pk},
        )
        _, filas = self._tabla_xlsx(resp.content, "Fecha")
        self.assertEqual(filas, [])
        texto = self._texto_pdf(self._get(
            self.b["usuario"], REPORTE_MOVIMIENTOS_EXPORTAR_URL, {"tipo_movimiento": "ENTRADA"},
        ).content)
        self.assertNotIn("acme-rp", texto)
        self.assertIn("globex-rp", texto)

    def test_muchas_filas_se_paginan_con_encabezado(self):
        variantes = [
            ProductoVariante(
                producto=self.a["producto"], empresa=self.a["empresa"], color=self.color, talla=self.talla,
                sku=f"acme-rp-X{i:03d}", precio_base="1",
            )
            for i in range(120)
        ]
        ProductoVariante.objects.bulk_create(variantes)
        Existencia.objects.bulk_create([
            Existencia(almacen=self.a["almacen"], producto=self.a["producto"], producto_variante=v, cantidad="1")
            for v in variantes
        ])
        resp = self._get(self.a["usuario"], REPORTE_EXISTENCIAS_EXPORTAR_URL, {"almacen": self.a["almacen"].pk})
        from pypdf import PdfReader
        import io
        paginas = PdfReader(io.BytesIO(resp.content)).pages
        self.assertGreater(len(paginas), 2)
        for pagina in paginas:
            texto = pagina.extract_text()
            self.assertIn("Exist. Final", texto)
            self.assertIn(f"de {len(paginas)}", texto)
        self.assertIn("acme-rp-X119", self._texto_pdf(resp.content))

    def test_xlsx_guarda_como_texto_lo_que_parece_formula(self):
        from openpyxl import load_workbook
        import io
        prefijos = ["=1+1", "+52 81", "-5", "@SUMA(A1)", "＠x", "#N/A"]
        producto = Producto.objects.create(
            tipo=_tipo_producto(), empresa=self.a["empresa"], nombre='=HYPERLINK("http://x","Ver")',
        )
        for i, sku in enumerate(prefijos):
            variante = ProductoVariante.objects.create(
                producto=producto, empresa=self.a["empresa"], color=self.color, talla=self.talla,
                sku=sku, precio_base="1",
            )
            Existencia.objects.create(almacen=self.a["almacen"], producto=producto, producto_variante=variante, cantidad=str(i))
        resp = self._get(
            self.a["usuario"], REPORTE_EXISTENCIAS_EXPORTAR_URL, {"formato": "xlsx", "almacen": self.a["almacen"].pk},
        )
        hoja = load_workbook(io.BytesIO(resp.content)).active
        celdas = {fila[0].value: fila for fila in hoja.iter_rows() if fila[0].value in prefijos}
        self.assertEqual(set(celdas), set(prefijos))
        for sku, fila in celdas.items():
            self.assertEqual((fila[0].value, fila[0].data_type), (sku, "s"))
            self.assertEqual((fila[1].value, fila[1].data_type), ('=HYPERLINK("http://x","Ver")', "s"))
            self.assertEqual(fila[5].data_type, "n")  # las cantidades siguen siendo números

    def test_accept_del_archivo_o_comodin_descarga_y_los_errores_salen_en_json(self):
        cliente = APIClient()
        cliente.force_authenticate(user=self.a["usuario"])
        base = {**PERIODO_REPORTE, "almacen": self.a["almacen"].pk}
        casos = (
            ("pdf", "application/pdf", "application/pdf"),
            ("xlsx", CONTENT_TYPE_XLSX, CONTENT_TYPE_XLSX),
            ("pdf", "*/*", "application/pdf"),
            ("xlsx", "application/json, text/plain, */*", CONTENT_TYPE_XLSX),
        )
        for formato, accept, esperado in casos:
            for url, extra in (
                (REPORTE_EXISTENCIAS_EXPORTAR_URL, {}),
                (REPORTE_MOVIMIENTOS_EXPORTAR_URL, {"tipo_movimiento": "ENTRADA"}),
            ):
                resp = cliente.get(url, {**base, **extra, "formato": formato}, HTTP_ACCEPT=accept)
                self.assertEqual((resp.status_code, resp["Content-Type"]), (200, esperado), (url, accept))
        # Un 400 pedido como PDF/XLSX sale en JSON.
        for accept in ("application/pdf", CONTENT_TYPE_XLSX):
            resp = cliente.get(REPORTE_EXISTENCIAS_EXPORTAR_URL, PERIODO_REPORTE, HTTP_ACCEPT=accept)
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp["Content-Type"], "application/json")
            self.assertEqual(list(resp.json()), ["almacen"])

    def test_encabezado_no_muestra_nombres_de_otra_empresa(self):
        base = {"almacen": self.a["almacen"].pk}
        ajenos = {
            "empresa": self.b["empresa"].pk, "sucursal": self.b["matriz"].pk,
            "producto": self.b["producto"].pk, "producto_variante": self.b["variante"].pk,
        }
        # Cada filtro ajeno por separado (con alcance propio, el nombre se buscaría
        # en BD) y todos juntos (alcance vacío).
        for params in [{clave: valor} for clave, valor in ajenos.items()] + [ajenos]:
            resp = self._get(self.a["usuario"], REPORTE_EXISTENCIAS_EXPORTAR_URL, {**base, **params})
            self.assertEqual(resp.status_code, 200, params)
            texto = self._texto_pdf(resp.content)
            for nombre in ("globex-rp SA", "Norte globex-rp", "Matriz globex-rp", "Playera globex-rp", "globex-rp-AZ-M"):
                self.assertNotIn(nombre, texto, params)
            for clave, etiqueta in (
                ("empresa", "Empresa"), ("sucursal", "Sucursal"),
                ("producto", "Producto"), ("producto_variante", "Variante"),
            ):
                if clave in params:
                    self.assertIn(f"{etiqueta} #{params[clave]}", texto, params)
            resp = self._get(self.a["usuario"], REPORTE_EXISTENCIAS_EXPORTAR_URL, {**base, **params, "formato": "xlsx"})
            valores = {v for fila in self._hoja(resp.content).iter_rows(values_only=True) for v in fila if v}
            self.assertFalse({"globex-rp SA", "Matriz globex-rp", "Playera globex-rp", "globex-rp-AZ-M"} & valores, params)
        # Control: los propios sí se resuelven por nombre.
        texto = self._texto_pdf(self._get(self.a["usuario"], REPORTE_EXISTENCIAS_EXPORTAR_URL, {
            **base, "empresa": self.a["empresa"].pk, "sucursal": self.a["matriz"].pk,
            "producto": self.a["producto"].pk, "producto_variante": self.a["variante"].pk,
        }).content)
        for nombre in ("acme-rp SA", "Matriz acme-rp", "Playera acme-rp", "acme-rp-AZ-M"):
            self.assertIn(nombre, texto)


class PartirTextoPdfTests(SimpleTestCase):
    """``_partir`` (EC-814): mismas líneas que el corte carácter por carácter
    anterior, sin su costo cuadrático."""

    @staticmethod
    def _partir_anterior(texto, fuente, ancho, tamano=8):
        from reportlab.lib.utils import simpleSplit
        from reportlab.pdfbase.pdfmetrics import stringWidth
        lineas = []
        for linea in simpleSplit(texto, fuente, tamano, ancho) or [texto]:
            while stringWidth(linea, fuente, tamano) > ancho and len(linea) > 1:
                corte = len(linea) - 1
                while corte > 1 and stringWidth(linea[:corte], fuente, tamano) > ancho:
                    corte -= 1
                lineas.append(linea[:corte])
                linea = linea[corte:]
            lineas.append(linea)
        return lineas

    def test_mismas_lineas_que_el_corte_anterior(self):
        from inventarios.services.reportes_exportacion import _partir
        casos = [
            "QA-5000-100-['S', 'L', 'F']",
            "SKU-" + "X1Y2Z3" * 60,
            "BOTA BORCEGUI DIELECTRICA CON CASCO DE POLIAMIDA " + "W" * 120 + " FIN",
            "Ñandú — ññññññññññññññññññññññññññññññññ",
            "corto",
            "",
            "MMMM",
        ]
        for texto in casos:
            for fuente in ("Helvetica", "Helvetica-Bold"):
                for ancho in (3, 20, 58.5, 120):
                    self.assertEqual(
                        _partir(texto, fuente, ancho), self._partir_anterior(texto, fuente, ancho), (texto, fuente, ancho),
                    )
