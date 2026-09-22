"""Tests del scope multi-tenant de ``RecepcionViewSet``.

El branch de interés: el chequeo de ``empresa`` corría ANTES del de
``is_superuser``, así que un superusuario CON empresa asignada quedaba acotado a
esa empresa en vez de ver todas — al revés que ``PedidoViewSet``/
``EmpleadoViewSet``, donde el superusuario se evalúa primero.

Ejecutar SIEMPRE con una BD desechable; el ``.env`` del repo apunta a Supabase
de producción. Ejemplo con un settings de override a SQLite en memoria:

    python manage.py test compras --settings=sqlite_settings
"""

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from catalogo.models import Color, Producto, ProductoVariante
from compras.api.views import RecepcionViewSet
from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion
from inventarios.models import Almacen, Existencia, MovimientoInventario, MovimientoInventarioDetalle, Ubicacion
from nucleo.models import Empresa, Moneda, SatFormaPago, SatMetodoPago, SatRegimenFiscal, SerieFolio, Sucursal
from terceros.models import Proveedor
from usuarios.models import Usuario

RECEPCIONES_URL = "/api/v1/compras/recepciones/"


class RecepcionViewSetScopeTenantTests(TestCase):
    @classmethod
    def _tenant(cls, codigo, email):
        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        sucursal = Sucursal.objects.create(
            empresa=empresa, codigo=codigo[:3].upper(), nombre=codigo
        )
        almacen = Almacen.objects.create(
            empresa=empresa, sucursal=sucursal, codigo="ALM", nombre=f"Almacen {codigo}"
        )
        usuario = Usuario.objects.create(
            username=email, email=email, empresa=empresa, sucursal_default=sucursal
        )
        recepcion = Recepcion.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            almacen=almacen,
            usuario=usuario,
            folio=f"RC-{codigo}",
            fecha_recepcion=timezone.now(),
        )
        return {"empresa": empresa, "usuario": usuario, "recepcion": recepcion}

    @classmethod
    def setUpTestData(cls):
        cls.a = cls._tenant("acme-rc", "a@acme-rc.test")
        cls.b = cls._tenant("globex-rc", "b@globex-rc.test")

        cls.sin_empresa = Usuario.objects.create(
            username="huerfano-rc", email="huerfano@nowhere-rc.test"
        )
        cls.superuser = Usuario.objects.create(
            username="root-rc", email="root@nowhere-rc.test", is_superuser=True
        )
        cls.superuser_con_empresa = Usuario.objects.create(
            username="root-rc-a",
            email="root-a@acme-rc.test",
            empresa=cls.a["empresa"],
            is_superuser=True,
        )

    def _ids(self, user, url=RECEPCIONES_URL):
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
        return [row["id"] for row in resp.json()]

    # --- el branch roto -------------------------------------------------------

    def test_superuser_con_empresa_asignada_ve_todas_las_recepciones(self):
        """Antes quedaba acotado a su propia empresa por el orden del chequeo."""
        self.assertEqual(
            sorted(self._ids(self.superuser_con_empresa)),
            sorted([self.a["recepcion"].pk, self.b["recepcion"].pk]),
        )

    def test_superuser_con_empresa_puede_hacer_retrieve_de_otra_empresa(self):
        client = APIClient()
        client.force_authenticate(user=self.superuser_con_empresa)
        resp = client.get(f"{RECEPCIONES_URL}{self.b['recepcion'].pk}/")
        self.assertEqual(resp.status_code, 200)

    # --- no regresión en las ramas que ya eran correctas ----------------------

    def test_superuser_sin_empresa_sigue_viendo_todo(self):
        self.assertEqual(
            sorted(self._ids(self.superuser)),
            sorted([self.a["recepcion"].pk, self.b["recepcion"].pk]),
        )

    def test_usuario_normal_sigue_acotado_a_su_empresa(self):
        self.assertEqual(self._ids(self.a["usuario"]), [self.a["recepcion"].pk])

    def test_usuario_normal_no_hace_retrieve_de_otra_empresa(self):
        client = APIClient()
        client.force_authenticate(user=self.a["usuario"])
        resp = client.get(f"{RECEPCIONES_URL}{self.b['recepcion'].pk}/")
        self.assertEqual(resp.status_code, 404)

    def test_usuario_sin_empresa_no_ve_ninguna_recepcion(self):
        self.assertEqual(self._ids(self.sin_empresa), [])

    def test_filtro_tipo_origen_sigue_vivo(self):
        url = f"{RECEPCIONES_URL}?tipo_origen=OP"
        self.assertEqual(self._ids(self.a["usuario"], url), [])


