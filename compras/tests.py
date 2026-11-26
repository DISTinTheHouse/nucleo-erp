"""Tests del scope multi-tenant de ``RecepcionViewSet``.

Decisión vigente (#834): el superusuario queda acotado a ``user.empresa``,
igual que el listado de OC; sin empresa no ve recepciones.

No correr con ``manage.py test``: crearía la BD de test en el Supabase de
producción, y el proyecto ya no usa SQLite. Los cambios se verifican contra
producción en solo lectura o dentro de una transacción con rollback (ver
``CLAUDE.md``, sección Pruebas); estos casos documentan el comportamiento esperado.
"""

import copy
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from auditoria.models import AuditoriaEvento
from catalogo.models import CategoriaProducto, Color, Producto, ProductoVariante, TipoProducto
from compras.api.views import CalidadInspeccionViewSet, OrdenCompraViewSet, RecepcionViewSet
from compras.models import (
    CalidadInspeccion,
    CalidadInspeccionDetalle,
    OrdenCompra,
    OrdenCompraDetalle,
    OrdenCompraRutaCritica,
    Recepcion,
    RecepcionDetalle,
    RecepcionRFIDEncuadre,
    RecepcionRFIDLectura,
)
from finanzas.models import FacturaProveedor, FacturaProveedorDetalle
from hr.models import Empleado, Puesto
from inventarios.models import Almacen, Existencia, MovimientoInventario, MovimientoInventarioDetalle, Ubicacion
from nucleo.models import Departamento, Empresa, Moneda, SatFormaPago, SatMetodoPago, SatRegimenFiscal, SerieFolio, Sucursal
from terceros.models import Proveedor
from usuarios.models import Usuario


def _tipo_producto():
    """Tipo de producto obligatorio (#871); PT tiene pk=3 como en producción."""
    return TipoProducto.objects.get_or_create(codigo="PT", defaults={"pk": 3, "nombre": "Producto terminado"})[0]


def _conceder(usuario, *claves):
    """GRANT directo de ``claves`` al usuario en su empresa activa."""
    from seguridad.models import Permiso, UsuarioPermiso

    for clave in claves:
        permiso, _ = Permiso.objects.get_or_create(clave=clave, defaults={"nombre": clave})
        UsuarioPermiso.objects.create(
            usuario=usuario, permiso=permiso, tipo=UsuarioPermiso.TIPO_GRANT, empresa=usuario.empresa,
        )


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

    # --- superusuario acotado a su empresa (#834) ------------------------------

    def test_superuser_con_empresa_asignada_solo_ve_las_de_su_empresa(self):
        self.assertEqual(self._ids(self.superuser_con_empresa), [self.a["recepcion"].pk])

    def test_superuser_con_empresa_no_hace_retrieve_de_otra_empresa(self):
        client = APIClient()
        client.force_authenticate(user=self.superuser_con_empresa)
        resp = client.get(f"{RECEPCIONES_URL}{self.b['recepcion'].pk}/")
        self.assertEqual(resp.status_code, 404)

    def test_superuser_sin_empresa_no_ve_recepciones(self):
        self.assertEqual(self._ids(self.superuser), [])

    # --- no regresión en las ramas que ya eran correctas ----------------------

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
    """El movimiento formal de Calidad conserva la variante de cada renglón.

    Antes se probaba sobre ``RecepcionViewSet._actualizar_existencias``/
    ``_crear_movimiento_formal_recepcion``. Esos métodos ya no existen: el
    abono a Existencia y el ``MovimientoInventario`` se movieron a
    ``CalidadInspeccionViewSet._abonar_renglon``/``_crear_movimiento_formal``
    (ver DOCS/arquitectura/flujo-recepcion-calidad-compras.md). Se prueba
    directo sobre esos métodos, igual que antes.
    """

    @classmethod
    def setUpTestData(cls):
        empresa = Empresa.objects.create(codigo="acme-rv", razon_social="acme-rv SA")
        sucursal = Sucursal.objects.create(empresa=empresa, codigo="ARV", nombre="acme-rv")
        cls.almacen = Almacen.objects.create(empresa=empresa, sucursal=sucursal, codigo="ALM", nombre="Almacen")
        cls.ubicacion = Ubicacion.objects.create(almacen=cls.almacen, pasillo="1")
        usuario = Usuario.objects.create(username="u@acme-rv.test", email="u@acme-rv.test", empresa=empresa)
        color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000")
        cls.producto_pt = Producto.objects.create(tipo=_tipo_producto(), empresa=empresa, nombre="Playera")
        cls.variante = ProductoVariante.objects.create(
            producto=cls.producto_pt, empresa=empresa, color=color, sku="PLA-NEG", precio_base="1",
        )
        cls.producto_mp = Producto.objects.create(tipo=_tipo_producto(), empresa=empresa, nombre="Hilo")
        cls.recepcion = Recepcion.objects.create(
            empresa=empresa, sucursal=sucursal, almacen=cls.almacen, usuario=usuario,
            folio="RC-RV-1", fecha_recepcion=timezone.now(),
        )

    def test_detalle_guarda_la_variante_y_conserva_lo_demas(self):
        # Renglón de OP: siempre trae variante. Renglón de OC: nunca (mismo
        # caso que antes, ahora como ``RecepcionDetalle`` ya existente en vez
        # de un ``detalle_payload`` crudo).
        detalle_pt = RecepcionDetalle.objects.create(
            recepcion=self.recepcion, producto=self.producto_pt, producto_variante=self.variante,
            ubicacion=self.ubicacion, cantidad_recibida=Decimal("5"),
        )
        detalle_mp = RecepcionDetalle.objects.create(
            recepcion=self.recepcion, producto=self.producto_mp, producto_variante=None,
            cantidad_recibida=Decimal("3"),
        )

        view = CalidadInspeccionViewSet()
        movimientos = [
            view._abonar_renglon(self.recepcion, detalle_pt, Decimal("5")),
            view._abonar_renglon(self.recepcion, detalle_mp, Decimal("3")),
        ]
        movimiento = view._crear_movimiento_formal(self.recepcion, movimientos)

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

        # Admin: el onboarding exige R-/C-WMS-RECEPCIONES (#1295); el admin pasa.
        cls.usuario = Usuario.objects.create(
            username="u@acme-ra.test", email="u@acme-ra.test", empresa=cls.empresa, is_admin_empresa=True,
        )
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
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Tela")
        cls.oc_detalle = OrdenCompraDetalle.objects.create(
            orden_compra=cls.oc, producto=cls.producto, sucursal=cls.suc_1, cantidad=100,
        )

    def _recibir(self, user, almacen, cantidad="1", serie_codigo="RC"):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.post(
            RECEPCION_ONBOARDING_URL,
            {
                "recepcion": {"orden_compra": self.oc.pk, "almacen": almacen.pk, "serie_codigo": serie_codigo},
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
        self.assertEqual(resp.json(), {campo: [mensaje]})  # forma de DRF en toda llave (#1296)
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
        sin_empresa = Usuario.objects.create(username="n@acme-ra.test", email="n@acme-ra.test", is_admin_empresa=True)
        sin_empresa.sucursales.add(self.suc_1)
        self._assert_rechazo_sin_escrituras(
            sin_empresa, self.almacen, "empresa", "El usuario no tiene empresa asignada.",
        )

    def test_superusuario_sin_empresa_es_rechazado(self):
        # Mismo alcance que el GET de onboarding y el listado (#875): antes se
        # saltaba la pertenencia de la OC/OP y recibía contra cualquier empresa.
        root = Usuario.objects.create(username="r@acme-ra.test", email="r@acme-ra.test", is_superuser=True)
        self._assert_rechazo_sin_escrituras(
            root, self.almacen, "empresa", "El usuario no tiene empresa asignada.",
        )

    def test_convencion_de_compras_no_exige_sucursal_asignada_al_usuario(self):
        # Compras acota por empresa; el almacén queda atado a la sucursal de la
        # orden, no a ``user.sucursales``. Se fija el comportamiento actual.
        solo_suc_2 = Usuario.objects.create(
            username="s2@acme-ra.test", email="s2@acme-ra.test", empresa=self.empresa, is_admin_empresa=True,
        )
        solo_suc_2.sucursales.add(self.suc_2)
        resp = self._recibir(solo_suc_2, self.almacen)
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_recepcion_legitima_registra_el_conteo_y_pasa_a_calidad(self):
        # Recepción ya no toca Existencia/MovimientoInventario: solo cuenta.
        # Ver DOCS/arquitectura/flujo-recepcion-calidad-compras.md.
        resp = self._recibir(self.usuario, self.almacen, "5")
        self.assertEqual(resp.status_code, 200, resp.content)
        recepcion = Recepcion.objects.get(pk=resp.json()["recepcion"]["id"])
        self.assertEqual((recepcion.almacen_id, recepcion.empresa_id, recepcion.sucursal_id),
                         (self.almacen.pk, self.empresa.pk, self.suc_1.pk))
        self.assertEqual(recepcion.estatus, Recepcion.EstatusRecepcion.EN_CALIDAD)
        detalle = RecepcionDetalle.objects.get(recepcion=recepcion)
        self.assertEqual((detalle.producto_id, detalle.cantidad_recibida), (self.producto.pk, Decimal("5")))
        self.assertFalse(Existencia.objects.filter(almacen=self.almacen, producto=self.producto).exists())
        self.assertFalse(MovimientoInventario.objects.filter(recepcion=recepcion).exists())

    # --- serie de folios (#1291, #1292) ----------------------------------------

    def test_serie_elegida_que_la_sucursal_no_tiene_es_400(self):
        # Antes se cambiaba sin avisar por la RC de la sucursal (#1292).
        antes = self._huella()
        resp = self._recibir(self.usuario, self.almacen, serie_codigo="RT")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"serie_codigo": ["La serie RT no está activa en la sucursal acme-ra 1."]})
        self.assertEqual(self._huella(), antes)

    def test_sin_serie_usa_la_de_la_sucursal(self):
        resp = self._recibir(self.usuario, self.almacen, serie_codigo="")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["recepcion"]["folio"].startswith("RC"))

    def test_series_recepcion_sin_tope_y_por_sucursal(self):
        # Más de las 50 que cortaba el GET (#1291): RC/RT/RZ en 20 sucursales (la serie
        # es única por sucursal y tipo, ``uq_serie_folio_sucursal_tipo_serie``).
        for i in range(20):
            sucursal = Sucursal.objects.create(empresa=self.empresa, codigo=f"S{i:02d}", nombre=f"acme-ra S{i:02d}")
            for serie in ("RC", "RT", "RZ"):
                SerieFolio.objects.create(
                    empresa=self.empresa, sucursal=sucursal, tipo_documento="RECEPCION", serie=serie)
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        series = client.get(RECEPCION_ONBOARDING_URL).json()["catalogos"]["series_recepcion"]
        self.assertEqual(len(series), 61)
        series = client.get(RECEPCION_ONBOARDING_URL, {"sucursal_id": self.suc_1.pk}).json()["catalogos"]["series_recepcion"]
        self.assertEqual([s["sucursal_id"] for s in series], [self.suc_1.pk])

    # --- permiso de WMS (#1295) -------------------------------------------------

    def test_sin_permiso_de_wms_es_403(self):
        sin_permiso = Usuario.objects.create(username="w@acme-ra.test", email="w@acme-ra.test", empresa=self.empresa)
        client = APIClient()
        client.force_authenticate(user=sin_permiso)
        self.assertEqual(client.get(RECEPCION_ONBOARDING_URL).status_code, 403)
        antes = self._huella()
        self.assertEqual(self._recibir(sin_permiso, self.almacen).status_code, 403)
        self.assertEqual(self._huella(), antes)

    def test_con_permiso_de_wms_recibe(self):
        from seguridad.models import Permiso, UsuarioPermiso
        con_permiso = Usuario.objects.create(username="c@acme-ra.test", email="c@acme-ra.test", empresa=self.empresa)
        for clave in ("R-WMS-RECEPCIONES", "C-WMS-RECEPCIONES"):
            UsuarioPermiso.objects.create(
                usuario=con_permiso, permiso=Permiso.objects.create(clave=clave, nombre=clave),
                tipo=UsuarioPermiso.TIPO_GRANT,
            )
        client = APIClient()
        client.force_authenticate(user=con_permiso)
        self.assertEqual(client.get(RECEPCION_ONBOARDING_URL).status_code, 200)
        self.assertEqual(self._recibir(con_permiso, self.almacen).status_code, 200)


