"""Verificación de los 4 defectos corregidos en ``finanzas``.

Ejecutar SIEMPRE con BD desechable (el ``.env`` del repo apunta a Supabase de
producción):

    python manage.py test finanzas --settings=sqlite_settings

Nota: SQLite ignora ``select_for_update()`` (Django lo omite en backends sin
soporte), así que estos tests cubren el filtro por empresa y los guards, no la
semántica del lock.
"""

import inspect
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.viewsets import ViewSetMixin

from catalogo.models import Producto, Talla
from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle
from finanzas.api import views as finanzas_views
from finanzas.api.serializers import PolizaDetalleRelacionadoSerializer
from finanzas.api.views import (
    CobroViewSet,
    ErroresDeNegocioComo400Mixin,
    FacturaProveedorViewSet,
)
from finanzas.exceptions import ErrorDeNegocio
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
    Pago,
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
    ``update()`` de renglones queda fuera (ver doc/bloqueo-lineas-hijas-finanzas.md)."""

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