class RecepcionMovimientoFormalVarianteTests(TestCase):
    """El movimiento formal de la recepción conserva la variante de cada renglón.

    Se prueba sobre los dos pasos que ``onboarding`` encadena dentro de su
    ``atomic``: ``_actualizar_existencias`` (el stock) y
    ``_crear_movimiento_formal_recepcion`` (el detalle).
    """

    @classmethod
    def setUpTestData(cls):
        empresa = Empresa.objects.create(codigo="acme-rv", razon_social="acme-rv SA")
        sucursal = Sucursal.objects.create(empresa=empresa, codigo="ARV", nombre="acme-rv")
        cls.almacen = Almacen.objects.create(empresa=empresa, sucursal=sucursal, codigo="ALM", nombre="Almacen")
        cls.ubicacion = Ubicacion.objects.create(almacen=cls.almacen, pasillo="1")
        usuario = Usuario.objects.create(username="u@acme-rv.test", email="u@acme-rv.test", empresa=empresa)
        color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000")
        cls.producto_pt = Producto.objects.create(empresa=empresa, nombre="Playera")
        cls.variante = ProductoVariante.objects.create(
            producto=cls.producto_pt, empresa=empresa, color=color, sku="PLA-NEG", precio_base="1",
        )
        cls.producto_mp = Producto.objects.create(empresa=empresa, nombre="Hilo")
        cls.recepcion = Recepcion.objects.create(
            empresa=empresa, sucursal=sucursal, almacen=cls.almacen, usuario=usuario,
            folio="RC-RV-1", fecha_recepcion=timezone.now(),
        )

    def _renglon(self, producto, variante, cantidad, ubicacion=None):
        # Misma forma que ``onboarding`` arma en ``detalle_payload``.
        return {
            "orden_compra_detalle": None, "orden_produccion_detalle": None,
            "producto": producto, "producto_variante": variante,
            "cantidad_recibida": Decimal(cantidad), "ubicacion": ubicacion,
            "lote": None, "serie": None,
        }

    def test_detalle_guarda_la_variante_y_conserva_lo_demas(self):
        view = RecepcionViewSet()
        movimientos = view._actualizar_existencias(
            self.recepcion,
            [
                # Renglón de OP: siempre trae variante.
                self._renglon(self.producto_pt, self.variante, "5", self.ubicacion.pk),
                # Renglón de OC: ``OrdenCompraDetalle`` no tiene variante.
                self._renglon(self.producto_mp, None, "3"),
            ],
        )
        movimiento = view._crear_movimiento_formal_recepcion(self.recepcion, movimientos)

        detalles = {
            d.producto_id: d
            for d in MovimientoInventarioDetalle.objects.filter(movimiento_inventario=movimiento)
        }
        self.assertEqual(len(detalles), 2)
        con_variante = detalles[self.producto_pt.pk]
        self.assertEqual(con_variante.producto_variante_id, self.variante.pk)
        self.assertEqual(con_variante.cantidad, Decimal("5"))
        self.assertEqual((con_variante.ubicacion_origen_id, con_variante.ubicacion_destino_id), (None, self.ubicacion.pk))
        sin_variante = detalles[self.producto_mp.pk]
        self.assertIsNone(sin_variante.producto_variante_id)
        self.assertEqual(sin_variante.cantidad, Decimal("3"))
        self.assertEqual((sin_variante.ubicacion_origen_id, sin_variante.ubicacion_destino_id), (None, None))

        # El stock se mueve igual que antes: una existencia por renglón.
        self.assertEqual(
            Existencia.objects.get(producto_variante=self.variante, ubicacion=self.ubicacion, almacen=self.almacen).cantidad,
            Decimal("5"),
        )
        self.assertEqual(
            Existencia.objects.get(producto=self.producto_mp, producto_variante=None, almacen=self.almacen).cantidad,
            Decimal("3"),
        )


RECEPCION_ONBOARDING_URL = "/api/v1/compras/recepciones/onboarding/"