class RecepcionOnboardingRenglonesTests(TestCase):
    """EC-894: ``POST /recepciones/onboarding/`` exige renglones con cantidad > 0.

    Con ``detalle: []`` se creaba la recepción En calidad sin renglones, se
    consumía el folio y la OC pasaba a Parcialmente recibida sin nada recibido.
    Todo rechazo es 400 sin escrituras: ni recepción, ni folio, ni estatus de
    la OC/OP. Se cubren ambos orígenes (OC y OP).
    """

    MSG_SIN_RENGLONES = "La recepción debe tener al menos un renglón con cantidad recibida."

    def _ref(self, origen, renglon, etiqueta=True):
        """``#N (campo pk[, etiqueta])`` como lo arma ``_ref_renglon`` (#1188): el segundo
        renglón del payload (``renglon=2``) es el ``*_detalle_2``."""
        segundo = renglon == 2
        if origen == "oc":
            detalle = self.oc_detalle_2 if segundo else self.oc_detalle
            campo, nombre = "orden_compra_detalle", detalle.producto.nombre
        else:
            detalle = self.op_detalle_2 if segundo else self.op_detalle
            campo, nombre = "orden_produccion_detalle", f"SKU {detalle.producto_variante.sku}"
        return f"#{renglon} ({campo} {detalle.pk}" + (f", {nombre})" if etiqueta else ")")

    def _msg_cantidad(self, origen, renglon):
        # El serializer valida antes y no conoce el producto: solo la referencia.
        return f"El renglón {self._ref(origen, renglon, etiqueta=False)} debe tener cantidad_recibida > 0."

    def _msg_excede(self, origen, renglon):
        return f"El renglón {self._ref(origen, renglon)} excede la cantidad pendiente por recibir."

    @classmethod
    def setUpTestData(cls):
        from catalogo.models import Talla
        from nucleo.models import UnidadMedida
        from produccion.models import ListaMaterialBom, OrdenProduccion, OrdenProduccionDetalle

        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General de Ley")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Pago en una sola exhibición")

        cls.empresa = Empresa.objects.create(codigo="ec894", razon_social="EC894 SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="E94", nombre="EC894 1")
        cls.serie = SerieFolio.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, tipo_documento="RECEPCION", serie="RC",
        )
        cls.almacen = Almacen.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, codigo="A94", nombre="A94")
        cls.usuario = Usuario.objects.create(username="u@ec894.test", email="u@ec894.test", empresa=cls.empresa)
        _conceder(cls.usuario, "R-WMS-RECEPCIONES", "C-WMS-RECEPCIONES")  # #1295, #1412
        proveedor = Proveedor.objects.create(
            empresa=cls.empresa, nombre="Proveedor", moneda=moneda, sat_regimen_fiscal=regimen,
            sat_forma_pago=forma, sat_metodo_pago=metodo, codigo="PROV-894", razon_social="Proveedor SA",
            telefono="8100000000", contacto_principal="Contacto", rfc="XAXX010101000", email="p@ec894.test",
        )
        tipo = _tipo_producto()

        # Origen OC: renglones de 100 y de 5.
        cls.oc = OrdenCompra.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, proveedor=proveedor, moneda=moneda, usuario=cls.usuario,
            fecha_oc=timezone.now().date(), estatus=OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
        )
        cls.producto = Producto.objects.create(tipo=tipo, empresa=cls.empresa, nombre="Tela 894")
        cls.oc_detalle = OrdenCompraDetalle.objects.create(
            orden_compra=cls.oc, producto=cls.producto, sucursal=cls.sucursal, cantidad=100,
        )
        cls.oc_detalle_2 = OrdenCompraDetalle.objects.create(
            orden_compra=cls.oc, producto=Producto.objects.create(tipo=tipo, empresa=cls.empresa, nombre="Hilo 894"),
            sucursal=cls.sucursal, cantidad=5,
        )

        # Origen OP: renglones de 10 y de 3 de producto terminado.
        color = Color.objects.create(nombre="Negro 894", codigo="N94", codigo_hex="#000000")
        playera = Producto.objects.create(tipo=tipo, empresa=cls.empresa, nombre="Playera 894")
        unidad = UnidadMedida.objects.create(clave="PZA-894", nombre="Pieza")
        cls.op = OrdenProduccion.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-894-1")
        renglones_op = []
        for talla, cantidad in (("M-894", 10), ("G-894", 3)):
            variante = ProductoVariante.objects.create(
                producto=playera, empresa=cls.empresa, color=color,
                talla=Talla.objects.create(nombre=talla), sku=f"PLA-894-{talla}", precio_base="1",
            )
            renglones_op.append(OrdenProduccionDetalle.objects.create(
                op=cls.op, cantidad=cantidad, producto_variante=variante, unidad=unidad,
                bom=ListaMaterialBom.objects.create(empresa=cls.empresa, producto_variante=variante, activo=True),
            ))
        cls.op_detalle, cls.op_detalle_2 = renglones_op

    # --- helpers --------------------------------------------------------------

    def _post(self, origen, detalle):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        if origen == "oc":
            encabezado = {"orden_compra": self.oc.pk}
        else:
            encabezado = {"orden_produccion": self.op.pk}
        encabezado.update({"almacen": self.almacen.pk, "serie_codigo": "RC"})
        return client.post(
            RECEPCION_ONBOARDING_URL, {"recepcion": encabezado, "detalle": detalle}, format="json",
        )

    def _renglon(self, origen, cantidad, segundo=False):
        if origen == "oc":
            detalle = self.oc_detalle_2 if segundo else self.oc_detalle
            return {"orden_compra_detalle": detalle.pk, "cantidad_recibida": cantidad}
        detalle = self.op_detalle_2 if segundo else self.op_detalle
        return {"orden_produccion_detalle": detalle.pk, "cantidad_recibida": cantidad}

    def _huella(self):
        self.oc.refresh_from_db()
        self.op.refresh_from_db()
        self.serie.refresh_from_db()
        return (
            Recepcion.objects.count(),
            RecepcionDetalle.objects.count(),
            self.serie.folio_actual,
            self.oc.estatus,
            self.op.estatus_op,
        )

    def _assert_400_sin_escrituras(self, origen, detalle, cuerpo):
        antes = self._huella()
        resp = self._post(origen, detalle)
        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertEqual(body, cuerpo)
        # El frontend (useCreateReceipt.ts) toma ``Object.values(body).flat()[0]``
        # como mensaje: tiene que salir un string, nunca un dict anidado.
        primero = [m for v in body.values() for m in (v if isinstance(v, list) else [v])][0]
        self.assertIsInstance(primero, str)
        self.assertEqual(self._huella(), antes)

    # --- detalle vacío ----------------------------------------------------------

    def test_detalle_vacio_es_400_sin_escrituras(self):
        for origen in ("oc", "op"):
            with self.subTest(origen=origen):
                self._assert_400_sin_escrituras(origen, [], {"detalle": [self.MSG_SIN_RENGLONES]})

    # --- cantidad no positiva ---------------------------------------------------

    def test_cantidad_cero_o_negativa_es_400_sin_escrituras(self):
        for origen in ("oc", "op"):
            for cantidad in ("0", "-1", "0.0000"):
                with self.subTest(origen=origen, cantidad=cantidad):
                    self._assert_400_sin_escrituras(
                        origen, [self._renglon(origen, cantidad)], {"detalle": [self._msg_cantidad(origen, 1)]},
                    )

    def test_segundo_renglon_en_cero_es_400_sin_escrituras(self):
        for origen in ("oc", "op"):
            with self.subTest(origen=origen):
                self._assert_400_sin_escrituras(
                    origen,
                    [self._renglon(origen, "1"), self._renglon(origen, "0", segundo=True)],
                    {"detalle": [self._msg_cantidad(origen, 2)]},
                )

    def test_cantidad_minima_representable_se_acepta(self):
        resp = self._post("oc", [self._renglon("oc", "0.0001")])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(RecepcionDetalle.objects.get().cantidad_recibida, Decimal("0.0001"))

    # --- excede lo pendiente ------------------------------------------------------

    def test_cantidad_mayor_a_lo_pendiente_es_400_sin_escrituras(self):
        casos = {"oc": "100.0001", "op": "11"}
        for origen, cantidad in casos.items():
            with self.subTest(origen=origen):
                self._assert_400_sin_escrituras(
                    origen, [self._renglon(origen, cantidad)], {"detalle": [self._msg_excede(origen, 1)]},
                )

    def test_segundo_renglon_excede_lo_pendiente_es_400_sin_escrituras(self):
        # El primer renglón es válido; el segundo pide más de su pendiente (5 en
        # la OC, 3 en la OP). No debe quedar escrito ni el primero.
        casos = {"oc": "6", "op": "4"}
        for origen, cantidad in casos.items():
            with self.subTest(origen=origen):
                self._assert_400_sin_escrituras(
                    origen,
                    [self._renglon(origen, "1"), self._renglon(origen, cantidad, segundo=True)],
                    {"detalle": [self._msg_excede(origen, 2)]},
                )

    # --- regresión: recepción válida ----------------------------------------------

    def test_recepcion_valida_de_oc_pasa_a_calidad(self):
        folio_antes = self.serie.folio_actual
        resp = self._post("oc", [self._renglon("oc", "40")])
        self.assertEqual(resp.status_code, 200, resp.content)
        recepcion = Recepcion.objects.get(pk=resp.json()["recepcion"]["id"])
        self.assertEqual(recepcion.estatus, Recepcion.EstatusRecepcion.EN_CALIDAD)
        self.assertEqual(
            list(RecepcionDetalle.objects.filter(recepcion=recepcion).values_list("orden_compra_detalle_id", "cantidad_recibida")),
            [(self.oc_detalle.pk, Decimal("40"))],
        )
        self.serie.refresh_from_db()
        self.assertEqual(self.serie.folio_actual, folio_antes + 1)
        self.oc.refresh_from_db()
        self.assertEqual(self.oc.estatus, OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA)

    def test_recepcion_valida_de_op_pasa_a_calidad(self):
        resp = self._post("op", [self._renglon("op", "4")])
        self.assertEqual(resp.status_code, 200, resp.content)
        recepcion = Recepcion.objects.get(pk=resp.json()["recepcion"]["id"])
        self.assertEqual(
            (recepcion.tipo_origen, recepcion.op_id, recepcion.estatus),
            (Recepcion.TipoOrigen.ORDEN_PRODUCCION, self.op.pk, Recepcion.EstatusRecepcion.EN_CALIDAD),
        )
        self.assertEqual(
            list(RecepcionDetalle.objects.filter(recepcion=recepcion).values_list("orden_produccion_detalle_id", "cantidad_recibida")),
            [(self.op_detalle.pk, Decimal("4"))],
        )

    # --- restricción en BD ----------------------------------------------------------

    def test_la_bd_rechaza_un_renglon_sin_cantidad_positiva(self):
        from django.db import IntegrityError, transaction

        recepcion = Recepcion.objects.create(
            tipo_origen="OC", orden_compra=self.oc, empresa=self.empresa, sucursal=self.sucursal,
            almacen=self.almacen, usuario=self.usuario, folio="RC-894-BD", fecha_recepcion=timezone.now(),
        )
        for cantidad in (Decimal("0"), Decimal("-1")):
            with self.subTest(cantidad=cantidad), self.assertRaises(IntegrityError), transaction.atomic():
                RecepcionDetalle.objects.create(
                    recepcion=recepcion, orden_compra_detalle=self.oc_detalle, producto=self.producto,
                    cantidad_recibida=cantidad,
                )


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
            producto = Producto.objects.create(tipo=_tipo_producto(), empresa=empresa, nombre=f"Insumo {codigo}")
            return {"empresa": empresa, "sucursal": sucursal, "moneda": moneda, "proveedor": proveedor, "producto": producto}

        cls.a = tenant("acme-oc")
        cls.b = tenant("globex-oc")
        cls.usuario = Usuario.objects.create(
            username="u@acme-oc.test", email="u@acme-oc.test", empresa=cls.a["empresa"],
            sucursal_default=cls.a["sucursal"],
        )
        # #1412: alta y edición exigen C-/E-COMPRAS-OC; aceptar, serie activa (#1142, #1264).
        _conceder(cls.usuario, "A-COMPRAS-OC", "C-COMPRAS-OC", "E-COMPRAS-OC")
        SerieFolio.objects.create(
            empresa=cls.a["empresa"], sucursal=cls.a["sucursal"], tipo_documento="ORDEN_COMPRA", serie="OC",
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

    def test_sin_moneda_la_oc_toma_la_del_proveedor(self):
        Proveedor.objects.filter(pk=self.a["proveedor"].pk).update(moneda=self.moneda_global)
        api = self._client(self.usuario)
        prov = next(
            p for p in api.get(f"{ORDENES_URL}onboarding/").json()["busqueda"]["proveedores"]
            if p["id"] == self.a["proveedor"].pk
        )
        self.assertEqual((prov["moneda_id"], prov["moneda__codigo_iso"]), (self.moneda_global.pk, "USD"))
        # #809 (379d5f0): fuera de MXN el tipo de cambio es obligatorio.
        cuerpo = {
            "orden_compra": {"proveedor": self.a["proveedor"].pk, "tipo_cambio": "17.50"},
            "detalle": [self._linea(self.a["producto"])],
        }
        resp = api.post(f"{ORDENES_URL}onboarding/", cuerpo, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(OrdenCompra.objects.latest("pk").moneda_id, self.moneda_global.pk)
        # Al editar, ``moneda: null`` conserva la guardada (#824).
        oc = OrdenCompra.objects.latest("pk")
        resp = api.post(f"{ORDENES_URL}onboarding/", {"orden_compra_id": oc.pk, "orden_compra": {"moneda": None}}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        oc.refresh_from_db()
        self.assertEqual(oc.moneda_id, self.moneda_global.pk)
        # Moneda privada de otra empresa en el proveedor: ni se ofrece ni se usa (#825).
        Proveedor.objects.filter(pk=self.a["proveedor"].pk).update(moneda=self.b["moneda"])
        prov = next(
            p for p in api.get(f"{ORDENES_URL}onboarding/").json()["busqueda"]["proveedores"]
            if p["id"] == self.a["proveedor"].pk
        )
        self.assertIsNone(prov["moneda_id"])
        resp = api.post(f"{ORDENES_URL}onboarding/", {"orden_compra": {"proveedor": self.a["proveedor"].pk, "tipo_cambio": "17.50"}, "detalle": [self._linea(self.a["producto"])]}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(OrdenCompra.objects.latest("pk").moneda_id, self.a["moneda"].pk)
        Proveedor.objects.filter(pk=self.a["proveedor"].pk).update(moneda=self.moneda_global)
        # La moneda elegida en el body manda sobre la del proveedor.
        cuerpo["orden_compra"]["moneda"] = self.a["moneda"].pk
        resp = api.post(f"{ORDENES_URL}onboarding/", cuerpo, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(OrdenCompra.objects.latest("pk").moneda_id, self.a["moneda"].pk)


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

    def test_onboarding_solo_sucursales_asignadas(self):
        otra = Sucursal.objects.create(empresa=self.a["empresa"], codigo="ZZZ", nombre="No asignada")
        get = self._client(self.usuario).get(f"{ORDENES_URL}onboarding/")
        self.assertEqual([s["id_sucursal"] for s in get.data["catalogos"]["sucursales"]], [self.a["sucursal"].pk])
        root = self._client(self.superuser).get(f"{ORDENES_URL}onboarding/")
        self.assertIn(otra.pk, [s["id_sucursal"] for s in root.data["catalogos"]["sucursales"]])
        base = {"sucursal": otra.pk, "proveedor": self.a["proveedor"].pk, "moneda": self.a["moneda"].pk}
        resp = self._client(self.usuario).post(
            f"{ORDENES_URL}onboarding/", {"orden_compra": base, "detalle": [self._linea(self.a["producto"])]}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("sucursal", resp.data)

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

    def test_aceptar_y_cancelar_sin_a_compras_oc_devuelven_403(self):
        # #1320: el front sólo muestra «Confirmar» y «Cancelar» con A-COMPRAS-OC.
        sin_permiso = Usuario.objects.create(
            username="s@acme-oc.test", email="s@acme-oc.test", empresa=self.a["empresa"],
            sucursal_default=self.a["sucursal"],
        )
        antes = self._huella()
        for accion, body in (("aceptar", {}), ("cancelar", {"motivo_cancelacion": "Sin stock"})):
            with self.subTest(accion=accion):
                resp = self._client(sin_permiso).post(f"{ORDENES_URL}{self.oc.pk}/{accion}/", body, format="json")

                self.assertEqual(resp.status_code, 403, resp.content)
                self.assertEqual(self._huella(), antes)

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
            resp = client.put(
                f"{ORDENES_URL}{self.oc.pk}/", {"orden_compra": {"moneda": moneda.pk, "tipo_cambio": "17.50"}}, format="json"
            )
            self.assertEqual(resp.status_code, 200, resp.content)
            self.oc.refresh_from_db()
            self.assertEqual(self.oc.moneda_id, moneda.pk)

    def test_crear_editar_y_aceptar_legitimos_siguen_funcionando(self):
        client = self._client(self.usuario)
        crea = client.post(
            f"{ORDENES_URL}onboarding/",
            {"orden_compra": {"proveedor": self.a["proveedor"].pk, "tipo_cambio": "17.50"},
             "detalle": [self._linea(self.a["producto"], 4)]},
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


Estatus = OrdenCompra.EstatusOrdenCompra


class OrdenCompraCancelacionTests(TestCase):
    """``POST ordenes/{id}/cancelar/``, la terminalidad de CANCELADA y el DELETE.

    Cancelar anula la OC ante el proveedor y la deja visible; el DELETE es la
    baja de una OC capturada por error. SQLite ignora ``select_for_update``: la
    relectura bajo el lock se ejerce con un ``get_object`` obsoleto simulado, no
    con dos transacciones reales.
    """

    MOTIVO_REQUERIDO = {"motivo_cancelacion": "El motivo de cancelación es requerido."}
    NO_CANCELABLE = {"estatus": "La orden ya no puede cancelarse."}

    @classmethod
    def setUpTestData(cls):
        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General de Ley")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Pago en una sola exhibición")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")

        def tenant(codigo):
            empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
            sucursal = Sucursal.objects.create(empresa=empresa, codigo=codigo[:3].upper(), nombre=codigo)
            proveedor = Proveedor.objects.create(
                empresa=empresa, nombre=f"Proveedor {codigo}", moneda=cls.moneda, sat_regimen_fiscal=regimen,
                sat_forma_pago=forma, sat_metodo_pago=metodo, codigo=f"PROV-{codigo}", razon_social="Prov SA",
                telefono="8100000000", contacto_principal="Contacto", rfc="XAXX010101000", email=f"p@{codigo}.test",
            )
            almacen = Almacen.objects.create(empresa=empresa, sucursal=sucursal, codigo="ALM", nombre="Almacen")
            producto = Producto.objects.create(tipo=_tipo_producto(), empresa=empresa, nombre=f"Insumo {codigo}")
            usuario = Usuario.objects.create(
                username=f"u@{codigo}.test", email=f"u@{codigo}.test", empresa=empresa, sucursal_default=sucursal,
            )
            # #1408: los códigos que hoy exigen las vistas (alta, edición, baja, autorizar/
            # cancelar la OC y recibirla).
            _conceder(usuario, "A-COMPRAS-OC", "C-COMPRAS-OC", "E-COMPRAS-OC", "D-COMPRAS-OC",
                      "R-WMS-RECEPCIONES", "C-WMS-RECEPCIONES")
            return {
                "empresa": empresa, "sucursal": sucursal, "proveedor": proveedor,
                "almacen": almacen, "producto": producto, "usuario": usuario,
            }

        cls.a = tenant("acme-cx")
        cls.b = tenant("globex-cx")
        SerieFolio.objects.create(
            empresa=cls.a["empresa"], sucursal=cls.a["sucursal"], tipo_documento="RECEPCION", serie="RC",
        )
        # #1408: el alta de OC exige serie activa en la sucursal.
        SerieFolio.objects.create(
            empresa=cls.a["empresa"], sucursal=cls.a["sucursal"], tipo_documento="ORDEN_COMPRA", serie="OC",
        )
        cls.sin_empresa = Usuario.objects.create(username="n@nowhere-cx.test", email="n@nowhere-cx.test")

    # --- helpers ---------------------------------------------------------------

    def _client(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.a["usuario"])
        return client

    def _oc(self, estatus, motivo=None):
        oc = OrdenCompra.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"], proveedor=self.a["proveedor"],
            moneda=self.moneda, usuario=self.a["usuario"], fecha_oc=timezone.now().date(),
            estatus=estatus, motivo_cancelacion=motivo,
        )
        OrdenCompraDetalle.objects.create(
            orden_compra=oc, producto=self.a["producto"], sucursal=self.a["sucursal"], cantidad=10,
        )
        return oc

    def _recepcion(self, oc, estatus=Recepcion.EstatusRecepcion.RECIBIDA, activo=True):
        return Recepcion.objects.create(
            orden_compra=oc, empresa=self.a["empresa"], sucursal=self.a["sucursal"],
            proveedor=self.a["proveedor"], almacen=self.a["almacen"], usuario=self.a["usuario"],
            folio=f"RC-CX-{Recepcion.objects.count() + 1}", fecha_recepcion=timezone.now(),
            estatus=estatus, activo=activo,
        )

    def _factura(self, oc, estatus, activo=True, recepcion=None):
        # ``FacturaProveedor.recepcion`` es NOT NULL; por omisión se cuelga de
        # una recepción cancelada para que solo la factura pueda bloquear.
        if recepcion is None:
            recepcion = self._recepcion(oc, estatus=Recepcion.EstatusRecepcion.CANCELADA)
        return FacturaProveedor.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"], proveedor=self.a["proveedor"],
            oc=oc, recepcion=recepcion, moneda=self.moneda, estatus=estatus, activo=activo,
        )

    def _cancelar(self, oc, motivo="Proveedor sin stock", user=None, **extra):
        body = {} if motivo is None else {"motivo_cancelacion": motivo}
        return self._client(user).post(f"{ORDENES_URL}{oc.pk}/cancelar/", body, format="json", **extra)

    def _estado(self, oc):
        oc = OrdenCompra.objects.get(pk=oc.pk)
        return (oc.estatus, oc.motivo_cancelacion, oc.activo)

    def _eventos(self, oc, accion):
        return AuditoriaEvento.objects.filter(
            modulo="compras", accion=accion, tabla="ordenes_compra", id_registro=str(oc.pk),
        )

    def _assert_rechazo(self, resp, esperado, oc, antes, status_code=400):
        self.assertEqual(resp.status_code, status_code, resp.content)
        if esperado is not None:
            self.assertEqual(resp.json(), esperado)
        self.assertEqual(self._estado(oc), antes)
        self.assertFalse(AuditoriaEvento.objects.filter(modulo="compras").exists())

    # --- cancelar una recepción (#1307) -----------------------------------------

    def _cancelar_recepcion(self, rc, motivo="Captura duplicada", user=None):
        return self._client(user).post(f"{RECEPCIONES_URL}{rc.pk}/cancelar/", {"motivo": motivo}, format="json")

    def test_cancelar_recepcion_en_calidad_reabre_la_oc_y_deja_cancelarla(self):
        admin = Usuario.objects.create(username="adm@acme-cx.test", email="adm@acme-cx.test",
                                       empresa=self.a["empresa"], is_admin_empresa=True)
        oc = self._oc(Estatus.PARCIALMENTE_RECIBIDA)
        rc = self._recepcion(oc, estatus=Recepcion.EstatusRecepcion.EN_CALIDAD)
        RecepcionDetalle.objects.create(recepcion=rc, orden_compra_detalle=oc.ordencompradetalle_set.get(),
                                        producto=self.a["producto"], cantidad_recibida=4)
        self.assertEqual(self._cancelar(oc).status_code, 400)  # con recepción viva
        r = self._cancelar_recepcion(rc, user=admin)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Recepcion.objects.get(pk=rc.pk).estatus, Recepcion.EstatusRecepcion.CANCELADA)
        self.assertEqual(OrdenCompra.objects.get(pk=oc.pk).estatus, Estatus.AUTORIZADA)
        self.assertEqual(self._cancelar(oc).status_code, 200)

    def test_cancelar_recepcion_despues_de_calidad_o_ajena_no_procede(self):
        admin = Usuario.objects.create(username="adm2@acme-cx.test", email="adm2@acme-cx.test",
                                       empresa=self.a["empresa"], is_admin_empresa=True)
        oc = self._oc(Estatus.RECIBIDA)
        for estatus in (Recepcion.EstatusRecepcion.CERRADA, Recepcion.EstatusRecepcion.RECIBIDA):
            with self.subTest(estatus=estatus):
                rc = self._recepcion(oc, estatus=estatus)
                r = self._cancelar_recepcion(rc, user=admin)
                self.assertEqual(r.status_code, 400, r.content)
                self.assertIn("estatus", r.json())
                self.assertEqual(Recepcion.objects.get(pk=rc.pk).estatus, estatus)
        rc = self._recepcion(oc, estatus=Recepcion.EstatusRecepcion.EN_CALIDAD)
        self._factura(oc, FacturaProveedor.FacturaProveedorStatus.BORRADOR, recepcion=rc)
        r = self._cancelar_recepcion(rc, user=admin)
        self.assertEqual(r.status_code, 400, r.content)  # factura de OC: no es de maquila
        self.assertIn("facturas_proveedores", r.json())
        ajeno = Usuario.objects.create(username="adm@globex-cx.test", email="adm@globex-cx.test",
                                       empresa=self.b["empresa"], is_admin_empresa=True)
        self.assertEqual(self._cancelar_recepcion(rc, user=ajeno).status_code, 404)
        self.assertFalse(AuditoriaEvento.objects.filter(tabla="recepciones").exists())

    # --- cancelar: caminos permitidos ------------------------------------------

    def test_cancela_desde_cada_estatus_permitido(self):
        for estatus in (Estatus.BORRADOR, Estatus.POR_AUTORIZAR, Estatus.AUTORIZADA):
            with self.subTest(estatus=estatus):
                oc = self._oc(estatus)
                resp = self._cancelar(oc, "  Proveedor sin stock  ")
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertEqual(self._estado(oc), (Estatus.CANCELADA, "Proveedor sin stock", True))
                body = resp.json()
                self.assertEqual((body["id"], body["estatus"]), (oc.pk, Estatus.CANCELADA))
                self.assertEqual(body["estatus_label"], "Cancelada")
                self.assertEqual(body["motivo_cancelacion"], "Proveedor sin stock")
                # Misma forma que el retrieve.
                for campo in ("detalles", "recepciones", "pedido_vinculado", "documentos"):
                    self.assertIn(campo, body)

    def test_cancelar_escribe_evento_de_auditoria(self):
        oc = self._oc(Estatus.AUTORIZADA)
        resp = self._cancelar(oc)
        self.assertEqual(resp.status_code, 200, resp.content)
        evento = self._eventos(oc, "CANCELAR").get()
        self.assertEqual((evento.empresa_id, evento.usuario_id), (self.a["empresa"].pk, self.a["usuario"].pk))
        self.assertEqual(evento.antes_json, {"estatus": Estatus.AUTORIZADA, "motivo_cancelacion": None})
        self.assertEqual(
            evento.despues_json, {"estatus": Estatus.CANCELADA, "motivo_cancelacion": "Proveedor sin stock"},
        )

    # ``AuditoriaEvento.ip`` es ``inet`` en Postgres; SQLite no reproduce el
    # fallo del cast, así que se afirma el valor guardado. ``IS_VERCEL`` hace que
    # ``get_client_ip`` lea el ``X-Forwarded-For``, como en producción.
    @override_settings(IS_VERCEL=True)
    def test_auditoria_guarda_solo_ips_validas(self):
        casos = (
            ({"HTTP_X_FORWARDED_FOR": "1.2.3.4, 10.0.0.1"}, "1.2.3.4"),
            ({"HTTP_X_FORWARDED_FOR": "foo"}, None),
            ({"REMOTE_ADDR": ""}, None),
        )
        for extra, esperado in casos:
            with self.subTest(extra=extra):
                oc = self._oc(Estatus.AUTORIZADA)
                self.assertEqual(self._cancelar(oc, **extra).status_code, 200)
                self.assertEqual(self._eventos(oc, "CANCELAR").get().ip, esperado)

                oc = self._oc(Estatus.BORRADOR)
                self.assertEqual(self._client().delete(f"{ORDENES_URL}{oc.pk}/", **extra).status_code, 204)
                self.assertEqual(self._eventos(oc, "DELETE").get().ip, esperado)

    # ``test_auditoria_de_la_recepcion_guarda_solo_ips_validas`` se quitó de
    # aquí: recepciones/onboarding/ ya no crea AuditoriaEvento (ni toca
    # Existencia) — eso vive ahora en CalidadInspeccionViewSet, que reusa la
    # misma validación de IP (``_ip_auditoria``) pero no tiene cobertura
    # propia todavía. Gap conocido, no se inventó un fixture de Empleado sin
    # poder correrlo.

    def test_oc_cancelada_sigue_visible_en_list_y_detail(self):
        oc = self._oc(Estatus.AUTORIZADA)
        self.assertEqual(self._cancelar(oc).status_code, 200)
        client = self._client()

        listado = client.get(ORDENES_URL)
        self.assertEqual(listado.status_code, 200)
        fila = next(row for row in listado.json() if row["id"] == oc.pk)
        self.assertEqual((fila["estatus"], fila["motivo_cancelacion"], fila["activo"]),
                         (Estatus.CANCELADA, "Proveedor sin stock", True))

        detalle = client.get(f"{ORDENES_URL}{oc.pk}/")
        self.assertEqual(detalle.status_code, 200)
        self.assertEqual(detalle.json()["motivo_cancelacion"], "Proveedor sin stock")

    def test_recepcion_cancelada_o_dada_de_baja_no_bloquea(self):
        oc = self._oc(Estatus.AUTORIZADA)
        self._recepcion(oc, estatus=Recepcion.EstatusRecepcion.CANCELADA)
        self._recepcion(oc, activo=False)
        self.assertEqual(self._cancelar(oc).status_code, 200)

    def test_factura_cancelada_no_bloquea(self):
        oc = self._oc(Estatus.AUTORIZADA)
        self._factura(oc, FacturaProveedor.FacturaProveedorStatus.CANCELADA)
        self.assertEqual(self._cancelar(oc).status_code, 200)

    # --- cancelar: rechazos ------------------------------------------------------

    def test_rechaza_estatus_4_5_y_6(self):
        for estatus in (Estatus.PARCIALMENTE_RECIBIDA, Estatus.RECIBIDA, Estatus.CANCELADA):
            with self.subTest(estatus=estatus):
                # Re-cancelar no pisa el motivo original.
                oc = self._oc(estatus, motivo="original" if estatus == Estatus.CANCELADA else None)
                antes = self._estado(oc)
                self._assert_rechazo(self._cancelar(oc), self.NO_CANCELABLE, oc, antes)

    def test_rechaza_con_recepcion_activa_aunque_el_estatus_lo_permita(self):
        for estatus in (Estatus.AUTORIZADA, Estatus.POR_AUTORIZAR):
            with self.subTest(estatus=estatus):
                oc = self._oc(estatus)
                self._recepcion(oc, estatus=Recepcion.EstatusRecepcion.BORRADOR)
                antes = self._estado(oc)
                resp = self._cancelar(oc)
                self._assert_rechazo(
                    resp, {"recepciones": "La orden tiene recepciones registradas y no puede cancelarse."}, oc, antes,
                )

    def test_rechaza_con_factura_viva_aunque_el_estatus_lo_permita(self):
        casos = (
            (FacturaProveedor.FacturaProveedorStatus.BORRADOR, True),
            (FacturaProveedor.FacturaProveedorStatus.REGISTRADA, True),
            # Dar de baja la factura no cancela la CxP que generó al registrarse.
            (FacturaProveedor.FacturaProveedorStatus.REGISTRADA, False),
        )
        for estatus_factura, activo in casos:
            with self.subTest(estatus_factura=estatus_factura, activo=activo):
                oc = self._oc(Estatus.AUTORIZADA)
                self._factura(oc, estatus_factura, activo=activo)
                antes = self._estado(oc)
                resp = self._cancelar(oc)
                self._assert_rechazo(
                    resp,
                    {"facturas_proveedores": "La orden tiene facturas de proveedor sin cancelar y no puede cancelarse."},
                    oc,
                    antes,
                )

    def test_motivo_faltante_o_vacio_es_400(self):
        oc = self._oc(Estatus.AUTORIZADA)
        antes = self._estado(oc)
        for motivo in (None, "", "   ", 123):
            with self.subTest(motivo=motivo):
                self._assert_rechazo(self._cancelar(oc, motivo), self.MOTIVO_REQUERIDO, oc, antes)

    def test_oc_de_otra_empresa_o_usuario_sin_empresa_es_404(self):
        oc = self._oc(Estatus.AUTORIZADA)
        antes = self._estado(oc)
        for user in (self.b["usuario"], self.sin_empresa):
            with self.subTest(user=user.email):
                self._assert_rechazo(self._cancelar(oc, user=user), None, oc, antes, status_code=404)

    def test_oc_dada_de_baja_es_404(self):
        oc = self._oc(Estatus.POR_AUTORIZAR)
        OrdenCompra.objects.filter(pk=oc.pk).update(activo=False)
        self._assert_rechazo(self._cancelar(oc), None, oc, self._estado(oc), status_code=404)

    def test_cancelar_relee_el_estatus_bajo_el_lock(self):
        # ``get_object`` devuelve la OC como estaba antes de que una recepción
        # concurrente la pasara a RECIBIDA; la decisión debe usar la fila actual.
        oc = self._oc(Estatus.AUTORIZADA)
        obsoleta = copy.copy(OrdenCompra.objects.get(pk=oc.pk))
        OrdenCompra.objects.filter(pk=oc.pk).update(estatus=Estatus.RECIBIDA)
        antes = self._estado(oc)
        with mock.patch.object(OrdenCompraViewSet, "get_object", return_value=obsoleta):
            resp = self._cancelar(oc)
        self._assert_rechazo(resp, self.NO_CANCELABLE, oc, antes)

    # --- CANCELADA es terminal ---------------------------------------------------

    def test_put_rechaza_oc_cancelada(self):
        oc = self._oc(Estatus.CANCELADA, motivo="original")
        antes = self._estado(oc)
        resp = self._client().put(f"{ORDENES_URL}{oc.pk}/", {"orden_compra": {"referencia": "R-1"}}, format="json")
        self._assert_rechazo(resp, {"estatus": "La orden está cancelada y no puede modificarse."}, oc, antes)

    def test_onboarding_edicion_rechaza_oc_cancelada(self):
        oc = self._oc(Estatus.CANCELADA, motivo="original")
        antes = self._estado(oc)
        resp = self._client().post(
            f"{ORDENES_URL}onboarding/", {"orden_compra_id": oc.pk, "orden_compra": {"referencia": "R-1"}},
            format="json",
        )
        self._assert_rechazo(resp, {"estatus": "La orden ya no puede editarse."}, oc, antes)

    def test_aceptar_rechaza_oc_cancelada(self):
        oc = self._oc(Estatus.CANCELADA, motivo="original")
        antes = self._estado(oc)
        resp = self._client().post(f"{ORDENES_URL}{oc.pk}/aceptar/", {}, format="json")
        self._assert_rechazo(resp, {"estatus": "La orden ya no puede aceptarse."}, oc, antes)

    def test_aceptar_relee_el_estatus_bajo_el_lock(self):
        # Antes el estatus se leía de ``get_object`` fuera del lock: una OC
        # cancelada entre esa lectura y el ``select_for_update`` se aceptaba.
        oc = self._oc(Estatus.POR_AUTORIZAR)
        obsoleta = copy.copy(OrdenCompra.objects.get(pk=oc.pk))
        OrdenCompra.objects.filter(pk=oc.pk).update(estatus=Estatus.CANCELADA, motivo_cancelacion="concurrente")
        antes = self._estado(oc)
        with mock.patch.object(OrdenCompraViewSet, "get_object", return_value=obsoleta):
            resp = self._client().post(f"{ORDENES_URL}{oc.pk}/aceptar/", {}, format="json")
        self._assert_rechazo(resp, {"estatus": "La orden ya no puede aceptarse."}, oc, antes)

    def test_aceptar_avisa_a_almacen_y_produccion(self):
        """#1317: la OC autorizada avisa a Almacén (WMS) al confirmar; con
        pre-orden, Producción recibe ``pre_orden_oc_autorizada`` (``AVISOS``)."""
        from produccion.services.pre_orden_service import AVISOS

        admin = Usuario.objects.create(username="acep@acme-cx.test", email="acep@acme-cx.test",
                                       empresa=self.a["empresa"], is_admin_empresa=True)
        oc = self._oc(Estatus.POR_AUTORIZAR)
        with mock.patch("notificaciones.services.notificacion_service.crear_notificacion_por_rol") as avisar:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self._client(admin).post(
                    f"{ORDENES_URL}{oc.pk}/aceptar/", {"proveedor": self.a["proveedor"].pk}, format="json",
                )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(avisar.call_count, 1)
        kwargs = avisar.call_args.kwargs
        self.assertEqual((kwargs["tipo"], kwargs["clave_departamento"]), ("oc_autorizada", "WMS"))
        self.assertEqual(kwargs["data"]["orden_compra_id"], oc.pk)
        self.assertIn("oc_autorizada", AVISOS)

    def test_aceptar_sin_renglones_sigue_rechazandose(self):
        oc = self._oc(Estatus.POR_AUTORIZAR)
        OrdenCompraDetalle.objects.filter(orden_compra=oc).delete()
        antes = self._estado(oc)
        resp = self._client().post(f"{ORDENES_URL}{oc.pk}/aceptar/", {}, format="json")
        self._assert_rechazo(resp, {"detalle": "Agrega al menos un producto antes de aceptar."}, oc, antes)

    def test_aceptar_sin_contabilidad_no_devuelve_importes(self):
        """#1332: ``aceptar/`` filtra importes por rol, como ``retrieve``/``cancelar``."""
        oc = self._oc(Estatus.POR_AUTORIZAR)
        OrdenCompra.objects.filter(pk=oc.pk).update(folio="OC-CX-1")
        resp = self._client().post(f"{ORDENES_URL}{oc.pk}/aceptar/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["orden_compra"]
        self.assertEqual(data["estatus"], Estatus.AUTORIZADA)
        for campo in ("subtotal", "impuestos", "total", "gran_total"):
            self.assertNotIn(campo, data)

    def test_recepcion_rechaza_oc_cancelada_y_no_la_ofrece(self):
        oc = self._oc(Estatus.CANCELADA, motivo="original")
        detalle = OrdenCompraDetalle.objects.get(orden_compra=oc)
        antes = self._estado(oc)
        client = self._client()
        resp = client.post(
            RECEPCION_ONBOARDING_URL,
            {
                "recepcion": {"orden_compra": oc.pk, "almacen": self.a["almacen"].pk, "serie_codigo": "RC"},
                "detalle": [{"orden_compra_detalle": detalle.pk, "cantidad_recibida": "1"}],
            },
            format="json",
        )
        # Forma de DRF, lista por llave (#1189, #1296).
        self._assert_rechazo(resp, {"estatus": ["La orden de compra no está disponible para recepción."]}, oc, antes)
        self.assertFalse(Recepcion.objects.filter(orden_compra=oc).exists())

        ofrecidas = client.get(RECEPCION_ONBOARDING_URL).json()["busqueda"]["ordenes_compra"]
        self.assertNotIn(oc.pk, [row["id"] for row in ofrecidas])

    def test_recepcion_ofrece_y_filtra_por_tipo_compra(self):
        mp = self._oc(Estatus.AUTORIZADA)
        pt = self._oc(Estatus.AUTORIZADA)
        OrdenCompra.objects.filter(pk=pt.pk).update(tipo_compra=OrdenCompra.TipoCompra.PRODUCTO_TERMINADO)
        client = self._client()

        def ofrecidas(**params):
            resp = client.get(RECEPCION_ONBOARDING_URL, params)
            self.assertEqual(resp.status_code, 200)
            return {r["id"]: r["tipo_compra"] for r in resp.json()["busqueda"]["ordenes_compra"]}

        self.assertEqual(ofrecidas(), {mp.pk: "MP", pt.pk: "PT"})
        self.assertEqual(ofrecidas(tipo_compra="PT"), {pt.pk: "PT"})
        self.assertEqual(ofrecidas(tipo_compra="mp"), {mp.pk: "MP"})
        self.assertEqual(client.get(RECEPCION_ONBOARDING_URL, {"tipo_compra": "XX"}).status_code, 400)

    def test_onboarding_get_descuenta_lo_rechazado_por_calidad_del_recibido(self):
        """#511: el GET calcula el pendiente con la misma regla que el POST."""
        oc = self._oc(Estatus.AUTORIZADA)
        linea = OrdenCompraDetalle.objects.get(orden_compra=oc)
        recepcion = self._recepcion(oc)
        rd = RecepcionDetalle.objects.create(
            recepcion=recepcion, orden_compra_detalle=linea, producto=self.a["producto"],
            cantidad_recibida=linea.cantidad,
        )
        empresa, sucursal = self.a["empresa"], self.a["sucursal"]
        departamento = Departamento.objects.create(empresa=empresa, sucursal=sucursal, codigo="CAL", nombre="Calidad")
        inspector = Empleado.objects.create(
            empresa=empresa, sucursal=sucursal, departamento=departamento,
            puesto=Puesto.objects.create(empresa=empresa, nombre="Inspector"),
            numero_empleado="EMP-9", nombre="Ana", apellido_paterno="Lopez", fecha_ingreso=timezone.localdate(),
        )
        inspeccion = CalidadInspeccion.objects.create(
            recepcion=recepcion, inspector=inspector, fecha=timezone.localdate(), estado="rechazada",
        )
        CalidadInspeccionDetalle.objects.create(
            calidad_inspeccion=inspeccion, recepcion_detalle=rd, cantidad_inspeccionada=linea.cantidad,
            cantidad_aprobada=linea.cantidad - 1, cantidad_rechazada=1, resultado="rechazo", motivo_rechazo="x",
        )

        resp = self._client().get(RECEPCION_ONBOARDING_URL)

        fila = next(r for r in resp.json()["busqueda"]["ordenes_compra"] if r["id"] == oc.pk)
        renglon = fila["detalle"][0]
        self.assertEqual(Decimal(renglon["cantidad_pendiente"]), Decimal(1))
        self.assertEqual(Decimal(renglon["cantidad_recibida"]), Decimal(linea.cantidad) - 1)

    def test_onboarding_get_orden_compra_id_trae_solo_esa_orden(self):
        a = self._oc(Estatus.AUTORIZADA)
        self._oc(Estatus.AUTORIZADA)
        client = self._client()

        resp = client.get(RECEPCION_ONBOARDING_URL, {"orden_compra_id": a.pk})

        self.assertEqual([r["id"] for r in resp.json()["busqueda"]["ordenes_compra"]], [a.pk])
        self.assertEqual(client.get(RECEPCION_ONBOARDING_URL, {"orden_compra_id": "x"}).status_code, 400)

    def test_tipo_compra_es_filtro_del_listado_y_no_del_detalle(self):
        oc = self._oc(Estatus.AUTORIZADA)
        recepcion = self._recepcion(oc)
        client = self._client()

        # Retrieve: el query param no cambia la respuesta.
        for valor in ("XX", "PT"):
            resp = client.get(f"{RECEPCIONES_URL}{recepcion.pk}/", {"tipo_compra": valor})
            self.assertEqual(resp.status_code, 200, resp.content)
        # Listado: filtra y valida.
        self.assertEqual(client.get(RECEPCIONES_URL, {"tipo_compra": "XX"}).status_code, 400)
        # Usuario sin empresa: 200 vacío aunque el valor sea inválido.
        sin_empresa = Usuario.objects.create(username="sin-emp", email="sin@emp.test")
        c = APIClient()
        c.force_authenticate(user=sin_empresa)
        resp = c.get(RECEPCIONES_URL, {"tipo_compra": "XX"})
        self.assertEqual(resp.status_code, 200, resp.content)

    # --- PUT con recepciones o facturas reales ------------------------------------

    def _put_reemplaza_renglones(self, oc):
        return self._client().put(
            f"{ORDENES_URL}{oc.pk}/",
            {"detalle": [{"producto": self.a["producto"].pk, "cantidad": 5, "precio": "1.00"}]},
            format="json",
        )

    def test_put_rechaza_oc_con_recepcion_aunque_el_estatus_lo_permita(self):
        # El estatus dice AUTORIZADA (p. ej. regresado desde el admin), pero la
        # recepción existe: reemplazar renglones borraría su detalle en cascada.
        oc = self._oc(Estatus.AUTORIZADA)
        linea = OrdenCompraDetalle.objects.get(orden_compra=oc)
        recepcion = self._recepcion(oc, estatus=Recepcion.EstatusRecepcion.CANCELADA)
        rd = RecepcionDetalle.objects.create(
            recepcion=recepcion, orden_compra_detalle=linea, producto=self.a["producto"], cantidad_recibida=2,
        )
        antes = self._estado(oc)
        resp = self._put_reemplaza_renglones(oc)
        self._assert_rechazo(
            resp, {"recepciones": "La orden tiene recepciones registradas y no puede modificarse."}, oc, antes,
        )
        self.assertTrue(OrdenCompraDetalle.objects.filter(pk=linea.pk).exists())
        self.assertTrue(RecepcionDetalle.objects.filter(pk=rd.pk).exists())

    def test_put_rechaza_oc_con_factura_aunque_el_estatus_lo_permita(self):
        # La factura se cuelga de la recepción de otra OC para que solo ella bloquee.
        otra = self._oc(Estatus.AUTORIZADA)
        oc = self._oc(Estatus.AUTORIZADA)
        linea = OrdenCompraDetalle.objects.get(orden_compra=oc)
        recepcion_otra = self._recepcion(otra)
        rd = RecepcionDetalle.objects.create(
            recepcion=recepcion_otra, orden_compra_detalle=OrdenCompraDetalle.objects.get(orden_compra=otra),
            producto=self.a["producto"], cantidad_recibida=1,
        )
        factura = self._factura(oc, FacturaProveedor.FacturaProveedorStatus.CANCELADA, recepcion=recepcion_otra)
        fd = FacturaProveedorDetalle.objects.create(
            factura_proveedor=factura, oc_detalle=linea, recepcion_detalle=rd, producto=self.a["producto"],
        )
        antes = self._estado(oc)
        resp = self._put_reemplaza_renglones(oc)
        self._assert_rechazo(
            resp, {"facturas_proveedores": "La orden tiene facturas de proveedor y no puede modificarse."}, oc, antes,
        )
        self.assertTrue(OrdenCompraDetalle.objects.filter(pk=linea.pk).exists())
        self.assertTrue(FacturaProveedorDetalle.objects.filter(pk=fd.pk).exists())

    # --- motivo_cancelacion no es escribible ---------------------------------------

    def test_motivo_no_se_escribe_por_put_ni_onboarding(self):
        client = self._client()
        oc = self._oc(Estatus.POR_AUTORIZAR)

        put = client.put(
            f"{ORDENES_URL}{oc.pk}/",
            {"orden_compra": {"referencia": "R-1", "motivo_cancelacion": "x"}, "motivo_cancelacion": "y"},
            format="json",
        )
        self.assertEqual(put.status_code, 200, put.content)
        edita = client.post(
            f"{ORDENES_URL}onboarding/",
            {"orden_compra_id": oc.pk, "orden_compra": {"referencia": "R-2", "motivo_cancelacion": "x"},
             "motivo_cancelacion": "y"},
            format="json",
        )
        self.assertEqual(edita.status_code, 200, edita.content)
        oc.refresh_from_db()
        self.assertEqual((oc.referencia, oc.motivo_cancelacion), ("R-2", None))

        crea = client.post(
            f"{ORDENES_URL}onboarding/",
            {"orden_compra": {"proveedor": self.a["proveedor"].pk, "motivo_cancelacion": "x"},
             "motivo_cancelacion": "y",
             "detalle": [{"producto": self.a["producto"].pk, "cantidad": 1, "precio": "1.00"}]},
            format="json",
        )
        self.assertEqual(crea.status_code, 200, crea.content)
        nueva = OrdenCompra.objects.get(pk=crea.json()["orden_compra"]["id"])
        self.assertIsNone(nueva.motivo_cancelacion)
        self.assertEqual(nueva.estatus, Estatus.POR_AUTORIZAR)

    # --- DELETE (baja por error de captura) ------------------------------------------

    def _eliminar(self, oc, user=None):
        return self._client(user).delete(f"{ORDENES_URL}{oc.pk}/")

    def test_delete_permitido_en_borrador_y_por_autorizar(self):
        for estatus in (Estatus.BORRADOR, Estatus.POR_AUTORIZAR):
            with self.subTest(estatus=estatus):
                oc = self._oc(estatus)
                resp = self._eliminar(oc)
                self.assertEqual(resp.status_code, 204, resp.content)
                self.assertEqual(self._estado(oc), (estatus, None, False))
                evento = self._eventos(oc, "DELETE").get()
                self.assertEqual(evento.usuario_id, self.a["usuario"].pk)
                self.assertEqual(evento.despues_json, {"estatus": estatus, "activo": False})

    def test_delete_actualiza_updated_at(self):
        oc = self._oc(Estatus.BORRADOR)
        antes = timezone.now() - timedelta(days=1)
        OrdenCompra.objects.filter(pk=oc.pk).update(updated_at=antes)
        self.assertEqual(self._eliminar(oc).status_code, 204)
        self.assertGreater(OrdenCompra.objects.get(pk=oc.pk).updated_at, antes)

    def test_delete_rechaza_otros_estatus(self):
        mensaje = {"estatus": "Solo se puede eliminar una orden en borrador o pendiente de confirmar."}
        for estatus in (Estatus.AUTORIZADA, Estatus.PARCIALMENTE_RECIBIDA, Estatus.RECIBIDA, Estatus.CANCELADA):
            with self.subTest(estatus=estatus):
                oc = self._oc(estatus)
                self._assert_rechazo(self._eliminar(oc), mensaje, oc, self._estado(oc))

    def test_delete_rechaza_con_cualquier_recepcion(self):
        # A diferencia de cancelar, aun una recepción cancelada o dada de baja
        # prueba que la OC existió.
        for kwargs in ({}, {"estatus": Recepcion.EstatusRecepcion.CANCELADA}, {"activo": False}):
            with self.subTest(**kwargs):
                oc = self._oc(Estatus.POR_AUTORIZAR)
                self._recepcion(oc, **kwargs)
                self._assert_rechazo(
                    self._eliminar(oc),
                    {"recepciones": "La orden tiene recepciones registradas y no puede eliminarse."},
                    oc,
                    self._estado(oc),
                )

    def test_delete_rechaza_con_cualquier_factura(self):
        # La factura exige una recepción (NOT NULL), que ya bloquearía por sí
        # sola; para ejercer esta guarda se cuelga de la recepción de otra OC.
        otra = self._oc(Estatus.AUTORIZADA)
        for estatus_factura in FacturaProveedor.FacturaProveedorStatus.values:
            with self.subTest(estatus_factura=estatus_factura):
                oc = self._oc(Estatus.POR_AUTORIZAR)
                self._factura(oc, estatus_factura, recepcion=self._recepcion(otra))
                self._assert_rechazo(
                    self._eliminar(oc),
                    {"facturas_proveedores": "La orden tiene facturas de proveedor y no puede eliminarse."},
                    oc,
                    self._estado(oc),
                )

    def test_delete_de_otra_empresa_es_404(self):
        oc = self._oc(Estatus.BORRADOR)
        self._assert_rechazo(
            self._eliminar(oc, user=self.b["usuario"]), {"detail": "Orden de compra no encontrada."},
            oc, self._estado(oc), status_code=404,
        )


class HistorialOCProveedorTests(TestCase):
    """EC-399 ``historial-ordenes-compra``: #311 (empresa), #312 (montos),
    #313 (validación y contrato), #314 (queries)."""

    @classmethod
    def setUpTestData(cls):
        from compras.models import OrdenCompra
        from nucleo.models import Moneda, SatFormaPago, SatMetodoPago, SatRegimenFiscal, Sucursal

        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Una exhibición")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.otra = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="MTY")
        cls.sucursal_otra = Sucursal.objects.create(empresa=cls.otra, codigo="GDL", nombre="GDL")

        def proveedor(codigo):
            return Proveedor.objects.create(
                empresa=cls.empresa, nombre=codigo, moneda=cls.moneda, sat_regimen_fiscal=regimen,
                sat_forma_pago=forma, sat_metodo_pago=metodo, codigo=codigo, razon_social=codigo,
                telefono="8100000000", contacto_principal="Ana", rfc="XAXX010101000", email=f"{codigo}@p.test",
            )

        cls.proveedor = proveedor("P1")
        cls.proveedor_vacio = proveedor("P2")
        cls.admin = Usuario.objects.create(username="admin", email="a@acme.test", empresa=cls.empresa, is_admin_empresa=True)
        cls.almacenista = Usuario.objects.create(username="alm", email="alm@acme.test", empresa=cls.empresa)
        cls.root = Usuario.objects.create(username="root", email="r@acme.test", empresa=cls.empresa, is_superuser=True)

        E = OrdenCompra.EstatusOrdenCompra
        cls.oc_autorizada = cls._oc(cls.empresa, cls.sucursal, E.AUTORIZADA, "100.50", "2026-03-10")
        cls.oc_cancelada = cls._oc(cls.empresa, cls.sucursal, E.CANCELADA, "50.00", "2026-03-11")
        # Par cruzado: OC de otra empresa con un proveedor de ACME (datos previos al candado de escritura).
        cls.oc_cruzada = cls._oc(cls.otra, cls.sucursal_otra, E.AUTORIZADA, "999.00", "2026-03-12")

    @classmethod
    def _oc(cls, empresa, sucursal, estatus, gran_total, fecha):
        from compras.models import OrdenCompra
        return OrdenCompra.objects.create(
            empresa=empresa, sucursal=sucursal, proveedor=cls.proveedor, moneda=cls.moneda,
            estatus=estatus, gran_total=gran_total, fecha_oc=fecha, usuario=cls.admin,
        )

    def _get(self, user, query="", proveedor=None):
        client = APIClient()
        client.force_authenticate(user=user)
        p = proveedor or self.proveedor
        return client.get(f"/api/v1/terceros/proveedores/{p.pk}/historial-ordenes-compra/{query}")

    def test_oc_de_otra_empresa_no_aparece(self):
        for user in (self.admin, self.root):
            with self.subTest(user=user.username):
                data = self._get(user).json()

                self.assertEqual(data["count"], 2)
                self.assertNotIn(self.oc_cruzada.pk, [r["id"] for r in data["results"]])
                self.assertEqual(data["resumen"]["total_ordenes"], 2)
                self.assertEqual(data["resumen"]["monto_por_moneda"], [{"moneda": "MXN", "total": "100.50"}])

    def test_por_estatus_usa_el_entero(self):
        data = self._get(self.admin).json()

        self.assertEqual(data["resumen"]["por_estatus"], {
            str(self.oc_autorizada.estatus): 1, str(self.oc_cancelada.estatus): 1,
        })

    def test_sin_permiso_de_contabilidad_no_ve_montos(self):
        historial = self._get(self.almacenista).json()
        client = APIClient()
        client.force_authenticate(user=self.almacenista)
        listado = client.get("/api/v1/compras/ordenes/").json()
        filas = listado["results"] if isinstance(listado, dict) else listado

        self.assertNotIn("gran_total", historial["results"][0])
        self.assertNotIn("monto_por_moneda", historial["resumen"])
        self.assertTrue(filas)
        self.assertNotIn("gran_total", filas[0])

    def test_con_permiso_de_contabilidad_ve_montos_en_el_listado(self):
        client = APIClient()
        client.force_authenticate(user=self.admin)
        listado = client.get("/api/v1/compras/ordenes/").json()
        filas = listado["results"] if isinstance(listado, dict) else listado

        self.assertIn("gran_total", filas[0])

    def test_parametros_invalidos_responden_400(self):
        for query in (
            "?fecha_inicio=2026-02-30", "?fecha_inicio=abc", "?fecha_final=2026-13-01",
            "?estatus=99", "?estatus=abc", "?fecha_inicio=2026-03-12&fecha_final=2026-03-10",
        ):
            with self.subTest(query=query):
                self.assertEqual(self._get(self.admin, query).status_code, 400)

    def test_proveedor_sin_oc(self):
        resp = self._get(self.admin, proveedor=self.proveedor_vacio)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 0)
        self.assertEqual(resp.json()["resumen"], {"total_ordenes": 0, "por_estatus": {}, "monto_por_moneda": []})

    def test_queries_no_crecen_con_las_filas(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries():
            with CaptureQueriesContext(connection) as ctx:
                self._get(self.admin)
            return len(ctx)

        antes = queries()
        for dia in range(1, 6):
            self._oc(self.empresa, self.sucursal, self.oc_autorizada.estatus, "1.00", f"2026-04-0{dia}")

        self.assertEqual(queries(), antes)

    def _ids_listado(self, user, query=""):
        client = APIClient()
        client.force_authenticate(user=user)
        data = client.get(f"/api/v1/compras/ordenes/{query}").json()
        return [r["id"] for r in (data["results"] if isinstance(data, dict) else data)]

    def test_sin_contabilidad_las_queries_no_crecen_con_las_filas(self):
        """#836: el permiso se evalúa una vez por petición, no por fila."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries():
            with CaptureQueriesContext(connection) as listado:
                self._ids_listado(self.almacenista)
            with CaptureQueriesContext(connection) as historial:
                self._get(self.almacenista)
            return len(listado), len(historial)

        antes = queries()
        for dia in range(1, 6):
            self._oc(self.empresa, self.sucursal, self.oc_autorizada.estatus, "1.00", f"2026-04-0{dia}")

        self.assertEqual(queries(), antes)

    def test_ordenar_por_importe_sin_contabilidad_se_ignora(self):
        """#837: ordenar por un importe oculto deja el orden por defecto."""
        por_defecto = self._ids_listado(self.almacenista)
        for query in ("?ordering=gran_total", "?ordering=-gran_total", "?ordering=subtotal"):
            with self.subTest(query=query):
                self.assertEqual(self._ids_listado(self.almacenista, query), por_defecto)
        # Quien sí ve importes conserva el ordenamiento.
        self.assertEqual(
            self._ids_listado(self.admin, "?ordering=gran_total"),
            [self.oc_cancelada.pk, self.oc_autorizada.pk],
        )
        self.assertEqual(
            self._ids_listado(self.almacenista, "?ordering=fecha_oc"),
            [self.oc_autorizada.pk, self.oc_cancelada.pk],
        )

    def test_search_filtra_el_listado(self):
        """#839: ``?search=`` ya no se ignora en OC ni en proveedores."""
        self.assertEqual(self._ids_listado(self.admin, "?search=zzz-no-existe"), [])
        self.assertEqual(
            sorted(self._ids_listado(self.admin, "?search=P1")),
            sorted([self.oc_autorizada.pk, self.oc_cancelada.pk]),
        )
        client = APIClient()
        client.force_authenticate(user=self.admin)
        data = client.get("/api/v1/terceros/proveedores/?search=P2").json()
        filas = data["results"] if isinstance(data, dict) else data
        self.assertEqual([f["id"] for f in filas], [self.proveedor_vacio.pk])

    def test_paginacion_opcional_filtros_y_desempate(self):
        """#863: ``?limit=`` pagina con ``count``; sin él, lista completa."""
        client = APIClient()
        client.force_authenticate(user=self.admin)
        pagina = client.get("/api/v1/compras/ordenes/?limit=1&ordering=fecha_oc").json()

        self.assertEqual(pagina["count"], 2)
        self.assertEqual([r["id"] for r in pagina["results"]], [self.oc_autorizada.pk])
        self.assertIsNotNone(pagina["next"])
        self.assertIsInstance(client.get("/api/v1/compras/ordenes/").json(), list)
        self.assertEqual(
            self._ids_listado(self.admin, f"?estatus={self.oc_cancelada.estatus}"), [self.oc_cancelada.pk],
        )
        self.assertEqual(client.get("/api/v1/compras/ordenes/?estatus=99").status_code, 400)
        self.assertEqual(client.get("/api/v1/compras/ordenes/?proveedor=x").status_code, 400)

    def test_limit_y_offset_invalidos_responden_400(self):
        """#950: un ``?limit=`` que no es entero positivo ya no devuelve la lista
        completa sin paginar; ``offset`` negativo o no entero, tampoco se ignora."""
        client = APIClient()
        client.force_authenticate(user=self.admin)
        for params in ("limit=abc", "limit=0", "limit=-1", "limit=1.5", "limit=1&offset=-1", "limit=1&offset=x"):
            with self.subTest(params=params):
                resp = client.get(f"/api/v1/compras/ordenes/?{params}")
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertEqual(set(resp.json()), {params.split("&")[-1].split("=")[0]})
        # Vacío cuenta como ausente: lista completa; ``limit`` sobre el tope se recorta.
        self.assertIsInstance(client.get("/api/v1/compras/ordenes/?limit=").json(), list)
        self.assertEqual(client.get("/api/v1/compras/ordenes/?limit=9999").json()["count"], 2)
        self.assertEqual(client.get("/api/v1/compras/ordenes/?limit=1&offset=").json()["count"], 2)

    def test_onboarding_avisa_si_la_lista_se_recorto(self):
        """#838: ``*_truncado`` indica si faltan elementos por el tope."""
        client = APIClient()
        client.force_authenticate(user=self.admin)
        recortada = client.get("/api/v1/compras/ordenes/onboarding/?proveedor_limit=1").json()["busqueda"]
        completa = client.get("/api/v1/compras/ordenes/onboarding/?proveedor_limit=2").json()["busqueda"]

        self.assertEqual(len(recortada["proveedores"]), 1)
        self.assertTrue(recortada["proveedores_truncado"])
        self.assertEqual(len(completa["proveedores"]), 2)
        self.assertFalse(completa["proveedores_truncado"])
        self.assertFalse(completa["variantes_truncado"])


class OrdenCompraPutPlanoYFechaLocalTests(TestCase):
    """#329: PUT con encabezado plano valida igual que el anidado (400, no 500).
    #330: ``fecha_oc`` y ``hoy`` usan el día local, no el de UTC."""

    @classmethod
    def setUpTestData(cls):
        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Una exhibición")
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="MTY")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.proveedor = Proveedor.objects.create(
            empresa=cls.empresa, nombre="P1", moneda=cls.moneda, sat_regimen_fiscal=regimen,
            sat_forma_pago=forma, sat_metodo_pago=metodo, codigo="P1", razon_social="P1",
            telefono="8100000000", contacto_principal="Ana", rfc="XAXX010101000", email="p1@p.test",
        )
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Tela")
        cls.usuario = Usuario.objects.create(
            username="compras", email="c@acme.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )

    def setUp(self):
        self.client_api = APIClient()
        self.client_api.force_authenticate(user=self.usuario)
        self.oc = OrdenCompra.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, proveedor=self.proveedor, moneda=self.moneda,
            usuario=self.usuario, fecha_oc="2026-10-01", estatus=OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR,
        )

    def _put(self, body):
        body = {"proveedor": self.proveedor.pk, "detalles": [{"producto": self.producto.pk, "cantidad": 1, "precio": 10}], **body}
        return self.client_api.put(f"{ORDENES_URL}{self.oc.pk}/", body, format="json")

    def test_put_plano_con_valores_invalidos_responde_400(self):
        for campo, valor in (("fecha_vencimiento", "no-es-fecha"), ("porcentaje_iva", "abc")):
            with self.subTest(campo=campo):
                resp = self._put({campo: valor})

                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertIn(campo, resp.json())

    def test_put_plano_con_fecha_vacia_no_responde_500(self):
        self.assertIn(self._put({"fecha_vencimiento": ""}).status_code, (200, 400))

    def test_put_plano_valido_aplica_los_valores(self):
        resp = self._put({"fecha_vencimiento": "2026-12-01", "porcentaje_iva": "8.00"})

        self.assertEqual(resp.status_code, 200, resp.content)
        self.oc.refresh_from_db()
        self.assertEqual(str(self.oc.fecha_vencimiento), "2026-12-01")
        self.assertEqual(self.oc.porcentaje_iva, Decimal("8.00"))

    def test_fecha_oc_usa_el_dia_local(self):
        # 2026-10-08 01:30 UTC = 2026-10-07 19:30 en Ciudad de México.
        noche_local = timezone.datetime(2026, 10, 8, 1, 30, tzinfo=timezone.UTC)
        with mock.patch("django.utils.timezone.now", return_value=noche_local):
            resp = self.client_api.post(
                f"{ORDENES_URL}onboarding/",
                {"orden_compra": {"proveedor": self.proveedor.pk},
                 "detalle": [{"producto": self.producto.pk, "cantidad": 1, "precio": "10.00"}]},
                format="json",
            )

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["orden_compra"]["fecha_oc"], "2026-10-07")


class RecepcionKpisTests(TestCase):
    """``GET /recepciones/kpis/`` (EC-434): los 4 KPIs son reales."""

    URL = "/api/v1/compras/recepciones/kpis/"

    @classmethod
    def setUpTestData(cls):
        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General de Ley")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Pago en una sola exhibición")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.otra = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="MTY")
        cls.proveedor = Proveedor.objects.create(
            empresa=cls.empresa, nombre="Proveedor Test", moneda=cls.moneda, sat_regimen_fiscal=regimen,
            sat_forma_pago=forma, sat_metodo_pago=metodo, codigo="PROV-1", razon_social="Prov SA",
            telefono="8100000000", contacto_principal="Contacto", rfc="XAXX010101000", email="p@acme.test",
        )
        cls.almacen = Almacen.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, codigo="ALM", nombre="Almacen")
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Insumo Test")
        # Ve montos (admin de empresa): estos tests fijan los cálculos.
        cls.usuario = Usuario.objects.create(
            username="u@acme.test", email="u@acme.test", empresa=cls.empresa, sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )
        cls.sin_empresa = Usuario.objects.create(username="se", email="se@nowhere.test")

        departamento = Departamento.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, codigo="CAL", nombre="Calidad",
        )
        puesto = Puesto.objects.create(empresa=cls.empresa, nombre="Inspector")
        cls.inspector = Empleado.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, departamento=departamento, puesto=puesto,
            numero_empleado="EMP-1", nombre="Juan", apellido_paterno="Perez",
            fecha_ingreso=timezone.localdate(),
        )

        # OC #1: AUTORIZADA, ordenó 10 @ $5.00, recibió 6 -> parcial, cumplimiento 60%.
        cls.oc1 = OrdenCompra.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, proveedor=cls.proveedor, moneda=cls.moneda,
            usuario=cls.usuario, fecha_oc=timezone.now().date(),
            estatus=OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
        )
        cls.oc1_det = OrdenCompraDetalle.objects.create(
            orden_compra=cls.oc1, producto=cls.producto, sucursal=cls.sucursal, cantidad=10, precio=Decimal("5.00"),
        )
        cls.rec1 = Recepcion.objects.create(
            orden_compra=cls.oc1, empresa=cls.empresa, sucursal=cls.sucursal, proveedor=cls.proveedor,
            almacen=cls.almacen, usuario=cls.usuario, folio="RC-1", fecha_recepcion=timezone.now(),
            estatus=Recepcion.EstatusRecepcion.RECIBIDA,
        )
        cls.rec1_det = RecepcionDetalle.objects.create(
            recepcion=cls.rec1, orden_compra_detalle=cls.oc1_det, producto=cls.producto, cantidad_recibida=6,
        )

        # OC #2: RECIBIDA completa, 5 @ $10.00, recibido 5 -> cumplimiento 100%.
        cls.oc2 = OrdenCompra.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, proveedor=cls.proveedor, moneda=cls.moneda,
            usuario=cls.usuario, fecha_oc=timezone.now().date(),
            estatus=OrdenCompra.EstatusOrdenCompra.RECIBIDA,
        )
        oc2_det = OrdenCompraDetalle.objects.create(
            orden_compra=cls.oc2, producto=cls.producto, sucursal=cls.sucursal, cantidad=5, precio=Decimal("10.00"),
        )
        rec2 = Recepcion.objects.create(
            orden_compra=cls.oc2, empresa=cls.empresa, sucursal=cls.sucursal, proveedor=cls.proveedor,
            almacen=cls.almacen, usuario=cls.usuario, folio="RC-2", fecha_recepcion=timezone.now(),
            estatus=Recepcion.EstatusRecepcion.RECIBIDA,
        )
        rec2_det = RecepcionDetalle.objects.create(
            recepcion=rec2, orden_compra_detalle=oc2_det, producto=cls.producto, cantidad_recibida=5,
        )

        # Factura de la OC #1: facturado a $6.00 (vs pactado $5.00) -> diferencia +$1 x 6 = $6.
        factura = FacturaProveedor.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, proveedor=cls.proveedor, oc=cls.oc1, recepcion=cls.rec1,
            moneda=cls.moneda, estatus=FacturaProveedor.FacturaProveedorStatus.REGISTRADA,
        )
        FacturaProveedorDetalle.objects.create(
            factura_proveedor=factura, oc_detalle=cls.oc1_det, recepcion_detalle=cls.rec1_det,
            producto=cls.producto, cantidad=6, precio_unitario=Decimal("6.00"),
        )

        # Calidad rechaza 2 de lo recibido en la OC #1 -> valor = 2 * $5.00 = $10.
        inspeccion = CalidadInspeccion.objects.create(
            recepcion=cls.rec1, inspector=cls.inspector, fecha=timezone.localdate(), estado="aprobada_condicion",
        )
        CalidadInspeccionDetalle.objects.create(
            calidad_inspeccion=inspeccion, recepcion_detalle=cls.rec1_det,
            cantidad_inspeccionada=6, cantidad_aprobada=4, cantidad_rechazada=2,
            resultado="rechazo", motivo_rechazo="Material defectuoso",
        )

    def _get(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.usuario)
        return client.get(self.URL)

    def _sin_contabilidad(self):
        return Usuario.objects.create(
            username="almacen@acme.test", email="almacen@acme.test", empresa=self.empresa,
            sucursal_default=self.sucursal,
        )

    def test_sin_permiso_de_contabilidad_no_ve_montos(self):
        """#377: mismos campos ocultos que el listado de OC."""
        data = self._get(self._sin_contabilidad()).json()

        self.assertEqual(data["diferencia_precio"], {"disponible": False, "motivo": "Sin permiso de contabilidad."})
        self.assertNotIn("valor_rechazado", data["material_rechazado"])
        self.assertNotIn("por_moneda", data["material_rechazado"])
        self.assertTrue(all("valor_linea" not in f for f in data["material_rechazado"]["drill_down"]))
        self.assertIn("cantidad_rechazada", data["material_rechazado"])
        self.assertIn("pct", data["cumplimiento_cantidad"])

    def test_listado_filtra_por_almacen_orden_compra_y_tipo_compra(self):
        """#889: ``?almacen=``/``?orden_compra=`` en recepciones y ``?tipo_compra=`` en OC."""
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        url = "/api/v1/compras/recepciones/"

        def ids(base, query):
            resp = client.get(f"{base}?{query}")
            self.assertEqual(resp.status_code, 200, (query, resp.content))
            data = resp.json()
            return {r["id"] for r in (data["results"] if isinstance(data, dict) else data)}

        self.assertEqual(ids(url, f"orden_compra={self.oc1.pk}"), {self.rec1.pk})
        self.assertEqual(len(ids(url, f"almacen={self.almacen.pk}")), 2)
        otro = Almacen.objects.create(empresa=self.empresa, sucursal=self.sucursal, codigo="ALM2", nombre="Otro")
        self.assertEqual(ids(url, f"almacen={otro.pk}"), set())
        self.assertEqual(ids(url, f"orden_compra={self.oc1.pk}&limit=1&search=RC"), {self.rec1.pk})
        # EC-829: solo dígitos ASCII; ``int()`` leía ``1_0`` como 10 y ``٣`` como 3.
        for query in ("almacen=x", "orden_compra=1.5", "almacen=1_0", "orden_compra=%D9%A3", "almacen=%2B5"):
            self.assertEqual(client.get(f"{url}?{query}").status_code, 400, query)

        ordenes = "/api/v1/compras/ordenes/"
        OrdenCompra.objects.filter(pk=self.oc2.pk).update(tipo_compra=OrdenCompra.TipoCompra.PRODUCTO_TERMINADO)
        self.assertEqual(ids(ordenes, "tipo_compra=PT"), {self.oc2.pk})
        self.assertEqual(ids(ordenes, "tipo_compra=mp"), {self.oc1.pk})
        self.assertEqual(client.get(f"{ordenes}?tipo_compra=XX").status_code, 400)

        # Sin empresa: lista vacía, sin 400 (#511).
        sin_empresa = APIClient()
        sin_empresa.force_authenticate(user=self.sin_empresa)
        self.assertEqual(sin_empresa.get(f"{url}?almacen=x").json(), [])
        self.assertEqual(sin_empresa.get(f"{ordenes}?tipo_compra=XX").json(), [])

    def test_cumplimiento_cantidad(self):
        resp = self._get()

        self.assertEqual(resp.status_code, 200, resp.content)
        kpi = resp.data["cumplimiento_cantidad"]
        self.assertTrue(kpi["disponible"])
        self.assertEqual(kpi["cantidad_ordenada"], Decimal("15"))  # 10 + 5
        # Recibido NETO (#381): 6 + 5 menos 2 rechazados por Calidad.
        self.assertEqual(kpi["cantidad_recibida"], Decimal("9"))
        self.assertAlmostEqual(kpi["pct"], 60.0, places=1)
        self.assertFalse(kpi["mezcla_unidades"])
        self.assertEqual(len(kpi["drill_down"]), 1)
        self.assertEqual(kpi["drill_down"][0]["oc_id"], self.oc1.pk)
        self.assertEqual(kpi["drill_down"][0]["cantidad_recibida"], Decimal("4"))
        self.assertFalse(kpi["drill_down_truncado"])

    def test_recepciones_parciales(self):
        resp = self._get()

        kpi = resp.data["recepciones_parciales"]
        self.assertEqual(kpi["ocs_parciales"], 1)
        self.assertEqual(kpi["ocs_recibidas_o_parciales"], 2)
        self.assertEqual(kpi["pct"], 50.0)

    def test_diferencia_precio(self):
        resp = self._get()

        kpi = resp.data["diferencia_precio"]
        self.assertTrue(kpi["disponible"])
        mxn = kpi["por_moneda"][0]
        self.assertEqual(mxn["moneda"], "MXN")
        self.assertEqual(mxn["costo_facturado"], Decimal("36.00"))  # 6 * 6.00
        self.assertEqual(mxn["costo_pactado_oc"], Decimal("30.00"))  # 6 * 5.00
        self.assertEqual(mxn["diferencia"], Decimal("6.00"))
        self.assertEqual(len(kpi["drill_down"]), 1)
        self.assertEqual(kpi["lineas_evaluadas"], 1)
        # #384: el drill-down no expone nombres de lookup de Django.
        self.assertEqual(
            set(kpi["drill_down"][0]),
            {"id", "factura_id", "factura_folio", "producto", "moneda", "precio_facturado", "precio_pactado",
             "diferencia_linea"},
        )

    def _factura_extra(self, **kwargs):
        factura = FacturaProveedor.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, proveedor=self.proveedor, oc=self.oc1,
            recepcion=self.rec1, moneda=self.moneda, **kwargs,
        )
        FacturaProveedorDetalle.objects.create(
            factura_proveedor=factura, oc_detalle=self.oc1_det, recepcion_detalle=self.rec1_det,
            producto=self.producto, cantidad=6, precio_unitario=Decimal("100.00"),
        )
        return factura

    def test_diferencia_precio_ignora_borradores_e_inactivas(self):
        """#379: solo facturas Registradas y activas."""
        self._factura_extra(estatus=FacturaProveedor.FacturaProveedorStatus.BORRADOR)
        self._factura_extra(estatus=FacturaProveedor.FacturaProveedorStatus.REGISTRADA, activo=False)

        kpi = self._get().data["diferencia_precio"]

        self.assertEqual(kpi["por_moneda"][0]["costo_facturado"], Decimal("36.00"))
        self.assertEqual(kpi["lineas_evaluadas"], 1)

    def test_diferencia_precio_sin_lineas_se_distingue_de_diferencia_cero(self):
        """#383: ``lineas_evaluadas`` = 0 cuando no hubo nada que evaluar."""
        FacturaProveedor.objects.filter(empresa=self.empresa).update(
            estatus=FacturaProveedor.FacturaProveedorStatus.BORRADOR
        )

        kpi = self._get().data["diferencia_precio"]

        self.assertEqual(kpi["lineas_evaluadas"], 0)
        self.assertEqual(kpi["por_moneda"], [])

    def test_material_rechazado(self):
        resp = self._get()

        kpi = resp.data["material_rechazado"]
        self.assertTrue(kpi["disponible"])
        self.assertEqual(kpi["cantidad_rechazada"], Decimal("2"))
        self.assertEqual(kpi["por_moneda"], [{"moneda": "MXN", "valor_rechazado": Decimal("10.00")}])  # 2 * 5.00
        self.assertEqual(kpi["drill_down"][0]["motivo_rechazo"], "Material defectuoso")
        self.assertEqual(kpi["drill_down"][0]["recepcion_folio"], "RC-1")
        self.assertEqual(kpi["drill_down"][0]["producto"], "Insumo Test")

    # --- helpers para armar más datos --------------------------------------

    def _oc(self, empresa=None, sucursal=None, proveedor=None, moneda=None, estatus=None, fecha_oc=None, usuario=None):
        return OrdenCompra.objects.create(
            empresa=empresa or self.empresa, sucursal=sucursal or self.sucursal,
            proveedor=proveedor or self.proveedor, moneda=moneda or self.moneda,
            usuario=usuario or self.usuario, fecha_oc=fecha_oc or timezone.now().date(),
            estatus=estatus or OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
        )

    def _linea_recibida(self, oc, producto=None, cantidad=10, recibida=10, precio="5.00", folio=None,
                        tipo_origen=None, rechazada=0, resultado="rechazo", empresa=None, sucursal=None, almacen=None):
        """OC detalle + recepción + renglón recibido (+ rechazo de Calidad si ``rechazada``)."""
        producto = producto or self.producto
        det = OrdenCompraDetalle.objects.create(
            orden_compra=oc, producto=producto, sucursal=sucursal or self.sucursal, cantidad=cantidad,
            precio=Decimal(precio),
        )
        rec = Recepcion.objects.create(
            orden_compra=None if tipo_origen == Recepcion.TipoOrigen.ORDEN_PRODUCCION else oc,
            tipo_origen=tipo_origen or Recepcion.TipoOrigen.ORDEN_COMPRA,
            empresa=empresa or self.empresa, sucursal=sucursal or self.sucursal, proveedor=oc.proveedor,
            almacen=almacen or self.almacen, usuario=oc.usuario,
            folio=folio or f"RC-X{Recepcion.objects.count() + 100}", fecha_recepcion=timezone.now(),
            estatus=Recepcion.EstatusRecepcion.RECIBIDA,
        )
        rec_det = RecepcionDetalle.objects.create(
            recepcion=rec,
            orden_compra_detalle=None if tipo_origen == Recepcion.TipoOrigen.ORDEN_PRODUCCION else det,
            producto=producto, cantidad_recibida=recibida,
        )
        if rechazada:
            inspeccion = CalidadInspeccion.objects.create(
                recepcion=rec, inspector=self.inspector, fecha=timezone.localdate(), estado="rechazada",
            )
            CalidadInspeccionDetalle.objects.create(
                calidad_inspeccion=inspeccion, recepcion_detalle=rec_det, cantidad_inspeccionada=recibida,
                cantidad_aprobada=recibida - rechazada, cantidad_rechazada=rechazada, resultado=resultado,
                motivo_rechazo="Prueba",
            )
        return det, rec, rec_det

    def _documentos_de_otra_empresa(self):
        sucursal = Sucursal.objects.create(empresa=self.otra, codigo="GDL", nombre="GDL")
        almacen = Almacen.objects.create(empresa=self.otra, sucursal=sucursal, codigo="ALG", nombre="Almacen G")
        usuario = Usuario.objects.create(username="ug", email="ug@globex.test", empresa=self.otra)
        producto = Producto.objects.create(tipo=_tipo_producto(), empresa=self.otra, nombre="Insumo G")
        oc = self._oc(empresa=self.otra, sucursal=sucursal, usuario=usuario, estatus=OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA)
        return self._linea_recibida(
            oc, producto=producto, cantidad=100, recibida=100, rechazada=40, folio="RC-GLOBEX",
            empresa=self.otra, sucursal=sucursal, almacen=almacen,
        )

    # --- #384/#380: aislamiento y superusuario -------------------------------

    def test_otra_empresa_no_contamina_los_totales(self):
        self._documentos_de_otra_empresa()

        data = self._get().data

        # Si hubiera fuga cross-tenant los totales cambiarían.
        self.assertEqual(data["cumplimiento_cantidad"]["cantidad_ordenada"], Decimal("15"))
        self.assertEqual(data["cumplimiento_cantidad"]["cantidad_recibida"], Decimal("9"))
        self.assertEqual(data["material_rechazado"]["cantidad_rechazada"], Decimal("2"))
        self.assertEqual(data["recepciones_parciales"]["ocs_recibidas_o_parciales"], 2)

    def test_usuario_sin_empresa_no_ve_nada(self):
        resp = self._get(self.sin_empresa)

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["cumplimiento_cantidad"]["disponible"])

    def test_superusuario_con_empresa_solo_ve_su_empresa_como_el_listado(self):
        self._documentos_de_otra_empresa()
        root = Usuario.objects.create(
            username="root", email="root@acme.test", empresa=self.empresa, is_superuser=True, is_staff=True,
        )

        data = self._get(root).data

        self.assertEqual(data["cumplimiento_cantidad"]["cantidad_ordenada"], Decimal("15"))
        self.assertEqual(data["material_rechazado"]["cantidad_rechazada"], Decimal("2"))
        self.assertTrue(data["puede_ver_importes"])

    def test_superusuario_sin_empresa_no_recibe_ceros_como_disponibles(self):
        self._documentos_de_otra_empresa()
        root = Usuario.objects.create(username="root2", email="root2@nowhere.test", is_superuser=True, is_staff=True)

        data = self._get(root).data

        self.assertFalse(data["cumplimiento_cantidad"]["disponible"])
        self.assertFalse(data["puede_ver_importes"])  # #860

    # --- #357/#384/#390: parámetros -------------------------------------------

    def _get_q(self, query, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.usuario)
        return client.get(self.URL + query)

    def test_parametro_no_soportado_responde_400(self):
        resp = self._get_q("?almacen=999&meta_cumplimiento=95")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("almacen", str(resp.data["parametros"]))
        self.assertIn("meta_cumplimiento", str(resp.data["parametros"]))

    def test_parametros_invalidos_responden_400(self):
        for query in ("?sucursal=x", "?fecha_desde=2026-99-01", "?fecha_desde=2026-02-01&fecha_hasta=2026-01-01"):
            self.assertEqual(self._get_q(query).status_code, 400, query)

    def test_filtros_de_sucursal_proveedor_y_fechas_se_aplican(self):
        otra_suc = Sucursal.objects.create(empresa=self.empresa, codigo="GDL", nombre="GDL")
        resp = self._get_q(f"?sucursal={otra_suc.pk}")
        self.assertEqual(resp.data["cumplimiento_cantidad"]["cantidad_ordenada"], 0)
        self.assertEqual(resp.data["filtros"]["sucursal"], otra_suc.pk)

        resp = self._get_q(f"?sucursal={self.sucursal.pk}&proveedor={self.proveedor.pk}")
        self.assertEqual(resp.data["cumplimiento_cantidad"]["cantidad_ordenada"], Decimal("15"))

        manana = (timezone.now().date() + timedelta(days=1)).isoformat()
        self.assertEqual(self._get_q(f"?fecha_desde={manana}").data["cumplimiento_cantidad"]["cantidad_ordenada"], 0)
        hoy = timezone.now().date().isoformat()
        self.assertEqual(
            self._get_q(f"?fecha_desde={hoy}&fecha_hasta={hoy}").data["cumplimiento_cantidad"]["cantidad_ordenada"],
            Decimal("15"),
        )

    # --- #381: neto y unidades ------------------------------------------------

    def test_rechazo_y_nueva_recepcion_sobre_la_misma_linea(self):
        """Tras rechazar 2 y recibir de nuevo 2, la OC 1 queda en 6 netas, no en 8 brutas."""
        RecepcionDetalle.objects.create(
            recepcion=self.rec1, orden_compra_detalle=self.oc1_det, producto=self.producto, cantidad_recibida=2,
        )

        kpi = self._get().data["cumplimiento_cantidad"]

        self.assertEqual(kpi["cantidad_recibida"], Decimal("11"))  # (6 + 2 - 2) + 5
        self.assertEqual(kpi["drill_down"][0]["cantidad_recibida"], Decimal("6"))

    def test_oc_parcial_con_recibido_bruto_completo_sigue_en_el_drill_down(self):
        """Como OC 126 del issue: 4 ordenadas, 4 recibidas, 2 rechazadas."""
        oc = self._oc(estatus=OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA)
        self._linea_recibida(oc, cantidad=4, recibida=4, rechazada=2)

        folios = [f["oc_id"] for f in self._get().data["cumplimiento_cantidad"]["drill_down"]]

        self.assertIn(oc.pk, folios)

    def test_cantidades_de_unidades_distintas_no_se_suman(self):
        from nucleo.models import UnidadMedida

        metros = UnidadMedida.objects.create(clave="MTR", nombre="Metro")
        piezas = UnidadMedida.objects.create(clave="PZA", nombre="Pieza")
        tela = Producto.objects.create(tipo=_tipo_producto(), empresa=self.empresa, nombre="Tela", unidad_medida=metros)
        boton = Producto.objects.create(tipo=_tipo_producto(), empresa=self.empresa, nombre="Botón", unidad_medida=piezas)
        oc = self._oc()
        self._linea_recibida(oc, producto=tela, cantidad=1000, recibida=500, rechazada=30)
        self._linea_recibida(oc, producto=boton, cantidad=10, recibida=10, rechazada=1)

        data = self._get().data

        cumplimiento = data["cumplimiento_cantidad"]
        self.assertTrue(cumplimiento["mezcla_unidades"])
        self.assertIsNone(cumplimiento["pct"])
        self.assertIsNone(cumplimiento["cantidad_ordenada"])
        por_unidad = {u["unidad"]: u for u in cumplimiento["por_unidad"]}
        self.assertEqual(por_unidad["MTR"]["cantidad_recibida"], Decimal("470"))
        self.assertEqual(por_unidad["PZA"]["cantidad_recibida"], Decimal("9"))
        self.assertIn(None, {u["unidad"] for u in cumplimiento["por_unidad"]})  # productos sin unidad
        rechazado = data["material_rechazado"]
        self.assertTrue(rechazado["mezcla_unidades"])
        self.assertIsNone(rechazado["cantidad_rechazada"])
        self.assertEqual({u["unidad"] for u in rechazado["por_unidad"]}, {"MTR", "PZA", None})

    # --- #382: qué es un rechazo ----------------------------------------------

    def test_rechazos_de_recepciones_de_produccion_no_cuentan(self):
        oc = self._oc()
        self._linea_recibida(
            oc, cantidad=5, recibida=5, rechazada=5, tipo_origen=Recepcion.TipoOrigen.ORDEN_PRODUCCION,
        )

        kpi = self._get().data["material_rechazado"]

        self.assertEqual(kpi["cantidad_rechazada"], Decimal("2"))
        self.assertEqual(kpi["drill_down_total"], 1)

    def test_solo_resultado_rechazo_cuenta_como_rechazado(self):
        oc = self._oc()
        self._linea_recibida(oc, cantidad=5, recibida=5, rechazada=3, resultado="liberado")
        self._linea_recibida(oc, cantidad=5, recibida=5, rechazada=4, resultado="concesion lazzar")

        kpi = self._get().data["material_rechazado"]

        self.assertEqual(kpi["cantidad_rechazada"], Decimal("2"))

    # --- #378: monedas ---------------------------------------------------------

    def test_valor_rechazado_y_diferencia_de_precio_se_separan_por_moneda(self):
        usd = Moneda.objects.create(codigo_iso="USD", nombre="Dólar")
        oc = self._oc(moneda=usd, estatus=OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA)
        det, rec, rec_det = self._linea_recibida(oc, cantidad=10, recibida=10, precio="2.00", rechazada=3)
        factura = FacturaProveedor.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, proveedor=self.proveedor, oc=oc, recepcion=rec,
            moneda=usd, estatus=FacturaProveedor.FacturaProveedorStatus.REGISTRADA,
        )
        FacturaProveedorDetalle.objects.create(
            factura_proveedor=factura, oc_detalle=det, recepcion_detalle=rec_det, producto=self.producto,
            cantidad=10, precio_unitario=Decimal("3.00"),
        )

        data = self._get().data

        valor = {m["moneda"]: m["valor_rechazado"] for m in data["material_rechazado"]["por_moneda"]}
        self.assertEqual(valor, {"MXN": Decimal("10.00"), "USD": Decimal("6.00")})
        precio = {m["moneda"]: m for m in data["diferencia_precio"]["por_moneda"]}
        self.assertEqual(precio["MXN"]["diferencia"], Decimal("6.00"))
        self.assertEqual(precio["USD"]["diferencia"], Decimal("10.00"))

    def test_factura_en_moneda_distinta_a_su_oc_no_se_compara(self):
        usd = Moneda.objects.create(codigo_iso="USD", nombre="Dólar")
        FacturaProveedor.objects.filter(empresa=self.empresa).update(moneda=usd)

        kpi = self._get().data["diferencia_precio"]

        self.assertEqual(kpi["lineas_evaluadas"], 0)
        self.assertEqual(kpi["lineas_moneda_distinta"], 1)
        self.assertEqual(kpi["por_moneda"], [])

    # --- #384: drill-downs truncados -------------------------------------------

    def test_drill_down_truncado_se_indica_y_el_orden_es_estable(self):
        oc = self._oc()
        for i in range(22):
            self._linea_recibida(oc, cantidad=5, recibida=5, rechazada=1, folio=f"RC-T{i:02d}")

        kpi = self._get().data["material_rechazado"]

        self.assertEqual(kpi["drill_down_total"], 23)  # 22 + el de setUp
        self.assertTrue(kpi["drill_down_truncado"])
        self.assertEqual(len(kpi["drill_down"]), 20)
        # Primero el de 2 piezas; los de 1 pieza van por id descendente (desempate).
        self.assertEqual(kpi["drill_down"][0]["cantidad_rechazada"], Decimal("2"))
        ids = [f["id"] for f in kpi["drill_down"][1:]]
        self.assertEqual(ids, sorted(ids, reverse=True))


