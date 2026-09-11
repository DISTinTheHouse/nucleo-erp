"""Verificación de los 4 defectos corregidos en ``finanzas``.

Ejecutar SIEMPRE con BD desechable (el ``.env`` del repo apunta a Supabase de
producción):

    python manage.py test finanzas --settings=sqlite_settings

Nota: SQLite ignora ``select_for_update()`` (Django lo omite en backends sin
soporte), así que estos tests cubren el filtro por empresa y los guards, no la
semántica del lock.
"""

import importlib
import inspect
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models.signals import post_delete
from django.test import TestCase
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient
from rest_framework.viewsets import ViewSetMixin

from catalogo.models import Producto, Talla
from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle
from finanzas.api import views as finanzas_views
from finanzas.api.serializers import CuentaPorPagarSerializer, PolizaDetalleRelacionadoSerializer
from finanzas.api.views import (
    CobroViewSet,
    CuentaPorPagarViewSet,
    ErroresDeNegocioComo400Mixin,
    FacturaProveedorViewSet,
    NotaCreditoViewSet,
)
from finanzas.exceptions import (
    CONCURRENT_OPERATION_MESSAGE,
    ConcurrentOperationError,
    ErrorDeNegocio,
)
from finanzas.models import (
    Banco,
    CentroCosto,
    Cobro,
    CuentaBancaria,
    CuentaContable,
    CuentaPorCobrar,
    CuentaPorPagar,
    Factura,
    FacturaDetalle,
    FacturaProveedor,
    NotaCredito,
    NotaCreditoDetalle,
    Pago,
    PagoDetalle,
    Poliza,
    PolizaDetalle,
)
from inventarios.models import Almacen
from nucleo.models import (
    Empresa,
    Moneda,
    SatFormaPago,
    SatMetodoPago,
    SatRegimenFiscal,
    SerieFolio,
    Sucursal,
)
from terceros.models import Cliente, Proveedor
from usuarios.models import Usuario
from ventas.models import Pedido, PedidoDetalle, PedidoDetalleTalla

ONBOARDING_URL = "/api/v1/finanzas/facturas/onboarding/"
DESDE_PEDIDO_URL = "/api/v1/finanzas/facturas/desde-pedido/"
PENDIENTE_COBRO_URL = "/api/v1/finanzas/facturas/registrar-pendiente-cobro/"
CXC_URL = "/api/v1/finanzas/cuentas-por-cobrar/"
POLIZAS_URL = "/api/v1/finanzas/polizas/"
COBROS_URL = "/api/v1/finanzas/cobros/"
PAGOS_URL = "/api/v1/finanzas/pagos/"
NOTAS_CREDITO_URL = "/api/v1/finanzas/notas-credito/"
FACTURAS_PROVEEDOR_URL = "/api/v1/finanzas/facturas-proveedor/"


class FinanzasBase(TestCase):
    @classmethod
    def _tenant(cls, codigo, codigo_sucursal, email, *, sucursal_activa=True):
        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        sucursal = Sucursal.objects.create(
            empresa=empresa,
            codigo=codigo_sucursal,
            nombre=codigo_sucursal,
            activo=sucursal_activa,
        )
        cliente = Cliente.objects.create(
            empresa=empresa, nombre=f"Cliente {codigo}", correo=f"pagos@{codigo}.test"
        )
        usuario = Usuario.objects.create(
            username=email, email=email, empresa=empresa, sucursal_default=sucursal
        )
        pedido = Pedido.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            cliente=cliente,
            moneda=cls.moneda,
            folio=f"PED-{codigo}",
            persona_pagos="Pagos",
            correo_facturas=email,
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )
        producto = Producto.objects.create(empresa=empresa, nombre=f"Prod {codigo}")
        detalle = PedidoDetalle.objects.create(
            pedido=pedido, producto=producto, precio_unitario=Decimal("100.00")
        )
        PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=cls.talla, cantidad=3
        )
        SerieFolio.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            tipo_documento="Factura",
            serie="A",
            folio_actual=0,
        )
        return {
            "empresa": empresa,
            "sucursal": sucursal,
            "cliente": cliente,
            "usuario": usuario,
            "pedido": pedido,
            "producto": producto,
            "detalle": detalle,
        }

    @classmethod
    def setUpTestData(cls):
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.talla = Talla.objects.create(nombre="M")
        cls.sat_regimen_fiscal = SatRegimenFiscal.objects.create(codigo="601", descripcion="General de Ley")
        cls.sat_forma_pago = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        cls.sat_metodo_pago = SatMetodoPago.objects.create(codigo="PUE", descripcion="Pago en una sola exhibición")
        cls.a = cls._tenant("acme", "MTY", "a@acme.test")
        cls.b = cls._tenant("globex", "GDL", "b@globex.test")

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _crear_cuentas_contables(self, empresa):
        cuenta_cargo = CuentaContable.objects.create(
            empresa=empresa, codigo="1050", nombre="Clientes",
            tipo=CuentaContable.CuentaTipo.ACTIVO,
        )
        cuenta_abono = CuentaContable.objects.create(
            empresa=empresa, codigo="4000", nombre="Ventas",
            tipo=CuentaContable.CuentaTipo.INGRESO,
        )
        centro_costo = CentroCosto.objects.create(empresa=empresa, codigo="CC01", nombre="General")
        return cuenta_cargo, cuenta_abono, centro_costo

    def _crear_cuenta_bancaria(self, empresa):
        banco = Banco.objects.create(empresa=empresa, nombre="Banco Test")
        return CuentaBancaria.objects.create(
            empresa=empresa, banco=banco, moneda=self.moneda, alias="Cuenta operativa",
        )

    def _crear_oc_recepcion_y_factura_proveedor(self, empresa, sucursal, usuario):
        proveedor = Proveedor.objects.create(
            empresa=empresa, nombre="Proveedor Test", moneda=self.moneda,
            sat_regimen_fiscal=self.sat_regimen_fiscal, sat_forma_pago=self.sat_forma_pago,
            sat_metodo_pago=self.sat_metodo_pago, codigo=f"PROV-{empresa.pk}", razon_social="Proveedor Test SA",
            telefono="8100000000", contacto_principal="Contacto", rfc="XAXX010101000",
            email="proveedor@test.mx",
        )
        producto = Producto.objects.create(empresa=empresa, nombre="Insumo Test")
        oc = OrdenCompra.objects.create(
            empresa=empresa, sucursal=sucursal, proveedor=proveedor, moneda=self.moneda,
            usuario=usuario, fecha_oc=date.today(),
        )
        oc_detalle = OrdenCompraDetalle.objects.create(
            orden_compra=oc, producto=producto, sucursal=sucursal, cantidad=10, precio=Decimal("50.00"),
        )
        almacen = Almacen.objects.create(
            empresa=empresa, sucursal=sucursal, codigo=f"A-{oc.pk}", nombre="Almacen Test",
        )
        recepcion = Recepcion.objects.create(
            orden_compra=oc, empresa=empresa, sucursal=sucursal, proveedor=proveedor,
            almacen=almacen, usuario=usuario, folio=f"REC-{empresa.pk}-{oc.pk}",
            fecha_recepcion=timezone.now(),
        )
        recepcion_detalle = RecepcionDetalle.objects.create(
            recepcion=recepcion, orden_compra_detalle=oc_detalle, producto=producto,
            cantidad_recibida=10,
        )
        factura_proveedor = FacturaProveedor.objects.create(
            empresa=empresa, sucursal=sucursal, proveedor=proveedor, oc=oc,
            recepcion=recepcion, moneda=self.moneda,
        )
        return proveedor, producto, oc, oc_detalle, recepcion, recepcion_detalle, factura_proveedor