class RecepcionOnboardingAlmacenScopeTests(TestCase):
    """``POST /recepciones/onboarding/``: a qué almacén se puede recibir.

    Convención de compras (acotada por empresa, no por sucursales del usuario):
    la orden (OC/OP) debe ser de la empresa del usuario, y el almacén debe ser de
    la MISMA empresa y sucursal que la orden. Toda la validación corre antes de
    la primera escritura; un rechazo no deja recepción, movimiento ni stock.
    """

    @classmethod
    def setUpTestData(cls):
        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General de Ley")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Pago en una sola exhibición")

        cls.empresa = Empresa.objects.create(codigo="acme-ra", razon_social="acme-ra SA")
        cls.otra_empresa = Empresa.objects.create(codigo="globex-ra", razon_social="globex-ra SA")
        cls.suc_1 = Sucursal.objects.create(empresa=cls.empresa, codigo="AR1", nombre="acme-ra 1")
        cls.suc_2 = Sucursal.objects.create(empresa=cls.empresa, codigo="AR2", nombre="acme-ra 2")
        suc_otra = Sucursal.objects.create(empresa=cls.otra_empresa, codigo="GR1", nombre="globex-ra 1")
        SerieFolio.objects.create(empresa=cls.empresa, sucursal=cls.suc_1, tipo_documento="RECEPCION", serie="RC")

        cls.almacen = Almacen.objects.create(empresa=cls.empresa, sucursal=cls.suc_1, codigo="A1", nombre="A1")
        cls.almacen_otra_sucursal = Almacen.objects.create(
            empresa=cls.empresa, sucursal=cls.suc_2, codigo="A2", nombre="A2",
        )
        cls.almacen_otra_empresa = Almacen.objects.create(
            empresa=cls.otra_empresa, sucursal=suc_otra, codigo="G1", nombre="G1",
        )
        cls.almacen_sin_empresa = Almacen.objects.create(sucursal=cls.suc_1, codigo="SE", nombre="Sin empresa")
        cls.almacen_sin_sucursal = Almacen.objects.create(empresa=cls.empresa, codigo="SS", nombre="Sin sucursal")

        cls.usuario = Usuario.objects.create(username="u@acme-ra.test", email="u@acme-ra.test", empresa=cls.empresa)
        cls.usuario.sucursales.add(cls.suc_1)
        proveedor = Proveedor.objects.create(
            empresa=cls.empresa, nombre="Proveedor", moneda=moneda, sat_regimen_fiscal=regimen,
            sat_forma_pago=forma, sat_metodo_pago=metodo, codigo="PROV-RA", razon_social="Proveedor SA",
            telefono="8100000000", contacto_principal="Contacto", rfc="XAXX010101000", email="p@acme-ra.test",
        )
        cls.oc = OrdenCompra.objects.create(
            empresa=cls.empresa, sucursal=cls.suc_1, proveedor=proveedor, moneda=moneda, usuario=cls.usuario,
            fecha_oc=timezone.now().date(), estatus=OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
        )
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Tela")
        cls.oc_detalle = OrdenCompraDetalle.objects.create(
            orden_compra=cls.oc, producto=cls.producto, sucursal=cls.suc_1, cantidad=100,
        )

    def _recibir(self, user, almacen, cantidad="1"):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.post(
            RECEPCION_ONBOARDING_URL,
            {
                "recepcion": {"orden_compra": self.oc.pk, "almacen": almacen.pk, "serie_codigo": "RC"},
                "detalle": [{"orden_compra_detalle": self.oc_detalle.pk, "cantidad_recibida": cantidad}],
            },
            format="json",
        )

    def _huella(self):
        return (
            Recepcion.objects.count(),
            MovimientoInventario.objects.count(),
            sorted(Existencia.objects.values_list("almacen_id", "cantidad")),
            SerieFolio.objects.get(empresa=self.empresa).folio_actual,
        )

    def _assert_rechazo_sin_escrituras(self, user, almacen, campo, mensaje):
        antes = self._huella()
        resp = self._recibir(user, almacen)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {campo: mensaje})
        self.assertEqual(self._huella(), antes)

    # --- almacén fuera de la empresa/sucursal de la orden ------------------------

    def test_almacen_de_otra_empresa_es_rechazado(self):
        self._assert_rechazo_sin_escrituras(
            self.usuario, self.almacen_otra_empresa, "almacen", "El almacén no pertenece a la empresa de la orden.",
        )

    def test_almacen_de_otra_sucursal_de_la_empresa_es_rechazado(self):
        self._assert_rechazo_sin_escrituras(
            self.usuario, self.almacen_otra_sucursal, "almacen", "El almacén no pertenece a la sucursal de la orden.",
        )

    def test_almacen_sin_empresa_es_rechazado(self):
        # Antes la guarda era ``... and almacen.empresa_id and ...``: sin empresa se saltaba.
        self._assert_rechazo_sin_escrituras(
            self.usuario, self.almacen_sin_empresa, "almacen", "El almacén no pertenece a la empresa de la orden.",
        )

    def test_almacen_sin_sucursal_es_rechazado(self):
        # Misma guarda gemela: ``... and almacen.sucursal_id and ...`` se saltaba.
        self._assert_rechazo_sin_escrituras(
            self.usuario, self.almacen_sin_sucursal, "almacen", "El almacén no pertenece a la sucursal de la orden.",
        )

    # --- quién puede recibir ---------------------------------------------------

    def test_usuario_sin_empresa_es_rechazado(self):
        sin_empresa = Usuario.objects.create(username="n@acme-ra.test", email="n@acme-ra.test")
        sin_empresa.sucursales.add(self.suc_1)
        self._assert_rechazo_sin_escrituras(
            sin_empresa, self.almacen, "empresa", "El usuario no tiene empresa asignada.",
        )

    def test_superusuario_sin_empresa_sigue_recibiendo(self):
        root = Usuario.objects.create(username="r@acme-ra.test", email="r@acme-ra.test", is_superuser=True)
        resp = self._recibir(root, self.almacen, "2")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Existencia.objects.get(almacen=self.almacen).cantidad, Decimal("2"))

    def test_convencion_de_compras_no_exige_sucursal_asignada_al_usuario(self):
        # Compras acota por empresa; el almacén queda atado a la sucursal de la
        # orden, no a ``user.sucursales``. Se fija el comportamiento actual.
        solo_suc_2 = Usuario.objects.create(username="s2@acme-ra.test", email="s2@acme-ra.test", empresa=self.empresa)
        solo_suc_2.sucursales.add(self.suc_2)
        resp = self._recibir(solo_suc_2, self.almacen)
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_recepcion_legitima_actualiza_stock_y_movimiento(self):
        resp = self._recibir(self.usuario, self.almacen, "5")
        self.assertEqual(resp.status_code, 200, resp.content)
        recepcion = Recepcion.objects.get(pk=resp.json()["recepcion"]["id"])
        self.assertEqual((recepcion.almacen_id, recepcion.empresa_id, recepcion.sucursal_id),
                         (self.almacen.pk, self.empresa.pk, self.suc_1.pk))
        self.assertEqual(Existencia.objects.get(almacen=self.almacen, producto=self.producto).cantidad, Decimal("5"))
        detalle = MovimientoInventarioDetalle.objects.get(movimiento_inventario__recepcion=recepcion)
        self.assertEqual((detalle.producto_id, detalle.cantidad), (self.producto.pk, Decimal("5")))