class OrdenCompraKpisTests(TestCase):
    """``GET /ordenes/kpis/`` (EC-432): 3 de 4 KPIs son reales; ciclo_compra
    queda ``disponible: False`` -- el flujo Requisición → Solicitud de compra
    no se usa hoy, así que no hay fecha de requisición contra qué medir."""

    URL = "/api/v1/compras/ordenes/kpis/"

    @classmethod
    def setUpTestData(cls):
        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General de Ley")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Pago en una sola exhibición")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="MTY")
        cls.proveedor = Proveedor.objects.create(
            empresa=cls.empresa, nombre="Proveedor Test", moneda=cls.moneda, sat_regimen_fiscal=regimen,
            sat_forma_pago=forma, sat_metodo_pago=metodo, codigo="PROV-1", razon_social="Prov SA",
            telefono="8100000000", contacto_principal="Contacto", rfc="XAXX010101000", email="p@acme.test",
        )
        # Ve montos (admin de empresa): estos tests fijan los cálculos.
        cls.usuario = Usuario.objects.create(
            username="u@acme.test", email="u@acme.test", empresa=cls.empresa, sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )
        cls.sin_empresa = Usuario.objects.create(username="se", email="se@nowhere.test")

        tela = CategoriaProducto.objects.create(empresa=cls.empresa, nombre="Tela", codigo="TEL", descripcion="")
        hilo = CategoriaProducto.objects.create(empresa=cls.empresa, nombre="Hilo", codigo="HIL", descripcion="")
        cls.prod_tela = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Tela Azul", categoria_producto=tela)
        cls.prod_hilo = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Hilo Blanco", categoria_producto=hilo)

        hoy = timezone.now().date()

        # OC abierta (AUTORIZADA), no vencida.
        cls.oc_abierta = OrdenCompra.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, proveedor=cls.proveedor, moneda=cls.moneda,
            usuario=cls.usuario, fecha_oc=hoy, estatus=OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
            gran_total=Decimal("1000.00"), fecha_entrega_estimada=hoy + timedelta(days=5),
        )
        OrdenCompraDetalle.objects.create(
            orden_compra=cls.oc_abierta, producto=cls.prod_tela, sucursal=cls.sucursal,
            cantidad=10, precio=Decimal("50.00"), importe=Decimal("500.00"),
        )
        OrdenCompraDetalle.objects.create(
            orden_compra=cls.oc_abierta, producto=cls.prod_hilo, sucursal=cls.sucursal,
            cantidad=10, precio=Decimal("50.00"), importe=Decimal("500.00"),
        )

        # OC vencida sin recibir (PARCIALMENTE_RECIBIDA, fecha ya pasada).
        cls.oc_vencida = OrdenCompra.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, proveedor=cls.proveedor, moneda=cls.moneda,
            usuario=cls.usuario, fecha_oc=hoy - timedelta(days=20),
            estatus=OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
            gran_total=Decimal("300.00"), fecha_entrega_estimada=hoy - timedelta(days=3),
        )
        OrdenCompraDetalle.objects.create(
            orden_compra=cls.oc_vencida, producto=cls.prod_tela, sucursal=cls.sucursal,
            cantidad=5, precio=Decimal("60.00"), importe=Decimal("300.00"),
        )

        # OC recibida (cerrada): no cuenta como abierta ni como vencida.
        OrdenCompra.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, proveedor=cls.proveedor, moneda=cls.moneda,
            usuario=cls.usuario, fecha_oc=hoy - timedelta(days=30),
            estatus=OrdenCompra.EstatusOrdenCompra.RECIBIDA,
            gran_total=Decimal("999.00"), fecha_entrega_estimada=hoy - timedelta(days=25),
        )

    def _get(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.usuario)
        return client.get(self.URL)

    def _sin_contabilidad(self):
        return Usuario.objects.create(
            username="almacen@acme.test", email="almacen@acme.test", empresa=self.empresa,
            sucursal_default=self.sucursal,
        )

    def test_sin_permiso_de_contabilidad_no_ve_montos(self):
        """#373: mismos campos ocultos que el listado de OC."""
        data = self._get(self._sin_contabilidad()).json()

        self.assertNotIn("monto", data["ocs_abiertas"])
        self.assertTrue(all("monto" not in f for f in data["ocs_abiertas"]["por_estatus"]))
        self.assertIn("total", data["ocs_abiertas"])
        self.assertEqual(data["gasto_por_categoria"], {"disponible": False, "motivo": "Sin permiso de contabilidad."})

    def test_ocs_abiertas(self):
        resp = self._get()

        self.assertEqual(resp.status_code, 200, resp.content)
        kpi = resp.data["ocs_abiertas"]
        self.assertTrue(kpi["disponible"])
        self.assertEqual(kpi["total"], 2)  # abierta + vencida (ambas en estatus abierto)
        self.assertEqual(kpi["por_moneda"], [{"moneda": "MXN", "total": 2, "monto": Decimal("1300.00")}])
        self.assertNotIn("monto", kpi)

    def test_ocs_vencidas_sin_recibir(self):
        resp = self._get()

        kpi = resp.data["ocs_vencidas_sin_recibir"]
        self.assertTrue(kpi["disponible"])
        self.assertEqual(kpi["total"], 1)
        self.assertEqual(kpi["drill_down"][0]["oc_id"], self.oc_vencida.pk)
        self.assertEqual(kpi["drill_down"][0]["dias_vencida"], 3)

    def test_parametros_no_soportados_responden_400(self):
        """#950: ``ordenes/kpis/`` no acepta filtros; un parámetro → 400, como
        ``recepciones/kpis/``, también sin empresa."""
        for user in (self.usuario, self.sin_empresa):
            for params in ("parametro_inventado=1", "sucursal=1", "fecha_desde=2026-01-01"):
                with self.subTest(user=user.username, params=params):
                    client = APIClient()
                    client.force_authenticate(user=user)
                    resp = client.get(f"{self.URL}?{params}")
                    self.assertEqual(resp.status_code, 400, resp.content)
                    self.assertIn("parametros", resp.json())
        self.assertEqual(self._get().status_code, 200)

    def test_ciclo_compra_no_disponible(self):
        resp = self._get()
        self.assertFalse(resp.data["ciclo_compra"]["disponible"])

    def test_gasto_por_categoria(self):
        resp = self._get()

        categorias = {c["categoria"]: c["monto"] for c in resp.data["gasto_por_categoria"]["categorias"]}
        self.assertEqual(categorias["Tela"], Decimal("800.00"))  # 500 (abierta) + 300 (vencida)
        self.assertEqual(categorias["Hilo"], Decimal("500.00"))

    def test_montos_de_oc_en_otra_moneda_no_se_suman(self):
        """#374: una OC en USD va en su propia fila, no dentro del total MXN."""
        usd = Moneda.objects.create(codigo_iso="USD", nombre="Dólar")
        oc = OrdenCompra.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, proveedor=self.proveedor, moneda=usd,
            usuario=self.usuario, fecha_oc=timezone.now().date(),
            estatus=OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA, gran_total=Decimal("94838.16"),
        )
        OrdenCompraDetalle.objects.create(
            orden_compra=oc, producto=self.prod_tela, sucursal=self.sucursal,
            cantidad=1, precio=Decimal("94838.16"), importe=Decimal("94838.16"),
        )

        data = self._get().data

        abiertas = {m["moneda"]: m for m in data["ocs_abiertas"]["por_moneda"]}
        self.assertEqual(abiertas["MXN"]["monto"], Decimal("1300.00"))
        self.assertEqual(abiertas["USD"]["monto"], Decimal("94838.16"))
        self.assertEqual(data["ocs_abiertas"]["total"], 3)
        por_estatus = {(f["estatus"], f["moneda"]): f["monto"] for f in data["ocs_abiertas"]["por_estatus"]}
        self.assertEqual(por_estatus[(4, "USD")], Decimal("94838.16"))
        self.assertEqual(por_estatus[(4, "MXN")], Decimal("300.00"))
        gasto = {(c["categoria"], c["moneda"]): c["monto"] for c in data["gasto_por_categoria"]["categorias"]}
        self.assertEqual(gasto[("Tela", "MXN")], Decimal("800.00"))
        self.assertEqual(gasto[("Tela", "USD")], Decimal("94838.16"))

    def test_usuario_sin_empresa_no_ve_nada(self):
        resp = self._get(self.sin_empresa)

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["ocs_abiertas"]["disponible"])
        self.assertFalse(resp.data["puede_ver_importes"])

    def test_superusuario_sin_empresa_no_recibe_ceros_como_disponibles(self):
        # #861: mismo criterio que recepciones y proveedores (#834).
        root = Usuario.objects.create(username="root", email="root@nowhere.test", is_superuser=True, is_staff=True)

        data = self._get(root).data

        self.assertFalse(data["ocs_abiertas"]["disponible"])
        self.assertEqual(data["ocs_abiertas"]["motivo"], "Usuario sin empresa asignada.")
        self.assertFalse(data["puede_ver_importes"])