class Defecto1OnboardingAislamiento(FinanzasBase):
    """POST /facturas/onboarding/ debe acotar ``pedido`` y ``pedido_detalle`` a
    la empresa del usuario y bloquear la doble facturación, igual que
    ``desde_pedido``."""

    def _payload(self, pedido, detalle, cantidad=3):
        return {
            "pedido": pedido.pk,
            "factura_detalles": [
                {"pedido_detalle": detalle.pk, "cantidad": cantidad}
            ],
        }

    def test_rechaza_pedido_de_otra_empresa(self):
        client = self._client(self.a["usuario"])
        resp = client.post(
            ONBOARDING_URL,
            self._payload(self.b["pedido"], self.b["detalle"]),
            format="json",
        )
        self.assertEqual(resp.status_code, 404, resp.data)
        self.assertFalse(Factura.objects.exists())

    def test_rechaza_pedido_detalle_de_otra_empresa(self):
        client = self._client(self.a["usuario"])
        payload = self._payload(self.a["pedido"], self.b["detalle"])
        resp = client.post(ONBOARDING_URL, payload, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("factura_detalles", resp.data)
        self.assertFalse(Factura.objects.exists())

    def test_bloquea_segunda_factura_del_mismo_pedido(self):
        client = self._client(self.a["usuario"])
        payload = self._payload(self.a["pedido"], self.a["detalle"])
        primera = client.post(ONBOARDING_URL, payload, format="json")
        self.assertEqual(primera.status_code, 200, primera.data)

        segunda = client.post(ONBOARDING_URL, payload, format="json")
        self.assertEqual(segunda.status_code, 400, segunda.data)
        self.assertIn("pedido", segunda.data)
        self.assertEqual(Factura.objects.count(), 1)

    def test_mismo_mensaje_de_guard_que_desde_pedido(self):
        """El contrato del guard debe ser idéntico al de ``desde_pedido``."""
        client_a = self._client(self.a["usuario"])
        client_b = self._client(self.b["usuario"])

        client_a.post(
            ONBOARDING_URL,
            self._payload(self.a["pedido"], self.a["detalle"]),
            format="json",
        )
        onboarding = client_a.post(
            ONBOARDING_URL,
            self._payload(self.a["pedido"], self.a["detalle"]),
            format="json",
        )

        client_b.post(DESDE_PEDIDO_URL, {"pedido": self.b["pedido"].pk}, format="json")
        desde_pedido = client_b.post(
            DESDE_PEDIDO_URL, {"pedido": self.b["pedido"].pk}, format="json"
        )

        self.assertEqual(onboarding.status_code, desde_pedido.status_code)
        self.assertEqual(str(onboarding.data["pedido"][0]), str(desde_pedido.data["pedido"][0]))

    def test_acepta_pedido_propio(self):
        client = self._client(self.a["usuario"])
        resp = client.post(
            ONBOARDING_URL,
            self._payload(self.a["pedido"], self.a["detalle"]),
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        factura = Factura.objects.get()
        self.assertEqual(factura.empresa_id, self.a["empresa"].pk)
        self.assertEqual(factura.pedido_id, self.a["pedido"].pk)
        self.assertEqual(factura.cliente_id, self.a["cliente"].pk)
        self.assertEqual(factura.total, Decimal("300.00"))

    def test_pedido_ausente_devuelve_400_no_500(self):
        client = self._client(self.a["usuario"])
        resp = client.post(
            ONBOARDING_URL,
            {"factura_detalles": [{"pedido_detalle": self.a["detalle"].pk, "cantidad": 1}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("pedido", resp.data)

    def test_desde_pedido_sigue_rechazando_otra_empresa(self):
        """Referencia: no se alteró el aislamiento de ``desde_pedido``."""
        client = self._client(self.a["usuario"])
        resp = client.post(
            DESDE_PEDIDO_URL, {"pedido": self.b["pedido"].pk}, format="json"
        )
        self.assertEqual(resp.status_code, 404, resp.data)

    def test_rechaza_serie_folio_de_otra_empresa(self):
        """``serie_folio`` es el tercer FK escribible del serializer y también
        se resolvía contra el manager por defecto."""
        serie_ajena = SerieFolio.objects.get(sucursal=self.b["sucursal"])
        payload = self._payload(self.a["pedido"], self.a["detalle"])
        payload["serie_folio"] = serie_ajena.pk

        resp = self._client(self.a["usuario"]).post(
            ONBOARDING_URL, payload, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("serie_folio", resp.data)
        self.assertFalse(Factura.objects.exists())

    def test_rechaza_linea_de_otro_pedido_de_la_misma_empresa(self):
        """Acotar las líneas solo por empresa dejaba facturar renglones (y
        precios) de un pedido distinto al que se está facturando."""
        otro_pedido = Pedido.objects.create(
            empresa=self.a["empresa"],
            sucursal=self.a["sucursal"],
            cliente=self.a["cliente"],
            moneda=self.moneda,
            folio="PED-acme-2",
            persona_pagos="Pagos",
            correo_facturas="a@acme.test",
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )
        linea_ajena = PedidoDetalle.objects.create(
            pedido=otro_pedido,
            producto=self.a["producto"],
            precio_unitario=Decimal("999.00"),
        )

        resp = self._client(self.a["usuario"]).post(
            ONBOARDING_URL,
            self._payload(self.a["pedido"], linea_ajena, cantidad=1),
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("factura_detalles", resp.data)
        self.assertFalse(Factura.objects.exists())

    def test_rechaza_factura_sin_lineas(self):
        """Una factura vacía se colaba con total 0.00 y, por el guard de doble
        facturación, dejaba el pedido inhabilitado para siempre."""
        serie = SerieFolio.objects.get(sucursal=self.a["sucursal"])
        client = self._client(self.a["usuario"])

        resp = client.post(
            ONBOARDING_URL,
            {"pedido": self.a["pedido"].pk, "factura_detalles": []},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("factura_detalles", resp.data)
        self.assertFalse(Factura.objects.exists())

        # El rechazo ocurre antes de consumir folio.
        serie.refresh_from_db()
        self.assertEqual(serie.folio_actual, 0)

        # Y el pedido sigue siendo facturable.
        posterior = client.post(
            DESDE_PEDIDO_URL, {"pedido": self.a["pedido"].pk}, format="json"
        )
        self.assertEqual(posterior.status_code, 201, posterior.data)


class Defecto2PolizaDetalleSerializer(FinanzasBase):
    """``PolizaDetalleRelacionadoSerializer`` ya no revienta por ``source``
    redundante, y sigue devolviendo los mismos enteros."""

    def _crear_cuentas(self, empresa):
        CuentaContable.objects.create(
            empresa=empresa, codigo="1050", nombre="Clientes",
            tipo=CuentaContable.CuentaTipo.ACTIVO,
        )
        CuentaContable.objects.create(
            empresa=empresa, codigo="4000", nombre="Ventas",
            tipo=CuentaContable.CuentaTipo.INGRESO,
        )
        CuentaContable.objects.create(
            empresa=empresa, codigo="2080", nombre="IVA trasladado",
            tipo=CuentaContable.CuentaTipo.PASIVO,
        )
        return CentroCosto.objects.create(
            empresa=empresa, codigo="CC01", nombre="General"
        )

    def test_serializer_se_instancia_sin_error(self):
        campos = list(PolizaDetalleRelacionadoSerializer().fields)
        self.assertIn("cuenta_contable_id", campos)
        self.assertIn("centro_costo_id", campos)

    def test_devuelve_los_enteros_correctos(self):
        empresa = self.a["empresa"]
        centro_costo = self._crear_cuentas(empresa)
        cuenta = CuentaContable.objects.filter(empresa=empresa, codigo="1050").get()
        poliza = Poliza.objects.create(
            empresa=empresa, sucursal=self.a["sucursal"], centro_costo=centro_costo,
            folio="POL-000001", folio_consecutivo=1,
        )
        detalle = PolizaDetalle.objects.create(
            poliza=poliza, cuenta_contable=cuenta, centro_costo=centro_costo,
            cargo=Decimal("116.00"), abono=Decimal("0.00"), orden=1,
        )

        data = PolizaDetalleRelacionadoSerializer(detalle).data
        self.assertEqual(data["cuenta_contable_id"], cuenta.pk)
        self.assertEqual(data["centro_costo_id"], centro_costo.pk)
        self.assertIsInstance(data["cuenta_contable_id"], int)
        self.assertIsInstance(data["centro_costo_id"], int)
        self.assertEqual(data["cuenta_contable_codigo"], "1050")
        self.assertEqual(data["centro_costo_nombre"], "General")

    def test_detalle_de_cxc_responde_200_con_polizas(self):
        """El escenario que antes daba 500: CxC creada por
        ``registrar-pendiente-cobro`` (que siempre genera póliza)."""
        empresa = self.a["empresa"]
        self._crear_cuentas(empresa)
        client = self._client(self.a["usuario"])

        creada = client.post(
            PENDIENTE_COBRO_URL,
            {
                "cliente": self.a["cliente"].pk,
                "moneda": self.moneda.pk,
                "folio": "F-001",
                "subtotal": "100.00",
                "descuento": "0.00",
                "impuestos": "16.00",
                "total": "116.00",
            },
            format="json",
        )
        self.assertEqual(creada.status_code, 201, creada.data)
        cxc_id = creada.data["cuenta_por_cobrar"]["id"]

        detalle = client.get(f"{CXC_URL}{cxc_id}/")
        self.assertEqual(detalle.status_code, 200, detalle.data)
        self.assertTrue(detalle.data["polizas"], "la póliza debe venir serializada")
        renglon = detalle.data["polizas"][0]["detalles"][0]
        self.assertIsInstance(renglon["cuenta_contable_id"], int)
        self.assertIsInstance(renglon["centro_costo_id"], int)


class Defecto3FechaEmision(FinanzasBase):
    """``fecha_emision``/``fecha`` se fijan al crear y ya no se reescriben."""

    CAMPOS = [
        (Factura, "fecha_emision"),
        (CuentaPorCobrar, "fecha_emision"),
        (FacturaProveedor, "fecha_emision"),
        (CuentaPorPagar, "fecha_emision"),
        (Poliza, "fecha"),
    ]

    def test_los_cinco_campos_son_auto_now_add(self):
        for modelo, nombre in self.CAMPOS:
            with self.subTest(modelo=modelo.__name__):
                campo = modelo._meta.get_field(nombre)
                self.assertFalse(campo.auto_now, f"{modelo.__name__}.{nombre} sigue en auto_now")
                self.assertTrue(campo.auto_now_add)
                # auto_now_add mantiene el campo no editable: los serializers
                # con fields='__all__' lo siguen exponiendo como read-only.
                self.assertFalse(campo.editable)

    def test_factura_no_reescribe_fecha_emision_en_save(self):
        factura = Factura.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"],
            cliente=self.a["cliente"], moneda=self.moneda, folio="F-100",
        )
        antigua = date(2020, 1, 15)
        Factura.objects.filter(pk=factura.pk).update(fecha_emision=antigua)
        factura.refresh_from_db()

        factura.observaciones = "editada"
        factura.save()
        factura.refresh_from_db()

        self.assertEqual(factura.fecha_emision, antigua)

    def test_cuenta_por_cobrar_no_reescribe_fecha_emision_en_save(self):
        factura = Factura.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"],
            cliente=self.a["cliente"], moneda=self.moneda, folio="F-101",
        )
        cxc = CuentaPorCobrar.objects.create(
            cliente=self.a["cliente"], factura=factura,
            total=Decimal("100.00"), saldo=Decimal("100.00"),
        )
        antigua = date(2019, 6, 30)
        CuentaPorCobrar.objects.filter(pk=cxc.pk).update(fecha_emision=antigua)
        cxc.refresh_from_db()

        # El ciclo real de una CxC: se abona y cambia de estatus.
        cxc.saldo = Decimal("0.00")
        cxc.estatus = CuentaPorCobrar.EstatusCxC.PAGADA
        cxc.save()
        cxc.refresh_from_db()

        self.assertEqual(cxc.fecha_emision, antigua)

    def test_poliza_no_reescribe_fecha_en_save(self):
        centro_costo = CentroCosto.objects.create(
            empresa=self.a["empresa"], codigo="CC01", nombre="General"
        )
        poliza = Poliza.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"],
            centro_costo=centro_costo, folio="POL-000001", folio_consecutivo=1,
        )
        antigua = date(2021, 3, 1)
        Poliza.objects.filter(pk=poliza.pk).update(fecha=antigua)
        poliza.refresh_from_db()

        poliza.concepto = "reclasificado"
        poliza.save()
        poliza.refresh_from_db()

        self.assertEqual(poliza.fecha, antigua)

    def test_fecha_emision_sigue_siendo_read_only_en_el_serializer(self):
        """No hay cambio de contrato: sigue siendo de solo lectura."""
        from finanzas.api.serializers import CuentaPorCobrarSerializer, FacturaSerializer

        self.assertTrue(FacturaSerializer().fields["fecha_emision"].read_only)
        self.assertTrue(CuentaPorCobrarSerializer().fields["fecha_emision"].read_only)


class Defecto4SucursalDefault(FinanzasBase):
    """``desde_pedido`` y ``onboarding`` sin ``sucursal_default`` responden 400
    limpio (antes 500)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Usuario de la empresa A sin sucursal_default.
        cls.sin_sucursal = Usuario.objects.create(
            username="sin@acme.test", email="sin@acme.test", empresa=cls.a["empresa"]
        )
        # Tenant cuya única sucursal está inactiva -> no hay respaldo posible.
        cls.c = cls._tenant("initech", "QRO", "c@initech.test", sucursal_activa=False)
        cls.c_sin_sucursal = Usuario.objects.create(
            username="sin@initech.test", email="sin@initech.test", empresa=cls.c["empresa"]
        )

    def test_sin_sucursal_default_usa_el_respaldo_y_factura(self):
        client = self._client(self.sin_sucursal)
        resp = client.post(
            DESDE_PEDIDO_URL, {"pedido": self.a["pedido"].pk}, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        factura = Factura.objects.get()
        self.assertEqual(factura.sucursal_id, self.a["sucursal"].pk)

    def test_sin_sucursal_disponible_devuelve_400(self):
        client = self._client(self.c_sin_sucursal)
        resp = client.post(
            DESDE_PEDIDO_URL, {"pedido": self.c["pedido"].pk}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("sucursal", resp.data)
        self.assertFalse(Factura.objects.exists())

    def test_mismo_contrato_400_que_registrar_pendiente_cobro(self):
        client = self._client(self.c_sin_sucursal)
        desde_pedido = client.post(
            DESDE_PEDIDO_URL, {"pedido": self.c["pedido"].pk}, format="json"
        )
        pendiente = client.post(
            PENDIENTE_COBRO_URL,
            {
                "cliente": self.c["cliente"].pk,
                "moneda": self.moneda.pk,
                "subtotal": "100.00",
                "total": "100.00",
            },
            format="json",
        )
        self.assertEqual(desde_pedido.status_code, pendiente.status_code)
        self.assertEqual(
            str(desde_pedido.data["sucursal"][0]), str(pendiente.data["sucursal"][0])
        )

    def test_sucursal_default_de_otra_empresa_no_se_usa(self):
        """``_get_default_sucursal`` valida la empresa: aporta aislamiento."""
        usuario = Usuario.objects.create(
            username="cruzado@acme.test",
            email="cruzado@acme.test",
            empresa=self.a["empresa"],
            sucursal_default=self.b["sucursal"],
        )
        client = self._client(usuario)
        resp = client.post(
            DESDE_PEDIDO_URL, {"pedido": self.a["pedido"].pk}, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        factura = Factura.objects.get()
        self.assertEqual(factura.sucursal_id, self.a["sucursal"].pk)
        self.assertNotEqual(factura.sucursal_id, self.b["sucursal"].pk)

    def test_onboarding_sin_sucursal_default_usa_el_respaldo(self):
        """``store_factura`` leía ``sucursal_default`` por su cuenta: sin ella
        el folio no resolvía y el error escapaba como 500."""
        client = self._client(self.sin_sucursal)
        resp = client.post(
            ONBOARDING_URL,
            {
                "pedido": self.a["pedido"].pk,
                "factura_detalles": [
                    {"pedido_detalle": self.a["detalle"].pk, "cantidad": 3}
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        factura = Factura.objects.get()
        self.assertEqual(factura.sucursal_id, self.a["sucursal"].pk)

    def test_onboarding_sin_sucursal_disponible_devuelve_400(self):
        client = self._client(self.c_sin_sucursal)
        resp = client.post(
            ONBOARDING_URL,
            {
                "pedido": self.c["pedido"].pk,
                "factura_detalles": [
                    {"pedido_detalle": self.c["detalle"].pk, "cantidad": 1}
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("sucursal", resp.data)
        self.assertFalse(Factura.objects.exists())


class Defecto5PendienteCobroDobleFacturacion(FinanzasBase):
    """``registrar_pendiente_cobro`` aceptaba un ``pedido`` ya facturado: no
    aplicaba el guard de doble facturación ni bloqueaba el Pedido."""

    def test_rechaza_pedido_ya_facturado(self):
        client = self._client(self.a["usuario"])
        primera = client.post(
            DESDE_PEDIDO_URL, {"pedido": self.a["pedido"].pk}, format="json"
        )
        self.assertEqual(primera.status_code, 201, primera.data)

        segunda = client.post(
            PENDIENTE_COBRO_URL,
            {
                "cliente": self.a["cliente"].pk,
                "moneda": self.moneda.pk,
                "pedido": self.a["pedido"].pk,
                "folio": "F-DUP",
                "subtotal": "100.00",
                "total": "100.00",
            },
            format="json",
        )
        self.assertEqual(segunda.status_code, 400, segunda.data)
        self.assertIn("pedido", segunda.data)
        self.assertEqual(
            Factura.objects.filter(pedido=self.a["pedido"], activo=True).count(), 1
        )

    def test_sin_pedido_sigue_registrando(self):
        """No regresión: el guard solo aplica cuando viene ``pedido``."""
        empresa = self.a["empresa"]
        CuentaContable.objects.create(
            empresa=empresa, codigo="1050", nombre="Clientes",
            tipo=CuentaContable.CuentaTipo.ACTIVO,
        )
        CuentaContable.objects.create(
            empresa=empresa, codigo="4000", nombre="Ventas",
            tipo=CuentaContable.CuentaTipo.INGRESO,
        )
        CentroCosto.objects.create(empresa=empresa, codigo="CC01", nombre="General")

        resp = self._client(self.a["usuario"]).post(
            PENDIENTE_COBRO_URL,
            {
                "cliente": self.a["cliente"].pk,
                "moneda": self.moneda.pk,
                "folio": "F-001",
                "subtotal": "100.00",
                "total": "100.00",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Factura.objects.count(), 1)


class Defecto6AltaLineasHijas(FinanzasBase):
    """Alta de líneas hijas en los 5 documentos de finanzas. Solo ``create()``;
    ``update()`` de renglones queda fuera (ver DOCS/negocio/bloqueo-lineas-hijas-finanzas.md)."""

    # -- Póliza -----------------------------------------------------------

    def test_poliza_crea_detalles_anidados(self):
        empresa = self.a["empresa"]
        cuenta_cargo, cuenta_abono, centro_costo = self._crear_cuentas_contables(empresa)
        client = self._client(self.a["usuario"])

        resp = client.post(
            POLIZAS_URL,
            {
                "sucursal": self.a["sucursal"].pk,
                "centro_costo": centro_costo.pk,
                "tipo": Poliza.PolizaTipo.DIARIO.value,
                "poliza_detalles": [
                    {"cuenta_contable": cuenta_cargo.pk, "centro_costo": centro_costo.pk, "cargo": "100.00", "abono": "0.00", "orden": 1},
                    {"cuenta_contable": cuenta_abono.pk, "centro_costo": centro_costo.pk, "cargo": "0.00", "abono": "100.00", "orden": 2},
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        poliza = Poliza.objects.get(pk=resp.data["id"])
        self.assertEqual(poliza.poliza_detalles.count(), 2)
        self.assertEqual(resp.data["total_cargos"], "100.00")
        self.assertEqual(resp.data["total_abonos"], "100.00")

    def test_poliza_rechaza_cuenta_contable_de_otra_empresa(self):
        empresa_a = self.a["empresa"]
        _, _, centro_costo = self._crear_cuentas_contables(empresa_a)
        cuenta_ajena, _, _ = self._crear_cuentas_contables(self.b["empresa"])
        client = self._client(self.a["usuario"])

        resp = client.post(
            POLIZAS_URL,
            {
                "sucursal": self.a["sucursal"].pk,
                "centro_costo": centro_costo.pk,
                "poliza_detalles": [
                    {"cuenta_contable": cuenta_ajena.pk, "cargo": "100.00", "abono": "0.00"},
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("cuenta_contable", resp.data)
        self.assertFalse(PolizaDetalle.objects.filter(poliza__empresa=empresa_a).exists())

    def test_poliza_update_no_escribe_detalles(self):
        """PATCH con poliza_detalles no crea/edita líneas."""
        empresa = self.a["empresa"]
        cuenta_cargo, _, centro_costo = self._crear_cuentas_contables(empresa)
        poliza = Poliza.objects.create(empresa=empresa, sucursal=self.a["sucursal"], centro_costo=centro_costo)
        client = self._client(self.a["usuario"])

        resp = client.patch(
            f"{POLIZAS_URL}{poliza.pk}/",
            {"poliza_detalles": [{"cuenta_contable": cuenta_cargo.pk, "cargo": "50.00"}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(poliza.poliza_detalles.count(), 0)

    # -- Cobro --------------------------------------------------------------

    def test_cobro_crea_detalle_y_aplica_saldo(self):
        empresa = self.a["empresa"]
        cuenta_bancaria = self._crear_cuenta_bancaria(empresa)
        factura = Factura.objects.create(
            empresa=empresa, sucursal=self.a["sucursal"], cliente=self.a["cliente"], moneda=self.moneda,
        )
        cxc = CuentaPorCobrar.objects.create(
            empresa=empresa, cliente=self.a["cliente"], factura=factura,
            total=Decimal("100.00"), saldo=Decimal("100.00"),
        )
        client = self._client(self.a["usuario"])

        resp = client.post(
            COBROS_URL,
            {
                "cliente": self.a["cliente"].pk,
                "cuenta_bancaria": cuenta_bancaria.pk,
                "total_cobrado": "100.00",
                "cobro_detalles": [{"cxc": cxc.pk, "importe_aplicado": "100.00"}],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        cobro = Cobro.objects.get(pk=resp.data["id"])
        self.assertEqual(cobro.cobro_detalles.count(), 1)
        self.assertEqual(cobro.estatus, Cobro.Estatus.APLICADO)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("0.00"))
        self.assertEqual(cxc.estatus, CuentaPorCobrar.EstatusCxC.PAGADA)

    def test_cobro_rechaza_cxc_de_otra_empresa(self):
        empresa_a = self.a["empresa"]
        cuenta_bancaria = self._crear_cuenta_bancaria(empresa_a)
        factura_b = Factura.objects.create(
            empresa=self.b["empresa"], sucursal=self.b["sucursal"], cliente=self.b["cliente"], moneda=self.moneda,
        )
        cxc_ajena = CuentaPorCobrar.objects.create(
            empresa=self.b["empresa"], cliente=self.b["cliente"], factura=factura_b,
            total=Decimal("100.00"), saldo=Decimal("100.00"),
        )
        client = self._client(self.a["usuario"])

        resp = client.post(
            COBROS_URL,
            {
                "cliente": self.a["cliente"].pk,
                "cuenta_bancaria": cuenta_bancaria.pk,
                "total_cobrado": "100.00",
                "cobro_detalles": [{"cxc": cxc_ajena.pk, "importe_aplicado": "100.00"}],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("cobro_detalles", resp.data)

    # -- Pago -----------------------------------------------------------------

    def test_pago_crea_detalle_y_aplica_saldo(self):
        empresa = self.a["empresa"]
        cuenta_bancaria = self._crear_cuenta_bancaria(empresa)
        _, _, _, _, _, _, factura_proveedor = self._crear_oc_recepcion_y_factura_proveedor(
            empresa, self.a["sucursal"], self.a["usuario"]
        )
        cxp = CuentaPorPagar.objects.create(
            empresa=empresa, proveedor=factura_proveedor.proveedor, factura_proveedor=factura_proveedor,
            total=Decimal("50.00"), saldo=Decimal("50.00"),
        )
        client = self._client(self.a["usuario"])

        resp = client.post(
            PAGOS_URL,
            {
                "proveedor": factura_proveedor.proveedor.pk,
                "cuenta_bancaria": cuenta_bancaria.pk,
                "total_pagado": "50.00",
                "pago_detalles": [{"cxp": cxp.pk, "importe_aplicado": "50.00"}],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        pago = Pago.objects.get(pk=resp.data["id"])
        self.assertEqual(pago.pago_detalles.count(), 1)
        cxp.refresh_from_db()
        self.assertEqual(cxp.saldo, Decimal("0.00"))
        self.assertEqual(cxp.estatus, CuentaPorPagar.EstatusCxP.PAGADA)

    # -- Nota de crédito --------------------------------------------------------

    def test_nota_credito_crea_detalle(self):
        empresa = self.a["empresa"]
        factura = Factura.objects.create(
            empresa=empresa, sucursal=self.a["sucursal"], cliente=self.a["cliente"], moneda=self.moneda,
            total=Decimal("116.00"),
        )
        cxc = CuentaPorCobrar.objects.create(
            empresa=empresa, cliente=self.a["cliente"], factura=factura,
            total=Decimal("116.00"), saldo=Decimal("116.00"),
        )
        factura_detalle = FacturaDetalle.objects.create(
            factura=factura, pedido_detalle=self.a["detalle"], producto=self.a["producto"],
            cantidad=1, precio_unitario=Decimal("100.00"), total=Decimal("116.00"),
        )
        client = self._client(self.a["usuario"])

        resp = client.post(
            NOTAS_CREDITO_URL,
            {
                "factura": factura.pk,
                "cliente": self.a["cliente"].pk,
                "estatus": NotaCredito.Estatus.EMITIDA.value,
                "total": "116.00",
                "nota_credito_detalles": [
                    {"factura_detalle": factura_detalle.pk, "cantidad": 1, "total": "116.00"},
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        nota = NotaCredito.objects.get(pk=resp.data["id"])
        self.assertEqual(nota.nota_credito_detalles.count(), 1)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("0.00"))

    def test_nota_credito_rechaza_factura_detalle_de_otra_factura(self):
        empresa = self.a["empresa"]
        factura = Factura.objects.create(
            empresa=empresa, sucursal=self.a["sucursal"], cliente=self.a["cliente"], moneda=self.moneda,
        )
        otra_factura = Factura.objects.create(
            empresa=empresa, sucursal=self.a["sucursal"], cliente=self.a["cliente"], moneda=self.moneda,
        )
        detalle_ajeno = FacturaDetalle.objects.create(
            factura=otra_factura, pedido_detalle=self.a["detalle"], producto=self.a["producto"],
            cantidad=1, total=Decimal("50.00"),
        )
        client = self._client(self.a["usuario"])

        resp = client.post(
            NOTAS_CREDITO_URL,
            {
                "factura": factura.pk,
                "cliente": self.a["cliente"].pk,
                "total": "50.00",
                "nota_credito_detalles": [
                    {"factura_detalle": detalle_ajeno.pk, "cantidad": 1, "total": "50.00"},
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("factura_detalle", resp.data)

    # -- Factura de proveedor ----------------------------------------------

    def test_factura_proveedor_crea_detalle(self):
        empresa = self.a["empresa"]
        (
            proveedor, producto, oc, oc_detalle, recepcion, recepcion_detalle, _,
        ) = self._crear_oc_recepcion_y_factura_proveedor(empresa, self.a["sucursal"], self.a["usuario"])
        client = self._client(self.a["usuario"])

        resp = client.post(
            FACTURAS_PROVEEDOR_URL,
            {
                "proveedor": proveedor.pk,
                "sucursal": self.a["sucursal"].pk,
                "oc": oc.pk,
                "recepcion": recepcion.pk,
                "moneda": self.moneda.pk,
                "factura_proveedor_detalles": [
                    {
                        "oc_detalle": oc_detalle.pk,
                        "recepcion_detalle": recepcion_detalle.pk,
                        "producto": producto.pk,
                        "cantidad": "10",
                        "precio_unitario": "50.00",
                        "total": "500.00",
                    },
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        factura_proveedor = FacturaProveedor.objects.get(pk=resp.data["id"])
        self.assertEqual(factura_proveedor.factura_proveedor_detalles.count(), 1)

    def test_factura_proveedor_rechaza_detalle_de_otra_oc(self):
        empresa = self.a["empresa"]
        (
            proveedor, producto, oc, _oc_detalle, recepcion, recepcion_detalle, _,
        ) = self._crear_oc_recepcion_y_factura_proveedor(empresa, self.a["sucursal"], self.a["usuario"])
        _, _, otra_oc, otro_oc_detalle, _, _, _ = self._crear_oc_recepcion_y_factura_proveedor(
            empresa, self.a["sucursal"], self.a["usuario"]
        )
        client = self._client(self.a["usuario"])

        resp = client.post(
            FACTURAS_PROVEEDOR_URL,
            {
                "proveedor": proveedor.pk,
                "sucursal": self.a["sucursal"].pk,
                "oc": oc.pk,
                "recepcion": recepcion.pk,
                "moneda": self.moneda.pk,
                "factura_proveedor_detalles": [
                    {
                        "oc_detalle": otro_oc_detalle.pk,
                        "recepcion_detalle": recepcion_detalle.pk,
                        "producto": producto.pk,
                        "cantidad": "10",
                        "total": "500.00",
                    },
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("oc_detalle", resp.data)


class Defecto7ErroresDeNegocioYTransacciones(FinanzasBase):
    """Regresión de los arreglos de plomería de errores y de transacciones.

    Cada bloque anota el arreglo que blinda. Todos fallan si ese arreglo se
    revierte: es justo lo que faltó en 5898286, cuyos 10 tests pasaban igual con
    el bug puesto.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Únicos fixtures nuevos: los dos perfiles que la base no tenía y que
        # las pruebas de ``empresa`` server-side necesitan.
        cls.superusuario = Usuario.objects.create_superuser(
            username="root@test.mx", email="root@test.mx", password="x",
        )
        cls.admin_empresa = Usuario.objects.create(
            username="admin@acme.test", email="admin@acme.test",
            empresa=cls.a["empresa"], sucursal_default=cls.a["sucursal"],
            is_admin_empresa=True,
        )

    # -- helpers locales ----------------------------------------------------

    def _cxc(self, monto="100.00"):
        empresa = self.a["empresa"]
        factura = Factura.objects.create(
            empresa=empresa, sucursal=self.a["sucursal"],
            cliente=self.a["cliente"], moneda=self.moneda,
        )
        return CuentaPorCobrar.objects.create(
            empresa=empresa, cliente=self.a["cliente"], factura=factura,
            total=Decimal(monto), saldo=Decimal(monto),
        )

    def _cxp(self, tenant, monto="100.00"):
        proveedor, _, _, _, _, _, factura_proveedor = self._crear_oc_recepcion_y_factura_proveedor(
            tenant["empresa"], tenant["sucursal"], tenant["usuario"],
        )
        cxp = CuentaPorPagar.objects.create(
            empresa=tenant["empresa"], proveedor=proveedor,
            factura_proveedor=factura_proveedor,
            total=Decimal(monto), saldo=Decimal(monto),
        )
        return proveedor, cxp

    def _poliza_cuadrada(self, client, centro_costo, cargo, abono):
        return client.post(
            POLIZAS_URL,
            {
                "sucursal": self.a["sucursal"].pk,
                "centro_costo": centro_costo.pk,
                "tipo": Poliza.PolizaTipo.DIARIO.value,
                "poliza_detalles": [
                    {"cuenta_contable": cargo.pk, "cargo": "100.00", "abono": "0.00"},
                    {"cuenta_contable": abono.pk, "cargo": "0.00", "abono": "100.00"},
                ],
            },
            format="json",
        )

    def _asserta_error_de_campo(self, resp, campo, fragmento=None):
        """El cuerpo de error siempre es ``{"campo": ["mensaje", ...]}``."""
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn(campo, resp.data)
        self.assertIsInstance(resp.data[campo], list, resp.data)
        self.assertTrue(resp.data[campo], resp.data)
        if fragmento is not None:
            self.assertIn(fragmento, str(resp.data[campo][0]))

    # == 2. PATCH con líneas anidadas se ignora, no revienta =================
    # Blinda: CreateOnlyNestedLinesMixin en CobroSerializer y PagoSerializer.
    # Sin el mixin, DRF levanta AssertionError en raise_errors_on_nested_writes
    # y estas pruebas fallan con 500.

    def test_cobro_patch_ignora_lineas_y_actualiza_encabezado(self):
        cuenta_bancaria = self._crear_cuenta_bancaria(self.a["empresa"])
        cxc = self._cxc()
        cobro = Cobro.objects.create(
            empresa=self.a["empresa"], cliente=self.a["cliente"],
            cuenta_bancaria=cuenta_bancaria, total_cobrado=Decimal("0.00"),
            estatus=Cobro.Estatus.BORRADOR.value, observaciones="antes",
        )
        client = self._client(self.a["usuario"])

        resp = client.patch(
            f"{COBROS_URL}{cobro.pk}/",
            {
                "observaciones": "despues",
                "cobro_detalles": [{"cxc": cxc.pk, "importe_aplicado": "10.00"}],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        cobro.refresh_from_db()
        self.assertEqual(cobro.cobro_detalles.count(), 0)
        self.assertEqual(cobro.observaciones, "despues")

    def test_pago_patch_ignora_lineas_y_actualiza_encabezado(self):
        cuenta_bancaria = self._crear_cuenta_bancaria(self.a["empresa"])
        proveedor, cxp = self._cxp(self.a)
        pago = Pago.objects.create(
            empresa=self.a["empresa"], proveedor=proveedor,
            cuenta_bancaria=cuenta_bancaria, total_pagado=Decimal("0.00"),
            estatus=Pago.Estatus.BORRADOR.value, observaciones="antes",
        )
        client = self._client(self.a["usuario"])

        resp = client.patch(
            f"{PAGOS_URL}{pago.pk}/",
            {
                "observaciones": "despues",
                "pago_detalles": [{"cxp": cxp.pk, "importe_aplicado": "10.00"}],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        pago.refresh_from_db()
        self.assertEqual(pago.pago_detalles.count(), 0)
        self.assertEqual(pago.observaciones, "despues")

    def test_nota_credito_patch_ignora_lineas(self):
        empresa = self.a["empresa"]
        factura = Factura.objects.create(
            empresa=empresa, sucursal=self.a["sucursal"],
            cliente=self.a["cliente"], moneda=self.moneda,
        )
        factura_detalle = FacturaDetalle.objects.create(
            factura=factura, pedido_detalle=self.a["detalle"], producto=self.a["producto"],
            cantidad=1, total=Decimal("50.00"),
        )
        nota = NotaCredito.objects.create(
            factura=factura, cliente=self.a["cliente"], total=Decimal("50.00"),
            estatus=NotaCredito.Estatus.BORRADOR.value, motivo="antes",
        )
        client = self._client(self.a["usuario"])

        resp = client.patch(
            f"{NOTAS_CREDITO_URL}{nota.pk}/",
            {
                "motivo": "despues",
                "nota_credito_detalles": [
                    {"factura_detalle": factura_detalle.pk, "cantidad": 1, "total": "50.00"},
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        nota.refresh_from_db()
        self.assertEqual(nota.nota_credito_detalles.count(), 0)
        self.assertEqual(nota.motivo, "despues")

    def test_factura_proveedor_patch_ignora_lineas(self):
        (
            _, producto, _, oc_detalle, _, recepcion_detalle, factura_proveedor,
        ) = self._crear_oc_recepcion_y_factura_proveedor(
            self.a["empresa"], self.a["sucursal"], self.a["usuario"],
        )
        client = self._client(self.a["usuario"])

        resp = client.patch(
            f"{FACTURAS_PROVEEDOR_URL}{factura_proveedor.pk}/",
            {
                "observaciones": "despues",
                "factura_proveedor_detalles": [
                    {
                        "oc_detalle": oc_detalle.pk,
                        "recepcion_detalle": recepcion_detalle.pk,
                        "producto": producto.pk,
                        "cantidad": "1.00",
                    },
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        factura_proveedor.refresh_from_db()
        self.assertEqual(factura_proveedor.factura_proveedor_detalles.count(), 0)
        self.assertEqual(factura_proveedor.observaciones, "despues")

    # == 3. Póliza: contabilizar y cancelar ==================================
    # Blinda: quitar ``updated_at`` de ``update_fields`` en PolizaService.
    # Con el campo inexistente, Django levanta ValueError y ambas dan 500.

    def test_poliza_contabilizar_cambia_estatus(self):
        cargo, abono, centro_costo = self._crear_cuentas_contables(self.a["empresa"])
        client = self._client(self.a["usuario"])
        creada = self._poliza_cuadrada(client, centro_costo, cargo, abono)
        self.assertEqual(creada.status_code, 201, creada.data)

        resp = client.post(f"{POLIZAS_URL}{creada.data['id']}/contabilizar/", {}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        poliza = Poliza.objects.get(pk=creada.data["id"])
        self.assertEqual(poliza.estatus, Poliza.PolizaStatus.CONTABILIZADA.value)

    def test_poliza_cancelar_cambia_estatus(self):
        cargo, abono, centro_costo = self._crear_cuentas_contables(self.a["empresa"])
        client = self._client(self.a["usuario"])
        creada = self._poliza_cuadrada(client, centro_costo, cargo, abono)

        resp = client.post(f"{POLIZAS_URL}{creada.data['id']}/cancelar/", {}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        poliza = Poliza.objects.get(pk=creada.data["id"])
        self.assertEqual(poliza.estatus, Poliza.PolizaStatus.CANCELADA.value)

    # == 4 y 6. Reglas de negocio: 400 con cuerpo en forma de lista ==========
    # Blinda: ErroresDeNegocioComo400Mixin + ErrorDeNegocio en los servicios.
    # Sin la traducción, la ValidationError de Django escapa como 500.

    def test_cobro_importe_mayor_al_saldo_devuelve_400(self):
        cuenta_bancaria = self._crear_cuenta_bancaria(self.a["empresa"])
        cxc = self._cxc("100.00")
        client = self._client(self.a["usuario"])

        resp = client.post(
            COBROS_URL,
            {
                "cliente": self.a["cliente"].pk,
                "cuenta_bancaria": cuenta_bancaria.pk,
                "total_cobrado": "500.00",
                "cobro_detalles": [{"cxc": cxc.pk, "importe_aplicado": "500.00"}],
            },
            format="json",
        )
        self._asserta_error_de_campo(resp, "cobro_detalles", "excede saldo")
        self.assertEqual(Cobro.objects.count(), 0)

    def test_cobro_suma_distinta_al_total_devuelve_400(self):
        cuenta_bancaria = self._crear_cuenta_bancaria(self.a["empresa"])
        cxc = self._cxc("100.00")
        client = self._client(self.a["usuario"])

        resp = client.post(
            COBROS_URL,
            {
                "cliente": self.a["cliente"].pk,
                "cuenta_bancaria": cuenta_bancaria.pk,
                "total_cobrado": "100.00",
                "cobro_detalles": [{"cxc": cxc.pk, "importe_aplicado": "50.00"}],
            },
            format="json",
        )
        self._asserta_error_de_campo(resp, "total_cobrado", "debe coincidir con total_cobrado")

    def test_pago_sin_lineas_devuelve_400(self):
        cuenta_bancaria = self._crear_cuenta_bancaria(self.a["empresa"])
        proveedor, _ = self._cxp(self.a)
        client = self._client(self.a["usuario"])

        resp = client.post(
            PAGOS_URL,
            {
                "proveedor": proveedor.pk,
                "cuenta_bancaria": cuenta_bancaria.pk,
                "total_pagado": "100.00",
            },
            format="json",
        )
        self._asserta_error_de_campo(resp, "pago_detalles", "al menos un detalle")
        self.assertEqual(Pago.objects.count(), 0)

    def test_contabilizar_poliza_descuadrada_devuelve_400(self):
        cargo, _, centro_costo = self._crear_cuentas_contables(self.a["empresa"])
        client = self._client(self.a["usuario"])
        creada = client.post(
            POLIZAS_URL,
            {
                "sucursal": self.a["sucursal"].pk,
                "centro_costo": centro_costo.pk,
                "poliza_detalles": [{"cuenta_contable": cargo.pk, "cargo": "100.00", "abono": "0.00"}],
            },
            format="json",
        )
        self.assertEqual(creada.status_code, 201, creada.data)

        resp = client.post(f"{POLIZAS_URL}{creada.data['id']}/contabilizar/", {}, format="json")

        self._asserta_error_de_campo(resp, "poliza_detalles", "debe ser igual a la")
        poliza = Poliza.objects.get(pk=creada.data["id"])
        self.assertEqual(poliza.estatus, Poliza.PolizaStatus.BORRADOR.value)

    def test_validacion_del_viewset_tambien_viene_en_lista(self):
        """Las dos fuentes de error —viewset y servicio— comparten forma."""
        _, _, centro_costo = self._crear_cuentas_contables(self.a["empresa"])
        cuenta_ajena, _, _ = self._crear_cuentas_contables(self.b["empresa"])
        client = self._client(self.a["usuario"])

        resp = client.post(
            POLIZAS_URL,
            {
                "sucursal": self.a["sucursal"].pk,
                "centro_costo": centro_costo.pk,
                "poliza_detalles": [{"cuenta_contable": cuenta_ajena.pk, "cargo": "100.00"}],
            },
            format="json",
        )
        self._asserta_error_de_campo(resp, "cuenta_contable", "no pertenece a la misma empresa")

    # == 5 y 9. El estrechamiento y la cobertura por construcción ============
    # Blinda: ``isinstance(exc, ErrorDeNegocio)`` en el mixin y las bases
    # FinanzasBase*ViewSet. No hay endpoint que levante una ValidationError de
    # Django no-de-negocio sin añadir código de producción, así que el
    # estrechamiento se prueba en la propia frontera.

    def test_mixin_convierte_error_de_negocio_a_400(self):
        viewset = CobroViewSet()
        viewset.headers = {}

        resp = viewset.handle_exception(
            ErrorDeNegocio({"cobro_detalles": ["Importe excede saldo."]})
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("cobro_detalles", resp.data)
        self.assertIsInstance(resp.data["cobro_detalles"], list)

    def test_mixin_deja_pasar_la_validation_error_de_django(self):
        viewset = CobroViewSet()
        viewset.headers = {}

        with self.assertRaises(DjangoValidationError):
            viewset.handle_exception(
                DjangoValidationError({"campo": ["Dato corrupto en base."]})
            )

    def test_todos_los_viewsets_de_finanzas_traducen_el_error_de_negocio(self):
        # Se filtra por herencia y no por sufijo del nombre: hay viewsets que no
        # terminan en "ViewSet" (``ClienteViewSetContabilidad``) y un filtro por
        # nombre los dejaría fuera del barrido sin avisar.
        # ``dict.fromkeys`` deduplica: ``ClienteViewSet`` es un alias de
        # ``ClienteViewSetContabilidad`` y ``getmembers`` lo devuelve dos veces.
        viewsets_finanzas = list(dict.fromkeys(
            clase
            for _, clase in inspect.getmembers(finanzas_views, inspect.isclass)
            if clase.__module__ == finanzas_views.__name__
            and issubclass(clase, ViewSetMixin)
            and not clase.__name__.startswith("FinanzasBase")
        ))
        self.assertEqual(len(viewsets_finanzas), 17, sorted(c.__name__ for c in viewsets_finanzas))
        sin_cubrir = [
            c.__name__ for c in viewsets_finanzas
            if not issubclass(c, ErroresDeNegocioComo400Mixin)
        ]
        self.assertEqual(sin_cubrir, [])

    def test_un_viewset_sin_servicios_tambien_traduce(self):
        """FacturaProveedorViewSet no llevaba el mixin antes de las bases."""
        viewset = FacturaProveedorViewSet()
        viewset.headers = {}

        resp = viewset.handle_exception(ErrorDeNegocio({"oc": ["Regla de negocio."]}))

        self.assertEqual(resp.status_code, 400)
        self.assertIn("oc", resp.data)

    # == 7. Tenencia por línea: 400 y CERO filas =============================
    # Blinda: ``@transaction.atomic`` en PolizaViewSet.perform_create y en
    # FacturaProveedorViewSet.perform_create. Sin él el encabezado ya está
    # confirmado y queda huérfano pese al 400.

    def test_poliza_con_linea_ajena_no_deja_encabezado_huerfano(self):
        _, _, centro_costo = self._crear_cuentas_contables(self.a["empresa"])
        cuenta_ajena, _, _ = self._crear_cuentas_contables(self.b["empresa"])
        client = self._client(self.a["usuario"])
        polizas_antes = Poliza.objects.count()

        resp = client.post(
            POLIZAS_URL,
            {
                "sucursal": self.a["sucursal"].pk,
                "centro_costo": centro_costo.pk,
                "poliza_detalles": [{"cuenta_contable": cuenta_ajena.pk, "cargo": "100.00"}],
            },
            format="json",
        )

        self._asserta_error_de_campo(resp, "cuenta_contable")
        self.assertEqual(Poliza.objects.count(), polizas_antes)
        self.assertEqual(PolizaDetalle.objects.count(), 0)

    def test_poliza_con_segunda_linea_ajena_revierte_la_primera(self):
        cargo, _, centro_costo = self._crear_cuentas_contables(self.a["empresa"])
        cuenta_ajena, _, _ = self._crear_cuentas_contables(self.b["empresa"])
        client = self._client(self.a["usuario"])
        polizas_antes = Poliza.objects.count()

        resp = client.post(
            POLIZAS_URL,
            {
                "sucursal": self.a["sucursal"].pk,
                "centro_costo": centro_costo.pk,
                "poliza_detalles": [
                    {"cuenta_contable": cargo.pk, "cargo": "100.00", "abono": "0.00"},
                    {"cuenta_contable": cuenta_ajena.pk, "cargo": "0.00", "abono": "100.00"},
                ],
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(Poliza.objects.count(), polizas_antes)
        self.assertEqual(PolizaDetalle.objects.count(), 0)

    def test_factura_proveedor_con_linea_invalida_no_deja_encabezado_huerfano(self):
        (
            proveedor, _, oc, oc_detalle, recepcion, recepcion_detalle, _,
        ) = self._crear_oc_recepcion_y_factura_proveedor(
            self.a["empresa"], self.a["sucursal"], self.a["usuario"],
        )
        producto_ajeno = Producto.objects.create(empresa=self.b["empresa"], nombre="Insumo ajeno")
        client = self._client(self.a["usuario"])
        facturas_antes = FacturaProveedor.objects.count()

        resp = client.post(
            FACTURAS_PROVEEDOR_URL,
            {
                "sucursal": self.a["sucursal"].pk,
                "proveedor": proveedor.pk,
                "oc": oc.pk,
                "recepcion": recepcion.pk,
                "moneda": self.moneda.pk,
                "folio": "FP-HUERFANA",
                "factura_proveedor_detalles": [
                    {
                        "oc_detalle": oc_detalle.pk,
                        "recepcion_detalle": recepcion_detalle.pk,
                        "producto": producto_ajeno.pk,
                        "cantidad": "1.00",
                    },
                ],
            },
            format="json",
        )

        self._asserta_error_de_campo(resp, "producto", "no pertenece a la misma empresa")
        self.assertEqual(FacturaProveedor.objects.count(), facturas_antes)
        self.assertFalse(FacturaProveedor.objects.filter(folio="FP-HUERFANA").exists())

    def test_pago_con_cxp_de_otra_empresa_se_rechaza(self):
        cuenta_bancaria = self._crear_cuenta_bancaria(self.a["empresa"])
        proveedor_propio, _ = self._cxp(self.a)
        _, cxp_ajena = self._cxp(self.b)
        client = self._client(self.a["usuario"])
        pagos_antes = Pago.objects.count()

        resp = client.post(
            PAGOS_URL,
            {
                "proveedor": proveedor_propio.pk,
                "cuenta_bancaria": cuenta_bancaria.pk,
                "total_pagado": "100.00",
                "estatus": Pago.Estatus.BORRADOR.value,
                "pago_detalles": [{"cxp": cxp_ajena.pk, "importe_aplicado": "100.00"}],
            },
            format="json",
        )

        self._asserta_error_de_campo(resp, "pago_detalles", "no pertenece a la misma empresa")
        self.assertEqual(Pago.objects.count(), pagos_antes)

    # == 8. ``empresa`` la resuelve el servidor ==============================
    # Blinda: EmpresaResueltaEnServidorMixin.

    def test_usuario_normal_no_manda_empresa_y_cae_en_la_suya(self):
        cargo, _, centro_costo = self._crear_cuentas_contables(self.a["empresa"])
        client = self._client(self.a["usuario"])

        resp = client.post(
            POLIZAS_URL,
            {
                "sucursal": self.a["sucursal"].pk,
                "centro_costo": centro_costo.pk,
                "poliza_detalles": [{"cuenta_contable": cargo.pk, "cargo": "1.00"}],
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        poliza = Poliza.objects.get(pk=resp.data["id"])
        self.assertEqual(poliza.empresa_id, self.a["empresa"].pk)

    def test_empresa_ajena_en_el_payload_se_ignora(self):
        cargo, _, centro_costo = self._crear_cuentas_contables(self.a["empresa"])
        client = self._client(self.admin_empresa)

        resp = client.post(
            POLIZAS_URL,
            {
                "empresa": self.b["empresa"].pk,
                "sucursal": self.a["sucursal"].pk,
                "centro_costo": centro_costo.pk,
                "poliza_detalles": [{"cuenta_contable": cargo.pk, "cargo": "1.00"}],
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        poliza = Poliza.objects.get(pk=resp.data["id"])
        self.assertEqual(poliza.empresa_id, self.a["empresa"].pk)
        self.assertFalse(Poliza.objects.filter(empresa=self.b["empresa"]).exists())

    def test_superusuario_si_manda_empresa_explicita(self):
        cargo, _, centro_costo = self._crear_cuentas_contables(self.b["empresa"])
        client = self._client(self.superusuario)

        resp = client.post(
            POLIZAS_URL,
            {
                "empresa": self.b["empresa"].pk,
                "sucursal": self.b["sucursal"].pk,
                "centro_costo": centro_costo.pk,
                "poliza_detalles": [{"cuenta_contable": cargo.pk, "cargo": "1.00"}],
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        poliza = Poliza.objects.get(pk=resp.data["id"])
        self.assertEqual(poliza.empresa_id, self.b["empresa"].pk)

    def test_patch_no_reasigna_la_empresa(self):
        _, _, centro_costo = self._crear_cuentas_contables(self.a["empresa"])
        poliza = Poliza.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"], centro_costo=centro_costo,
        )
        client = self._client(self.a["usuario"])

        resp = client.patch(
            f"{POLIZAS_URL}{poliza.pk}/",
            {"empresa": self.b["empresa"].pk, "concepto": "editado"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        poliza.refresh_from_db()
        self.assertEqual(poliza.empresa_id, self.a["empresa"].pk)
        self.assertEqual(poliza.concepto, "editado")


class Defecto8NotaCreditoCancelarRevierteCxC(FinanzasBase):
    """Cancelar una nota de crédito emitida devuelve el importe a la CxC.

    Antes, ``cancelar`` sólo cambiaba el estatus: el saldo de la CxC quedaba
    rebajado para siempre. Cobro y Pago sí revierten al cancelar.
    """

    def _factura_con_cxc(self, total="1000.00"):
        empresa = self.a["empresa"]
        factura = Factura.objects.create(
            empresa=empresa, sucursal=self.a["sucursal"],
            cliente=self.a["cliente"], moneda=self.moneda, total=Decimal(total),
        )
        cxc = CuentaPorCobrar.objects.create(
            empresa=empresa, cliente=self.a["cliente"], factura=factura,
            total=Decimal(total), saldo=Decimal(total),
        )
        return factura, cxc

    def test_cancelar_nota_emitida_devuelve_el_saldo_y_recalcula_estatus(self):
        factura, cxc = self._factura_con_cxc("1000.00")
        client = self._client(self.a["usuario"])

        resp = client.post(
            NOTAS_CREDITO_URL,
            {
                "factura": factura.pk, "cliente": self.a["cliente"].pk,
                "estatus": NotaCredito.Estatus.EMITIDA.value, "total": "250.00",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        nota_id = resp.data["id"]
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("750.00"))
        self.assertEqual(cxc.estatus, CuentaPorCobrar.EstatusCxC.PARCIAL)

        resp = client.post(f"{NOTAS_CREDITO_URL}{nota_id}/cancelar/", {}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["estatus"], NotaCredito.Estatus.CANCELADA.value)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("1000.00"))
        self.assertEqual(cxc.estatus, CuentaPorCobrar.EstatusCxC.PENDIENTE)

    def test_cancelar_nota_que_dejo_la_cxc_pagada_la_regresa_a_pendiente(self):
        factura, cxc = self._factura_con_cxc("500.00")
        client = self._client(self.a["usuario"])
        resp = client.post(
            NOTAS_CREDITO_URL,
            {
                "factura": factura.pk, "cliente": self.a["cliente"].pk,
                "estatus": NotaCredito.Estatus.EMITIDA.value, "total": "500.00",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("0.00"))
        self.assertEqual(cxc.estatus, CuentaPorCobrar.EstatusCxC.PAGADA)

        client.post(f"{NOTAS_CREDITO_URL}{resp.data['id']}/cancelar/", {}, format="json")

        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("500.00"))
        self.assertEqual(cxc.estatus, CuentaPorCobrar.EstatusCxC.PENDIENTE)

    def test_cancelar_nota_en_borrador_no_acredita_nada(self):
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = NotaCredito.objects.create(
            factura=factura, cliente=self.a["cliente"], total=Decimal("300.00"),
            estatus=NotaCredito.Estatus.BORRADOR.value,
        )
        client = self._client(self.a["usuario"])

        resp = client.post(f"{NOTAS_CREDITO_URL}{nota.pk}/cancelar/", {}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        nota.refresh_from_db()
        self.assertEqual(nota.estatus, NotaCredito.Estatus.CANCELADA.value)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("1000.00"))
        self.assertEqual(cxc.estatus, CuentaPorCobrar.EstatusCxC.PENDIENTE)

    def test_cancelar_dos_veces_no_revierte_dos_veces(self):
        factura, cxc = self._factura_con_cxc("1000.00")
        client = self._client(self.a["usuario"])
        resp = client.post(
            NOTAS_CREDITO_URL,
            {
                "factura": factura.pk, "cliente": self.a["cliente"].pk,
                "estatus": NotaCredito.Estatus.EMITIDA.value, "total": "400.00",
            },
            format="json",
        )
        nota_id = resp.data["id"]
        client.post(f"{NOTAS_CREDITO_URL}{nota_id}/cancelar/", {}, format="json")

        resp = client.post(f"{NOTAS_CREDITO_URL}{nota_id}/cancelar/", {}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("1000.00"))

    def test_cancelar_nota_de_otra_empresa_sigue_fuera_de_alcance(self):
        empresa_b = self.b["empresa"]
        factura = Factura.objects.create(
            empresa=empresa_b, sucursal=self.b["sucursal"],
            cliente=self.b["cliente"], moneda=self.moneda, total=Decimal("100.00"),
        )
        cxc = CuentaPorCobrar.objects.create(
            empresa=empresa_b, cliente=self.b["cliente"], factura=factura,
            total=Decimal("100.00"), saldo=Decimal("100.00"),
        )
        nota = NotaCredito.objects.create(
            factura=factura, cliente=self.b["cliente"], total=Decimal("100.00"),
            estatus=NotaCredito.Estatus.EMITIDA.value,
        )
        client = self._client(self.a["usuario"])

        resp = client.post(f"{NOTAS_CREDITO_URL}{nota.pk}/cancelar/", {}, format="json")

        self.assertEqual(resp.status_code, 404)
        nota.refresh_from_db()
        cxc.refresh_from_db()
        self.assertEqual(nota.estatus, NotaCredito.Estatus.EMITIDA.value)
        self.assertEqual(cxc.saldo, Decimal("100.00"))


class Defecto9NotaCreditoFechaEmisionYCxCFaltante(FinanzasBase):
    """``fecha_emision`` deja de moverse y emitir sin CxC deja de pasar en silencio.

    ``fecha_emision`` era ``auto_now``: cancelar la nota la reescribía al día
    de hoy. Y ``aplicar_nota_credito`` hacía ``return`` cuando la factura no
    tenía CxC, así que la emisión respondía 201 sin acreditar nada.
    """

    def _factura(self, total="1000.00"):
        return Factura.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"],
            cliente=self.a["cliente"], moneda=self.moneda, total=Decimal(total),
        )

    def _factura_con_cxc(self, total="1000.00"):
        factura = self._factura(total)
        cxc = CuentaPorCobrar.objects.create(
            empresa=self.a["empresa"], cliente=self.a["cliente"], factura=factura,
            total=Decimal(total), saldo=Decimal(total),
        )
        return factura, cxc

    def _emitir(self, client, factura, total):
        return client.post(
            NOTAS_CREDITO_URL,
            {
                "factura": factura.pk, "cliente": self.a["cliente"].pk,
                "estatus": NotaCredito.Estatus.EMITIDA.value, "total": total,
            },
            format="json",
        )

    def test_fecha_emision_se_estampa_al_emitir(self):
        factura, _ = self._factura_con_cxc()
        resp = self._emitir(self._client(self.a["usuario"]), factura, "100.00")

        self.assertEqual(resp.status_code, 201, resp.data)
        nota = NotaCredito.objects.get(pk=resp.data["id"])
        self.assertEqual(nota.fecha_emision, timezone.localdate())

    def test_cancelar_no_mueve_la_fecha_de_emision(self):
        factura, _ = self._factura_con_cxc()
        client = self._client(self.a["usuario"])
        resp = self._emitir(client, factura, "100.00")
        nota = NotaCredito.objects.get(pk=resp.data["id"])
        # La nota se emitió hace un mes; ``auto_now`` reescribía este valor en
        # cada save(), incluido el de cancelar.
        ayer = timezone.localdate() - timedelta(days=30)
        NotaCredito.objects.filter(pk=nota.pk).update(fecha_emision=ayer)

        resp = client.post(f"{NOTAS_CREDITO_URL}{nota.pk}/cancelar/", {}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        nota.refresh_from_db()
        self.assertEqual(nota.estatus, NotaCredito.Estatus.CANCELADA.value)
        self.assertEqual(nota.fecha_emision, ayer)

    def test_editar_una_nota_no_mueve_la_fecha_de_emision(self):
        factura = self._factura()
        nota = NotaCredito.objects.create(
            factura=factura, cliente=self.a["cliente"], total=Decimal("50.00"),
            estatus=NotaCredito.Estatus.BORRADOR.value, motivo="antes",
        )
        ayer = timezone.localdate() - timedelta(days=30)
        NotaCredito.objects.filter(pk=nota.pk).update(fecha_emision=ayer)

        resp = self._client(self.a["usuario"]).patch(
            f"{NOTAS_CREDITO_URL}{nota.pk}/", {"motivo": "despues"}, format="json",
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        nota.refresh_from_db()
        self.assertEqual(nota.motivo, "despues")
        self.assertEqual(nota.fecha_emision, ayer)

    def test_emitir_sin_cxc_es_rechazado(self):
        factura = self._factura()

        resp = self._emitir(self._client(self.a["usuario"]), factura, "100.00")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("factura", resp.data)
        self.assertIsInstance(resp.data["factura"], list)
        self.assertEqual(NotaCredito.objects.count(), 0)

    def test_patch_a_emitida_sin_cxc_es_rechazado(self):
        factura = self._factura()
        nota = NotaCredito.objects.create(
            factura=factura, cliente=self.a["cliente"], total=Decimal("100.00"),
            estatus=NotaCredito.Estatus.BORRADOR.value,
        )

        resp = self._client(self.a["usuario"]).patch(
            f"{NOTAS_CREDITO_URL}{nota.pk}/",
            {"estatus": NotaCredito.Estatus.EMITIDA.value},
            format="json",
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["factura"], list)
        nota.refresh_from_db()
        self.assertEqual(nota.estatus, NotaCredito.Estatus.BORRADOR.value)

    def test_emitir_con_cxc_sigue_aplicando(self):
        factura, cxc = self._factura_con_cxc("1000.00")

        resp = self._emitir(self._client(self.a["usuario"]), factura, "400.00")

        self.assertEqual(resp.status_code, 201, resp.data)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("600.00"))
        self.assertEqual(cxc.estatus, CuentaPorCobrar.EstatusCxC.PARCIAL)

    def test_emitir_por_encima_del_saldo_sigue_rechazado(self):
        factura, cxc = self._factura_con_cxc("100.00")

        resp = self._emitir(self._client(self.a["usuario"]), factura, "999.00")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["total"], list)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("100.00"))


class Defecto10NotaCreditoDobleAplicacionConcurrente(FinanzasBase):
    """Emitir/cancelar la misma nota dos veces a la vez ya no duplica el crédito.

    ``perform_update`` leía el estatus previo (``anterior``) del objeto cargado
    fuera de cualquier lock, y ``cancelar_nota_credito`` evaluaba su guard de
    doble cancelación igual. Dos peticiones simultáneas (dos pestañas, un
    reintento, la API directa) leían ambas el estado viejo, concluían las dos
    que eran la primera y aplicaban/revertían el importe dos veces sobre la CxC.

    Ahora la fila de la nota se bloquea (``select_for_update``) *antes* de leer
    el estatus que decide, en el mismo orden nota -> CxC que ya usaba el
    servicio, así que la segunda petición espera y ve el estado ya cambiado.

    Limitación de estos tests: SQLite ignora ``select_for_update()``, así que no
    se puede reproducir la carrera con hilos reales. Se simula de forma
    determinista lo único que el lock arregla —la lectura obsoleta— haciendo que
    la segunda petición entre con la instancia que cargó *antes* de que la
    primera confirmara. Con el código anterior estos tests fallan (el saldo se
    mueve dos veces); con el lock la relectura ve el estado real.
    """

    def _factura_con_cxc(self, total="1000.00"):
        empresa = self.a["empresa"]
        factura = Factura.objects.create(
            empresa=empresa, sucursal=self.a["sucursal"],
            cliente=self.a["cliente"], moneda=self.moneda, total=Decimal(total),
        )
        cxc = CuentaPorCobrar.objects.create(
            empresa=empresa, cliente=self.a["cliente"], factura=factura,
            total=Decimal(total), saldo=Decimal(total),
        )
        return factura, cxc

    def _nota_borrador(self, factura, total="250.00"):
        return NotaCredito.objects.create(
            factura=factura, cliente=self.a["cliente"], total=Decimal(total),
            estatus=NotaCredito.Estatus.BORRADOR.value,
        )

    def _emitir(self, client, nota):
        return client.patch(
            f"{NOTAS_CREDITO_URL}{nota.pk}/",
            {"estatus": NotaCredito.Estatus.EMITIDA.value},
            format="json",
        )

    def test_emitir_dos_veces_seguidas_no_aplica_el_credito_dos_veces(self):
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        client = self._client(self.a["usuario"])

        resp = self._emitir(client, nota)
        self.assertEqual(resp.status_code, 200, resp.data)
        cxc.refresh_from_db()
        saldo_tras_la_primera = cxc.saldo
        self.assertEqual(saldo_tras_la_primera, Decimal("750.00"))

        resp = self._emitir(client, nota)

        self.assertEqual(resp.status_code, 200, resp.data)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, saldo_tras_la_primera)

    def test_emision_concurrente_simulada_no_aplica_el_credito_dos_veces(self):
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        client = self._client(self.a["usuario"])
        # Instancia tal como la cargó la petición B antes de que A confirmara:
        # para ella la nota sigue en Borrador.
        obsoleta = NotaCredito.objects.get(pk=nota.pk)
        self.assertEqual(obsoleta.estatus, NotaCredito.Estatus.BORRADOR.value)

        self.assertEqual(self._emitir(client, nota).status_code, 200)
        cxc.refresh_from_db()
        saldo_tras_la_primera = cxc.saldo
        self.assertEqual(saldo_tras_la_primera, Decimal("750.00"))

        with patch.object(NotaCreditoViewSet, "get_object", return_value=obsoleta):
            resp = self._emitir(client, nota)

        self.assertEqual(resp.status_code, 200, resp.data)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, saldo_tras_la_primera)
        self.assertEqual(cxc.estatus, CuentaPorCobrar.EstatusCxC.PARCIAL)
        nota.refresh_from_db()
        self.assertEqual(nota.estatus, NotaCredito.Estatus.EMITIDA.value)

    def test_cancelacion_concurrente_simulada_no_revierte_dos_veces(self):
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        client = self._client(self.a["usuario"])
        self.assertEqual(self._emitir(client, nota).status_code, 200)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("750.00"))
        # Instancia de la petición B: cargada cuando la nota aún estaba Emitida.
        obsoleta = NotaCredito.objects.get(pk=nota.pk)
        self.assertEqual(obsoleta.estatus, NotaCredito.Estatus.EMITIDA.value)

        resp = client.post(f"{NOTAS_CREDITO_URL}{nota.pk}/cancelar/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        cxc.refresh_from_db()
        saldo_tras_la_primera = cxc.saldo
        self.assertEqual(saldo_tras_la_primera, Decimal("1000.00"))

        with patch.object(NotaCreditoViewSet, "get_object", return_value=obsoleta):
            resp = client.post(
                f"{NOTAS_CREDITO_URL}{nota.pk}/cancelar/", {}, format="json",
            )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["estatus"], NotaCredito.Estatus.CANCELADA.value)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, saldo_tras_la_primera)
        self.assertEqual(cxc.estatus, CuentaPorCobrar.EstatusCxC.PENDIENTE)

    def test_emision_y_cancelacion_normales_no_cambian(self):
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        ayer = timezone.localdate() - timedelta(days=30)
        NotaCredito.objects.filter(pk=nota.pk).update(fecha_emision=ayer)
        client = self._client(self.a["usuario"])

        resp = self._emitir(client, nota)
        self.assertEqual(resp.status_code, 200, resp.data)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("750.00"))
        self.assertEqual(cxc.estatus, CuentaPorCobrar.EstatusCxC.PARCIAL)

        resp = client.post(f"{NOTAS_CREDITO_URL}{nota.pk}/cancelar/", {}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("1000.00"))
        nota.refresh_from_db()
        self.assertEqual(nota.estatus, NotaCredito.Estatus.CANCELADA.value)
        self.assertEqual(nota.fecha_emision, ayer)

    def test_el_lock_no_afecta_al_rechazo_por_total_mayor_al_saldo(self):
        factura, cxc = self._factura_con_cxc("100.00")
        nota = self._nota_borrador(factura, "300.00")

        resp = self._emitir(self._client(self.a["usuario"]), nota)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("total", resp.data)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("100.00"))
        nota.refresh_from_db()
        self.assertEqual(nota.estatus, NotaCredito.Estatus.BORRADOR.value)

    def test_emitir_una_nota_borrada_a_la_vez_responde_404_y_no_la_resucita(self):
        # Si la fila desaparece entre get_object() y el lock, continuar con la
        # instancia obsoleta hacía que save() la reinsertara con el mismo pk
        # (UPDATE de 0 filas -> INSERT) y aplicara el crédito igualmente.
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        obsoleta = NotaCredito.objects.get(pk=nota.pk)
        NotaCredito.objects.filter(pk=nota.pk).delete()

        with patch.object(NotaCreditoViewSet, "get_object", return_value=obsoleta):
            resp = self._emitir(self._client(self.a["usuario"]), nota)

        self.assertEqual(resp.status_code, 404, resp.data)
        self.assertEqual(NotaCredito.objects.count(), 0)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("1000.00"))

    def test_cancelar_una_nota_borrada_a_la_vez_responde_404(self):
        # El servicio salía en silencio y la vista respondía 200 serializando el
        # objeto en memoria: el cliente veía éxito sin cancelación ni devolución.
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        client = self._client(self.a["usuario"])
        self.assertEqual(self._emitir(client, nota).status_code, 200)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("750.00"))
        obsoleta = NotaCredito.objects.get(pk=nota.pk)
        NotaCredito.objects.filter(pk=nota.pk).delete()

        with patch.object(NotaCreditoViewSet, "get_object", return_value=obsoleta):
            resp = client.post(
                f"{NOTAS_CREDITO_URL}{nota.pk}/cancelar/", {}, format="json",
            )

        self.assertEqual(resp.status_code, 404, resp.data)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("750.00"))

    def test_editar_una_nota_de_otra_empresa_sigue_fuera_de_alcance(self):
        factura = Factura.objects.create(
            empresa=self.b["empresa"], sucursal=self.b["sucursal"],
            cliente=self.b["cliente"], moneda=self.moneda, total=Decimal("100.00"),
        )
        cxc = CuentaPorCobrar.objects.create(
            empresa=self.b["empresa"], cliente=self.b["cliente"], factura=factura,
            total=Decimal("100.00"), saldo=Decimal("100.00"),
        )
        nota = NotaCredito.objects.create(
            factura=factura, cliente=self.b["cliente"], total=Decimal("50.00"),
            estatus=NotaCredito.Estatus.BORRADOR.value,
        )

        resp = self._emitir(self._client(self.a["usuario"]), nota)

        self.assertEqual(resp.status_code, 404)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("100.00"))
        nota.refresh_from_db()
        self.assertEqual(nota.estatus, NotaCredito.Estatus.BORRADOR.value)


class Defecto11NotaCreditoTransicionesDeEstatus(FinanzasBase):
    """El ``estatus`` deja de ser un campo que el cliente pueda mover a mano.

    ``estatus`` es escribible (``fields = "__all__"``) y ``perform_update`` sólo
    bloqueaba editar una nota ya Cancelada, así que dos rutas SECUENCIALES —que
    el lock de concurrencia no cubre— corrompían el saldo de la CxC:

    * ``Emitida -> Borrador`` no revertía nada, y la siguiente emisión volvía a
      descontar el total: la misma doble aplicación, sin concurrencia.
    * ``PATCH estatus="Cancelada"`` cancelaba saltándose
      ``cancelar_nota_credito``, así que el importe nunca regresaba a la CxC, y
      encima dejaba la nota inservible (ya no se puede editar por cancelada y la
      acción ``cancelar`` sale por su guard de doble cancelación).

    Y ``perform_destroy`` leía el estatus fuera de todo bloqueo y sin
    transacción: un borrado concurrente con una emisión borraba el documento y
    dejaba la CxC rebajada sin nada que lo respalde.

    Ninguna de las dos transiciones malas es alcanzable desde la UI (sólo manda
    ``PATCH {estatus: "Emitida"}`` sobre borradores), así que estos 400 son un
    blindaje del API, no un cambio visible en el flujo del usuario.
    """

    def _factura_con_cxc(self, total="1000.00"):
        empresa = self.a["empresa"]
        factura = Factura.objects.create(
            empresa=empresa, sucursal=self.a["sucursal"],
            cliente=self.a["cliente"], moneda=self.moneda, total=Decimal(total),
        )
        cxc = CuentaPorCobrar.objects.create(
            empresa=empresa, cliente=self.a["cliente"], factura=factura,
            total=Decimal(total), saldo=Decimal(total),
        )
        return factura, cxc

    def _nota_borrador(self, factura, total="250.00"):
        return NotaCredito.objects.create(
            factura=factura, cliente=self.a["cliente"], total=Decimal(total),
            estatus=NotaCredito.Estatus.BORRADOR.value, motivo="antes",
        )

    def _patch_estatus(self, client, nota, estatus):
        return client.patch(
            f"{NOTAS_CREDITO_URL}{nota.pk}/", {"estatus": estatus}, format="json",
        )

    def test_bajar_una_nota_emitida_a_borrador_es_rechazado(self):
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        client = self._client(self.a["usuario"])
        self._patch_estatus(client, nota, NotaCredito.Estatus.EMITIDA.value)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("750.00"))

        resp = self._patch_estatus(client, nota, NotaCredito.Estatus.BORRADOR.value)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["estatus"], list)
        self.assertIn("Cancélala", str(resp.data["estatus"][0]))
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("750.00"))
        nota.refresh_from_db()
        self.assertEqual(nota.estatus, NotaCredito.Estatus.EMITIDA.value)

    def test_el_rodeo_emitida_borrador_emitida_ya_no_aplica_dos_veces(self):
        # El bug completo: el paso intermedio a Borrador era el que dejaba a la
        # nota "sin emitir" para el backend sin haber devuelto nada a la CxC.
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        client = self._client(self.a["usuario"])
        self._patch_estatus(client, nota, NotaCredito.Estatus.EMITIDA.value)
        cxc.refresh_from_db()
        saldo_tras_emitir = cxc.saldo
        self.assertEqual(saldo_tras_emitir, Decimal("750.00"))

        self._patch_estatus(client, nota, NotaCredito.Estatus.BORRADOR.value)
        self._patch_estatus(client, nota, NotaCredito.Estatus.EMITIDA.value)

        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, saldo_tras_emitir)

    def test_cancelar_una_emitida_por_patch_de_estatus_es_rechazado(self):
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        client = self._client(self.a["usuario"])
        self._patch_estatus(client, nota, NotaCredito.Estatus.EMITIDA.value)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("750.00"))

        resp = self._patch_estatus(client, nota, NotaCredito.Estatus.CANCELADA.value)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["estatus"], list)
        self.assertIn("cancelar", str(resp.data["estatus"][0]))
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("750.00"))
        nota.refresh_from_db()
        self.assertEqual(nota.estatus, NotaCredito.Estatus.EMITIDA.value)

        # La puerta buena sigue abierta y sí devuelve el importe.
        resp = client.post(f"{NOTAS_CREDITO_URL}{nota.pk}/cancelar/", {}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("1000.00"))
        self.assertEqual(cxc.estatus, CuentaPorCobrar.EstatusCxC.PENDIENTE)

    def test_cancelar_un_borrador_por_patch_de_estatus_tambien_es_rechazado(self):
        # Aquí no hay saldo que arruinar (un borrador nunca tocó la CxC), pero
        # la cancelación tiene una sola puerta: el cliente no puede saber si la
        # nota sigue en Borrador, y ese es justo el estado que se lee obsoleto.
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        client = self._client(self.a["usuario"])

        resp = self._patch_estatus(client, nota, NotaCredito.Estatus.CANCELADA.value)

        self.assertEqual(resp.status_code, 400, resp.data)
        nota.refresh_from_db()
        self.assertEqual(nota.estatus, NotaCredito.Estatus.BORRADOR.value)

        resp = client.post(f"{NOTAS_CREDITO_URL}{nota.pk}/cancelar/", {}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        nota.refresh_from_db()
        self.assertEqual(nota.estatus, NotaCredito.Estatus.CANCELADA.value)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("1000.00"))

    def test_reemitir_una_nota_ya_emitida_sigue_siendo_un_200_idempotente(self):
        # La UI ofrece "Emitir" según el estatus del renglón cacheado: con la
        # lista desactualizada manda Emitida sobre una Emitida. No es una
        # transición (el estatus no cambia) y debe seguir respondiendo 200.
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        client = self._client(self.a["usuario"])
        self._patch_estatus(client, nota, NotaCredito.Estatus.EMITIDA.value)
        cxc.refresh_from_db()
        saldo_tras_emitir = cxc.saldo

        resp = self._patch_estatus(client, nota, NotaCredito.Estatus.EMITIDA.value)

        self.assertEqual(resp.status_code, 200, resp.data)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, saldo_tras_emitir)

    def test_el_ciclo_de_vida_legitimo_completo_sigue_funcionando(self):
        factura, cxc = self._factura_con_cxc("1000.00")
        client = self._client(self.a["usuario"])

        resp = client.post(
            NOTAS_CREDITO_URL,
            {
                "factura": factura.pk, "cliente": self.a["cliente"].pk,
                "estatus": NotaCredito.Estatus.BORRADOR.value, "total": "250.00",
                "motivo": "antes",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        nota = NotaCredito.objects.get(pk=resp.data["id"])
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("1000.00"))

        # Editar la cabecera de un borrador, sin tocar el estatus, sigue siendo
        # una edición legítima.
        resp = client.patch(
            f"{NOTAS_CREDITO_URL}{nota.pk}/", {"motivo": "despues"}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        nota.refresh_from_db()
        self.assertEqual(nota.motivo, "despues")
        self.assertEqual(nota.estatus, NotaCredito.Estatus.BORRADOR.value)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("1000.00"))

        resp = self._patch_estatus(client, nota, NotaCredito.Estatus.EMITIDA.value)
        self.assertEqual(resp.status_code, 200, resp.data)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("750.00"))
        self.assertEqual(cxc.estatus, CuentaPorCobrar.EstatusCxC.PARCIAL)

        resp = client.post(f"{NOTAS_CREDITO_URL}{nota.pk}/cancelar/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("1000.00"))
        self.assertEqual(cxc.estatus, CuentaPorCobrar.EstatusCxC.PENDIENTE)
        nota.refresh_from_db()
        self.assertEqual(nota.estatus, NotaCredito.Estatus.CANCELADA.value)

    def test_eliminar_un_borrador_sigue_funcionando(self):
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")

        resp = self._client(self.a["usuario"]).delete(f"{NOTAS_CREDITO_URL}{nota.pk}/")

        self.assertEqual(resp.status_code, 204)
        self.assertEqual(NotaCredito.objects.count(), 0)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("1000.00"))

    def test_eliminar_una_nota_emitida_sigue_rechazado(self):
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        client = self._client(self.a["usuario"])
        self._patch_estatus(client, nota, NotaCredito.Estatus.EMITIDA.value)

        resp = client.delete(f"{NOTAS_CREDITO_URL}{nota.pk}/")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(NotaCredito.objects.filter(pk=nota.pk).count(), 1)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("750.00"))

    def test_borrado_concurrente_con_una_emision_no_descuadra_la_cxc(self):
        # B carga la nota en Borrador y decide borrarla; A la emite y confirma
        # antes. Sin lock ni relectura, B pasaba su guard con el estatus viejo y
        # borraba el documento: la CxC quedaba rebajada 250 sin nada detrás.
        factura, cxc = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        client = self._client(self.a["usuario"])
        obsoleta = NotaCredito.objects.get(pk=nota.pk)
        self.assertEqual(obsoleta.estatus, NotaCredito.Estatus.BORRADOR.value)

        self._patch_estatus(client, nota, NotaCredito.Estatus.EMITIDA.value)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("750.00"))

        with patch.object(NotaCreditoViewSet, "get_object", return_value=obsoleta):
            resp = client.delete(f"{NOTAS_CREDITO_URL}{nota.pk}/")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(NotaCredito.objects.filter(pk=nota.pk).count(), 1)
        cxc.refresh_from_db()
        self.assertEqual(cxc.saldo, Decimal("750.00"))

    def test_eliminar_una_nota_borrada_a_la_vez_responde_404(self):
        factura, _ = self._factura_con_cxc("1000.00")
        nota = self._nota_borrador(factura, "250.00")
        obsoleta = NotaCredito.objects.get(pk=nota.pk)
        NotaCredito.objects.filter(pk=nota.pk).delete()

        with patch.object(NotaCreditoViewSet, "get_object", return_value=obsoleta):
            resp = self._client(self.a["usuario"]).delete(
                f"{NOTAS_CREDITO_URL}{nota.pk}/"
            )

        self.assertEqual(resp.status_code, 404)

    def test_eliminar_una_nota_de_otra_empresa_sigue_fuera_de_alcance(self):
        factura = Factura.objects.create(
            empresa=self.b["empresa"], sucursal=self.b["sucursal"],
            cliente=self.b["cliente"], moneda=self.moneda, total=Decimal("100.00"),
        )
        nota = NotaCredito.objects.create(
            factura=factura, cliente=self.b["cliente"], total=Decimal("50.00"),
            estatus=NotaCredito.Estatus.BORRADOR.value,
        )

        resp = self._client(self.a["usuario"]).delete(f"{NOTAS_CREDITO_URL}{nota.pk}/")

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(NotaCredito.objects.filter(pk=nota.pk).count(), 1)


class AdminFinanzasSmokeTests(TestCase):
    """El registro en el admin (finanzas/admin.py) no lo cubre ningún otro
    test: ``manage.py check`` valida que ``autocomplete_fields`` apunte a un
    ModelAdmin registrado, pero no que el changelist realmente renderice (un
    campo mal escrito en ``list_display``/``search_fields`` sólo revienta en
    request). Recorre TODO lo que finanzas tenga registrado en el admin, así
    que un modelo nuevo queda cubierto sin tocar este test."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = Usuario.objects.create(
            username="admin_finanzas",
            email="admin_finanzas@test.mx",
            is_superuser=True,
            is_staff=True,
            is_active=True,
        )

    def test_changelist_de_cada_modelo_de_finanzas_renderiza(self):
        from django.contrib import admin as django_admin
        from django.urls import reverse

        self.client.force_login(self.superuser)
        modelos_finanzas = [
            model for model in django_admin.site._registry
            if model._meta.app_label == "finanzas"
        ]
        self.assertTrue(modelos_finanzas, "no hay ningún modelo de finanzas registrado en el admin")

        for model in modelos_finanzas:
            url = reverse(f"admin:finanzas_{model._meta.model_name}_changelist")
            with self.subTest(modelo=model._meta.model_name):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200, f"{model._meta.model_name}: {resp.status_code}")

    def test_add_de_cada_modelo_de_finanzas_renderiza(self):
        """Cubre autocomplete_fields/inlines mal referenciados: el form de
        alta es donde Django los resuelve, el changelist no los toca."""
        from django.contrib import admin as django_admin
        from django.urls import reverse

        self.client.force_login(self.superuser)
        modelos_finanzas = [
            model for model in django_admin.site._registry
            if model._meta.app_label == "finanzas"
        ]

        for model in modelos_finanzas:
            url = reverse(f"admin:finanzas_{model._meta.model_name}_add")
            with self.subTest(modelo=model._meta.model_name):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200, f"{model._meta.model_name}: {resp.status_code}")


class Defecto12BorradoSinLineasHuerfanas(FinanzasBase):
    """Borrar una póliza o una nota de crédito se lleva también sus líneas.

    ``PolizaDetalle.poliza`` era ``SET_NULL``: borrar la póliza dejaba sus
    renglones en ``poliza_detalle`` con cuenta, cargo y abono intactos pero sin
    padre -- y sin empresa, porque la línea no tiene columna propia --. Un
    reporte agrupado por cuenta los habría sumado. ``NotaCreditoDetalle`` ya
    era ``CASCADE``; sus pruebas blindan que siga así.
    """

    def _poliza_con_lineas(self, estatus, tenant=None):
        tenant = tenant or self.a
        cargo, abono, centro_costo = self._crear_cuentas_contables(tenant["empresa"])
        poliza = Poliza.objects.create(
            empresa=tenant["empresa"], sucursal=tenant["sucursal"],
            centro_costo=centro_costo, estatus=estatus,
        )
        PolizaDetalle.objects.create(
            poliza=poliza, cuenta_contable=cargo, centro_costo=centro_costo,
            cargo=Decimal("100.00"), orden=1,
        )
        PolizaDetalle.objects.create(
            poliza=poliza, cuenta_contable=abono, centro_costo=centro_costo,
            abono=Decimal("100.00"), orden=2,
        )
        return poliza

    def _nota_con_linea(self, estatus):
        factura = Factura.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"],
            cliente=self.a["cliente"], moneda=self.moneda, total=Decimal("116.00"),
        )
        factura_detalle = FacturaDetalle.objects.create(
            factura=factura, pedido_detalle=self.a["detalle"], producto=self.a["producto"],
            cantidad=1, precio_unitario=Decimal("100.00"), total=Decimal("116.00"),
        )
        nota = NotaCredito.objects.create(
            factura=factura, cliente=self.a["cliente"], total=Decimal("116.00"),
            estatus=estatus,
        )
        NotaCreditoDetalle.objects.create(
            nota_credito=nota, factura_detalle=factura_detalle,
            cantidad=1, total=Decimal("116.00"),
        )
        return nota

    def _asserta_sin_polizas_huerfanas(self):
        self.assertEqual(PolizaDetalle.objects.filter(poliza__isnull=True).count(), 0)

    # -- Póliza --------------------------------------------------------------

    def test_borrar_poliza_en_borrador_elimina_sus_lineas(self):
        poliza = self._poliza_con_lineas(Poliza.PolizaStatus.BORRADOR.value)
        self.assertEqual(PolizaDetalle.objects.count(), 2)

        resp = self._client(self.a["usuario"]).delete(f"{POLIZAS_URL}{poliza.pk}/")

        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Poliza.objects.filter(pk=poliza.pk).exists())
        self.assertEqual(PolizaDetalle.objects.count(), 0)
        self._asserta_sin_polizas_huerfanas()

    def test_borrar_poliza_cancelada_elimina_sus_lineas(self):
        poliza = self._poliza_con_lineas(Poliza.PolizaStatus.CANCELADA.value)

        resp = self._client(self.a["usuario"]).delete(f"{POLIZAS_URL}{poliza.pk}/")

        self.assertEqual(resp.status_code, 204)
        self.assertEqual(PolizaDetalle.objects.count(), 0)
        self._asserta_sin_polizas_huerfanas()

    def test_borrar_poliza_fuera_de_la_api_tambien_elimina_sus_lineas(self):
        # El admin y los borrados en cascada (empresa, sucursal, centro de costo,
        # usuario) no pasan por perform_destroy: la regla vive en el FK.
        poliza = self._poliza_con_lineas(Poliza.PolizaStatus.BORRADOR.value)

        Poliza.objects.filter(pk=poliza.pk).delete()

        self.assertEqual(PolizaDetalle.objects.count(), 0)
        self._asserta_sin_polizas_huerfanas()

    def test_borrar_poliza_contabilizada_sigue_rechazado(self):
        poliza = self._poliza_con_lineas(Poliza.PolizaStatus.CONTABILIZADA.value)

        resp = self._client(self.a["usuario"]).delete(f"{POLIZAS_URL}{poliza.pk}/")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertTrue(Poliza.objects.filter(pk=poliza.pk).exists())
        self.assertEqual(poliza.poliza_detalles.count(), 2)

    def test_borrar_poliza_de_otra_empresa_sigue_fuera_de_alcance(self):
        poliza = self._poliza_con_lineas(Poliza.PolizaStatus.BORRADOR.value, tenant=self.b)

        resp = self._client(self.a["usuario"]).delete(f"{POLIZAS_URL}{poliza.pk}/")

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(poliza.poliza_detalles.count(), 2)

    def test_borrado_que_falla_a_mitad_no_deja_la_poliza_a_medias(self):
        poliza = self._poliza_con_lineas(Poliza.PolizaStatus.BORRADOR.value)
        lineas_al_fallar = []

        def falla_tras_borrar(sender, instance, **kwargs):
            # Para cuando se borra el encabezado las líneas ya salieron.
            lineas_al_fallar.append(PolizaDetalle.objects.filter(poliza_id=instance.pk).count())
            raise RuntimeError("fallo a mitad del borrado")

        post_delete.connect(falla_tras_borrar, sender=Poliza, dispatch_uid="falla_tras_borrar")
        self.addCleanup(post_delete.disconnect, sender=Poliza, dispatch_uid="falla_tras_borrar")
        with self.assertRaises(RuntimeError):
            # Punto de guardado propio: sin él, el rollback arrastraría la
            # transacción envolvente de TestCase.
            with transaction.atomic():
                self._client(self.a["usuario"]).delete(f"{POLIZAS_URL}{poliza.pk}/")

        self.assertEqual(lineas_al_fallar, [0])
        self.assertTrue(Poliza.objects.filter(pk=poliza.pk).exists())
        self.assertEqual(poliza.poliza_detalles.count(), 2)

    # -- Nota de crédito ------------------------------------------------------

    def test_borrar_nota_en_borrador_elimina_sus_lineas(self):
        nota = self._nota_con_linea(NotaCredito.Estatus.BORRADOR.value)
        self.assertEqual(NotaCreditoDetalle.objects.count(), 1)

        resp = self._client(self.a["usuario"]).delete(f"{NOTAS_CREDITO_URL}{nota.pk}/")

        self.assertEqual(resp.status_code, 204)
        self.assertFalse(NotaCredito.objects.filter(pk=nota.pk).exists())
        self.assertEqual(NotaCreditoDetalle.objects.count(), 0)

    def test_borrar_nota_cancelada_elimina_sus_lineas(self):
        nota = self._nota_con_linea(NotaCredito.Estatus.CANCELADA.value)

        resp = self._client(self.a["usuario"]).delete(f"{NOTAS_CREDITO_URL}{nota.pk}/")

        self.assertEqual(resp.status_code, 204)
        self.assertEqual(NotaCreditoDetalle.objects.count(), 0)

    def test_borrar_nota_emitida_sigue_rechazado_y_conserva_sus_lineas(self):
        nota = self._nota_con_linea(NotaCredito.Estatus.EMITIDA.value)

        resp = self._client(self.a["usuario"]).delete(f"{NOTAS_CREDITO_URL}{nota.pk}/")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertTrue(NotaCredito.objects.filter(pk=nota.pk).exists())
        self.assertEqual(nota.nota_credito_detalles.count(), 1)


class AccountsPayableOriginationAndGuardsTests(FinanzasBase):
    """EC-133: la CxP nace del registro de la factura de proveedor, el alta
    manual ya no la descuadra, y con pagos aplicados no se edita ni se borra."""

    ACCOUNTS_URL = "/api/v1/finanzas/cuentas-por-pagar/"
    MIGRATION = "finanzas.migrations.0013_cxp_unique_invoice_without_vencida"

    def _purchase_documents(self):
        proveedor, _, oc, _, recepcion, _, factura = self._crear_oc_recepcion_y_factura_proveedor(
            self.a["empresa"], self.a["sucursal"], self.a["usuario"],
        )
        return proveedor, oc, recepcion, factura

    def _invoice(self, total="1160.00", fecha_vencimiento=None):
        proveedor, _, _, factura = self._purchase_documents()
        factura.total = Decimal(total)
        factura.fecha_vencimiento = fecha_vencimiento
        factura.save()
        return proveedor, factura

    def _account(self, total="1000.00"):
        proveedor, factura = self._invoice(total)
        cxp = CuentaPorPagar.objects.create(
            empresa=self.a["empresa"], proveedor=proveedor, factura_proveedor=factura,
            total=Decimal(total), saldo=Decimal(total),
        )
        return proveedor, factura, cxp

    def _pay(self, proveedor, cxp, importe, estatus=None):
        cuenta = self._crear_cuenta_bancaria(self.a["empresa"])
        payload = {
            "proveedor": proveedor.pk,
            "cuenta_bancaria": cuenta.pk,
            "total_pagado": importe,
            "pago_detalles": [{"cxp": cxp.pk, "importe_aplicado": importe}],
        }
        if estatus is not None:
            payload["estatus"] = estatus
        resp = self._client(self.a["usuario"]).post(PAGOS_URL, payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        return Pago.objects.get(pk=resp.data["id"])

    def _post_invoice(self, **overrides):
        proveedor, oc, recepcion, _ = self._purchase_documents()
        payload = {
            "sucursal": self.a["sucursal"].pk,
            "proveedor": proveedor.pk,
            "oc": oc.pk,
            "recepcion": recepcion.pk,
            "moneda": self.moneda.pk,
            "total": "1160.00",
            "fecha_vencimiento": "2026-10-15",
            "estatus": FacturaProveedor.FacturaProveedorStatus.REGISTRADA.value,
        }
        payload.update(overrides)
        return self._client(self.a["usuario"]).post(FACTURAS_PROVEEDOR_URL, payload, format="json")

    def _post_account(self, data):
        return self._client(self.a["usuario"]).post(self.ACCOUNTS_URL, data, format="json")

    def _patch(self, url, data):
        return self._client(self.a["usuario"]).patch(url, data, format="json")

    # -- Origen automático ------------------------------------------------------

    def test_creating_invoice_as_registered_generates_one_account(self):
        resp = self._post_invoice()

        self.assertEqual(resp.status_code, 201, resp.data)
        factura = FacturaProveedor.objects.get(pk=resp.data["id"])
        cxp = CuentaPorPagar.objects.get(factura_proveedor=factura)
        self.assertEqual(cxp.empresa_id, factura.empresa_id)
        self.assertEqual(cxp.proveedor_id, factura.proveedor_id)
        self.assertEqual(cxp.total, Decimal("1160.00"))
        self.assertEqual(cxp.saldo, Decimal("1160.00"))
        self.assertEqual(cxp.estatus, CuentaPorPagar.EstatusCxP.PENDIENTE)
        self.assertEqual(cxp.fecha_vencimiento, date(2026, 10, 15))

    def test_creating_draft_invoice_generates_nothing(self):
        resp = self._post_invoice(estatus=FacturaProveedor.FacturaProveedorStatus.BORRADOR.value)

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertFalse(CuentaPorPagar.objects.exists())

    def test_registering_draft_invoice_generates_account(self):
        _, factura = self._invoice("500.00", fecha_vencimiento=date(2026, 11, 30))

        resp = self._patch(f"{FACTURAS_PROVEEDOR_URL}{factura.pk}/", {"estatus": "Registrada"})

        self.assertEqual(resp.status_code, 200, resp.data)
        cxp = CuentaPorPagar.objects.get(factura_proveedor=factura)
        self.assertEqual((cxp.total, cxp.saldo), (Decimal("500.00"), Decimal("500.00")))
        self.assertEqual(cxp.estatus, CuentaPorPagar.EstatusCxP.PENDIENTE)
        self.assertEqual(cxp.fecha_vencimiento, date(2026, 11, 30))

    def test_zero_total_invoice_generates_zero_pending_account(self):
        _, factura = self._invoice("0.00")

        resp = self._patch(f"{FACTURAS_PROVEEDOR_URL}{factura.pk}/", {"estatus": "Registrada"})

        self.assertEqual(resp.status_code, 200, resp.data)
        cxp = CuentaPorPagar.objects.get(factura_proveedor=factura)
        self.assertEqual((cxp.total, cxp.saldo), (Decimal("0.00"), Decimal("0.00")))
        self.assertEqual(cxp.estatus, CuentaPorPagar.EstatusCxP.PENDIENTE)

    def test_reentering_registered_neither_duplicates_nor_updates_account(self):
        _, factura = self._invoice("500.00")
        url = f"{FACTURAS_PROVEEDOR_URL}{factura.pk}/"

        for data in (
            {"estatus": "Registrada"},
            {"estatus": "Borrador", "total": "999.00"},
            {"estatus": "Registrada"},
            {"observaciones": "re-guardada"},
        ):
            resp = self._patch(url, data)
            self.assertEqual(resp.status_code, 200, resp.data)

        accounts = CuentaPorPagar.objects.filter(factura_proveedor=factura)
        self.assertEqual(accounts.count(), 1)
        self.assertEqual(accounts.get().total, Decimal("500.00"))
        self.assertEqual(accounts.get().saldo, Decimal("500.00"))

    # -- Alta manual ------------------------------------------------------------

    def test_manual_create_is_born_balanced_ignoring_saldo_and_estatus(self):
        proveedor, factura = self._invoice("300.00")

        resp = self._post_account({
            "proveedor": proveedor.pk, "factura_proveedor": factura.pk,
            "total": "300.00", "saldo": "1.00", "estatus": "Pagada",
        })

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["saldo"], "300.00")
        self.assertEqual(resp.data["estatus"], CuentaPorPagar.EstatusCxP.PENDIENTE.value)

    def test_manual_create_rejects_total_mismatch(self):
        proveedor, factura = self._invoice("300.00")

        resp = self._post_account({
            "proveedor": proveedor.pk, "factura_proveedor": factura.pk, "total": "299.99",
        })

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["total"], list)
        self.assertFalse(CuentaPorPagar.objects.exists())

    def test_manual_create_without_total_compares_as_zero(self):
        proveedor, factura = self._invoice("300.00")

        resp = self._post_account({"proveedor": proveedor.pk, "factura_proveedor": factura.pk})

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["total"], list)

    def test_manual_create_rejects_proveedor_mismatch(self):
        _, factura = self._invoice("300.00")
        other_proveedor, _, _, _ = self._purchase_documents()

        resp = self._post_account({
            "proveedor": other_proveedor.pk, "factura_proveedor": factura.pk, "total": "300.00",
        })

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["proveedor"], list)

    def test_manual_create_rejects_second_account_for_invoice(self):
        proveedor, factura, _ = self._account("1000.00")

        resp = self._post_account({
            "proveedor": proveedor.pk, "factura_proveedor": factura.pk, "total": "1000.00",
        })

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["factura_proveedor"], list)
        self.assertEqual(CuentaPorPagar.objects.filter(factura_proveedor=factura).count(), 1)

    def test_concurrent_duplicate_is_a_400_not_a_500(self):
        # Simula la carrera: la validación no ve la otra CxP y el duplicado llega
        # a la base, donde lo detiene uq_cxp_factura_proveedor.
        proveedor, factura, _ = self._account("1000.00")

        with patch.object(CuentaPorPagarSerializer, "_validate_single_account_per_invoice"):
            resp = self._post_account({
                "proveedor": proveedor.pk, "factura_proveedor": factura.pk, "total": "1000.00",
            })

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["factura_proveedor"], list)
        self.assertEqual(CuentaPorPagar.objects.filter(factura_proveedor=factura).count(), 1)

    def test_moving_account_to_invoice_that_has_one_is_rejected(self):
        _, _, cxp = self._account()
        _, other_invoice, _ = self._account()

        resp = self._patch(f"{self.ACCOUNTS_URL}{cxp.pk}/", {"factura_proveedor": other_invoice.pk})

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["factura_proveedor"], list)

    # -- Candado con pagos aplicados ---------------------------------------------

    def test_frozen_fields_rejected_with_applied_payment(self):
        proveedor, _, cxp = self._account("1000.00")
        self._pay(proveedor, cxp, "400.00")
        url = f"{self.ACCOUNTS_URL}{cxp.pk}/"

        for field, value in (("total", "2000.00"), ("saldo", "1000.00"), ("estatus", "Pendiente")):
            with self.subTest(field=field):
                resp = self._patch(url, {field: value})
                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertIsInstance(resp.data[field], list)

        cxp.refresh_from_db()
        self.assertEqual(cxp.total, Decimal("1000.00"))
        self.assertEqual(cxp.saldo, Decimal("600.00"))
        self.assertEqual(cxp.estatus, CuentaPorPagar.EstatusCxP.PARCIAL)

    def test_resending_current_values_and_editing_free_fields_is_allowed(self):
        proveedor, _, cxp = self._account("1000.00")
        self._pay(proveedor, cxp, "400.00")

        resp = self._patch(f"{self.ACCOUNTS_URL}{cxp.pk}/", {
            "total": "1000.00", "saldo": "600.00",
            "estatus": CuentaPorPagar.EstatusCxP.PARCIAL.value,
            "fecha_vencimiento": "2026-12-31", "observaciones": "renegociada",
        })

        self.assertEqual(resp.status_code, 200, resp.data)
        cxp.refresh_from_db()
        self.assertEqual(cxp.fecha_vencimiento, date(2026, 12, 31))
        self.assertEqual(cxp.observaciones, "renegociada")

    def test_frozen_fields_editable_without_applied_payment(self):
        proveedor, _, cxp = self._account("1000.00")
        # Un pago en borrador todavía no descontó nada: no congela.
        self._pay(proveedor, cxp, "400.00", estatus=Pago.Estatus.BORRADOR.value)

        resp = self._patch(
            f"{self.ACCOUNTS_URL}{cxp.pk}/",
            {"saldo": "900.00", "estatus": CuentaPorPagar.EstatusCxP.PARCIAL.value},
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        cxp.refresh_from_db()
        self.assertEqual(cxp.saldo, Decimal("900.00"))
        self.assertEqual(cxp.estatus, CuentaPorPagar.EstatusCxP.PARCIAL)

    def test_cancelling_the_payment_lifts_the_freeze(self):
        proveedor, _, cxp = self._account("1000.00")
        pago = self._pay(proveedor, cxp, "400.00")
        resp = self._client(self.a["usuario"]).post(f"{PAGOS_URL}{pago.pk}/cancelar/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)

        resp = self._patch(f"{self.ACCOUNTS_URL}{cxp.pk}/", {"saldo": "900.00"})

        self.assertEqual(resp.status_code, 200, resp.data)

    def test_vencida_is_no_longer_an_accepted_estatus(self):
        _, _, cxp = self._account("1000.00")

        resp = self._patch(f"{self.ACCOUNTS_URL}{cxp.pk}/", {"estatus": "Vencida"})

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["estatus"], list)
        # Al crear, estatus no es escribible: se ignora y la CxP nace Pendiente.
        proveedor, factura = self._invoice("50.00")
        resp = self._post_account({
            "proveedor": proveedor.pk, "factura_proveedor": factura.pk,
            "total": "50.00", "estatus": "Vencida",
        })
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["estatus"], CuentaPorPagar.EstatusCxP.PENDIENTE.value)

    # -- Borrado ----------------------------------------------------------------

    def test_deleting_account_with_applied_payment_is_rejected(self):
        proveedor, _, cxp = self._account("1000.00")
        self._pay(proveedor, cxp, "400.00")

        resp = self._client(self.a["usuario"]).delete(f"{self.ACCOUNTS_URL}{cxp.pk}/")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data, list)
        self.assertTrue(CuentaPorPagar.objects.filter(pk=cxp.pk).exists())
        self.assertEqual(PagoDetalle.objects.filter(cxp=cxp).count(), 1)

    def test_deleting_account_without_applied_payment_is_allowed(self):
        _, _, cxp = self._account()

        resp = self._client(self.a["usuario"]).delete(f"{self.ACCOUNTS_URL}{cxp.pk}/")

        self.assertEqual(resp.status_code, 204)
        self.assertFalse(CuentaPorPagar.objects.filter(pk=cxp.pk).exists())

    def test_deleting_invoice_whose_account_has_applied_payment_is_rejected(self):
        proveedor, factura, cxp = self._account("1000.00")
        self._pay(proveedor, cxp, "400.00")

        resp = self._client(self.a["usuario"]).delete(f"{FACTURAS_PROVEEDOR_URL}{factura.pk}/")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data, list)
        self.assertTrue(FacturaProveedor.objects.filter(pk=factura.pk).exists())
        self.assertTrue(CuentaPorPagar.objects.filter(pk=cxp.pk).exists())
        self.assertEqual(PagoDetalle.objects.filter(cxp=cxp).count(), 1)

    def test_deleting_invoice_without_applied_payment_cascades_to_account(self):
        _, factura, cxp = self._account()

        resp = self._client(self.a["usuario"]).delete(f"{FACTURAS_PROVEEDOR_URL}{factura.pk}/")

        self.assertEqual(resp.status_code, 204)
        self.assertFalse(FacturaProveedor.objects.filter(pk=factura.pk).exists())
        self.assertFalse(CuentaPorPagar.objects.filter(pk=cxp.pk).exists())

    # -- Migración 0013 ---------------------------------------------------------

    def test_migration_remaps_vencida_by_balance_and_is_a_noop_without_rows(self):
        migration = importlib.import_module(self.MIGRATION)
        _, _, paid = self._account("100.00")
        _, _, pending = self._account("100.00")
        _, _, partial = self._account("100.00")
        CuentaPorPagar.objects.filter(pk=paid.pk).update(saldo=Decimal("0.00"), estatus="Vencida")
        CuentaPorPagar.objects.filter(pk=pending.pk).update(estatus="Vencida")
        CuentaPorPagar.objects.filter(pk=partial.pk).update(saldo=Decimal("40.00"), estatus="Vencida")

        with patch("builtins.print") as printed:
            migration.remap_vencida_by_balance(django_apps, None)
            # La segunda pasada ya no encuentra filas: no imprime ni cambia nada.
            migration.remap_vencida_by_balance(django_apps, None)

        statuses = dict(CuentaPorPagar.objects.values_list("pk", "estatus"))
        self.assertEqual(statuses[paid.pk], "Pagada")
        self.assertEqual(statuses[pending.pk], "Pendiente")
        self.assertEqual(statuses[partial.pk], "Parcial")
        self.assertEqual(printed.call_count, 3)

    def test_migration_stops_on_duplicate_invoices_without_deleting(self):
        migration = importlib.import_module(self.MIGRATION)
        # Sin duplicados es un no-op.
        migration.fail_on_duplicate_invoices(django_apps, None)

        model = MagicMock()
        model.objects.values.return_value.annotate.return_value.filter.return_value = [
            {"factura_proveedor_id": 7, "accounts": 2},
        ]
        fake_apps = MagicMock()
        fake_apps.get_model.return_value = model

        with self.assertRaises(RuntimeError) as ctx:
            migration.fail_on_duplicate_invoices(fake_apps, None)

        self.assertIn("[7]", str(ctx.exception))
        self.assertFalse(any("delete" in str(call) for call in model.mock_calls))

    # -- Vencimiento en el alta manual ------------------------------------------

    def test_manual_create_copies_invoice_due_date_when_absent(self):
        proveedor, factura = self._invoice("300.00", fecha_vencimiento=date(2026, 12, 15))

        resp = self._post_account({
            "proveedor": proveedor.pk, "factura_proveedor": factura.pk, "total": "300.00",
        })

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["fecha_vencimiento"], "2026-12-15")
        cxp = CuentaPorPagar.objects.get(factura_proveedor=factura)
        self.assertEqual(cxp.fecha_vencimiento, date(2026, 12, 15))

    def test_manual_create_keeps_client_due_date(self):
        proveedor, factura = self._invoice("300.00", fecha_vencimiento=date(2026, 12, 15))

        resp = self._post_account({
            "proveedor": proveedor.pk, "factura_proveedor": factura.pk, "total": "300.00",
            "fecha_vencimiento": "2027-01-31",
        })

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["fecha_vencimiento"], "2027-01-31")

    def test_manual_create_explicit_null_due_date_falls_back_to_invoice(self):
        # Un null explícito cuenta como ausente: un campo de fecha vacío en el
        # formulario no debe dejar la CxP sin vencimiento.
        proveedor, factura = self._invoice("300.00", fecha_vencimiento=date(2026, 12, 15))

        resp = self._post_account({
            "proveedor": proveedor.pk, "factura_proveedor": factura.pk, "total": "300.00",
            "fecha_vencimiento": None,
        })

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["fecha_vencimiento"], "2026-12-15")

    def test_manual_create_without_due_date_anywhere_stays_null(self):
        proveedor, factura = self._invoice("300.00", fecha_vencimiento=None)

        resp = self._post_account({
            "proveedor": proveedor.pk, "factura_proveedor": factura.pk, "total": "300.00",
        })

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIsNone(resp.data["fecha_vencimiento"])

    # -- Re-apuntar la CxP a otra factura o proveedor ----------------------------

    def _aligned_invoice_for(self, proveedor, total):
        # Factura sin CxP del mismo proveedor y total: el cruce contra la factura
        # pasa, así que sólo el candado puede frenar el cambio.
        _, oc, recepcion, _ = self._purchase_documents()
        return FacturaProveedor.objects.create(
            empresa=self.a["empresa"], sucursal=self.a["sucursal"], proveedor=proveedor,
            oc=oc, recepcion=recepcion, moneda=self.moneda, total=Decimal(total),
        )

    def test_moving_paid_account_to_another_invoice_is_frozen(self):
        proveedor, factura, cxp = self._account("1000.00")
        self._pay(proveedor, cxp, "400.00")
        target = self._aligned_invoice_for(proveedor, "1000.00")

        resp = self._patch(f"{self.ACCOUNTS_URL}{cxp.pk}/", {"factura_proveedor": target.pk})

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["factura_proveedor"], list)
        cxp.refresh_from_db()
        self.assertEqual(cxp.factura_proveedor_id, factura.pk)

    def test_changing_paid_account_proveedor_is_frozen_even_if_aligned(self):
        proveedor, factura, cxp = self._account("1000.00")
        self._pay(proveedor, cxp, "400.00")
        other_proveedor, _, _, _ = self._purchase_documents()
        # La factura ya dice el otro proveedor: el cruce pasa y sólo queda el candado.
        FacturaProveedor.objects.filter(pk=factura.pk).update(proveedor=other_proveedor)

        resp = self._patch(f"{self.ACCOUNTS_URL}{cxp.pk}/", {"proveedor": other_proveedor.pk})

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("pagos aplicados", str(resp.data["proveedor"]))
        cxp.refresh_from_db()
        self.assertEqual(cxp.proveedor_id, proveedor.pk)

    def test_unpaid_account_cannot_be_patched_out_of_alignment(self):
        _, factura, cxp = self._account("1000.00")
        other_proveedor, _, _, _ = self._purchase_documents()
        url = f"{self.ACCOUNTS_URL}{cxp.pk}/"

        for data, field in (
            ({"proveedor": other_proveedor.pk}, "proveedor"),
            ({"total": "9999.00", "saldo": "9999.00"}, "total"),
        ):
            with self.subTest(field=field):
                resp = self._patch(url, data)
                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertIsInstance(resp.data[field], list)

        foreign_invoice = self._aligned_invoice_for(other_proveedor, "1000.00")
        resp = self._patch(url, {"factura_proveedor": foreign_invoice.pk})
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["proveedor"], list)

        cxp.refresh_from_db()
        self.assertEqual(cxp.factura_proveedor_id, factura.pk)
        self.assertEqual((cxp.total, cxp.saldo), (Decimal("1000.00"), Decimal("1000.00")))

    def test_unpaid_account_can_move_to_an_aligned_invoice(self):
        proveedor, _, cxp = self._account("1000.00")
        target = self._aligned_invoice_for(proveedor, "1000.00")

        resp = self._patch(f"{self.ACCOUNTS_URL}{cxp.pk}/", {"factura_proveedor": target.pk})

        self.assertEqual(resp.status_code, 200, resp.data)
        cxp.refresh_from_db()
        self.assertEqual(cxp.factura_proveedor_id, target.pk)

    def test_resending_unchanged_values_on_misaligned_account_is_allowed(self):
        # Una CxP ya desalineada (la factura cambió de total tras registrarse) sigue
        # editable en lo que no la ata a la factura.
        proveedor, factura, cxp = self._account("1000.00")
        FacturaProveedor.objects.filter(pk=factura.pk).update(total=Decimal("999.00"))

        resp = self._patch(f"{self.ACCOUNTS_URL}{cxp.pk}/", {
            "proveedor": proveedor.pk, "factura_proveedor": factura.pk,
            "total": "1000.00", "observaciones": "sin cambios de fondo",
        })

        self.assertEqual(resp.status_code, 200, resp.data)

    # -- Factura bloqueada en el alta manual -------------------------------------

    def test_manual_create_when_invoice_vanishes_before_lock_is_a_400(self):
        # El lock de la factura cierra la carrera con su borrado. SQLite ignora
        # select_for_update, así que aquí sólo se ejerce la rama en la que la
        # factura ya no existe al bloquearla; el orden de bloqueo real sólo se
        # puede comprobar en Postgres.
        proveedor, factura = self._invoice("300.00")
        original_validate = CuentaPorPagarSerializer.validate

        def validate_then_delete_invoice(serializer, attrs):
            attrs = original_validate(serializer, attrs)
            FacturaProveedor.objects.filter(pk=factura.pk).delete()
            return attrs

        with patch.object(CuentaPorPagarSerializer, "validate", validate_then_delete_invoice):
            resp = self._post_account({
                "proveedor": proveedor.pk, "factura_proveedor": factura.pk, "total": "300.00",
            })

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["factura_proveedor"], list)
        self.assertFalse(CuentaPorPagar.objects.exists())

    # -- Filas borradas entre get_object() y el lock -----------------------------

    def test_patching_invoice_deleted_before_lock_is_404(self):
        _, factura = self._invoice("500.00")
        stale = FacturaProveedor.objects.get(pk=factura.pk)
        FacturaProveedor.objects.filter(pk=factura.pk).delete()

        with patch.object(FacturaProveedorViewSet, "get_object", return_value=stale):
            resp = self._patch(f"{FACTURAS_PROVEEDOR_URL}{factura.pk}/", {"observaciones": "x"})

        self.assertEqual(resp.status_code, 404)
        # Sin el NotFound, save() habría reinsertado la factura con el mismo pk.
        self.assertFalse(FacturaProveedor.objects.filter(pk=factura.pk).exists())

    def test_deleting_invoice_deleted_before_lock_is_404(self):
        _, factura = self._invoice("500.00")
        stale = FacturaProveedor.objects.get(pk=factura.pk)
        FacturaProveedor.objects.filter(pk=factura.pk).delete()

        with patch.object(FacturaProveedorViewSet, "get_object", return_value=stale):
            resp = self._client(self.a["usuario"]).delete(f"{FACTURAS_PROVEEDOR_URL}{factura.pk}/")

        self.assertEqual(resp.status_code, 404)

    def test_patching_account_deleted_before_lock_is_404(self):
        _, _, cxp = self._account()
        stale = CuentaPorPagar.objects.get(pk=cxp.pk)
        CuentaPorPagar.objects.filter(pk=cxp.pk).delete()

        with patch.object(CuentaPorPagarViewSet, "get_object", return_value=stale):
            resp = self._patch(f"{self.ACCOUNTS_URL}{cxp.pk}/", {"observaciones": "x"})

        self.assertEqual(resp.status_code, 404)
        self.assertFalse(CuentaPorPagar.objects.filter(pk=cxp.pk).exists())

    # -- IntegrityError en la edición --------------------------------------------

    def test_update_duplicate_race_is_a_400(self):
        proveedor, factura, cxp = self._account("1000.00")
        target = self._aligned_invoice_for(proveedor, "1000.00")
        CuentaPorPagar.objects.create(
            empresa=self.a["empresa"], proveedor=proveedor, factura_proveedor=target,
            total=Decimal("1000.00"), saldo=Decimal("1000.00"),
        )

        with patch.object(CuentaPorPagarSerializer, "_validate_single_account_per_invoice"):
            resp = self._patch(f"{self.ACCOUNTS_URL}{cxp.pk}/", {"factura_proveedor": target.pk})

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsInstance(resp.data["factura_proveedor"], list)
        cxp.refresh_from_db()
        self.assertEqual(cxp.factura_proveedor_id, factura.pk)

    def test_unrelated_integrity_error_on_update_is_not_disguised_as_duplicate(self):
        _, _, cxp = self._account()

        with patch.object(
            CuentaPorPagarSerializer, "update", side_effect=IntegrityError("otra restricción")
        ):
            with self.assertRaises(IntegrityError):
                self._patch(f"{self.ACCOUNTS_URL}{cxp.pk}/", {"observaciones": "x"})

    # -- Esquema OpenAPI ----------------------------------------------------------

    def test_openapi_request_schemas_match_runtime_writability(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        components = schema["components"]["schemas"]
        create_fields = components["CuentaPorPagarCreateRequest"]["properties"]
        update_fields = components["CuentaPorPagarRequest"]["properties"]
        patch_fields = components["PatchedCuentaPorPagarRequest"]["properties"]

        for field in ("saldo", "estatus"):
            with self.subTest(field=field):
                self.assertNotIn(field, create_fields)
                self.assertIn(field, update_fields)
                self.assertIn(field, patch_fields)


class ConcurrencyConflictMappingTests(FinanzasBase):
    """Choques con otra transacción: 409, nunca un 500 sin mapear.

    Qué cubre esta suite y qué no. SQLite ignora ``select_for_update`` (con o sin
    NOWAIT), así que aquí no se pueden reproducir ni la contención real de un
    bloqueo NOWAIT (55P03) ni un deadlock real (40P01). Se prueba lo alcanzable:

    - que el guard de borrado pide sus bloqueos con ``nowait=True``;
    - que traduce el 55P03 del driver a ``ConcurrentOperationError`` y deja pasar
      cualquier otro error de base de datos;
    - que la frontera HTTP de finanzas convierte 40P01 y 55P03 en 409 y no enmascara
      los demás ``OperationalError``.

    El comportamiento real bajo contención (que el NOWAIT de verdad no espere y que
    Postgres aborte el deadlock con 40P01) sólo se puede verificar en Postgres.
    """

    ACCOUNTS_URL = "/api/v1/finanzas/cuentas-por-pagar/"
    GUARD_MODULE = "finanzas.services.cuenta_por_pagar_service"

    @staticmethod
    def _driver_error(sqlstate):
        # Django relanza el error del driver ``from`` el original: el SQLSTATE queda
        # en ``__cause__``, como con psycopg.
        cause = Exception("error del driver")
        cause.sqlstate = sqlstate
        error = OperationalError("error de base de datos")
        error.__cause__ = cause
        return error

    def _account(self):
        proveedor, _, _, _, _, _, factura = self._crear_oc_recepcion_y_factura_proveedor(
            self.a["empresa"], self.a["sucursal"], self.a["usuario"],
        )
        return CuentaPorPagar.objects.create(
            empresa=self.a["empresa"], proveedor=proveedor, factura_proveedor=factura,
            total=Decimal("100.00"), saldo=Decimal("100.00"),
        )

    def _delete_account(self, cxp):
        return self._client(self.a["usuario"]).delete(f"{self.ACCOUNTS_URL}{cxp.pk}/")

    def _guard_lock_mocks(self, failing, sqlstate):
        # Sustituye los modelos que usa el guard: la consulta de bloqueo de
        # ``failing`` lanza el error del driver y la otra devuelve filas vacías.
        payment_lines, accounts = MagicMock(), MagicMock()
        for mock, name in ((payment_lines, "lines"), (accounts, "accounts")):
            locked = mock.objects.select_for_update.return_value.filter.return_value.only.return_value
            if name == failing:
                locked.__iter__.side_effect = self._driver_error(sqlstate)
            else:
                locked.__iter__.return_value = iter([])
        return payment_lines, accounts

    # -- Frontera HTTP (C) --------------------------------------------------------

    def test_deadlock_and_lock_contention_map_to_409(self):
        cxp = self._account()

        for sqlstate in ("40P01", "55P03"):
            with self.subTest(sqlstate=sqlstate):
                with patch.object(
                    CuentaPorPagarViewSet, "perform_destroy", side_effect=self._driver_error(sqlstate)
                ):
                    resp = self._delete_account(cxp)

                self.assertEqual(resp.status_code, 409, resp.data)
                self.assertEqual(resp.data, [CONCURRENT_OPERATION_MESSAGE])

    def test_other_database_errors_are_not_masked(self):
        cxp = self._account()

        with patch.object(
            CuentaPorPagarViewSet, "perform_destroy", side_effect=self._driver_error("08006")
        ):
            with self.assertRaises(OperationalError):
                self._delete_account(cxp)

    def test_concurrent_operation_error_maps_to_409_with_its_message(self):
        cxp = self._account()

        with patch.object(
            CuentaPorPagarViewSet, "perform_destroy",
            side_effect=ConcurrentOperationError("mensaje del servicio"),
        ):
            resp = self._delete_account(cxp)

        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertEqual(resp.data, ["mensaje del servicio"])

    # -- Guard de borrado con NOWAIT (A) ------------------------------------------

    def test_delete_guard_locks_with_nowait_and_maps_contention_to_409(self):
        cxp = self._account()

        for failing in ("lines", "accounts"):
            with self.subTest(failing=failing):
                payment_lines, accounts = self._guard_lock_mocks(failing, "55P03")
                with patch(f"{self.GUARD_MODULE}.PagoDetalle", payment_lines), \
                        patch(f"{self.GUARD_MODULE}.CuentaPorPagar", accounts):
                    resp = self._delete_account(cxp)

                self.assertEqual(resp.status_code, 409, resp.data)
                self.assertIn("otra operación", str(resp.data[0]))
                payment_lines.objects.select_for_update.assert_called_once_with(nowait=True)
                if failing == "accounts":
                    accounts.objects.select_for_update.assert_called_once_with(nowait=True)
                self.assertTrue(CuentaPorPagar.objects.filter(pk=cxp.pk).exists())

    def test_delete_guard_leaves_a_deadlock_to_the_http_mapping(self):
        cxp = self._account()
        payment_lines, accounts = self._guard_lock_mocks("lines", "40P01")

        with patch(f"{self.GUARD_MODULE}.PagoDetalle", payment_lines), \
                patch(f"{self.GUARD_MODULE}.CuentaPorPagar", accounts):
            resp = self._delete_account(cxp)

        # El guard sólo traduce el 55P03; el 40P01 lo mapea la frontera HTTP.
        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertEqual(resp.data, [CONCURRENT_OPERATION_MESSAGE])
        self.assertTrue(CuentaPorPagar.objects.filter(pk=cxp.pk).exists())
