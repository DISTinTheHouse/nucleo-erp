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

from django.test import TestCase
from rest_framework.test import APIClient

from auditoria.models import AuditoriaEvento
from catalogo.models import Color, Producto, ProductoVariante
from inventarios.models import Almacen, Existencia, Ubicacion
from nucleo.models import Empresa, Sucursal
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