class OrdenCompraPedidosSeriesRendimientoTests(TestCase):
    """OC: referencia opcional, varios pedidos, serie de folio por tipo de compra
    (MP -> ORDEN_COMPRA/OC, PT -> ORDEN_COMPRA_PT/O) y alta/edición/aceptación sin N+1."""

    @classmethod
    def setUpTestData(cls):
        from ventas.models import Pedido
        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Una exhibición")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.otra = Empresa.objects.create(codigo="globex", razon_social="Globex SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="MTY")
        cls.proveedor = Proveedor.objects.create(
            empresa=cls.empresa, nombre="P1", moneda=cls.moneda, sat_regimen_fiscal=regimen,
            sat_forma_pago=forma, sat_metodo_pago=metodo, codigo="P1", razon_social="P1",
            telefono="8100000000", contacto_principal="Ana", rfc="XAXX010101000", email="p1@p.test",
        )
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Tela")
        # PT se compra por SKU (#792): una variante comprable para las O.
        cls.variante_pt = ProductoVariante.objects.create(
            producto=Producto.objects.create(
                empresa=cls.empresa, nombre="Camisa", tipo=_tipo_producto(),
            ),
            empresa=cls.empresa, color=Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000"),
            sku="CAM-PT", precio_base="1",
        )
        cls.usuario = Usuario.objects.create(
            username="compras", email="c@acme.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        from terceros.models import Cliente
        cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente")
        cliente_ajeno = Cliente.objects.create(empresa=cls.otra, nombre="Cliente X")
        cls.pedidos = [
            Pedido.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, cliente=cliente, moneda=cls.moneda, folio=f"PED-{i}")
            for i in range(3)
        ]
        cls.pedido_ajeno = Pedido.objects.create(
            empresa=cls.otra, sucursal=Sucursal.objects.create(empresa=cls.otra, codigo="GX", nombre="GX"),
            cliente=cliente_ajeno, moneda=cls.moneda, folio="PED-X",
        )
        SerieFolio.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, tipo_documento="ORDEN_COMPRA", serie="OC", relleno_ceros=4,
        )
        SerieFolio.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, tipo_documento="ORDEN_COMPRA_PT", serie="O", relleno_ceros=4,
        )

    def setUp(self):
        self.client_api = APIClient()
        self.client_api.force_authenticate(user=self.usuario)

    def _renglones(self, n=1):
        return [{"producto": self.producto.pk, "cantidad": 2, "precio": "10.00"} for _ in range(n)]

    def _crear(self, header=None, n=1):
        detalle = self._renglones(n)
        if (header or {}).get("tipo_compra") == "PT":
            detalle = [{"producto_variante": self.variante_pt.pk, "cantidad": 2, "precio": "10.00"} for _ in range(n)]
        body = {"orden_compra": {"proveedor": self.proveedor.pk, **(header or {})}, "detalle": detalle}
        return self.client_api.post(f"{ORDENES_URL}onboarding/", body, format="json")

    def test_referencia_es_opcional(self):
        resp = self._crear()

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNone(resp.json()["orden_compra"]["referencia"])
        self.assertEqual(self._crear({"referencia": ""}).status_code, 200)

    def test_una_oc_surte_varios_pedidos(self):
        ids = [p.pk for p in self.pedidos]
        resp = self._crear({"pedidos": ids})

        self.assertEqual(resp.status_code, 200, resp.content)
        oc = resp.json()["orden_compra"]
        self.assertEqual(oc["pedidos"], ids)
        self.assertEqual([p["folio"] for p in oc["pedidos_vinculados"]], ["PED-0", "PED-1", "PED-2"])
        self.assertEqual(oc["pedido"], ids[0])  # el FK legado refleja el primero
        detalle = self.client_api.get(f"{ORDENES_URL}{oc['id']}/").json()
        self.assertEqual(detalle["pedidos"], ids)
        listado = self.client_api.get(ORDENES_URL).json()
        filas = listado["results"] if isinstance(listado, dict) else listado
        self.assertEqual(filas[0]["pedidos"], ids)

    def test_pedido_unico_legado_se_refleja_en_pedidos(self):
        oc = self._crear({"pedido": self.pedidos[1].pk}).json()["orden_compra"]

        self.assertEqual(oc["pedido"], self.pedidos[1].pk)
        self.assertEqual(oc["pedidos"], [self.pedidos[1].pk])

    def test_editar_reemplaza_y_vacia_los_pedidos(self):
        oc = self._crear({"pedidos": [self.pedidos[0].pk, self.pedidos[1].pk]}).json()["orden_compra"]
        resp = self.client_api.put(
            f"{ORDENES_URL}{oc['id']}/", {"pedidos": [self.pedidos[2].pk]}, format="json",
        )
        self.assertEqual(resp.json()["orden_compra"]["pedidos"], [self.pedidos[2].pk])
        # Sin ``pedidos`` en el body no se tocan.
        resp = self.client_api.put(f"{ORDENES_URL}{oc['id']}/", {"observaciones": "x"}, format="json")
        self.assertEqual(resp.json()["orden_compra"]["pedidos"], [self.pedidos[2].pk])
        resp = self.client_api.put(f"{ORDENES_URL}{oc['id']}/", {"pedidos": []}, format="json")
        self.assertEqual(resp.json()["orden_compra"]["pedidos"], [])
        self.assertIsNone(resp.json()["orden_compra"]["pedido"])

    def test_pedido_de_otra_empresa_se_rechaza(self):
        resp = self._crear({"pedidos": [self.pedidos[0].pk, self.pedido_ajeno.pk]})

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("pedidos", resp.json())
        self.assertEqual(OrdenCompra.objects.count(), 0)

    def test_oc_legada_con_solo_fk_pedido_se_lista_en_pedidos(self):
        oc = OrdenCompra.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, proveedor=self.proveedor, moneda=self.moneda,
            usuario=self.usuario, fecha_oc="2026-10-01", pedido=self.pedidos[0],
        )
        self.assertEqual(self.client_api.get(f"{ORDENES_URL}{oc.pk}/").json()["pedidos"], [self.pedidos[0].pk])

    def _aceptar(self, header=None):
        oc = self._crear(header).json()["orden_compra"]
        resp = self.client_api.post(f"{ORDENES_URL}{oc['id']}/aceptar/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["orden_compra"]

    def test_folio_por_tipo_de_compra(self):
        self.assertEqual(self._aceptar()["folio"], "OC-0001")
        self.assertEqual(self._aceptar({"tipo_compra": "MP"})["folio"], "OC-0002")
        pt = self._aceptar({"tipo_compra": "PT"})
        self.assertEqual(pt["folio"], "O-0001")
        self.assertEqual(pt["tipo_compra"], "PT")
        self.assertEqual(self._aceptar({"tipo_compra": "PT"})["folio"], "O-0002")
        self.assertEqual(self._aceptar()["folio"], "OC-0003")

    def test_folio_usa_las_series_con_el_nombre_real_de_la_bd(self):
        """#502: en la BD las series se llaman "Orden de Compra" / "Orden de Compra PT"."""
        SerieFolio.objects.filter(tipo_documento__in=["ORDEN_COMPRA", "ORDEN_COMPRA_PT"]).update(
            tipo_documento="Orden de Compra"
        )
        SerieFolio.objects.filter(serie="O").update(tipo_documento="Orden de Compra PT")

        self.assertEqual(self._aceptar()["folio"], "OC-0001")
        self.assertEqual(self._aceptar({"tipo_compra": "PT"})["folio"], "O-0001")

    def test_onboarding_con_folio_no_cambia_el_tipo_ni_borra_el_folio(self):
        """#504: la edición por onboarding respeta las mismas reglas que el PUT."""
        oc = self._aceptar()
        # El PUT regresa la OC a POR_AUTORIZAR conservando el folio: ya es editable por onboarding.
        self.client_api.put(f"{ORDENES_URL}{oc['id']}/", {"observaciones": "y"}, format="json")
        body = {"orden_compra_id": oc["id"], "orden_compra": {"tipo_compra": "PT"}}
        resp = self.client_api.post(f"{ORDENES_URL}onboarding/", body, format="json")

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("tipo_compra", resp.json())

        body = {"orden_compra_id": oc["id"], "orden_compra": {"observaciones": "x"}}
        resp = self.client_api.post(f"{ORDENES_URL}onboarding/", body, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(OrdenCompra.objects.get(pk=oc["id"]).folio, "OC-0001")

    def test_el_primer_pedido_siempre_coincide_con_pedido_aunque_vengan_desordenados(self):
        """#503: ``pedidos[0]`` == ``pedido`` y el orden es estable."""
        ids = [self.pedidos[2].pk, self.pedidos[0].pk, self.pedidos[1].pk]
        oc = self._crear({"pedidos": ids}).json()["orden_compra"]

        esperado = [ids[0]] + sorted(ids[1:])
        self.assertEqual(oc["pedido"], ids[0])
        self.assertEqual(oc["pedidos"], esperado)
        self.assertEqual(self.client_api.get(f"{ORDENES_URL}{oc['id']}/").json()["pedidos"], esperado)

    def test_pt_sin_serie_es_400_sin_folio_de_respaldo(self):
        """#1142/#1264: ya no hay folio de respaldo (``O-{empresa}-{id}``); sin serie PT
        activa, aceptar responde 400 y la OC queda como estaba."""
        SerieFolio.objects.filter(tipo_documento="ORDEN_COMPRA_PT").delete()
        oc = self._crear({"tipo_compra": "PT"}).json()["orden_compra"]
        resp = self.client_api.post(f"{ORDENES_URL}{oc['id']}/aceptar/", {}, format="json")

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"folio": [
            "La sucursal no tiene serie de folios activa para Orden de Compra PT. Configúrala en Series y folios."]})
        self.assertEqual(OrdenCompra.objects.filter(pk=oc["id"]).values_list("folio", "estatus").get(),
                         (oc["folio"], oc["estatus"]))

    def test_tipo_compra_invalido_responde_400(self):
        self.assertEqual(self._crear({"tipo_compra": "XX"}).status_code, 400)

    def test_con_folio_no_se_puede_cambiar_el_tipo_de_compra(self):
        oc = self._aceptar()
        resp = self.client_api.put(f"{ORDENES_URL}{oc['id']}/", {"tipo_compra": "PT"}, format="json")

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("tipo_compra", resp.json())

    def test_totales_y_renglones_se_guardan_igual(self):
        resp = self._crear({"porcentaje_iva": "16.00"}, n=3)

        oc = resp.json()["orden_compra"]
        self.assertEqual(len(resp.json()["detalle"]), 3)
        self.assertEqual((oc["subtotal"], oc["total_iva"], oc["gran_total"], oc["total_piezas"]),
                         ("60.00", "9.60", "69.60", 6))
        # Los renglones nuevos siguen dejando historial.
        self.assertEqual(OrdenCompraDetalle.history.filter(orden_compra_id=oc["id"]).count(), 3)

    def _queries(self, fn):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            resp = fn()
        self.assertEqual(resp.status_code, 200, resp.content)
        return len(ctx)

    def test_queries_de_alta_y_aceptar_no_crecen_con_los_renglones(self):
        ids = [p.pk for p in self.pedidos]
        chico = self._queries(lambda: self._crear({"pedidos": ids}, n=2))
        grande = self._queries(lambda: self._crear({"pedidos": ids}, n=20))
        self.assertEqual(chico, grande)
        # Antes de la optimización: ~47 con 10 renglones. 23 hoy: el ``codigo_iso`` de
        # la moneda para ``tipo_cambio`` (379d5f0, #809) y el evento ``crear`` en la
        # bitácora (savepoint + INSERT + release; 28e6211). Medido igual con 2 y 20.
        self.assertLessEqual(grande, 23)

        def aceptar(n):
            oc_id = self._crear({}, n=n).json()["orden_compra"]["id"]
            return self._queries(lambda: self.client_api.post(f"{ORDENES_URL}{oc_id}/aceptar/", {}, format="json"))

        aceptar_chico, aceptar_grande = aceptar(2), aceptar(20)
        self.assertEqual(aceptar_chico, aceptar_grande)
        # Costo fijo de 16: los 12 de la optimización más el evento de estatus en la
        # bitácora (3; 28e6211) y ``necesidades.exists()`` de las pre-órdenes (1; d155e00).
        self.assertLessEqual(aceptar_grande, 16)

    def test_queries_de_edicion_no_crecen_con_los_pedidos(self):
        oc_id = self._crear({}, n=5).json()["orden_compra"]["id"]
        body = {"orden_compra_id": oc_id, "orden_compra": {"pedidos": [self.pedidos[0].pk]}, "detalle": self._renglones(5)}
        una = self._queries(lambda: self.client_api.post(f"{ORDENES_URL}onboarding/", body, format="json"))
        body["orden_compra"]["pedidos"] = [p.pk for p in self.pedidos]
        varias = self._queries(lambda: self.client_api.post(f"{ORDENES_URL}onboarding/", body, format="json"))
        self.assertLessEqual(varias, una + 2)