ORDENES_URL = "/api/v1/compras/ordenes/"


class OrdenCompraAislamientoEmpresaTests(TestCase):
    """OC (onboarding / PUT / aceptar): las FKs escritas son de la empresa de la orden.

    Compras acota por empresa (no por sucursales del usuario). La regla aplica a
    todos, superusuario incluido; toda la validación corre antes de la primera
    escritura. ``moneda`` es un catálogo híbrido: vale una global
    (``empresa`` nula) o una privada de la misma empresa.
    """

    @classmethod
    def setUpTestData(cls):
        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General de Ley")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Pago en una sola exhibición")
        cls.moneda_global = Moneda.objects.create(codigo_iso="USD", nombre="Dólar")

        def tenant(codigo):
            empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
            sucursal = Sucursal.objects.create(empresa=empresa, codigo=codigo[:3].upper(), nombre=codigo)
            moneda = Moneda.objects.create(codigo_iso=codigo[:3].upper(), nombre=codigo, empresa=empresa)
            empresa.moneda_base = moneda
            empresa.save(update_fields=["moneda_base"])
            proveedor = Proveedor.objects.create(
                empresa=empresa, nombre=f"Proveedor {codigo}", moneda=moneda, sat_regimen_fiscal=regimen,
                sat_forma_pago=forma, sat_metodo_pago=metodo, codigo=f"PROV-{codigo}", razon_social="Prov SA",
                telefono="8100000000", contacto_principal="Contacto", rfc="XAXX010101000", email=f"p@{codigo}.test",
            )
            producto = Producto.objects.create(empresa=empresa, nombre=f"Insumo {codigo}")
            return {"empresa": empresa, "sucursal": sucursal, "moneda": moneda, "proveedor": proveedor, "producto": producto}

        cls.a = tenant("acme-oc")
        cls.b = tenant("globex-oc")
        cls.usuario = Usuario.objects.create(
            username="u@acme-oc.test", email="u@acme-oc.test", empresa=cls.a["empresa"],
            sucursal_default=cls.a["sucursal"],
        )
        cls.superuser = Usuario.objects.create(
            username="root@acme-oc.test", email="root@acme-oc.test", empresa=cls.a["empresa"],
            sucursal_default=cls.a["sucursal"], is_superuser=True,
        )

    def setUp(self):
        self.oc = OrdenCompra.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"], proveedor=self.a["proveedor"],
            moneda=self.a["moneda"], usuario=self.usuario, fecha_oc=timezone.now().date(),
            estatus=OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR,
        )
        OrdenCompraDetalle.objects.create(
            orden_compra=self.oc, producto=self.a["producto"], sucursal=self.a["sucursal"], cantidad=3,
        )

    # --- helpers ---------------------------------------------------------------

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _linea(self, producto, cantidad=2):
        return {"producto": producto.pk, "cantidad": cantidad, "precio": "10.00"}

    def _huella(self):
        oc = OrdenCompra.objects.get(pk=self.oc.pk)
        return (
            OrdenCompra.objects.count(),
            (oc.sucursal_id, oc.proveedor_id, oc.moneda_id, oc.estatus, oc.folio),
            sorted(OrdenCompraDetalle.objects.values_list("orden_compra_id", "producto_id", "cantidad")),
        )

    def _campos_ajenos(self):
        return (
            ("sucursal", self.b["sucursal"].pk, "La sucursal no pertenece a la empresa de la orden."),
            ("proveedor", self.b["proveedor"].pk, "El proveedor no pertenece a la empresa de la orden."),
            ("moneda", self.b["moneda"].pk, "La moneda no está disponible para la empresa de la orden."),
        )

    def _assert_rechazo(self, resp, campo, mensaje, antes):
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {campo: mensaje})
        self.assertEqual(self._huella(), antes)

    # --- encabezado: create / edit / PUT ------------------------------------------

    def test_onboarding_crea_rechaza_fk_de_otra_empresa_incluso_al_superusuario(self):
        antes = self._huella()
        base = {"sucursal": self.a["sucursal"].pk, "proveedor": self.a["proveedor"].pk, "moneda": self.a["moneda"].pk}
        for user in (self.usuario, self.superuser):
            for campo, valor, mensaje in self._campos_ajenos():
                resp = self._client(user).post(
                    f"{ORDENES_URL}onboarding/",
                    {"orden_compra": {**base, campo: valor}, "detalle": [self._linea(self.a["producto"])]},
                    format="json",
                )
                self._assert_rechazo(resp, campo, mensaje, antes)

    def test_onboarding_edita_rechaza_fk_de_otra_empresa(self):
        antes = self._huella()
        for user in (self.usuario, self.superuser):
            for campo, valor, mensaje in self._campos_ajenos():
                resp = self._client(user).post(
                    f"{ORDENES_URL}onboarding/",
                    {"orden_compra_id": self.oc.pk, "orden_compra": {campo: valor}},
                    format="json",
                )
                self._assert_rechazo(resp, campo, mensaje, antes)

    def test_put_rechaza_fk_de_otra_empresa(self):
        antes = self._huella()
        for user in (self.usuario, self.superuser):
            for campo, valor, mensaje in self._campos_ajenos():
                resp = self._client(user).put(
                    f"{ORDENES_URL}{self.oc.pk}/", {"orden_compra": {campo: valor}}, format="json",
                )
                self._assert_rechazo(resp, campo, mensaje, antes)

    # --- renglones ---------------------------------------------------------------

    def test_renglon_con_producto_de_otra_empresa_es_rechazado(self):
        antes = self._huella()
        lineas = [self._linea(self.a["producto"]), self._linea(self.b["producto"])]
        mensaje = "El producto del renglón #2 no pertenece a la empresa de la orden."
        base = {"sucursal": self.a["sucursal"].pk, "proveedor": self.a["proveedor"].pk}
        for user in (self.usuario, self.superuser):
            client = self._client(user)
            crea = client.post(f"{ORDENES_URL}onboarding/", {"orden_compra": base, "detalle": lineas}, format="json")
            self._assert_rechazo(crea, "detalle", mensaje, antes)
            edita = client.post(
                f"{ORDENES_URL}onboarding/", {"orden_compra_id": self.oc.pk, "detalle": lineas}, format="json",
            )
            self._assert_rechazo(edita, "detalle", mensaje, antes)
            put = client.put(f"{ORDENES_URL}{self.oc.pk}/", {"detalle": lineas}, format="json")
            self._assert_rechazo(put, "detalle", mensaje, antes)

    # --- aceptar -----------------------------------------------------------------

    def test_aceptar_rechaza_proveedor_de_otra_empresa(self):
        antes = self._huella()
        for user in (self.usuario, self.superuser):
            resp = self._client(user).post(
                f"{ORDENES_URL}{self.oc.pk}/aceptar/", {"proveedor": self.b["proveedor"].pk}, format="json",
            )
            self._assert_rechazo(resp, "proveedor", "El proveedor no pertenece a la empresa de la orden.", antes)

    def test_aceptar_superusuario_sin_empresa_sigue_siendo_404(self):
        # ``get_object`` ya acota por ``user.empresa``: sin empresa, 404 antes de
        # la guarda; cerrarla no cambia nada.
        root = Usuario.objects.create(username="root2@acme-oc.test", email="root2@acme-oc.test", is_superuser=True)
        resp = self._client(root).post(f"{ORDENES_URL}{self.oc.pk}/aceptar/", {}, format="json")
        self.assertEqual(resp.status_code, 404)

    # --- flujos legítimos --------------------------------------------------------

    def test_moneda_global_o_propia_se_acepta(self):
        client = self._client(self.usuario)
        for moneda in (self.moneda_global, self.a["moneda"]):
            resp = client.put(f"{ORDENES_URL}{self.oc.pk}/", {"orden_compra": {"moneda": moneda.pk}}, format="json")
            self.assertEqual(resp.status_code, 200, resp.content)
            self.oc.refresh_from_db()
            self.assertEqual(self.oc.moneda_id, moneda.pk)

    def test_crear_editar_y_aceptar_legitimos_siguen_funcionando(self):
        client = self._client(self.usuario)
        crea = client.post(
            f"{ORDENES_URL}onboarding/",
            {"orden_compra": {"proveedor": self.a["proveedor"].pk}, "detalle": [self._linea(self.a["producto"], 4)]},
            format="json",
        )
        self.assertEqual(crea.status_code, 200, crea.content)
        oc = OrdenCompra.objects.get(pk=crea.json()["orden_compra"]["id"])
        self.assertEqual(
            (oc.empresa_id, oc.sucursal_id, oc.proveedor_id, oc.moneda_id),
            (self.a["empresa"].pk, self.a["sucursal"].pk, self.a["proveedor"].pk, self.a["moneda"].pk),
        )

        edita = client.post(
            f"{ORDENES_URL}onboarding/",
            {"orden_compra_id": oc.pk, "orden_compra": {"referencia": "R-1"},
             "detalle": [self._linea(self.a["producto"], 6)]},
            format="json",
        )
        self.assertEqual(edita.status_code, 200, edita.content)
        put = client.put(
            f"{ORDENES_URL}{oc.pk}/",
            {"orden_compra": {"sucursal": self.a["sucursal"].pk, "proveedor": self.a["proveedor"].pk},
             "detalle": [self._linea(self.a["producto"], 7)]},
            format="json",
        )
        self.assertEqual(put.status_code, 200, put.content)
        self.assertEqual(
            list(OrdenCompraDetalle.objects.filter(orden_compra=oc).values_list("producto_id", "cantidad")),
            [(self.a["producto"].pk, 7)],
        )

        acepta = client.post(f"{ORDENES_URL}{oc.pk}/aceptar/", {"proveedor": self.a["proveedor"].pk}, format="json")
        self.assertEqual(acepta.status_code, 200, acepta.content)
        oc.refresh_from_db()
        self.assertEqual(oc.estatus, OrdenCompra.EstatusOrdenCompra.AUTORIZADA)
        self.assertIsNotNone(oc.folio)
