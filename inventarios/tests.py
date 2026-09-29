"""Tests del scope multi-tenant de ``MovimientoOperacionViewSet``.

``GET /api/v1/inventarios/movimientos/`` NO sirve ``inventarios.Movimiento
Inventario``: sirve ``auditoria.AuditoriaEvento`` filtrado por
``modulo="inventarios", tabla="existencias"`` (es la forma documentada en
``DOCUMENTACION_API.md``, e incluye los eventos que WMS registra al crear una
transferencia). Esos eventos cargan ``antes_json``/``despues_json`` con el
detalle de existencias, así que el aislamiento por empresa es obligatorio.

Ejecutar SIEMPRE con una BD desechable; el ``.env`` del repo apunta a Supabase
de producción. Ejemplo con un settings de override a SQLite en memoria:

    python manage.py test inventarios --settings=sqlite_settings
"""

from decimal import Decimal
from unittest import mock

from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from auditoria.models import AuditoriaEvento
from catalogo.models import Color, Producto, ProductoVariante, Talla
from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle
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

    def test_multi_empresa_puede_acotar_a_una_de_las_suyas(self):
        """Con acceso a A y B, el param acota a B dentro de su propio universo."""
        url = f"{MOVIMIENTOS_URL}?empresa_id={self.b['empresa'].pk}"
        self.assertEqual(self._ids(self.multi_empresa, url), [self.b["evento"].pk])

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

    def test_multi_empresa_ve_ambas_empresas_de_su_scope(self):
        """El M2M ``empresas`` es acceso legítimo, igual que en el resto del app."""
        self.assertEqual(
            sorted(self._ids(self.multi_empresa)),
            sorted([self.a["evento"].pk, self.b["evento"].pk]),
        )

    def test_superuser_ve_todo(self):
        self.assertEqual(
            sorted(self._ids(self.superuser)),
            sorted([self.a["evento"].pk, self.b["evento"].pk]),
        )

    def test_superuser_con_empresa_asignada_sigue_viendo_todo(self):
        self.assertEqual(
            sorted(self._ids(self.superuser_con_empresa)),
            sorted([self.a["evento"].pk, self.b["evento"].pk]),
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
        producto = Producto.objects.create(empresa=empresa, nombre=f"Producto {codigo}")
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
        self.assertEqual(
            self._ids(self.superuser),
            sorted([self.a["existencia"].pk, self.b["existencia"].pk]),
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
        producto = Producto.objects.create(empresa=empresa, nombre=f"Producto {codigo}")
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


REPORTE_RESURTIDO_URL = "/api/v1/inventarios/existencias/reporte-resurtido/"


class ReporteResurtidoTests(TestCase):
    """``GET .../existencias/reporte-resurtido/``: disponible (almacenes de
    Producto Terminado) + en producción (detalle de OP activas) + pendiente
    de compra (OC no recibidas), agrupado por producto con desglose por
    talla en disponible/producción — mesa de control lo usa para decidir
    resurtido sin cruzar pantallas de producción y compras.

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

        producto = Producto.objects.create(empresa=empresa, nombre=f"Playera {codigo}", codigo=f"{codigo}-SKU")
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
        resp = self._client(user).get(REPORTE_RESURTIDO_URL, params or {})
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

    # --- alcance / filtros ------------------------------------------------

    def test_sin_almacenes_en_alcance_devuelve_vacio(self):
        sin_sucursal = Usuario.objects.create(
            username="sinsuc-rs", email="sinsuc@acme-rs.test", empresa=self.a["empresa"],
        )
        self.assertEqual(self._get(sin_sucursal), [])

    def test_filtro_por_producto_id_y_por_sku(self):
        otro_producto = Producto.objects.create(empresa=self.a["empresa"], nombre="Otro", codigo="OTRO-RS")
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