class OrdenCompraRutaCriticaTests(TestCase):
    """``ordenes/{id}/ruta-critica/``: fecha real de recibo + comentario, solo informativos."""

    @classmethod
    def setUpTestData(cls):
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")

        def tenant(codigo):
            empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
            sucursal = Sucursal.objects.create(empresa=empresa, codigo=codigo[:3].upper(), nombre=codigo)
            usuario = Usuario.objects.create(
                username=f"a@{codigo}.test", email=f"a@{codigo}.test", empresa=empresa, sucursal_default=sucursal,
            )
            return empresa, sucursal, usuario

        cls.empresa, cls.sucursal, cls.usuario = tenant("acme-rc")
        cls.otra, cls.otra_sucursal, _ = tenant("globex-rc")

    def _oc(self, empresa=None, sucursal=None):
        return OrdenCompra.objects.create(
            empresa=empresa or self.empresa, sucursal=sucursal or self.sucursal, moneda=self.moneda,
            usuario=self.usuario, fecha_oc=timezone.localdate(), folio=f"OC-{OrdenCompra.objects.count()}",
        )

    def setUp(self):
        from seguridad.models import Permiso, UsuarioPermiso

        permiso = Permiso.objects.create(clave="E-COMPRAS-OC", nombre="Órdenes de compra")
        UsuarioPermiso.objects.create(
            usuario=self.usuario, permiso=permiso, tipo=UsuarioPermiso.TIPO_GRANT, empresa=self.empresa,
        )
        self.api = APIClient()
        self.api.force_authenticate(user=self.usuario)

    def _url(self, oc):
        return f"/api/v1/compras/ordenes/{oc.pk}/ruta-critica/"

    def test_patch_sin_permiso_es_403_y_get_sigue_abierto(self):
        oc = self._oc()
        sin_permiso = Usuario.objects.create(
            username="b@acme-rc.test", email="b@acme-rc.test", empresa=self.empresa,
            sucursal_default=self.sucursal,
        )
        api = APIClient()
        api.force_authenticate(user=sin_permiso)

        self.assertEqual(api.get(self._url(oc)).status_code, 200)
        self.assertEqual(api.patch(self._url(oc), {"comentarios": "x"}, format="json").status_code, 403)
        self.assertFalse(OrdenCompraRutaCritica.objects.exists())

    def test_get_sin_captura_devuelve_defaults_y_no_escribe(self):
        oc = self._oc()
        resp = self.api.get(self._url(oc))

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json(), {"oc_id": oc.pk, "fecha_real_recibo": None, "comentarios": None, "updated_at": None})
        self.assertFalse(OrdenCompraRutaCritica.objects.exists())

    def test_patch_guarda_audita_quien_y_no_toca_la_oc(self):
        oc = self._oc()
        antes = OrdenCompra.objects.filter(pk=oc.pk).values().get()
        historial = oc.history.count()

        resp = self.api.patch(
            self._url(oc), {"fecha_real_recibo": "2026-10-14", "comentarios": "  Llegó completo  "}, format="json",
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["fecha_real_recibo"], "2026-10-14")
        self.assertEqual(resp.json()["comentarios"], "Llegó completo")
        self.assertEqual(OrdenCompra.objects.filter(pk=oc.pk).values().get(), antes)
        self.assertEqual(oc.history.count(), historial)
        self.assertFalse(Recepcion.objects.exists())
        evento = AuditoriaEvento.objects.get(accion="RUTA_CRITICA")
        self.assertEqual(evento.usuario, self.usuario)
        self.assertEqual(evento.id_registro, str(oc.pk))
        self.assertEqual(evento.despues_json["fecha_real_recibo"], "2026-10-14")
        self.assertIsNone(evento.antes_json["fecha_real_recibo"])

    def test_sin_cambios_no_audita_y_se_puede_limpiar(self):
        oc = self._oc()
        self.api.patch(self._url(oc), {"comentarios": "uno"}, format="json")
        self.api.patch(self._url(oc), {"comentarios": "uno"}, format="json")
        self.assertEqual(AuditoriaEvento.objects.filter(accion="RUTA_CRITICA").count(), 1)

        resp = self.api.patch(self._url(oc), {"comentarios": ""}, format="json")
        self.assertIsNone(resp.json()["comentarios"])
        self.assertEqual(AuditoriaEvento.objects.filter(accion="RUTA_CRITICA").count(), 2)

    def test_fecha_invalida_es_400(self):
        oc = self._oc()
        resp = self.api.patch(self._url(oc), {"fecha_real_recibo": "no-es-fecha"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(AuditoriaEvento.objects.filter(accion="RUTA_CRITICA").exists())

    def test_aislamiento_entre_empresas(self):
        ajena = self._oc(self.otra, self.otra_sucursal)

        self.assertEqual(self.api.get(self._url(ajena)).status_code, 404)
        self.assertEqual(self.api.patch(self._url(ajena), {"comentarios": "x"}, format="json").status_code, 404)
        self.assertFalse(OrdenCompraRutaCritica.objects.exists())

    def test_consultas_constantes(self):
        oc = self._oc()
        with self.assertNumQueries(2):
            self.api.get(self._url(oc))


class OrdenCompraVariantesPTTests(TestCase):
    """OC de PT por SKU: validación contra ``tipo_compra`` (#792), variante en
    las lecturas (#793), encuadre RFID por variante (#794) y límites por lista
    del onboarding (#795)."""

    @classmethod
    def setUpTestData(cls):
        from catalogo.models import Talla
        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Una exhibición")
        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.empresa = Empresa.objects.create(codigo="acme-pt", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="PT", nombre="PT")
        cls.almacen = Almacen.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, codigo="A", nombre="A")
        cls.proveedor = Proveedor.objects.create(
            empresa=cls.empresa, nombre="P1", moneda=moneda, sat_regimen_fiscal=regimen,
            sat_forma_pago=forma, sat_metodo_pago=metodo, codigo="P1", razon_social="P1",
            telefono="8100000000", contacto_principal="Ana", rfc="XAXX010101000", email="p1@p.test",
        )
        pt = _tipo_producto()
        color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000")
        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Camisa", tipo=pt)
        cls.ch, cls.g = [
            ProductoVariante.objects.create(
                producto=cls.producto, empresa=cls.empresa, color=color, talla=Talla.objects.create(nombre=t),
                sku=f"CAM-{t}", precio_base="1",
            )
            for t in ("CH", "G")
        ]
        cls.baja = ProductoVariante.objects.create(
            producto=cls.producto, empresa=cls.empresa, color=color, sku="CAM-X", precio_base="1", activo=False,
        )
        cls.usuario = Usuario.objects.create(
            username="pt", email="pt@acme.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=self.usuario)

    def _crear(self, tipo, detalle):
        body = {"orden_compra": {"proveedor": self.proveedor.pk, "tipo_compra": tipo}, "detalle": detalle}
        return self.api.post(f"{ORDENES_URL}onboarding/", body, format="json")

    def _oc_pt(self):
        resp = self._crear("PT", [
            {"producto_variante": self.ch.pk, "cantidad": 10},
            {"producto_variante": self.g.pk, "cantidad": 10},
        ])
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_mp_con_sku_es_400(self):
        resp = self._crear("MP", [{"producto_variante": self.ch.pk, "cantidad": 1}])
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("detalle", resp.json())

    def test_pt_sin_sku_es_400_salvo_renglon_legado(self):
        resp = self._crear("PT", [{"producto": self.producto.pk, "cantidad": 1}])
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("detalle", resp.json())
        # OC legada (renglón sin variante de ese producto): editarla lo conserva.
        oc = OrdenCompra.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, proveedor=self.proveedor, moneda=self.proveedor.moneda,
            usuario=self.usuario, fecha_oc=timezone.now().date(), tipo_compra="PT",
        )
        OrdenCompraDetalle.objects.create(orden_compra=oc, producto=self.producto, sucursal=self.sucursal, cantidad=2)
        body = {"orden_compra_id": oc.pk, "orden_compra": {}, "detalle": [
            {"producto": self.producto.pk, "cantidad": 3}, {"producto_variante": self.g.pk, "cantidad": 1},
        ]}
        resp = self.api.post(f"{ORDENES_URL}onboarding/", body, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_variante_inactiva_es_400(self):
        resp = self._crear("PT", [{"producto_variante": self.baja.pk, "cantidad": 1}])
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_pasar_a_mp_con_renglones_por_sku_es_400(self):
        oc = self._oc_pt()["orden_compra"]
        resp = self.api.put(f"{ORDENES_URL}{oc['id']}/", {"tipo_compra": "MP"}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("tipo_compra", resp.json())

    def test_lecturas_traen_sku_y_talla(self):
        data = self._oc_pt()
        self.assertEqual([(d["producto_variante_sku"], d["producto_variante_talla"]) for d in data["detalle"]],
                         [("CAM-CH", "CH"), ("CAM-G", "G")])
        detalles = self.api.get(f"{ORDENES_URL}{data['orden_compra']['id']}/").json()["detalles"]
        self.assertEqual([d["producto_variante_color"] for d in detalles], ["Negro", "Negro"])

    def test_rfid_asigna_el_tag_al_renglon_de_su_talla(self):
        oc_id = self._oc_pt()["orden_compra"]["id"]
        url = "/api/v1/compras/recepcion-rfid-encuadres/"
        enc = self.api.post(url, {"tipo_origen": "OC", "orden_compra": oc_id, "almacen": self.almacen.pk}, format="json").json()
        for i in range(3):
            self.api.post(f"{url}{enc['id']}/lecturas/", {"codigo_tag": f"CAM-G EPC{i}"}, format="json")
        resumen = self.api.get(f"{url}{enc['id']}/").json()["resumen"]
        self.assertEqual([(d["producto_variante_talla"], d["leido"]) for d in resumen["detalle"]],
                         [("CH", "0"), ("G", "3.0000")])

    def test_onboarding_limita_y_busca_por_lista(self):
        busqueda = self.api.get(f"{ORDENES_URL}onboarding/", {
            "tipo_compra": "PT", "variante_q": "CAM-G", "producto_limit": 1, "proveedor_limit": 1,
        }).json()["busqueda"]
        self.assertEqual([v["sku"] for v in busqueda["variantes"]], ["CAM-G"])
        self.assertEqual((len(busqueda["proveedores"]), len(busqueda["productos"])), (1, 1))


class ComprasDashboardRetiradoTests(TestCase):
    """EC-784: ``GET /compras/dashboard/`` se retiró (ignoraba ``puede_ver_importes``,
    al superusuario sin empresa le mostraba todas y sumaba monedas en
    ``top_proveedores``). El dashboard de Compras lo servirá ``ordenes/kpis/``."""

    def test_la_ruta_ya_no_existe(self):
        empresa = Empresa.objects.create(codigo="ec784-dash", razon_social="EC784 Dashboard SA")
        usuarios = (
            Usuario.objects.create(username="dash-u", email="dash-u@ec784.test", empresa=empresa),
            Usuario.objects.create(username="dash-su", email="dash-su@ec784.test", is_superuser=True),
        )
        for usuario in usuarios:
            with self.subTest(usuario=usuario.username):
                api = APIClient()
                api.force_authenticate(usuario)
                self.assertEqual(api.get("/api/v1/compras/dashboard/").status_code, 404)


class FiltroImportesOrdenCompraTests(TestCase):
    """EC-784: ``filtrar_campos_contabilidad_orden_compra`` falla cerrado. Un valor que
    ``deepcopy`` no puede copiar hacía que devolviera la OC sin filtrar; ahora quita
    los importes igual y no toca el original. Con permiso, la devuelve tal cual."""

    def test_falla_cerrado_y_no_modifica_el_original(self):
        import threading

        from compras.services.orden_compra_view_service import filtrar_campos_contabilidad_orden_compra

        empresa = Empresa.objects.create(codigo="ec784-oc", razon_social="EC784 OC SA")
        sin_permiso = Usuario.objects.create(username="oc-sin", email="oc-sin@ec784.test", empresa=empresa)
        con_permiso = Usuario.objects.create(
            username="oc-admin", email="oc-admin@ec784.test", empresa=empresa, is_admin_empresa=True,
        )
        data = {
            "id": 1, "folio": "OC-1", "gran_total": "116.00", "subtotal": "100.00", "no_copiable": threading.Lock(),
            "detalles": [{"id": 2, "cantidad": "1", "precio": "100.00", "importe": "100.00"}],
            "recepciones": [{"id": 3, "detalles": [{"id": 4, "cantidad": "1"}]}],
        }
        filtrado = filtrar_campos_contabilidad_orden_compra(data, sin_permiso)
        for campo in ("gran_total", "subtotal"):
            self.assertNotIn(campo, filtrado)
        self.assertNotIn("precio", filtrado["detalles"][0])
        self.assertNotIn("importe", filtrado["detalles"][0])
        self.assertEqual((filtrado["folio"], filtrado["detalles"][0]["cantidad"]), ("OC-1", "1"))
        self.assertEqual(filtrado["recepciones"][0]["detalles"][0]["id"], 4)
        # El original queda intacto.
        self.assertEqual((data["gran_total"], data["detalles"][0]["precio"]), ("116.00", "100.00"))
        self.assertIs(filtrar_campos_contabilidad_orden_compra(data, con_permiso), data)


class _OrdenesModoServidorBase:
    """Fixtures de EC-782 en compras: una OC por estatus en ACME, MXN y USD, una OC
    sin proveedor y una de otra empresa."""

    URL = "/api/v1/compras/ordenes/"

    @classmethod
    def setUpTestData(cls):
        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General de Ley")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Pago en una sola exhibición")
        cls.mxn = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.usd = Moneda.objects.create(codigo_iso="USD", nombre="Dólar")
        cls.empresa = Empresa.objects.create(codigo="acme-oc782", razon_social="ACME OC SA")
        cls.otra = Empresa.objects.create(codigo="globex-oc782", razon_social="Globex OC SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="AOC", nombre="Matriz")
        cls.sucursal_otra = Sucursal.objects.create(empresa=cls.otra, codigo="GOC", nombre="Globex")
        datos = dict(
            sat_regimen_fiscal=regimen, sat_forma_pago=forma, sat_metodo_pago=metodo,
            telefono="8100000000", contacto_principal="Contacto", rfc="XAXX010101000",
        )
        cls.proveedor = Proveedor.objects.create(
            empresa=cls.empresa, nombre="Telas Norte", moneda=cls.mxn, codigo="PRV-782", razon_social="Telas SA",
            email="t@acme.test", **datos,
        )
        cls.proveedor_otra = Proveedor.objects.create(
            empresa=cls.otra, nombre="Secreto", moneda=cls.mxn, codigo="PRV-G782", razon_social="Secreto SA",
            email="s@globex.test", **datos,
        )
        cls.admin = Usuario.objects.create(
            username="adm@acme-oc782.test", email="adm@acme-oc782.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.almacenista = Usuario.objects.create(
            username="alm@acme-oc782.test", email="alm@acme-oc782.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal,
        )
        cls.admin_otra = Usuario.objects.create(
            username="adm@globex-oc782.test", email="adm@globex-oc782.test", empresa=cls.otra,
            sucursal_default=cls.sucursal_otra, is_admin_empresa=True,
        )
        E = OrdenCompra.EstatusOrdenCompra
        hoy = timezone.localdate()
        cls.ocs = {}
        for estatus, total, moneda, entrega in (
            (E.BORRADOR, "100", cls.mxn, None),
            (E.POR_AUTORIZAR, "200", cls.mxn, None),
            (E.AUTORIZADA, "300", cls.mxn, hoy + timedelta(days=9)),
            (E.PARCIALMENTE_RECIBIDA, "400", cls.mxn, hoy + timedelta(days=5)),
            (E.RECIBIDA, "500", cls.usd, None),
            (E.CANCELADA, "99999", cls.mxn, None),
        ):
            cls.ocs[estatus] = cls._oc(estatus, total, moneda, entrega)
        # Otra parcial sin fecha (al final de "en curso") y una sin proveedor.
        cls.oc_parcial_sin_fecha = cls._oc(E.PARCIALMENTE_RECIBIDA, "50", cls.usd, None)
        cls.oc_sin_proveedor = cls._oc(E.AUTORIZADA, "10", cls.mxn, None, proveedor=None)
        cls.oc_otra = OrdenCompra.objects.create(
            empresa=cls.otra, sucursal=cls.sucursal_otra, proveedor=cls.proveedor_otra, moneda=cls.mxn,
            usuario=cls.admin_otra, fecha_oc=hoy, estatus=E.AUTORIZADA, gran_total=Decimal("777"),
        )

    @classmethod
    def _oc(cls, estatus, total, moneda, entrega, proveedor="default"):
        return OrdenCompra.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, moneda=moneda, usuario=cls.admin,
            proveedor=cls.proveedor if proveedor == "default" else proveedor,
            fecha_oc=timezone.localdate(), estatus=estatus, gran_total=Decimal(total),
            fecha_entrega_estimada=entrega,
        )

    def _get(self, url, params=None, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.admin)
        return client.get(url, params or {})

    def _ids(self, params, user=None, url=None):
        resp = self._get(url or self.URL, params, user)
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        return sorted(r["id"] for r in (data["results"] if isinstance(data, dict) else data))


class EstatusMultivalorListadosComprasTests(_OrdenesModoServidorBase, TestCase):
    """EC-782: ``?estatus=`` de OC y recepciones acepta uno o varios separados por coma
    (``filtrar_listado_compras``); un solo entero sigue igual."""

    def test_ordenes(self):
        E = OrdenCompra.EstatusOrdenCompra
        self.assertEqual(self._ids({"estatus": "3"}), sorted([self.ocs[E.AUTORIZADA].pk, self.oc_sin_proveedor.pk]))
        self.assertEqual(
            self._ids({"estatus": "3,4"}),
            sorted([self.ocs[E.AUTORIZADA].pk, self.oc_sin_proveedor.pk, self.ocs[E.PARCIALMENTE_RECIBIDA].pk,
                    self.oc_parcial_sin_fecha.pk]),
        )
        self.assertEqual(self._ids({"estatus": " 5 , 6 ,"}), sorted([self.ocs[E.RECIBIDA].pk, self.ocs[E.CANCELADA].pk]))
        # Paginado también; y otra empresa no se cuela.
        self.assertEqual(self._get(self.URL, {"estatus": "3,4", "limit": 2}).json()["count"], 4)
        self.assertEqual(self._ids({"estatus": "3,4"}, self.admin_otra), [self.oc_otra.pk])

    def test_ordenes_invalidos(self):
        for valor, mensaje in (("99", "Estatus inexistente."), ("3,99", "Estatus inexistente."),
                               ("abc", "Debe ser un entero válido."), ("3,x", "Debe ser un entero válido."),
                               (",", "Debe ser un entero válido.")):
            with self.subTest(valor=valor):
                resp = self._get(self.URL, {"estatus": valor})
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json(), {"estatus": mensaje})

    def test_recepciones(self):
        almacen = Almacen.objects.create(empresa=self.empresa, sucursal=self.sucursal, codigo="A-782", nombre="MP")
        R = Recepcion.EstatusRecepcion
        recepciones = {
            estatus: Recepcion.objects.create(
                empresa=self.empresa, sucursal=self.sucursal, almacen=almacen, usuario=self.admin,
                folio=f"RC-782-{estatus}", fecha_recepcion=timezone.now(), estatus=estatus,
            )
            for estatus in (R.BORRADOR, R.RECIBIDA, R.CANCELADA)
        }
        self.assertEqual(self._ids({"estatus": "2"}, url=RECEPCIONES_URL), [recepciones[R.RECIBIDA].pk])
        self.assertEqual(
            self._ids({"estatus": "1,6"}, url=RECEPCIONES_URL),
            sorted([recepciones[R.BORRADOR].pk, recepciones[R.CANCELADA].pk]),
        )
        self.assertEqual(self._get(RECEPCIONES_URL, {"estatus": "9"}).status_code, 400)
        self.assertEqual(self._get(RECEPCIONES_URL, {"estatus": "1,x"}).status_code, 400)


class OrdenCompraConProveedorTests(_OrdenesModoServidorBase, TestCase):
    """EC-782: ``?con_proveedor=`` para el selector de OC de facturas de proveedor."""

    def test_filtro(self):
        todas = self._ids({})
        con = self._ids({"con_proveedor": "true"})
        self.assertNotIn(self.oc_sin_proveedor.pk, con)
        self.assertEqual(sorted(con + [self.oc_sin_proveedor.pk]), todas)
        self.assertEqual(self._ids({"con_proveedor": "false"}), [self.oc_sin_proveedor.pk])
        # El selector: con proveedor y sin canceladas.
        self.assertEqual(len(self._ids({"con_proveedor": "true", "estatus": "1,2,3,4,5"})), 6)
        self.assertEqual(self._get(self.URL, {"con_proveedor": "tal-vez"}).status_code, 400)


class OrdenCompraPedidosVinculadosEstatusTests(_OrdenesModoServidorBase, TestCase):
    """EC-782: ``pedidos_vinculados`` trae ``estatus``, ``activo`` y ``cliente_nombre``
    del pedido, sin consultas extra."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from terceros.models import Cliente
        from ventas.models import Pedido

        cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente 782")

        def pedido(folio, **extra):
            return Pedido.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal, cliente=cliente, moneda=cls.mxn, folio=folio,
                persona_pagos="P", correo_facturas="p@acme.test", telefono_pagos="8100000000",
                forma_pago="03", metodo_pago="PUE", uso_cfdi="G03", cliente_nombre="Cliente 782", **extra,
            )

        cls.vigente = pedido("P-V782", estatus=3)
        cls.cancelado = pedido("P-C782", estatus=Pedido.ESTATUS_CANCELADO)
        cls.baja = pedido("P-B782", estatus=3, activo=False)
        cls.oc = cls.ocs[OrdenCompra.EstatusOrdenCompra.AUTORIZADA]
        cls.oc.pedido = cls.vigente
        cls.oc.save(update_fields=["pedido"])
        cls.oc.pedidos.set([cls.vigente, cls.cancelado, cls.baja])

    def test_listado_y_detalle(self):
        esperado = [
            {"id": self.vigente.pk, "folio": "P-V782", "estatus": 3, "activo": True, "cliente_nombre": "Cliente 782"},
            {"id": self.cancelado.pk, "folio": "P-C782", "estatus": 5, "activo": True, "cliente_nombre": "Cliente 782"},
            {"id": self.baja.pk, "folio": "P-B782", "estatus": 3, "activo": False, "cliente_nombre": "Cliente 782"},
        ]
        detalle = self._get(f"{self.URL}{self.oc.pk}/").json()
        self.assertEqual(detalle["pedidos_vinculados"], esperado)
        fila = next(r for r in self._get(self.URL).json() if r["id"] == self.oc.pk)
        self.assertEqual(fila["pedidos_vinculados"], esperado)
        self.assertEqual(fila["pedidos"], [p["id"] for p in esperado])

    def test_legado_solo_fk(self):
        oc = self.ocs[OrdenCompra.EstatusOrdenCompra.BORRADOR]
        oc.pedido = self.cancelado
        oc.save(update_fields=["pedido"])
        detalle = self._get(f"{self.URL}{oc.pk}/").json()
        self.assertEqual([(p["id"], p["estatus"]) for p in detalle["pedidos_vinculados"]], [(self.cancelado.pk, 5)])

    def test_sin_consultas_extra(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._get(self.URL)
        with CaptureQueriesContext(connection) as antes:
            self._get(self.URL)
        for oc in self.ocs.values():
            oc.pedidos.add(self.vigente, self.cancelado)
        with CaptureQueriesContext(connection) as despues:
            self._get(self.URL)
        self.assertEqual(len(despues), len(antes))


class OrdenCompraKpisDashboardTests(_OrdenesModoServidorBase, TestCase):
    """EC-782: ``kpis/`` cubre el dashboard de Compras sin cambiar sus claves previas."""

    URL_KPIS = "/api/v1/compras/ordenes/kpis/"

    def test_claves_previas_intactas(self):
        data = self._get(self.URL_KPIS).json()
        self.assertLessEqual(
            {"generado_en", "ocs_abiertas", "ocs_vencidas_sin_recibir", "ciclo_compra", "gasto_por_categoria",
             "puede_ver_importes"},
            set(data),
        )
        self.assertEqual(data["ocs_abiertas"]["total"], 6)  # 1, 2, 3 (x2), 4 (x2)

    def test_grupos_y_valor(self):
        data = self._get(self.URL_KPIS).json()
        self.assertEqual(data["ocs_por_grupo"], {
            "disponible": True, "activas": 7, "por_autorizar": 2, "autorizadas_o_completadas": 5,
            "en_curso": 2, "recibidas": 1, "canceladas": 1,
        })
        valor = {f["moneda"]: f for f in data["valor_ocs_activas"]["por_moneda"]}
        self.assertEqual(data["valor_ocs_activas"]["total"], 7)
        self.assertEqual((valor["MXN"]["total"], Decimal(str(valor["MXN"]["monto"]))), (5, Decimal("1010")))
        self.assertEqual((valor["USD"]["total"], Decimal(str(valor["USD"]["monto"]))), (2, Decimal("550")))

    def test_top_por_moneda(self):
        E = OrdenCompra.EstatusOrdenCompra
        monedas = {m["moneda"]: m["ocs"] for m in self._get(self.URL_KPIS).json()["top_ocs_por_moneda"]["monedas"]}
        # Sin la cancelada de 99,999; las 5 mayores de MXN y las 2 de USD.
        self.assertEqual([o["id"] for o in monedas["MXN"]], [
            self.ocs[E.PARCIALMENTE_RECIBIDA].pk, self.ocs[E.AUTORIZADA].pk, self.ocs[E.POR_AUTORIZAR].pk,
            self.ocs[E.BORRADOR].pk, self.oc_sin_proveedor.pk,
        ])
        self.assertEqual([o["id"] for o in monedas["USD"]], [self.ocs[E.RECIBIDA].pk, self.oc_parcial_sin_fecha.pk])
        primera = monedas["MXN"][0]
        self.assertEqual(
            (primera["moneda_codigo"], primera["proveedor_nombre"], Decimal(str(primera["gran_total"]))),
            ("MXN", "Telas Norte", Decimal("400")),
        )
        self.assertIsNone(monedas["MXN"][4]["proveedor_nombre"])

    def test_en_curso_por_entrega_mas_proxima(self):
        E = OrdenCompra.EstatusOrdenCompra
        drill = self._get(self.URL_KPIS).json()["ocs_en_curso_proxima_entrega"]["drill_down"]
        self.assertEqual([o["id"] for o in drill], [self.ocs[E.PARCIALMENTE_RECIBIDA].pk, self.oc_parcial_sin_fecha.pk])
        self.assertEqual(drill[0]["proveedor_nombre"], "Telas Norte")
        self.assertIsNone(drill[1]["fecha_entrega_estimada"])

    def test_en_curso_marca_el_tope(self):
        """Mismas llaves que los demás drill-downs (``marcar_tope``): tope de 8."""
        bloque = self._get(self.URL_KPIS).json()["ocs_en_curso_proxima_entrega"]
        self.assertEqual((bloque["drill_down_total"], bloque["drill_down_truncado"]), (2, False))
        for i in range(7):
            self._oc(OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA, "1", self.mxn, None)
        bloque = self._get(self.URL_KPIS).json()["ocs_en_curso_proxima_entrega"]
        self.assertEqual(len(bloque["drill_down"]), 8)
        self.assertEqual((bloque["drill_down_total"], bloque["drill_down_truncado"]), (9, True))

    def test_sin_permiso_sin_importes(self):
        data = self._get(self.URL_KPIS, user=self.almacenista).json()
        self.assertFalse(data["puede_ver_importes"])
        self.assertTrue(all("monto" not in f for f in data["valor_ocs_activas"]["por_moneda"]))
        self.assertEqual(data["valor_ocs_activas"]["total"], 7)
        self.assertEqual(data["top_ocs_por_moneda"], {"disponible": False, "motivo": "Sin permiso de contabilidad."})
        # Conteos y "en curso" no llevan importes.
        self.assertEqual(data["ocs_por_grupo"]["activas"], 7)
        self.assertEqual(len(data["ocs_en_curso_proxima_entrega"]["drill_down"]), 2)

    def test_otra_empresa_y_sin_empresa(self):
        data = self._get(self.URL_KPIS, user=self.admin_otra).json()
        self.assertEqual(data["ocs_por_grupo"]["activas"], 1)
        self.assertEqual([o["id"] for m in data["top_ocs_por_moneda"]["monedas"] for o in m["ocs"]], [self.oc_otra.pk])
        self.assertNotIn("Telas Norte", str(data))
        sin_empresa = Usuario.objects.create(username="se@oc782.test", email="se@oc782.test")
        data = self._get(self.URL_KPIS, user=sin_empresa).json()
        motivo = {"disponible": False, "motivo": "Usuario sin empresa asignada."}
        for clave in ("ocs_por_grupo", "valor_ocs_activas", "top_ocs_por_moneda", "ocs_en_curso_proxima_entrega"):
            self.assertEqual(data[clave], motivo)

    def test_numero_de_consultas(self):
        """``ocs_abiertas``, ``ocs_por_grupo`` y ``valor_ocs_activas`` salen de UNA consulta
        agrupada por estatus y moneda: 6 en total para un admin (antes 9). Las demás:
        vencidas (conteo + drill-down), gasto por categoría, top por moneda y en curso."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._get(self.URL_KPIS)
        with CaptureQueriesContext(connection) as ctx:
            self._get(self.URL_KPIS)
        self.assertEqual(len(ctx), 6, [q["sql"][:120] for q in ctx.captured_queries])

    def test_acepta_format_y_rechaza_filtros(self):
        """``validar_parametros`` deja pasar ``?format=`` de DRF en todos los ``kpis/``."""
        self.assertEqual(self._get(self.URL_KPIS, {"format": "json"}).status_code, 200)
        self.assertEqual(self._get(RECEPCIONES_URL + "kpis/", {"format": "json"}).status_code, 200)
        self.assertEqual(self._get(self.URL_KPIS, {"sucursal": "1"}).status_code, 400)

    def test_consultas_no_crecen_con_las_filas(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._get(self.URL_KPIS)
        with CaptureQueriesContext(connection) as antes:
            self._get(self.URL_KPIS)
        for i in range(6):
            self._oc(OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA, str(i + 1), self.usd, None)
        with CaptureQueriesContext(connection) as despues:
            self._get(self.URL_KPIS)
        self.assertEqual(len(despues), len(antes))


class _EncuadresRFIDFixture:
    """Encuadres RFID de OC y de OP con 0, 1 y varias lecturas, recibido parcial,
    rechazo de Calidad y una recepción cancelada que no debe contar (EC-778)."""

    URL_ENCUADRES = "/api/v1/compras/recepcion-rfid-encuadres/"

    @classmethod
    def setUpTestData(cls):
        from catalogo.models import Talla
        from nucleo.models import UnidadMedida
        from produccion.models import ListaMaterialBom

        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Una exhibición")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.unidad = UnidadMedida.objects.create(clave="PZA-778", nombre="Pieza")
        color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000")
        tallas = [Talla.objects.create(nombre=t) for t in ("CH", "M", "G")]
        tipo = _tipo_producto()
        cls.folios = 0
        cls.t = {}
        for clave in ("a", "b"):
            empresa = Empresa.objects.create(codigo=f"ec778-{clave}", razon_social=f"EC778 {clave} SA")
            sucursal = Sucursal.objects.create(empresa=empresa, codigo=f"E7{clave}", nombre=f"Suc {clave}")
            departamento = Departamento.objects.create(
                empresa=empresa, sucursal=sucursal, codigo="CAL", nombre="Calidad",
            )
            producto = Producto.objects.create(empresa=empresa, nombre=f"Playera {clave}", tipo=tipo)
            variantes = [
                ProductoVariante.objects.create(
                    producto=producto, empresa=empresa, color=color, talla=talla,
                    sku=f"PLA-{clave}-{talla.nombre}", precio_base="1",
                )
                for talla in tallas
            ]
            cls.t[clave] = {
                "empresa": empresa,
                "sucursal": sucursal,
                "almacen": Almacen.objects.create(
                    empresa=empresa, sucursal=sucursal, codigo="ALM", nombre=f"Almacén {clave}",
                ),
                "proveedor": Proveedor.objects.create(
                    empresa=empresa, nombre=f"Proveedor {clave}", moneda=cls.moneda, sat_regimen_fiscal=regimen,
                    sat_forma_pago=forma, sat_metodo_pago=metodo, codigo=f"P778{clave}", razon_social="P",
                    telefono="8100000000", contacto_principal="Ana", rfc="XAXX010101000",
                    email=f"p778{clave}@p.test",
                ),
                "usuario": Usuario.objects.create(
                    username=f"rfid778-{clave}", email=f"rfid778-{clave}@ec778.test", empresa=empresa,
                    sucursal_default=sucursal, is_admin_empresa=True,
                ),
                "producto": producto,
                "variantes": variantes,
                "bom": ListaMaterialBom.objects.create(empresa=empresa, producto_variante=variantes[0], activo=True),
                "inspector": Empleado.objects.create(
                    empresa=empresa, sucursal=sucursal, departamento=departamento,
                    puesto=Puesto.objects.create(empresa=empresa, nombre="Inspector"),
                    numero_empleado=f"EMP-778{clave}", nombre="Ana", apellido_paterno="Lopez",
                    fecha_ingreso=timezone.localdate(),
                ),
            }

        # OC con dos renglones: recibido 4 del primero, 1 rechazado por Calidad
        # y 5 en una recepción cancelada que no cuenta.
        cls.oc1, (cls.r1, r2) = cls._oc("a", [10, 10])
        rd = cls._recibir("a", cls.r1, 4, orden_compra=cls.oc1)
        cls._rechazar("a", rd, 1)
        cls._recibir("a", cls.r1, 5, orden_compra=cls.oc1, estatus=Recepcion.EstatusRecepcion.CANCELADA)
        cls.e_oc_varias = cls._encuadre("a", orden_compra=cls.oc1)
        cls._leer(cls.e_oc_varias, cls.r1, 2)
        cls._leer(cls.e_oc_varias, r2, 1)
        cls._leer(cls.e_oc_varias, None, 1)  # tag sin resolver: suma a total_sin_asignar
        cls.e_oc_vacio = cls._encuadre("a", orden_compra=cls.oc1)  # misma OC, cero lecturas
        # OP con dos renglones: recibido 6 del primero y 2 rechazados.
        cls.op1, (d1, d2) = cls._op("a", [20, 5])
        rd = cls._recibir("a", d1, 6, op=cls.op1)
        cls._rechazar("a", rd, 2)
        cls.e_op_una = cls._encuadre("a", op=cls.op1)
        cls._leer(cls.e_op_una, d2, 1)
        oc2, (r3,) = cls._oc("a", [3])
        cls.e_oc_una = cls._encuadre("a", orden_compra=oc2)
        cls._leer(cls.e_oc_una, r3, 1)
        # Otra empresa.
        oc_b, (rb,) = cls._oc("b", [7])
        cls.e_b = cls._encuadre("b", orden_compra=oc_b)
        cls._leer(cls.e_b, rb, 1)
        # Orden del listado: -created_at, -id.
        cls.ids_a = [cls.e_oc_una.pk, cls.e_op_una.pk, cls.e_oc_vacio.pk, cls.e_oc_varias.pk]

    @classmethod
    def _oc(cls, clave, cantidades):
        t = cls.t[clave]
        oc = OrdenCompra.objects.create(
            empresa=t["empresa"], sucursal=t["sucursal"], proveedor=t["proveedor"], moneda=cls.moneda,
            usuario=t["usuario"], fecha_oc=timezone.localdate(), tipo_compra="PT",
        )
        renglones = [
            OrdenCompraDetalle.objects.create(
                orden_compra=oc, producto=t["producto"], producto_variante=t["variantes"][i],
                sucursal=t["sucursal"], cantidad=cantidad,
            )
            for i, cantidad in enumerate(cantidades)
        ]
        return oc, renglones

    @classmethod
    def _op(cls, clave, cantidades):
        from produccion.models import OrdenProduccion, OrdenProduccionDetalle

        t = cls.t[clave]
        cls.folios += 1
        op = OrdenProduccion.objects.create(
            empresa=t["empresa"], sucursal=t["sucursal"], folio_op=f"OP-778-{clave}-{cls.folios}",
        )
        renglones = [
            OrdenProduccionDetalle.objects.create(
                op=op, bom=t["bom"], cantidad=cantidad, unidad=cls.unidad, producto_variante=t["variantes"][i],
            )
            for i, cantidad in enumerate(cantidades)
        ]
        return op, renglones

    @classmethod
    def _recibir(cls, clave, renglon, cantidad, orden_compra=None, op=None,
                 estatus=Recepcion.EstatusRecepcion.RECIBIDA):
        t = cls.t[clave]
        cls.folios += 1
        recepcion = Recepcion.objects.create(
            tipo_origen="OC" if orden_compra else "OP", orden_compra=orden_compra, op=op,
            empresa=t["empresa"], sucursal=t["sucursal"], proveedor=t["proveedor"] if orden_compra else None,
            almacen=t["almacen"], usuario=t["usuario"], folio=f"RC-778-{cls.folios}",
            fecha_recepcion=timezone.now(), estatus=estatus,
        )
        return RecepcionDetalle.objects.create(
            recepcion=recepcion, producto=t["producto"], cantidad_recibida=cantidad,
            orden_compra_detalle=renglon if orden_compra else None,
            orden_produccion_detalle=renglon if op else None,
        )

    @classmethod
    def _rechazar(cls, clave, recepcion_detalle, cantidad):
        inspeccion = CalidadInspeccion.objects.create(
            recepcion=recepcion_detalle.recepcion, inspector=cls.t[clave]["inspector"],
            fecha=timezone.localdate(), estado="rechazada",
        )
        CalidadInspeccionDetalle.objects.create(
            calidad_inspeccion=inspeccion, recepcion_detalle=recepcion_detalle,
            cantidad_inspeccionada=recepcion_detalle.cantidad_recibida,
            cantidad_aprobada=recepcion_detalle.cantidad_recibida - cantidad,
            cantidad_rechazada=cantidad, resultado="rechazo", motivo_rechazo="x",
        )

    @classmethod
    def _encuadre(cls, clave, orden_compra=None, op=None):
        t = cls.t[clave]
        return RecepcionRFIDEncuadre.objects.create(
            tipo_origen="OC" if orden_compra else "OP", orden_compra=orden_compra, op=op,
            empresa=t["empresa"], sucursal=t["sucursal"], proveedor=t["proveedor"] if orden_compra else None,
            almacen=t["almacen"], usuario=t["usuario"], fecha_recepcion=timezone.now(),
        )

    @classmethod
    def _leer(cls, encuadre, renglon, n):
        """``n`` lecturas del renglón (OC u OP según el encuadre); sin renglón, tags sin resolver."""
        variante = renglon.producto_variante if renglon is not None else None
        inicio = encuadre.lecturas.count()
        for i in range(inicio, inicio + n):
            RecepcionRFIDLectura.objects.create(
                encuadre=encuadre, codigo_tag=f"TAG-{encuadre.pk}-{i}",
                orden_compra_detalle=renglon if renglon is not None and encuadre.orden_compra_id else None,
                orden_produccion_detalle=renglon if renglon is not None and encuadre.op_id else None,
                producto=variante.producto if variante else None, producto_variante=variante,
                cantidad_leida=Decimal("1"),
            )

    def _agregar_lote(self, n_encuadres=3, n_lecturas=5):
        """Más encuadres (OC y OP alternados) con varias lecturas cada uno."""
        for i in range(n_encuadres):
            if i % 2:
                op, renglones = self._op("a", [8, 8])
                encuadre = self._encuadre("a", op=op)
            else:
                oc, renglones = self._oc("a", [8, 8, 8])
                encuadre = self._encuadre("a", orden_compra=oc)
                self._recibir("a", renglones[0], 2, orden_compra=oc)
            for j in range(n_lecturas):
                self._leer(encuadre, renglones[j % len(renglones)], 1)

    def _api(self, clave="a"):
        api = APIClient()
        api.force_authenticate(self.t[clave]["usuario"])
        return api


class RecepcionRFIDEncuadreConsultasTests(_EncuadresRFIDFixture, TestCase):
    """EC-778: el listado y el detalle de encuadres RFID no crecen en consultas con
    el número de encuadres ni de lecturas, y el resumen en bloque del listado es el
    mismo que el que el detalle calcula encuadre por encuadre."""

    # Listado: encuadres; lecturas (con producto y variante por JOIN); renglones
    # de OC y de OP; recibido + rechazado de cada lado. Detalle: encuadre,
    # lecturas, renglones, recibido y rechazado.
    CONSULTAS_LISTADO = 8
    CONSULTAS_DETALLE = 5

    def test_listado_no_crece_con_encuadres_ni_lecturas(self):
        api = self._api()
        with self.assertNumQueries(self.CONSULTAS_LISTADO):
            self.assertEqual(len(api.get(self.URL_ENCUADRES).json()), 4)
        self._agregar_lote(n_encuadres=4, n_lecturas=6)
        with self.assertNumQueries(self.CONSULTAS_LISTADO):
            self.assertEqual(len(api.get(self.URL_ENCUADRES).json()), 8)

    def test_detalle_no_crece_con_lecturas(self):
        api = self._api()
        for encuadre in (self.e_oc_vacio, self.e_oc_una, self.e_oc_varias, self.e_op_una):
            with self.subTest(encuadre=encuadre.pk), self.assertNumQueries(self.CONSULTAS_DETALLE):
                self.assertEqual(api.get(f"{self.URL_ENCUADRES}{encuadre.pk}/").status_code, 200)
        self._leer(self.e_oc_varias, self.r1, 12)
        with self.assertNumQueries(self.CONSULTAS_DETALLE):
            self.assertEqual(api.get(f"{self.URL_ENCUADRES}{self.e_oc_varias.pk}/").status_code, 200)

    def test_aceptar_no_crece_con_lecturas(self):
        api = self._api()
        with CaptureQueriesContext(connection) as pocas:
            self.assertEqual(api.post(f"{self.URL_ENCUADRES}{self.e_oc_una.pk}/aceptar/").status_code, 200)
        with CaptureQueriesContext(connection) as muchas:
            self.assertEqual(api.post(f"{self.URL_ENCUADRES}{self.e_oc_varias.pk}/aceptar/").status_code, 200)
        self.assertEqual(len(muchas), len(pocas))

    def test_listado_igual_al_detalle_de_cada_encuadre(self):
        """OC y OP, con y sin lecturas; las lecturas en orden de captura (id)."""
        self._agregar_lote(n_encuadres=3, n_lecturas=4)
        api = self._api()
        listado = api.get(self.URL_ENCUADRES).json()
        self.assertEqual(len(listado), 7)
        for fila in listado:
            with self.subTest(encuadre=fila["id"]):
                ids_lecturas = [l["id"] for l in fila["lecturas"]]
                self.assertEqual(ids_lecturas, sorted(ids_lecturas))
                self.assertEqual(fila, api.get(f"{self.URL_ENCUADRES}{fila['id']}/").json())

    def test_resumen_resta_recibido_y_rechazo_sin_cancelados(self):
        listado = {e["id"]: e for e in self._api().get(self.URL_ENCUADRES).json()}
        oc = listado[self.e_oc_varias.pk]["resumen"]
        self.assertEqual(
            [(d["ordenado"], d["ya_recibido"], d["esperado"], d["leido"], d["diferencia"]) for d in oc["detalle"]],
            [("10", "3.0000", "7.0000", "2.0000", "5.0000"), ("10", "0", "10", "1.0000", "9.0000")],
        )
        self.assertEqual(
            (oc["total_esperado"], oc["total_leido"], oc["total_sin_asignar"], oc["completo"]),
            ("17.0000", "4.0000", "1.0000", False),
        )
        vacio = listado[self.e_oc_vacio.pk]
        self.assertEqual(vacio["lecturas"], [])
        self.assertEqual([d["esperado"] for d in vacio["resumen"]["detalle"]], ["7.0000", "10"])
        self.assertEqual((vacio["resumen"]["total_leido"], vacio["resumen"]["total_sin_asignar"]), ("0", "0"))
        op = listado[self.e_op_una.pk]
        self.assertEqual(
            [(d["ordenado"], d["ya_recibido"], d["esperado"], d["leido"]) for d in op["resumen"]["detalle"]],
            [("20.00", "4.0000", "16.0000", "0"), ("5.00", "0", "5.00", "1.0000")],
        )
        self.assertEqual([(l["producto_nombre"], l["producto_variante_nombre"]) for l in op["lecturas"]],
                         [("Playera a", self.t["a"]["variantes"][1].nombre)])

    def test_aislamiento_por_empresa(self):
        api = self._api()
        self.assertEqual([e["id"] for e in api.get(self.URL_ENCUADRES).json()], self.ids_a)
        self.assertEqual(api.get(f"{self.URL_ENCUADRES}{self.e_b.pk}/").status_code, 404)
        resp = api.post(f"{self.URL_ENCUADRES}{self.e_b.pk}/lecturas/", {"codigo_tag": "PLA-b-CH"}, format="json")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(api.post(f"{self.URL_ENCUADRES}{self.e_b.pk}/aceptar/").status_code, 404)
        self.e_b.refresh_from_db()
        self.assertEqual((self.e_b.lecturas.count(), self.e_b.estatus), (1, "PENDIENTE"))
        self.assertEqual([e["id"] for e in self._api("b").get(self.URL_ENCUADRES).json()], [self.e_b.pk])

        sin_empresa = APIClient()
        sin_empresa.force_authenticate(Usuario.objects.create(username="sin778", email="sin778@ec778.test"))
        self.assertEqual(sin_empresa.get(self.URL_ENCUADRES).json(), [])
        # EC-798 (2ad952c): el superusuario ve solo su empresa activa; sin empresa, nada.
        root = APIClient()
        root.force_authenticate(Usuario.objects.create(username="su778", email="su778@ec778.test", is_superuser=True))
        self.assertEqual(root.get(self.URL_ENCUADRES).json(), [])
        root_a = APIClient()
        root_a.force_authenticate(Usuario.objects.create(
            username="su778a", email="su778a@ec778.test", is_superuser=True, empresa=self.t["a"]["empresa"],
        ))
        self.assertEqual([e["id"] for e in root_a.get(self.URL_ENCUADRES).json()], self.ids_a)
        self.assertEqual(root_a.get(f"{self.URL_ENCUADRES}{self.e_b.pk}/").status_code, 404)


class RecepcionRetrieveOrigenTallaTests(TestCase):
    """EC-879: folio del origen y talla en el retrieve de recepciones.

    ``orden_compra_folio`` y ``op_folio`` siempre vienen; el que no corresponde a
    ``tipo_origen`` es ``null``. Cada renglón trae ``producto_variante_talla``
    (``null`` sin variante o sin talla). El número de consultas no crece con los
    renglones, ni en ``GET /recepciones/{id}/`` ni en las recepciones anidadas de
    ``GET /ordenes/{id}/``.
    """

    @classmethod
    def setUpTestData(cls):
        from catalogo.models import Talla
        from produccion.models import OrdenProduccion

        cls.empresa = Empresa.objects.create(codigo="acme-879", razon_social="acme-879 SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="A79", nombre="acme-879")
        cls.almacen = Almacen.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, codigo="ALM", nombre="Almacen 879"
        )
        cls.usuario = Usuario.objects.create(
            username="u@acme-879.test", email="u@acme-879.test", empresa=cls.empresa,
            sucursal_default=cls.sucursal,
        )
        _conceder(cls.usuario, "A-COMPRAS-OC")
        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso 879")
        cls.oc = OrdenCompra.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, moneda=moneda, usuario=cls.usuario,
            fecha_oc=timezone.localdate(), folio="OC-879-1",
            estatus=OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
        )
        cls.op = OrdenProduccion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, folio_op="OP-879-1",
        )
        color = Color.objects.create(nombre="Negro 879", codigo="N79", codigo_hex="#000000")
        cls.producto_pt = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera 879")
        cls.producto_mp = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Hilo 879")
        cls.variantes = [
            ProductoVariante.objects.create(
                producto=cls.producto_pt, empresa=cls.empresa, color=color,
                talla=Talla.objects.create(nombre=nombre), sku=f"PLA-879-{nombre}", precio_base="1",
            )
            for nombre in ("CH", "M", "G", "XG", "2XG")
        ]
        cls.variante_sin_talla = ProductoVariante.objects.create(
            producto=cls.producto_pt, empresa=cls.empresa, color=color, sku="PLA-879-ST", precio_base="1",
        )
        cls.folios = 0

    @classmethod
    def _recepcion(cls, orden_compra=None, op=None, estatus=Recepcion.EstatusRecepcion.BORRADOR):
        cls.folios += 1
        return Recepcion.objects.create(
            tipo_origen="OC" if orden_compra else "OP", orden_compra=orden_compra, op=op,
            empresa=cls.empresa, sucursal=cls.sucursal, almacen=cls.almacen, usuario=cls.usuario,
            folio=f"RC-879-{cls.folios}", fecha_recepcion=timezone.now(), estatus=estatus,
        )

    def _renglon(self, recepcion, variante=None):
        return RecepcionDetalle.objects.create(
            recepcion=recepcion, producto=self.producto_pt if variante else self.producto_mp,
            producto_variante=variante, cantidad_recibida=Decimal("1"),
        )

    def _get(self, url):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(url)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return resp.json(), len(ctx)

    def _retrieve(self, recepcion):
        return self._get(f"{RECEPCIONES_URL}{recepcion.pk}/")

    def test_origen_oc_trae_folio_de_la_oc_y_op_folio_nulo(self):
        recepcion = self._recepcion(orden_compra=self.oc)
        data, _ = self._retrieve(recepcion)
        self.assertEqual(data["tipo_origen"], "OC")
        self.assertEqual(data["orden_compra_folio"], "OC-879-1")
        self.assertIsNone(data["op_folio"])

    def test_origen_op_trae_folio_op_y_orden_compra_folio_nulo(self):
        recepcion = self._recepcion(op=self.op)
        data, _ = self._retrieve(recepcion)
        self.assertEqual(data["tipo_origen"], "OP")
        self.assertEqual(data["op_folio"], "OP-879-1")
        self.assertIsNone(data["orden_compra_folio"])

    def test_oc_sin_folio_devuelve_null(self):
        # ``OrdenCompra.folio`` es nulable (se asigna al aceptar).
        oc = OrdenCompra.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, moneda=self.oc.moneda, usuario=self.usuario,
            fecha_oc=timezone.localdate(),
        )
        data, _ = self._retrieve(self._recepcion(orden_compra=oc))
        self.assertIsNone(data["orden_compra_folio"])
        self.assertIsNone(data["op_folio"])

    def test_talla_por_renglon(self):
        recepcion = self._recepcion(op=self.op)
        con_talla = self._renglon(recepcion, self.variantes[1])
        sin_variante = self._renglon(recepcion)
        sin_talla = self._renglon(recepcion, self.variante_sin_talla)
        data, _ = self._retrieve(recepcion)
        tallas = {d["id"]: d["producto_variante_talla"] for d in data["detalles"]}
        self.assertEqual(tallas, {con_talla.pk: "M", sin_variante.pk: None, sin_talla.pk: None})

    def test_consultas_del_retrieve_no_crecen_con_los_renglones(self):
        una = self._recepcion(op=self.op)
        self._renglon(una, self.variantes[0])
        varias = self._recepcion(op=self.op)
        for variante in self.variantes:
            self._renglon(varias, variante)
        self._renglon(varias)

        _, consultas_una = self._retrieve(una)
        data, consultas_varias = self._retrieve(varias)
        self.assertEqual(len(data["detalles"]), 6)
        self.assertEqual(consultas_una, consultas_varias)

    def test_retrieve_de_la_oc_trae_talla_anidada_sin_consultas_extra(self):
        url = f"/api/v1/compras/ordenes/{self.oc.pk}/"
        primera = self._recepcion(orden_compra=self.oc)
        self._renglon(primera, self.variantes[0])
        _, consultas_una = self._get(url)

        self._renglon(primera)
        segunda = self._recepcion(orden_compra=self.oc)
        for variante in self.variantes[1:]:
            self._renglon(segunda, variante)
        data, consultas_varias = self._get(url)

        self.assertEqual(consultas_una, consultas_varias)
        tallas = sorted(
            (d["producto_variante_talla"] or "") for r in data["recepciones"] for d in r["detalles"]
        )
        self.assertEqual(tallas, ["", "2XG", "CH", "G", "M", "XG"])

    def test_listado_trae_op_folio_sin_consultas_extra(self):
        de_op = self._recepcion(op=self.op)
        de_oc = self._recepcion(orden_compra=self.oc)
        filas, consultas_dos = self._get(RECEPCIONES_URL)
        por_id = {f["id"]: f for f in filas}
        self.assertEqual((por_id[de_op.pk]["op_folio"], por_id[de_op.pk]["orden_compra_folio"]), ("OP-879-1", None))
        self.assertEqual((por_id[de_oc.pk]["op_folio"], por_id[de_oc.pk]["orden_compra_folio"]), (None, "OC-879-1"))
        for _ in range(3):
            self._recepcion(op=self.op)
        filas, consultas_cinco = self._get(RECEPCIONES_URL)
        self.assertEqual(len(filas), 5)
        self.assertEqual(consultas_dos, consultas_cinco)

    def test_renglon_igual_en_el_retrieve_de_la_recepcion_y_en_el_de_la_oc(self):
        # Ambos retrieves sirven el mismo serializer de renglón: mismas llaves y valores.
        ubicacion = Ubicacion.objects.create(almacen=self.almacen, pasillo="9")
        recepcion = self._recepcion(orden_compra=self.oc)
        self._renglon(recepcion, self.variantes[2])
        self._renglon(recepcion)
        RecepcionDetalle.objects.create(
            recepcion=recepcion, producto=self.producto_pt, producto_variante=self.variante_sin_talla,
            ubicacion=ubicacion, cantidad_recibida=Decimal("2.5"),
        )
        de_recepcion, _ = self._retrieve(recepcion)
        de_oc, _ = self._get(f"/api/v1/compras/ordenes/{self.oc.pk}/")
        (anidada,) = [r for r in de_oc["recepciones"] if r["id"] == recepcion.pk]
        self.assertEqual(len(de_recepcion["detalles"]), 3)
        self.assertEqual(de_recepcion["detalles"], anidada["detalles"])

    def test_cancelar_oc_trae_talla_anidada_sin_consultas_extra(self):
        # ``cancelar`` solo rechaza recepciones activas no canceladas: una OC cuyas
        # recepciones están CANCELADA se cancela y su respuesta (forma del
        # retrieve) las anida con sus renglones.
        cancelada = Recepcion.EstatusRecepcion.CANCELADA

        def oc_con_renglones(variantes):
            oc = OrdenCompra.objects.create(
                empresa=self.empresa, sucursal=self.sucursal, moneda=self.oc.moneda, usuario=self.usuario,
                fecha_oc=timezone.localdate(), estatus=OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
            )
            recepcion = self._recepcion(orden_compra=oc, estatus=cancelada)
            for variante in variantes:
                self._renglon(recepcion, variante)
            return oc

        def cancelar(oc):
            client = APIClient()
            client.force_authenticate(user=self.usuario)
            with CaptureQueriesContext(connection) as ctx:
                resp = client.post(
                    f"/api/v1/compras/ordenes/{oc.pk}/cancelar/", {"motivo_cancelacion": "EC-879"}, format="json",
                )
            self.assertEqual(resp.status_code, 200, resp.content[:300])
            return resp.json(), len(ctx)

        _, consultas_una = cancelar(oc_con_renglones(self.variantes[:1]))
        data, consultas_varias = cancelar(oc_con_renglones([*self.variantes, None]))

        self.assertEqual(consultas_una, consultas_varias)
        tallas = sorted((d["producto_variante_talla"] or "") for r in data["recepciones"] for d in r["detalles"])
        self.assertEqual(tallas, ["", "2XG", "CH", "G", "M", "XG"])


class RecepcionSalidaMaquilaTests(TestCase):
    """Recepción de SM en WMS y factura de maquila (#1217, #1232-#1242).

    Reglas que se documentan aquí (``compras/services/maquila.py``):

    - SM de OP: se recibe a lo más lo pendiente de la SM por ``op_detalle``; puede
      ser parcial y la SM queda Enviada hasta completar sus piezas (#1232).
    - Las piezas de una SM Enviada quedan apartadas: la OP directa no las recibe
      (#1234) y el onboarding las informa en ``cantidad_en_maquila``.
    - Cada recepción de SM crea su factura de maquila en Borrador con líneas
      (#1242); la de OB/OR, por la SM completa y sin renglones de recepción.
    - SM de OB/OR: ``detalle`` no vacío o transportista ajeno → 400 (#1240).
    - ``busqueda.salidas_maquila`` sin ``total`` si no hay R-PRODUCCION-COSTOS
      (#1235) y sin SM de OP no recibible (#1236).
    - ``?tipo_origen=SM`` incluye las recepciones de SM de OP (#1238); listado y
      detalle exponen la SM y su origen (#1237).
    """

    setUpTestData = RecepcionOnboardingRenglonesTests.__dict__["setUpTestData"]

    def setUp(self):
        from produccion.models import SalidaMaquila, SalidaMaquilaDetalle

        self.maquilero = Proveedor.objects.filter(empresa=self.empresa).first()
        Proveedor.objects.filter(pk=self.maquilero.pk).update(es_maquilero=True, activo=True)
        self.sm = SalidaMaquila.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, op=self.op, centro_confeccion=self.maquilero,
            folio="SM-T1", fecha_envio=timezone.localdate(), total_piezas=4, total=20,
        )
        self.smd = SalidaMaquilaDetalle.objects.create(
            salida=self.sm, op_detalle=self.op_detalle, descripcion="Costura", cantidad=4,
            precio_unitario=5, importe=20,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.usuario)
        Usuario.objects.filter(pk=self.usuario.pk).update(is_admin_empresa=True)
        self.usuario.refresh_from_db()

    def _post(self, recepcion, detalle):
        cuerpo = {"recepcion": {"almacen": self.almacen.pk, "serie_codigo": "RC", **recepcion}, "detalle": detalle}
        return self.client.post(RECEPCION_ONBOARDING_URL, cuerpo, format="json")

    def _recibir_sm(self, cantidad, op_detalle=None):
        return self._post({"salida_maquila": self.sm.pk},
                          [{"orden_produccion_detalle": (op_detalle or self.op_detalle).pk, "cantidad_recibida": cantidad}])

    def test_parcial_deja_la_sm_enviada_y_factura_lo_recibido(self):
        r = self._recibir_sm(1)
        self.assertEqual(r.status_code, 200, r.data)
        self.sm.refresh_from_db()
        self.assertEqual(self.sm.estatus, "ENVIADA")
        factura = FacturaProveedor.objects.get(salida_maquila=self.sm)
        self.assertIsNone(factura.oc_id)
        self.assertEqual(factura.estatus, FacturaProveedor.FacturaProveedorStatus.BORRADOR)
        self.assertEqual(factura.total, Decimal("5.00"))
        linea = factura.factura_proveedor_detalles.get()
        self.assertEqual((linea.cantidad, linea.salida_maquila_detalle_id), (Decimal("1.00"), self.smd.pk))

    def test_completar_la_sm_la_marca_recibida_con_una_factura_por_recepcion(self):
        self.assertEqual(self._recibir_sm(1).status_code, 200)
        self.assertEqual(self._recibir_sm(3).status_code, 200)
        self.sm.refresh_from_db()
        self.assertEqual(self.sm.estatus, "RECIBIDA")
        self.assertIsNotNone(self.sm.fecha_recepcion)
        totales = list(FacturaProveedor.objects.filter(salida_maquila=self.sm).order_by("id").values_list("total", flat=True))
        self.assertEqual(totales, [Decimal("5.00"), Decimal("15.00")])

    def test_no_recibe_mas_que_las_piezas_de_la_sm(self):
        r = self._recibir_sm(5)  # la OP tiene 10, la SM solo 4
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Recepcion.objects.filter(salida_maquila=self.sm).exists())

    def test_renglon_que_no_va_en_la_sm(self):
        r = self._recibir_sm(1, op_detalle=self.op_detalle_2)
        self.assertEqual(r.status_code, 400)
        self.assertIn("no va en la SM", str(r.data["detalle"]))

    def test_la_op_directa_no_recibe_piezas_en_maquila(self):
        # 10 de la OP, 4 en maquila: la OP directa recibe a lo más 6.
        r = self._post({"orden_produccion": self.op.pk},
                       [{"orden_produccion_detalle": self.op_detalle.pk, "cantidad_recibida": 7}])
        self.assertEqual(r.status_code, 400)
        r = self._post({"orden_produccion": self.op.pk},
                       [{"orden_produccion_detalle": self.op_detalle.pk, "cantidad_recibida": 6}])
        self.assertEqual(r.status_code, 200, r.data)
        r = self._post({"orden_produccion": self.op.pk},
                       [{"orden_produccion_detalle": self.op_detalle.pk, "cantidad_recibida": 1}])
        self.assertEqual(r.status_code, 400)
        self.assertIn("SM-T1", str(r.data["detalle"]))
        # La SM sigue recibible por sus 4 piezas.
        self.assertEqual(self._recibir_sm(4).status_code, 200)

    def test_onboarding_get_informa_piezas_en_maquila_y_pendientes(self):
        self._recibir_sm(1)
        r = self.client.get(RECEPCION_ONBOARDING_URL)
        sm = next(s for s in r.data["busqueda"]["salidas_maquila"] if s["id"] == self.sm.pk)
        self.assertEqual(sm["piezas_pendientes"], "3.00")  # texto, como ``cantidad_pendiente``
        op = next(o for o in r.data["busqueda"]["ordenes_produccion"] if o["id"] == self.op.pk)
        renglon = next(d for d in op["detalle"] if d["id"] == self.op_detalle.pk)
        self.assertEqual(Decimal(renglon["cantidad_en_maquila"]), Decimal("3"))
        self.assertEqual(Decimal(renglon["cantidad_pendiente"]), Decimal("6"))
        # Tope al recibir con la SM: lo pendiente de esa SM en el renglón.
        self.assertEqual(renglon["salidas_maquila"], [{"id": self.sm.pk, "folio": "SM-T1", "cantidad_pendiente": "3.00"}])

    def test_muestra_de_fabricacion_especial_se_recibe_con_su_sm(self):
        """Renglón de OP con ``variante_produccion`` (sin catálogo): el onboarding
        trae su nombre/SKU y la recepción guarda la variante de producción, no un
        producto."""
        from catalogo.models import VarianteProductoProduccion
        from produccion.models import OrdenProduccionDetalle

        muestra = VarianteProductoProduccion.objects.create(empresa=self.empresa, nombre="Bata muestra", sku="MP-T1-M")
        OrdenProduccionDetalle.objects.filter(pk=self.op_detalle.pk).update(producto_variante=None, variante_produccion=muestra)
        r = self.client.get(RECEPCION_ONBOARDING_URL, {"q": "MP-T1"})
        op = next(o for o in r.data["busqueda"]["ordenes_produccion"] if o["id"] == self.op.pk)
        renglon = next(d for d in op["detalle"] if d["id"] == self.op_detalle.pk)
        self.assertEqual((renglon["producto_nombre"], renglon["sku"], renglon["variante_produccion_id"]),
                         ("Bata muestra", "MP-T1-M", muestra.pk))

        self.assertEqual(self._recibir_sm(2).status_code, 200)
        rd = RecepcionDetalle.objects.get(recepcion__salida_maquila=self.sm)
        self.assertEqual((rd.producto_id, rd.variante_produccion_id), (None, muestra.pk))
        self.assertEqual(FacturaProveedor.objects.get(salida_maquila=self.sm).total, Decimal("10.00"))

    def test_onboarding_get_sin_permiso_de_costos_no_trae_total(self):
        Usuario.objects.filter(pk=self.usuario.pk).update(is_admin_empresa=False)
        self.usuario.refresh_from_db()
        self.usuario.sucursales.add(self.sucursal)
        _conceder(self.usuario, "R-WMS-RECEPCIONES")  # leer el onboarding (#1295), sin costos
        r = self.client.get(RECEPCION_ONBOARDING_URL)
        filas = r.data["busqueda"]["salidas_maquila"]
        self.assertTrue(filas)
        self.assertTrue(all("total" not in f for f in filas))

    def test_onboarding_get_excluye_sm_de_op_no_recibible(self):
        from produccion.models import OrdenProduccion

        OrdenProduccion.objects.filter(pk=self.op.pk).update(estatus_op=OrdenProduccion.EstatusOrdenProduccion.COMPLETADO)
        r = self.client.get(RECEPCION_ONBOARDING_URL)
        self.assertNotIn(self.sm.pk, [s["id"] for s in r.data["busqueda"]["salidas_maquila"]])

    def test_tipo_origen_sm_incluye_las_de_op_y_expone_la_sm(self):
        self._recibir_sm(4)
        rc = Recepcion.objects.get(salida_maquila=self.sm)
        self.assertEqual(rc.tipo_origen, Recepcion.TipoOrigen.ORDEN_PRODUCCION)
        r = self.client.get(RECEPCIONES_URL, {"tipo_origen": "SM"})
        filas = r.data["results"] if isinstance(r.data, dict) else r.data
        fila = next(f for f in filas if f["id"] == rc.pk)
        self.assertEqual((fila["salida_maquila_folio"], fila["salida_maquila_origen"]), ("SM-T1", "OP"))
        r = self.client.get(f"{RECEPCIONES_URL}{rc.pk}/")
        self.assertEqual(r.data["salida_maquila"], self.sm.pk)
        self.assertEqual(r.data["salida_maquila_origen_folio"], self.op.folio_op)

    def test_misma_sm_cancelada_o_de_otra_empresa(self):
        from produccion.models import SalidaMaquila

        SalidaMaquila.objects.filter(pk=self.sm.pk).update(estatus="CANCELADA")
        self.assertEqual(self._recibir_sm(1).status_code, 400)
        otra = Empresa.objects.create(codigo="otra-sm", razon_social="Otra SA")
        Usuario.objects.filter(pk=self.usuario.pk).update(empresa=otra)
        self.usuario.refresh_from_db()
        r = self._recibir_sm(1)
        self.assertEqual(r.status_code, 400)

    def test_salida_maquila_con_orden_compra(self):
        r = self._post({"salida_maquila": self.sm.pk, "orden_compra": self.oc.pk},
                       [{"orden_produccion_detalle": self.op_detalle.pk, "cantidad_recibida": 1}])
        self.assertEqual(r.status_code, 400)

    def test_factura_de_maquila_se_ajusta_en_borrador_y_se_registra(self):
        self._recibir_sm(4)
        factura = FacturaProveedor.objects.get(salida_maquila=self.sm)
        linea = factura.factura_proveedor_detalles.get()
        url = f"/api/v1/finanzas/facturas-proveedor/{factura.pk}/"
        r = self.client.post(f"{url}ajustar-maquila/", {"lineas": [{"id": linea.pk, "impuesto": "3.20"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(r.data["total"]), Decimal("23.20"))
        r = self.client.get(url)
        self.assertIsNone(r.data["oc_folio"])
        self.assertEqual(r.data["salida_maquila_folio"], "SM-T1")


class RecepcionSalidaMaquilaServicioTests(TestCase):
    """SM de OB: se cuentan las piezas por renglón de la SM, sin inventario; la
    factura es por lo que regresó. Parcial: la SM sigue Enviada salvo
    ``cerrar_salida_maquila``, y lo que no regresó queda en ``piezas_faltantes``."""

    @classmethod
    def setUpTestData(cls):
        from produccion.models import OrdenesBordado, SalidaMaquila, SalidaMaquilaDetalle
        from terceros.models import Cliente, Transportista
        from ventas.models import Pedido

        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General de Ley")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Pago en una sola exhibición")
        cls.empresa = Empresa.objects.create(codigo="smob", razon_social="SMOB SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="SOB", nombre="SMOB 1")
        SerieFolio.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, tipo_documento="RECEPCION", serie="RC")
        cls.almacen = Almacen.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, codigo="AOB", nombre="AOB")
        cls.usuario = Usuario.objects.create(username="u@smob.test", email="u@smob.test", empresa=cls.empresa,
                                             is_admin_empresa=True)
        maquilero = Proveedor.objects.create(
            empresa=cls.empresa, nombre="Maquilero", moneda=moneda, sat_regimen_fiscal=regimen,
            sat_forma_pago=forma, sat_metodo_pago=metodo, codigo="MAQ-OB", razon_social="Maquilero SA",
            telefono="8100000000", contacto_principal="Contacto", rfc="XAXX010101000", email="m@smob.test",
            es_maquilero=True,
        )
        cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente OB")
        pedido = Pedido.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, cliente=cliente, moneda=moneda,
            persona_pagos="Pagos", correo_facturas="p@smob.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        ob = OrdenesBordado.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, pedido=pedido, folio_bordado="OB-T1")
        cls.sm = SalidaMaquila.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, ob=ob, centro_confeccion=maquilero,
            folio="SM-OB1", fecha_envio=timezone.localdate(), total_piezas=2, total=30,
        )
        cls.smd = SalidaMaquilaDetalle.objects.create(salida=cls.sm, descripcion="Bata", cantidad=2, precio_unitario=15, importe=30)
        otra = Empresa.objects.create(codigo="smob-2", razon_social="Otra SA")
        cls.transportista_ajeno = Transportista.objects.create(empresa=otra)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.usuario)

    def _post(self, detalle=(), **extra):
        cuerpo = {"recepcion": {"almacen": self.almacen.pk, "serie_codigo": "RC", "salida_maquila": self.sm.pk, **extra},
                  "detalle": list(detalle)}
        return self.client.post(RECEPCION_ONBOARDING_URL, cuerpo, format="json")

    def test_recibe_completa_sin_inventario_y_factura_con_lineas(self):
        movimientos = MovimientoInventario.objects.count()
        r = self._post()
        self.assertEqual(r.status_code, 200, r.data)
        self.sm.refresh_from_db()
        self.assertEqual(self.sm.estatus, "RECIBIDA")
        rc = Recepcion.objects.get(salida_maquila=self.sm)
        self.assertEqual(rc.tipo_origen, Recepcion.TipoOrigen.SALIDA_MAQUILA)
        # Sin renglones en el POST se recibe todo lo pendiente.
        renglon = rc.recepciondetalle_set.get()
        self.assertEqual((renglon.salida_maquila_detalle_id, renglon.cantidad_recibida), (self.smd.pk, Decimal("2")))
        self.assertEqual(MovimientoInventario.objects.count(), movimientos)
        factura = FacturaProveedor.objects.get(salida_maquila=self.sm)
        self.assertEqual((factura.total, factura.recepcion_id, factura.oc_id), (Decimal("30.00"), rc.pk, None))
        linea = factura.factura_proveedor_detalles.get()
        self.assertIsNone(linea.producto_id)
        self.assertEqual(linea.descripcion, "Bata")

    def test_renglon_de_op_o_excedido_es_400(self):
        for renglon in ({"orden_produccion_detalle": 1, "cantidad_recibida": 1},
                        {"salida_maquila_detalle": self.smd.pk, "cantidad_recibida": 3}):
            self.assertEqual(self._post(detalle=[renglon]).status_code, 400)
        self.assertFalse(Recepcion.objects.filter(salida_maquila=self.sm).exists())

    def test_onboarding_get_trae_los_renglones_de_la_sm(self):
        r = self.client.get(RECEPCION_ONBOARDING_URL)
        fila = next(s for s in r.data["busqueda"]["salidas_maquila"] if s["id"] == self.sm.pk)
        self.assertEqual([(d["id"], d["cantidad_pendiente"]) for d in fila["detalle"]], [(self.smd.pk, "2.00")])

    def test_parcial_sigue_enviada_y_cerrar_deja_el_faltante(self):
        uno = [{"salida_maquila_detalle": self.smd.pk, "cantidad_recibida": 1}]
        self.assertEqual(self._post(detalle=uno).status_code, 200)
        self.sm.refresh_from_db()
        self.assertEqual(self.sm.estatus, "ENVIADA")
        self.assertEqual(FacturaProveedor.objects.get(salida_maquila=self.sm).total, Decimal("15.00"))

        # Con 3 enviadas, la segunda recepción cierra la SM con 1 faltante.
        self.sm.detalles.update(cantidad=3)
        type(self.sm).objects.filter(pk=self.sm.pk).update(total_piezas=3)
        self.assertEqual(self._post(detalle=uno, cerrar_salida_maquila=True).status_code, 200)
        self.sm.refresh_from_db()
        self.assertEqual(self.sm.estatus, "RECIBIDA")
        r = self.client.get(f"/api/v1/produccion/salidas-maquila/{self.sm.pk}/")
        if r.status_code == 200:  # lectura con R-PRODUCCION-OP
            self.assertEqual((r.data["piezas_recibidas"], r.data["piezas_faltantes"]), ("2", "1"))

    def test_mas_de_2_decimales_es_400(self):
        """#1308: la SM va a centavos; 4 decimales dejarían un residuo que la API
        muestra como 0.00 y ya no se podría recibir."""
        r = self._post(detalle=[{"salida_maquila_detalle": self.smd.pk, "cantidad_recibida": "1.995"}])
        self.assertEqual(r.status_code, 400)
        self.assertIn("2 decimales", str(r.data))
        self.assertFalse(Recepcion.objects.filter(salida_maquila=self.sm).exists())

    def test_tope_y_cierre_con_el_pendiente_exacto(self):
        """#1308: un residuo menor a un centavo (de antes de la validación) se ve
        como 0.00 pero cuenta: la SM no se completa y sin renglones se recibe."""
        from compras.services.maquila import pendiente_por_renglon_sm

        rc = Recepcion.objects.create(
            tipo_origen=Recepcion.TipoOrigen.SALIDA_MAQUILA, salida_maquila=self.sm, empresa=self.empresa,
            sucursal=self.sucursal, almacen=self.almacen, usuario=self.usuario, fecha_recepcion=timezone.now(),
            estatus=Recepcion.EstatusRecepcion.CERRADA, activo=True,
        )
        RecepcionDetalle.objects.create(recepcion=rc, salida_maquila_detalle=self.smd, cantidad_recibida=Decimal("1.995"))
        self.assertEqual(pendiente_por_renglon_sm(self.sm.pk), {self.smd.pk: Decimal("0.005")})
        r = self.client.get(RECEPCION_ONBOARDING_URL)
        fila = next(s for s in r.data["busqueda"]["salidas_maquila"] if s["id"] == self.sm.pk)
        self.assertEqual(fila["detalle"][0]["cantidad_pendiente"], "0.00")  # solo para mostrar
        # 0.01 excede el residuo real; sin renglones se recibe exacto y se completa.
        self.assertEqual(self._post(detalle=[{"salida_maquila_detalle": self.smd.pk, "cantidad_recibida": "0.01"}]).status_code, 400)
        self.assertEqual(self._post().status_code, 200, "sin renglones recibe el residuo")
        self.sm.refresh_from_db()
        self.assertEqual(self.sm.estatus, "RECIBIDA")
        self.assertEqual(pendiente_por_renglon_sm(self.sm.pk), {self.smd.pk: Decimal("0")})

    def _cancelar_rc(self, recepcion, motivo="Captura duplicada"):
        return self.client.post(f"{RECEPCIONES_URL}{recepcion.pk}/cancelar/", {"motivo": motivo}, format="json")

    def test_cancelar_recepcion_cancela_la_factura_y_reabre_la_sm(self):
        """#1307: antes de calidad (la de OB/OR no pasa por calidad), la factura de
        maquila en Borrador se cancela y lo recibido vuelve a estar pendiente."""
        self.assertEqual(self._post().status_code, 200)
        rc = Recepcion.objects.get(salida_maquila=self.sm)
        r = self._cancelar_rc(rc)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["estatus"], Recepcion.EstatusRecepcion.CANCELADA)
        self.sm.refresh_from_db()
        self.assertEqual((self.sm.estatus, self.sm.fecha_recepcion), ("ENVIADA", None))
        self.assertEqual(FacturaProveedor.objects.get(recepcion=rc).estatus, "Cancelada")
        evento = AuditoriaEvento.objects.get(modulo="compras", accion="CANCELAR", tabla="recepciones")
        self.assertEqual(evento.despues_json["motivo"], "Captura duplicada")
        # Se puede volver a recibir todo, con su factura nueva.
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(FacturaProveedor.objects.filter(salida_maquila=self.sm, estatus="Borrador").count(), 1)
        # Ya cancelada: 400.
        self.assertEqual(self._cancelar_rc(rc).status_code, 400)

    def test_cancelar_recepcion_con_factura_registrada_o_sin_permiso(self):
        self.assertEqual(self._post().status_code, 200)
        rc = Recepcion.objects.get(salida_maquila=self.sm)
        no_admin = Usuario.objects.create(username="n@smob.test", email="n@smob.test", empresa=self.empresa)
        self.client.force_authenticate(user=no_admin)
        self.assertEqual(self._cancelar_rc(rc).status_code, 403)
        self.client.force_authenticate(user=self.usuario)
        self.assertEqual(self._cancelar_rc(rc, motivo="  ").status_code, 400)
        FacturaProveedor.objects.filter(recepcion=rc).update(estatus="Registrada")
        r = self._cancelar_rc(rc)
        self.assertEqual(r.status_code, 400)
        self.assertIn("facturas_proveedores", r.data)
        rc.refresh_from_db()
        self.sm.refresh_from_db()
        self.assertEqual((rc.estatus, self.sm.estatus), (Recepcion.EstatusRecepcion.CERRADA, "RECIBIDA"))

    def test_transportista_de_otra_empresa_es_400(self):
        r = self._post(transportista=self.transportista_ajeno.pk)
        self.assertEqual(r.status_code, 400)
        self.assertIn("transportista", r.data)

    def test_dos_veces_es_400(self):
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(self._post().status_code, 400)

    def test_pdf_fusionado_sin_oc(self):
        import io

        from django.core.files.uploadedfile import SimpleUploadedFile
        from reportlab.pdfgen import canvas

        self._post()
        factura = FacturaProveedor.objects.get(salida_maquila=self.sm)
        buffer = io.BytesIO()
        hoja = canvas.Canvas(buffer)
        hoja.drawString(50, 50, "factura")
        hoja.save()
        url = f"/api/v1/finanzas/facturas-proveedor/{factura.pk}/"
        self.client.post(f"{url}adjuntar-pdf/", {"archivo": SimpleUploadedFile("f.pdf", buffer.getvalue(), "application/pdf")},
                         format="multipart")
        r = self.client.get(f"{url}pdf-fusionado/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("SM-RC-Factura", r["Content-Disposition"])

    def test_cerrar_sigue_en_400(self):
        r = self.client.post(f"/api/v1/produccion/salidas-maquila/{self.sm.pk}/cerrar/")
        self.assertIn(r.status_code, (400, 403))


class RecepcionPuedeCancelarTests(TestCase):
    """``puede_cancelar`` del retrieve y ``POST recepciones/{id}/cancelar/`` (EC-926).

    Ambos usan ``compras.services.cancelacion_recepcion``: cada caso consulta el
    retrieve y luego intenta cancelar, y exige que coincidan. También cubre los
    campos persistentes de la cancelación (motivo, fecha y usuario).
    """

    @classmethod
    def setUpTestData(cls):
        from produccion.models import OrdenProduccion, SalidaMaquila

        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General de Ley")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Pago en una sola exhibición")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")

        def tenant(codigo):
            empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
            sucursal = Sucursal.objects.create(empresa=empresa, codigo=codigo[:3].upper(), nombre=codigo)
            proveedor = Proveedor.objects.create(
                empresa=empresa, nombre=f"Proveedor {codigo}", moneda=cls.moneda, sat_regimen_fiscal=regimen,
                sat_forma_pago=forma, sat_metodo_pago=metodo, codigo=f"PROV-{codigo}", razon_social="Prov SA",
                telefono="8100000000", contacto_principal="Contacto", rfc="XAXX010101000", email=f"p@{codigo}.test",
                es_maquilero=True,
            )
            almacen = Almacen.objects.create(empresa=empresa, sucursal=sucursal, codigo="ALM", nombre="Almacen")
            admin = Usuario.objects.create(
                username=f"adm@{codigo}.test", email=f"adm@{codigo}.test", empresa=empresa,
                is_admin_empresa=True, first_name="Ana", last_name="Admin",
            )
            return {"empresa": empresa, "sucursal": sucursal, "proveedor": proveedor, "almacen": almacen, "admin": admin}

        cls.a = tenant("acme-pc")
        cls.b = tenant("globex-pc")
        empresa, sucursal = cls.a["empresa"], cls.a["sucursal"]
        cls.no_admin = Usuario.objects.create(username="u@acme-pc.test", email="u@acme-pc.test", empresa=empresa)
        cls.superusuario = Usuario.objects.create(
            username="su@acme-pc.test", email="su@acme-pc.test", empresa=empresa, is_superuser=True,
        )
        cls.oc = OrdenCompra.objects.create(
            empresa=empresa, sucursal=sucursal, proveedor=cls.a["proveedor"], moneda=cls.moneda,
            usuario=cls.a["admin"], fecha_oc=timezone.localdate(), estatus=Estatus.PARCIALMENTE_RECIBIDA,
        )
        cls.op = OrdenProduccion.objects.create(empresa=empresa, sucursal=sucursal, folio_op="OP-PC-1")
        cls.op_completada = OrdenProduccion.objects.create(
            empresa=empresa, sucursal=sucursal, folio_op="OP-PC-2",
            estatus_op=OrdenProduccion.EstatusOrdenProduccion.COMPLETADO,
        )
        cls.sm = SalidaMaquila.objects.create(
            empresa=empresa, sucursal=sucursal, op=cls.op, centro_confeccion=cls.a["proveedor"],
            folio="SM-PC-1", fecha_envio=timezone.localdate(), total_piezas=1, total=10,
        )
        departamento = Departamento.objects.create(empresa=empresa, sucursal=sucursal, codigo="CAL", nombre="Calidad")
        cls.inspector = Empleado.objects.create(
            empresa=empresa, sucursal=sucursal, departamento=departamento,
            puesto=Puesto.objects.create(empresa=empresa, nombre="Inspector"),
            numero_empleado="EMP-PC", nombre="Ana", apellido_paterno="Lopez", fecha_ingreso=timezone.localdate(),
        )

    # --- helpers ---------------------------------------------------------------

    def _rc(self, estatus=Recepcion.EstatusRecepcion.EN_CALIDAD, tipo_origen="OC", **extra):
        E = Recepcion.TipoOrigen
        origen = {
            E.ORDEN_COMPRA: {"orden_compra": self.oc, "proveedor": self.a["proveedor"]},
            E.ORDEN_PRODUCCION: {"op": self.op},
            E.SALIDA_MAQUILA: {"salida_maquila": self.sm, "proveedor": self.a["proveedor"]},
        }[tipo_origen]
        return Recepcion.objects.create(
            tipo_origen=tipo_origen, empresa=self.a["empresa"], sucursal=self.a["sucursal"],
            almacen=self.a["almacen"], usuario=self.a["admin"], folio=f"RC-PC-{Recepcion.objects.count() + 1}",
            fecha_recepcion=timezone.now(), estatus=estatus, **{**origen, **extra},
        )

    def _factura(self, rc, estatus, **extra):
        return FacturaProveedor.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"], proveedor=self.a["proveedor"],
            recepcion=rc, moneda=self.moneda, estatus=estatus, **extra,
        )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _retrieve(self, rc, user=None):
        return self._client(user or self.a["admin"]).get(f"{RECEPCIONES_URL}{rc.pk}/")

    def _cancelar(self, rc, user=None, motivo="Captura duplicada"):
        return self._client(user or self.a["admin"]).post(
            f"{RECEPCIONES_URL}{rc.pk}/cancelar/", {"motivo": motivo}, format="json",
        )

    def _assert_coinciden(self, rc, esperado, user=None, status_code=200, clave=None):
        """``puede_cancelar`` del retrieve == resultado de ``cancelar/``."""
        r = self._retrieve(rc, user)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIs(r.data["puede_cancelar"], esperado)
        resp = self._cancelar(rc, user)
        self.assertEqual(resp.status_code, 200 if esperado else status_code, resp.content)
        if clave:
            self.assertEqual(list(resp.json()), [clave])
            self.assertIsInstance(resp.json()[clave], str)  # forma plana, sin lista
        return resp

    # --- la regla: retrieve y cancelar coinciden ---------------------------------

    def test_en_calidad_de_cada_origen_se_puede_cancelar(self):
        for tipo in ("OC", "OP", "SM"):
            with self.subTest(tipo=tipo):
                self._assert_coinciden(self._rc(tipo_origen=tipo), True)

    def test_superusuario_de_la_empresa_activa(self):
        self._assert_coinciden(self._rc(), True, user=self.superusuario)

    def test_no_admin_no_puede(self):
        self._assert_coinciden(self._rc(), False, user=self.no_admin, status_code=403, clave="detail")

    def test_cerrada_solo_si_es_de_sm(self):
        self._assert_coinciden(self._rc(Recepcion.EstatusRecepcion.CERRADA, tipo_origen="SM"), True)
        for tipo in ("OC", "OP"):
            with self.subTest(tipo=tipo):
                self._assert_coinciden(
                    self._rc(Recepcion.EstatusRecepcion.CERRADA, tipo_origen=tipo), False, status_code=400,
                    clave="estatus",
                )

    def test_otros_estatus_no(self):
        E = Recepcion.EstatusRecepcion
        for estatus in (E.BORRADOR, E.RECIBIDA, E.PARCIAL, E.CANCELADA):
            with self.subTest(estatus=estatus):
                self._assert_coinciden(self._rc(estatus), False, status_code=400, clave="estatus")

    def test_con_inspeccion_o_movimiento_no(self):
        rc = self._rc()
        CalidadInspeccion.objects.create(
            recepcion=rc, inspector=self.inspector, fecha=timezone.localdate(), estado="pendiente",
        )
        self._assert_coinciden(rc, False, status_code=400, clave="estatus")
        rc = self._rc()
        MovimientoInventario.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"], usuario=self.a["admin"], recepcion=rc,
        )
        self._assert_coinciden(rc, False, status_code=400, clave="estatus")

    def test_op_completada_no(self):
        rc = self._rc(tipo_origen="OP", op=self.op_completada)
        resp = self._assert_coinciden(rc, False, status_code=400, clave="op")
        self.assertIn("OP-PC-2", resp.json()["op"])

    def test_factura_viva_no_y_la_de_maquila_en_borrador_si(self):
        F = FacturaProveedor.FacturaProveedorStatus
        rc = self._rc()
        self._factura(rc, F.BORRADOR)  # factura de OC: no es de maquila
        self._assert_coinciden(rc, False, status_code=400, clave="facturas_proveedores")
        rc = self._rc(tipo_origen="SM")
        self._factura(rc, F.REGISTRADA, salida_maquila=self.sm, activo=False)  # dada de baja también cuenta
        self._assert_coinciden(rc, False, status_code=400, clave="facturas_proveedores")
        rc = self._rc(tipo_origen="SM")
        self._factura(rc, F.CANCELADA)
        maquila = self._factura(rc, F.BORRADOR, salida_maquila=self.sm)
        self._assert_coinciden(rc, True)
        maquila.refresh_from_db()
        self.assertEqual(maquila.estatus, F.CANCELADA)

    def test_otra_empresa_es_404(self):
        rc = self._rc()
        self.assertEqual(self._retrieve(rc, self.b["admin"]).status_code, 404)
        self.assertEqual(self._cancelar(rc, self.b["admin"]).status_code, 404)
        self.assertEqual(Recepcion.objects.get(pk=rc.pk).estatus, Recepcion.EstatusRecepcion.EN_CALIDAD)

    def test_puede_cancelar_no_va_en_el_listado(self):
        self._rc()
        r = self._client(self.a["admin"]).get(RECEPCIONES_URL)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("puede_cancelar", r.json()[0])
        self.assertIn("motivo_cancelacion", r.json()[0])

    # --- motivo y campos persistentes ------------------------------------------

    def test_motivo_mas_largo_que_el_campo_es_400(self):
        rc = self._rc()
        resp = self._cancelar(rc, motivo="x" * 501)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(list(resp.json()), ["motivo"])
        self.assertIsInstance(resp.json()["motivo"], str)
        self.assertEqual(Recepcion.objects.get(pk=rc.pk).estatus, Recepcion.EstatusRecepcion.EN_CALIDAD)
        self.assertEqual(self._cancelar(rc, motivo="  " + "y" * 500 + "  ").status_code, 200)
        self.assertEqual(Recepcion.objects.get(pk=rc.pk).motivo_cancelacion, "y" * 500)

    def test_cancelar_guarda_y_expone_motivo_fecha_y_usuario(self):
        rc = self._rc()
        motivo = "Se capturó dos veces la misma remisión del proveedor; la buena es la siguiente. " * 2
        antes = timezone.now()
        resp = self._cancelar(rc, motivo=f"  {motivo}  ")
        self.assertEqual(resp.status_code, 200, resp.content)
        rc.refresh_from_db()
        self.assertEqual(rc.motivo_cancelacion, motivo.strip())  # completo (más de 100)
        self.assertGreaterEqual(rc.fecha_cancelacion, antes)
        self.assertEqual(rc.cancelado_por_id, self.a["admin"].pk)
        self.assertEqual(rc.history.first().history_change_reason, motivo.strip()[:100])
        for data in (resp.json(), self._retrieve(rc).json()):
            self.assertEqual(data["motivo_cancelacion"], motivo.strip())
            self.assertEqual(data["cancelado_por"], self.a["admin"].pk)
            self.assertEqual(data["cancelado_por_nombre"], "Ana Admin")
            self.assertIsNotNone(data["fecha_cancelacion"])
        self.assertIs(self._retrieve(rc).data["puede_cancelar"], False)
        sin_cancelar = self._retrieve(self._rc()).json()
        self.assertEqual(
            [sin_cancelar[k] for k in ("motivo_cancelacion", "fecha_cancelacion", "cancelado_por", "cancelado_por_nombre")],
            [None, None, None, None],
        )

    def test_campos_de_cancelacion_son_de_solo_lectura(self):
        from compras.api.serializers import RecepcionRetrieveSerializer, RecepcionSerializer

        for serializer in (RecepcionSerializer, RecepcionRetrieveSerializer):
            campos = serializer().fields
            for nombre in ("motivo_cancelacion", "fecha_cancelacion", "cancelado_por", "cancelado_por_nombre"):
                with self.subTest(serializer=serializer.__name__, campo=nombre):
                    self.assertTrue(campos[nombre].read_only)

    # --- consultas -------------------------------------------------------------

    def test_listado_no_suma_consultas_por_fila_cancelada(self):
        client = self._client(self.a["admin"])
        self._cancelar(self._rc())
        with CaptureQueriesContext(connection) as una:
            self.assertEqual(client.get(RECEPCIONES_URL).status_code, 200)
        for _ in range(3):
            self._cancelar(self._rc())
        with CaptureQueriesContext(connection) as varias:
            self.assertEqual(len(client.get(RECEPCIONES_URL).json()), 4)
        self.assertEqual(len(varias), len(una))

    def test_retrieve_consultas_acotadas(self):
        """``puede_cancelar`` no suma consultas: sus bloqueos van anotados con
        ``Exists`` en la consulta del retrieve (EC-926). Recepción + renglones."""
        rc = self._rc()
        with self.assertNumQueries(2):
            r = self._retrieve(rc)
        self.assertIs(r.data["puede_cancelar"], True)
        with self.assertNumQueries(2):
            self.assertIs(self._retrieve(rc, self.no_admin).data["puede_cancelar"], False)
        cerrada = self._rc(Recepcion.EstatusRecepcion.CERRADA)
        with self.assertNumQueries(2):
            self.assertIs(self._retrieve(cerrada).data["puede_cancelar"], False)

    def test_borrar_al_usuario_que_cancelo_deja_cancelado_por_en_nulo(self):
        otro_admin = Usuario.objects.create(
            username="adm2@acme-pc.test", email="adm2@acme-pc.test", empresa=self.a["empresa"], is_admin_empresa=True,
        )
        rc = self._rc()
        self.assertEqual(self._cancelar(rc, otro_admin).status_code, 200)
        otro_admin.delete()  # antes PROTECT: ProtectedError
        rc.refresh_from_db()
        self.assertEqual((rc.estatus, rc.cancelado_por_id), (Recepcion.EstatusRecepcion.CANCELADA, None))
        self.assertEqual(rc.motivo_cancelacion, "Captura duplicada")
        self.assertIsNone(self._retrieve(rc).data["cancelado_por_nombre"])


class RecepcionCancelacionBackfillTests(TestCase):
    """``compras/migrations/0033``: motivo, fecha y usuario desde el evento ``CANCELAR``."""

    def test_recupera_del_evento_y_corta_el_motivo_largo(self):
        import importlib

        from django.apps import apps

        recuperar = importlib.import_module("compras.migrations.0033_recepcion_cancelacion_datos").recuperar
        empresa = Empresa.objects.create(codigo="bf-926", razon_social="BF SA")
        sucursal = Sucursal.objects.create(empresa=empresa, codigo="BF", nombre="BF")
        almacen = Almacen.objects.create(empresa=empresa, sucursal=sucursal, codigo="ALM", nombre="Almacen")
        usuario = Usuario.objects.create(username="u@bf-926.test", email="u@bf-926.test", empresa=empresa)

        def cancelada(folio):
            return Recepcion.objects.create(
                tipo_origen="OC", empresa=empresa, sucursal=sucursal, almacen=almacen, usuario=usuario, folio=folio,
                fecha_recepcion=timezone.now(), estatus=Recepcion.EstatusRecepcion.CANCELADA,
            )

        con_evento, sin_evento = cancelada("RC-BF-1"), cancelada("RC-BF-2")
        motivo = "m" * 520
        evento = AuditoriaEvento.objects.create(
            empresa=empresa, usuario=usuario, modulo="compras", accion="CANCELAR", tabla="recepciones",
            id_registro=str(con_evento.pk), antes_json={"estatus": 4}, despues_json={"estatus": 6, "motivo": motivo},
        )
        # Otro módulo o tabla con el mismo id no cuenta.
        AuditoriaEvento.objects.create(
            empresa=empresa, usuario=usuario, modulo="compras", accion="CANCELAR", tabla="ordenes_compra",
            id_registro=str(sin_evento.pk), despues_json={"motivo": "De una OC"},
        )

        with mock.patch("builtins.print"):
            self.assertEqual(recuperar(apps, None), (1, 1))
        con_evento.refresh_from_db()
        sin_evento.refresh_from_db()
        self.assertEqual(con_evento.motivo_cancelacion, "m" * 500)
        self.assertEqual(con_evento.fecha_cancelacion, evento.created_at)
        self.assertEqual(con_evento.cancelado_por_id, usuario.pk)
        self.assertEqual(
            (sin_evento.motivo_cancelacion, sin_evento.fecha_cancelacion, sin_evento.cancelado_por_id), (None, None, None),
        )
        with mock.patch("builtins.print"):
            self.assertEqual(recuperar(apps, None), (0, 0))  # no pisa lo ya escrito


class DevolucionProveedorCanceladoPorNombreTests(TestCase):
    """EC-966: ``cancelado_por_nombre`` de la devolución a proveedor con la regla de
    Recepciones (nombre completo o correo; ``null`` sin cancelar), sin consultas por fila.
    Empresa, proveedor, almacén y OC de ``OrdenCompraCancelacionTests``."""

    URL = "/api/v1/compras/devoluciones-proveedor/"

    @classmethod
    def setUpTestData(cls):
        OrdenCompraCancelacionTests.setUpTestData.__func__(cls)
        _conceder(cls.a["usuario"], "R-WMS-RECEPCIONES")
        empresa, sucursal = cls.a["empresa"], cls.a["sucursal"]
        cls.con_nombre = Usuario.objects.create(
            username="ana@acme-cx.test", email="ana@acme-cx.test", first_name="Ana", last_name="López",
            empresa=empresa,
        )
        cls.sin_nombre = Usuario.objects.create(username="sn@acme-cx.test", email="sn@acme-cx.test", empresa=empresa)
        oc = OrdenCompraCancelacionTests._oc(cls, Estatus.AUTORIZADA)
        cls.recepcion = OrdenCompraCancelacionTests._recepcion(cls, oc)
        departamento = Departamento.objects.create(empresa=empresa, sucursal=sucursal, codigo="CAL", nombre="Calidad")
        inspector = Empleado.objects.create(
            empresa=empresa, sucursal=sucursal, departamento=departamento,
            puesto=Puesto.objects.create(empresa=empresa, nombre="Inspector"),
            numero_empleado="EMP-966", nombre="Ana", apellido_paterno="Lopez", fecha_ingreso=timezone.localdate(),
        )
        cls.inspeccion = CalidadInspeccion.objects.create(
            recepcion=cls.recepcion, inspector=inspector, fecha=timezone.localdate(), estado="rechazada",
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.a["usuario"])

    def _devolucion(self, cancelado_por=None):
        from compras.models import DevolucionProveedor

        extra = {}
        if cancelado_por is not None:
            extra = {
                "estatus": DevolucionProveedor.Estatus.CANCELADA, "motivo_cancelacion": "x",
                "fecha_cancelacion": timezone.now(), "cancelado_por": cancelado_por,
            }
        return DevolucionProveedor.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"], calidad_inspeccion=self.inspeccion,
            recepcion=self.recepcion, proveedor=self.a["proveedor"], almacen=self.a["almacen"],
            motivo="Rechazo", usuario=self.a["usuario"], **extra,
        )

    def _listado(self):
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        data = resp.json()
        return {f["id"]: f for f in (data["results"] if isinstance(data, dict) and "results" in data else data)}

    def _nombre(self, dev):
        resp = self.client.get(f"{self.URL}{dev.pk}/")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        detalle = resp.json()["cancelado_por_nombre"]
        self.assertEqual(self._listado()[dev.pk]["cancelado_por_nombre"], detalle)
        return detalle

    def test_nombre_correo_o_null(self):
        self.assertEqual(self._nombre(self._devolucion(self.con_nombre)), "Ana López")
        self.assertEqual(self._nombre(self._devolucion(self.sin_nombre)), "sn@acme-cx.test")
        self.assertIsNone(self._nombre(self._devolucion()))

    def test_listado_no_crece_con_canceladas(self):
        def consultas():
            with CaptureQueriesContext(connection) as ctx:
                filas = self._listado()
            return len(ctx.captured_queries), len(filas)

        self._devolucion(self.con_nombre)
        una, _ = consultas()
        for n in range(3):
            usuario = Usuario.objects.create(
                username=f"c{n}@acme-cx.test", email=f"c{n}@acme-cx.test", first_name=f"C{n}",
                empresa=self.a["empresa"],
            )
            self._devolucion(usuario)
        self.assertEqual(consultas(), (una, 4))


class DevolucionProveedorContratoTests(TestCase):
    """EC-973: ``pendientes/`` con el artículo y sin inspecciones anuladas, la bandera
    ``tiene_devolucion_pendiente`` y el filtro ``?devolucion_pendiente=`` de calidad
    (misma regla, acotados a la empresa) y el listado de devoluciones paginado y con
    búsqueda. Empresas, proveedor, almacén y OC de ``OrdenCompraCancelacionTests``."""

    URL = "/api/v1/compras/devoluciones-proveedor/"
    CALIDAD_URL = "/api/v1/compras/calidad-inspecciones/"

    @classmethod
    def setUpTestData(cls):
        from catalogo.models import Talla

        OrdenCompraCancelacionTests.setUpTestData.__func__(cls)
        for clave in ("R-WMS-RECEPCIONES", "C-WMS-RECEPCIONES"):
            _conceder(cls.a["usuario"], clave)
            _conceder(cls.b["usuario"], clave)
        cls.variante = ProductoVariante.objects.create(
            producto=cls.a["producto"], empresa=cls.a["empresa"], sku="INS-973-M",
            color=Color.objects.create(nombre="Azul 973", codigo="A73", codigo_hex="#0000ff"),
            talla=Talla.objects.create(nombre="M-973"), precio_base="1",
        )
        cls.inspectores = {}
        for clave in ("a", "b"):
            t = getattr(cls, clave)
            departamento = Departamento.objects.create(
                empresa=t["empresa"], sucursal=t["sucursal"], codigo="CAL", nombre="Calidad")
            cls.inspectores[clave] = Empleado.objects.create(
                empresa=t["empresa"], sucursal=t["sucursal"], departamento=departamento,
                puesto=Puesto.objects.create(empresa=t["empresa"], nombre="Inspector"),
                numero_empleado=f"EMP-973-{clave}", nombre="Ana", apellido_paterno="Lopez",
                fecha_ingreso=timezone.localdate(),
            )

    def _client(self, user=None):
        client = APIClient()
        client.force_authenticate(user or self.a["usuario"])
        return client

    def _inspeccion(self, resultado="rechazo", tenant="a", estado="rechazada", variante=True):
        """Inspección de una recepción de 10 piezas: 6 aprobadas y 4 con ``resultado``."""
        t = getattr(self, tenant)
        recepcion = Recepcion.objects.create(
            empresa=t["empresa"], sucursal=t["sucursal"], proveedor=t["proveedor"], almacen=t["almacen"],
            usuario=t["usuario"], folio=f"RC-973-{Recepcion.objects.count() + 1}", fecha_recepcion=timezone.now(),
            estatus=Recepcion.EstatusRecepcion.RECIBIDA,
        )
        renglon = RecepcionDetalle.objects.create(
            recepcion=recepcion, producto=t["producto"], cantidad_recibida=10,
            producto_variante=self.variante if variante and tenant == "a" else None,
        )
        inspeccion = CalidadInspeccion.objects.create(
            recepcion=recepcion, inspector=self.inspectores[tenant], fecha=timezone.localdate(), estado=estado,
        )
        CalidadInspeccionDetalle.objects.create(
            calidad_inspeccion=inspeccion, recepcion_detalle=renglon, cantidad_inspeccionada=10,
            cantidad_aprobada=6, cantidad_rechazada=4 if resultado == "rechazo" else 0, resultado=resultado,
        )
        return inspeccion

    def _devolucion(self, inspeccion, cantidad, folio=None, estatus=None, tenant="a"):
        from compras.models import DevolucionProveedor, DevolucionProveedorDetalle

        t = getattr(self, tenant)
        dev = DevolucionProveedor.objects.create(
            empresa=t["empresa"], sucursal=t["sucursal"], calidad_inspeccion=inspeccion,
            recepcion=inspeccion.recepcion, proveedor=t["proveedor"], almacen=t["almacen"], motivo="Rechazo",
            usuario=t["usuario"], folio=folio or f"DVP-973-{DevolucionProveedor.objects.count() + 1}",
            estatus=estatus or DevolucionProveedor.Estatus.APLICADA,
        )
        detalle = inspeccion.detalles.get()
        DevolucionProveedorDetalle.objects.create(
            devolucion=dev, calidad_detalle=detalle, recepcion_detalle=detalle.recepcion_detalle,
            producto=detalle.recepcion_detalle.producto, cantidad=cantidad, costo_unitario=0, importe=0,
        )
        return dev

    def _pendientes(self, inspeccion, user=None):
        resp = self._client(user).get(f"{self.URL}pendientes/", {"calidad_inspeccion": inspeccion.pk})
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return resp.json()

    def _inspecciones(self, user=None, **params):
        resp = self._client(user).get(self.CALIDAD_URL, params)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return {i["id"]: i["tiene_devolucion_pendiente"] for i in resp.json()}

    # --- pendientes/ -------------------------------------------------------------

    def test_pendientes_trae_nombre_sku_talla_y_color(self):
        inspeccion = self._inspeccion()
        detalle = inspeccion.detalles.get()

        with CaptureQueriesContext(connection) as ctx:
            filas = self._pendientes(inspeccion)

        self.assertEqual(len(filas), 1)
        fila = filas[0]
        self.assertEqual(Decimal(fila.pop("cantidad_pendiente")), Decimal("4"))
        self.assertEqual(fila, {
            "calidad_detalle": detalle.pk, "recepcion_detalle": detalle.recepcion_detalle_id,
            "resultado": "rechazo", "producto": self.a["producto"].pk, "producto_nombre": "Insumo acme-cx",
            "producto_variante_sku": "INS-973-M", "producto_variante_talla": "M-973",
            "producto_variante_color": "Azul 973",
        })
        # Sin variante (materia prima): nombre del producto; SKU, talla y color null.
        sin_variante = self._pendientes(self._inspeccion("cuarentena", variante=False))[0]
        self.assertEqual(
            [sin_variante[k] for k in ("producto_nombre", "producto_variante_sku", "producto_variante_talla",
                                        "producto_variante_color")],
            ["Insumo acme-cx", None, None, None],
        )
        self.assertEqual(Decimal(sin_variante["cantidad_pendiente"]), Decimal("4"))
        # Sin consultas por renglón: más renglones, mismas consultas.
        otro = RecepcionDetalle.objects.create(
            recepcion=inspeccion.recepcion, producto=self.a["producto"], producto_variante=self.variante,
            cantidad_recibida=5,
        )
        CalidadInspeccionDetalle.objects.create(
            calidad_inspeccion=inspeccion, recepcion_detalle=otro, cantidad_inspeccionada=5,
            cantidad_rechazada=5, resultado="rechazo",
        )
        with CaptureQueriesContext(connection) as ctx2:
            self.assertEqual(len(self._pendientes(inspeccion)), 2)
        self.assertEqual(len(ctx2.captured_queries), len(ctx.captured_queries))

    def test_pendientes_descuenta_devuelto_y_excluye_anulada(self):
        from compras.models import DevolucionProveedor

        inspeccion = self._inspeccion()
        self._devolucion(inspeccion, "1.5")
        self._devolucion(inspeccion, "2", estatus=DevolucionProveedor.Estatus.CANCELADA)  # no cuenta
        self.assertEqual(Decimal(self._pendientes(inspeccion)[0]["cantidad_pendiente"]), Decimal("2.5"))

        CalidadInspeccion.objects.filter(pk=inspeccion.pk).update(estado=CalidadInspeccion.ESTADO_ANULADA)
        self.assertEqual(self._pendientes(inspeccion), [])

    def test_post_acepta_lo_que_ofrece_pendientes(self):
        inspeccion = self._inspeccion()
        fila = self._pendientes(inspeccion)[0]
        resp = self._client().post(self.URL, {
            "calidad_inspeccion": inspeccion.pk, "motivo": "Defecto",
            "detalles": [{"calidad_detalle": fila["calidad_detalle"], "cantidad": fila["cantidad_pendiente"]}],
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        # Devuelto completo: sin renglones, bandera false y fuera del filtro.
        self.assertEqual(self._pendientes(inspeccion), [])
        self.assertIs(self._inspecciones()[inspeccion.pk], False)
        self.assertNotIn(inspeccion.pk, self._inspecciones(devolucion_pendiente="true"))
        self.assertIn(inspeccion.pk, self._inspecciones(devolucion_pendiente="false"))

    def test_entradas_no_numericas_son_400(self):
        """#1402: ids no enteros en filtros y ``pendientes/`` y cantidades no finitas → 400."""
        for campo in ("recepcion", "calidad_inspeccion", "orden_compra", "proveedor"):
            with self.subTest(campo=campo):
                resp = self._client().get(self.URL, {campo: "abc"})
                self.assertEqual(resp.status_code, 400, resp.content[:300])
                self.assertEqual(resp.json(), {campo: "Debe ser un entero válido."})
        resp = self._client().get(f"{self.URL}pendientes/", {"calidad_inspeccion": "abc"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"calidad_inspeccion": "Debe ser un entero válido."})

        inspeccion = self._inspeccion()
        detalle = inspeccion.detalles.get()
        self.assertEqual(
            [d["id"] for d in self._client().get(self.URL, {"calidad_inspeccion": inspeccion.pk}).json()], [])
        for cantidad in ("NaN", "sNaN", "Infinity", "-Infinity", "abc"):
            with self.subTest(cantidad=cantidad):
                resp = self._client().post(self.URL, {
                    "calidad_inspeccion": inspeccion.pk, "motivo": "Defecto",
                    "detalles": [{"calidad_detalle": detalle.pk, "cantidad": cantidad}],
                }, format="json")
                self.assertEqual(resp.status_code, 400, resp.content[:300])
                self.assertEqual(resp.json(), {"detalles": "Cada renglón lleva calidad_detalle y cantidad."})

    # --- bandera y filtro de calidad -------------------------------------------

    def test_bandera_y_filtro_con_la_regla_de_pendientes(self):
        pendiente = self._inspeccion()
        cuarentena = self._inspeccion("cuarentena")
        parcial = self._inspeccion()
        self._devolucion(parcial, "1")
        devuelta = self._inspeccion()
        self._devolucion(devuelta, "4")
        liberada = self._inspeccion("liberado", estado="aprobada")
        anulada = self._inspeccion(estado=CalidadInspeccion.ESTADO_ANULADA)

        self.assertEqual(self._pendientes(devuelta), [])
        self.assertEqual(Decimal(self._pendientes(parcial)[0]["cantidad_pendiente"]), Decimal("3"))
        self.assertEqual(self._inspecciones(), {
            pendiente.pk: True, cuarentena.pk: True, parcial.pk: True,
            devuelta.pk: False, liberada.pk: False, anulada.pk: False,
        })
        self.assertEqual(
            set(self._inspecciones(devolucion_pendiente="true")), {pendiente.pk, cuarentena.pk, parcial.pk})
        self.assertEqual(
            set(self._inspecciones(devolucion_pendiente="false")), {devuelta.pk, liberada.pk, anulada.pk})
        # Detalle: misma bandera; el filtro no aplica fuera del listado.
        for inspeccion, esperado in ((pendiente, True), (devuelta, False)):
            resp = self._client().get(f"{self.CALIDAD_URL}{inspeccion.pk}/", {"devolucion_pendiente": "true"})
            self.assertEqual(resp.status_code, 200, resp.content[:300])
            self.assertIs(resp.json()["tiene_devolucion_pendiente"], esperado)
        resp = self._client().get(self.CALIDAD_URL, {"devolucion_pendiente": "quizas"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"devolucion_pendiente": "Debe ser true o false."})

    def test_bandera_en_la_respuesta_de_onboarding(self):
        """La respuesta del POST ``onboarding/`` serializa la inspección sin anotar:
        la bandera sale del cálculo de respaldo, con la misma regla."""
        for resultado, rechazada, esperado in (("rechazo", 4, True), ("liberado", 0, False)):
            with self.subTest(resultado=resultado):
                recepcion = Recepcion.objects.create(
                    empresa=self.a["empresa"], sucursal=self.a["sucursal"], proveedor=self.a["proveedor"],
                    almacen=self.a["almacen"], usuario=self.a["usuario"],
                    folio=f"RC-973-OB-{resultado}", fecha_recepcion=timezone.now(),
                    estatus=Recepcion.EstatusRecepcion.EN_CALIDAD,
                )
                renglon = RecepcionDetalle.objects.create(
                    recepcion=recepcion, producto=self.a["producto"], cantidad_recibida=10)
                resp = self._client().post(f"{self.CALIDAD_URL}onboarding/", {
                    "recepcion": recepcion.pk, "inspector": self.inspectores["a"].pk,
                    "detalle": [{"recepcion_detalle": renglon.pk, "cantidad_aprobada": str(10 - rechazada),
                                 "cantidad_rechazada": str(rechazada), "resultado": resultado}],
                }, format="json")
                self.assertEqual(resp.status_code, 200, resp.content[:300])
                self.assertIs(resp.json()["calidad_inspeccion"]["tiene_devolucion_pendiente"], esperado)

    def test_filtro_acotado_a_la_empresa(self):
        propia = self._inspeccion()
        ajena = self._inspeccion(tenant="b")
        self.assertEqual(set(self._inspecciones(devolucion_pendiente="true")), {propia.pk})
        self.assertEqual(self._inspecciones(self.b["usuario"], devolucion_pendiente="true"), {ajena.pk: True})
        self.assertEqual(self._client().get(f"{self.CALIDAD_URL}{ajena.pk}/").status_code, 404)
        resp = self._client().get(f"{self.URL}pendientes/", {"calidad_inspeccion": ajena.pk})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._inspecciones(self.sin_empresa, devolucion_pendiente="true"), {})

    def test_bandera_sin_n_mas_1(self):
        def consultas():
            with CaptureQueriesContext(connection) as ctx:
                filas = self._inspecciones()
            return len(ctx.captured_queries), len(filas)

        self._devolucion(self._inspeccion(), "1")
        una, _ = consultas()
        self._devolucion(self._inspeccion("cuarentena"), "1")
        self._inspeccion()
        self._inspeccion("liberado", estado="aprobada")
        self.assertEqual(consultas(), (una, 4))

    # --- listado de devoluciones -------------------------------------------------

    def test_listado_paginado_y_con_busqueda(self):
        inspeccion = self._inspeccion()
        todos = ["DVP-973-A", "DVP-973-B", "DVP-973-C"]
        for folio in todos:
            self._devolucion(inspeccion, "1", folio=folio)
        self._devolucion(self._inspeccion(tenant="b"), "1", folio="DVP-973-X", tenant="b")

        # Sin ``?limit=``: la lista completa, como antes.
        resp = self._client().get(self.URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 3)

        resp = self._client().get(self.URL, {"limit": 2})
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        pagina = resp.json()
        self.assertEqual(set(pagina), {"count", "next", "previous", "results"})
        self.assertEqual((pagina["count"], len(pagina["results"]), pagina["previous"]), (3, 2, None))
        self.assertIn("offset=2", pagina["next"])
        self.assertEqual(len(self._client().get(self.URL, {"limit": 2, "offset": 2}).json()["results"]), 1)
        self.assertEqual(self._client().get(self.URL, {"limit": 0}).status_code, 400)

        for termino, esperados in (("973-B", ["DVP-973-B"]), (inspeccion.recepcion.folio, todos),
                                   ("Proveedor acme", todos), ("DVP-973-X", [])):
            with self.subTest(search=termino):
                data = self._client().get(self.URL, {"search": termino, "limit": 50}).json()
                self.assertEqual(sorted(d["folio"] for d in data["results"]), esperados)
                self.assertEqual(data["count"], len(esperados))

        data = self._client().get(self.URL, {"ordering": "folio", "limit": 50}).json()
        self.assertEqual([d["folio"] for d in data["results"]], todos)
        # ``?search=`` no saca el detalle (``BusquedaSoloListadoFilter``).
        resp = self._client().get(f"{self.URL}{data['results'][0]['id']}/", {"search": "nada"})
        self.assertEqual(resp.status_code, 200)

    # --- #1409, #1419, #1420, #1421 ----------------------------------------------

    def test_errores_nombran_el_articulo_no_el_id(self):
        """#1421: el 400 de ``detalles`` nombra el renglón por su artículo, no por el
        id de ``CalidadInspeccionDetalle``; nada se escribe. #1409: ``cantidad_pendiente``
        con 4 decimales fijos."""
        from compras.models import DevolucionProveedor

        inspeccion = self._inspeccion()
        detalle = inspeccion.detalles.get()
        self.assertEqual(self._pendientes(inspeccion)[0]["cantidad_pendiente"], "4.0000")
        liberado = CalidadInspeccionDetalle.objects.create(
            calidad_inspeccion=inspeccion, cantidad_inspeccionada=5, cantidad_aprobada=5, resultado="liberado",
            recepcion_detalle=RecepcionDetalle.objects.create(
                recepcion=inspeccion.recepcion, producto=self.a["producto"], cantidad_recibida=5),
        )
        ajeno = self._inspeccion().detalles.get()
        for calidad_detalle, cantidad, mensaje in (
            (detalle.pk, "9", "Insumo acme-cx (SKU INS-973-M, talla M-973, color Azul 973): "
                              "se pueden devolver hasta 4."),
            (detalle.pk, "0", "Insumo acme-cx (SKU INS-973-M, talla M-973, color Azul 973): "
                              "se pueden devolver hasta 4."),
            (liberado.pk, "1", "Insumo acme-cx: no es rechazo ni cuarentena de esta inspección."),
            (ajeno.pk, "1", "Uno de los renglones no pertenece a esta inspección."),
        ):
            with self.subTest(calidad_detalle=calidad_detalle, cantidad=cantidad):
                resp = self._client().post(self.URL, {
                    "calidad_inspeccion": inspeccion.pk, "motivo": "Defecto",
                    "detalles": [{"calidad_detalle": calidad_detalle, "cantidad": cantidad}],
                }, format="json")
                self.assertEqual(resp.status_code, 400, resp.content[:300])
                self.assertEqual(resp.json(), {"detalles": mensaje})
        self.assertFalse(DevolucionProveedor.objects.exists())

    def test_alta_con_total_en_la_bitacora_y_articulo_en_el_detalle(self):
        """#1420: la bitácora registra el total real en el alta. #1409: el detalle trae
        nombre, SKU, talla y color como ``pendientes/``, sin consultas por renglón."""
        from auditoria.models import BitacoraDocumento

        inspeccion = self._inspeccion()
        oc = OrdenCompra.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"], proveedor=self.a["proveedor"],
            moneda=self.moneda, usuario=self.a["usuario"], fecha_oc=timezone.now().date(), estatus=Estatus.AUTORIZADA,
        )
        linea = OrdenCompraDetalle.objects.create(
            orden_compra=oc, producto=self.a["producto"], sucursal=self.a["sucursal"], cantidad=10,
            precio=Decimal("12.50"), importe=Decimal("125.00"),
        )
        RecepcionDetalle.objects.filter(pk=inspeccion.detalles.get().recepcion_detalle_id).update(
            orden_compra_detalle=linea)

        resp = self._client().post(
            self.URL, {"calidad_inspeccion": inspeccion.pk, "motivo": "Defecto"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        data = resp.json()
        self.assertEqual(data["total"], "50.00")  # 4 × 12.50
        alta = BitacoraDocumento.objects.get(
            tipo_documento="devolucion_proveedor", documento_id=str(data["id"]), accion="crear")
        self.assertEqual(alta.total, Decimal("50.00"))
        renglon = data["detalles"][0]
        self.assertEqual(
            [renglon[k] for k in ("producto_nombre", "producto_variante_sku", "producto_variante_talla",
                                  "producto_variante_color", "cantidad", "importe")],
            ["Insumo acme-cx", "INS-973-M", "M-973", "Azul 973", "4.0000", "50.00"],
        )

        def consultas():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self._client().get(self.URL).status_code, 200)
            return len(ctx.captured_queries)

        una = consultas()
        self._devolucion(self._inspeccion(), "1")
        self._devolucion(self._inspeccion(variante=False), "2")
        self.assertEqual(consultas(), una)

    def test_ordena_por_la_razon_social_que_muestra(self):
        """#1419: ``proveedor_nombre`` es la razón social y se ordena con
        ``?ordering=proveedor__razon_social`` (el nombre comercial va al revés)."""
        segundo = Proveedor.objects.get(pk=self.a["proveedor"].pk)
        segundo.pk, segundo._state.adding = None, True
        segundo.codigo, segundo.nombre, segundo.razon_social = "PROV-1419", "AAA comercial", "ZZZ SA"
        segundo.email = "p1419@acme-cx.test"
        segundo.save()
        Proveedor.objects.filter(pk=self.a["proveedor"].pk).update(nombre="ZZZ comercial", razon_social="AAA SA")
        self._devolucion(self._inspeccion(), "1", folio="DVP-1419-A")
        dev = self._devolucion(self._inspeccion(), "1", folio="DVP-1419-Z")
        type(dev).objects.filter(pk=dev.pk).update(proveedor=segundo)

        for orden, esperado in (("proveedor__razon_social", ["AAA SA", "ZZZ SA"]),
                                ("-proveedor__razon_social", ["ZZZ SA", "AAA SA"]),
                                ("proveedor__nombre", ["ZZZ SA", "AAA SA"])):
            with self.subTest(ordering=orden):
                resp = self._client().get(self.URL, {"ordering": orden})
                self.assertEqual(resp.status_code, 200, resp.content[:300])
                self.assertEqual([d["proveedor_nombre"] for d in resp.json()], esperado)

    # --- EC-975: almacén y sucursal por nombre; otra empresa ------------------------

    def _consultas(self, url):
        with CaptureQueriesContext(connection) as ctx:
            resp = self._client().get(url)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return resp.json(), len(ctx.captured_queries)

    def test_detalle_y_listado_traen_almacen_y_sucursal_sin_consultas_extra(self):
        """EC-975 (para el PDF de EC-976): ``almacen_nombre`` y ``sucursal_nombre`` como
        en recepciones, del ``select_related``: las consultas del detalle y del listado
        no cambian ni crecen con las filas."""
        dev = self._devolucion(self._inspeccion(), "1")
        esperado = {"almacen": self.a["almacen"].pk, "almacen_nombre": self.a["almacen"].nombre,
                    "sucursal_nombre": self.a["sucursal"].nombre}

        data, detalle = self._consultas(f"{self.URL}{dev.pk}/")
        self.assertEqual({k: data[k] for k in esperado}, esperado)
        filas, una = self._consultas(self.URL)
        self.assertEqual({k: filas[0][k] for k in esperado}, esperado)

        self._devolucion(self._inspeccion(), "1")
        self._devolucion(self._inspeccion(variante=False), "2")
        filas, tres = self._consultas(self.URL)
        self.assertEqual(len(filas), 3)
        self.assertEqual(tres, una)
        # Las mismas que antes de EC-975 (permiso de lectura + documentos + renglones);
        # sin ``sucursal``/``almacen`` en el ``select_related`` serían 2 más por documento.
        self.assertEqual((detalle, una), (3, 3))

    def test_detalle_de_otra_empresa_es_404(self):
        """La devolución de otra empresa no se ve ni se cancela (404) y no cambia."""
        from compras.models import DevolucionProveedor

        ajena = self._devolucion(self._inspeccion(tenant="b"), "1", tenant="b")
        self.assertEqual(self._client().get(f"{self.URL}{ajena.pk}/").status_code, 404)
        self.assertEqual(self._client().get(self.URL).json(), [])
        resp = self._client().post(f"{self.URL}{ajena.pk}/cancelar/", {"motivo": "No"}, format="json")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(DevolucionProveedor.objects.get(pk=ajena.pk).estatus, DevolucionProveedor.Estatus.APLICADA)
        # Su empresa sí la ve.
        self.assertEqual(self._client(self.b["usuario"]).get(f"{self.URL}{ajena.pk}/").status_code, 200)
