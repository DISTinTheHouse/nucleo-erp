from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from usuarios.models import Usuario
from nucleo.models import Empresa
from terceros.models import Cliente, Proveedor


class ClientePatchTest(TestCase):
    def setUp(self):
        self.empresa = Empresa.objects.create(codigo="test", razon_social="Test Empresa")
        self.user = Usuario.objects.create(
            username="test",
            email="test@test.com",
            empresa=self.empresa,
            is_admin_empresa=True
        )
        self.client_api = APIClient()
        self.client_api.force_authenticate(user=self.user)
        self.cliente = Cliente.objects.create(
            empresa=self.empresa,
            nombre="Cliente Original",
            razon_social="Razón Social Original",
            correo="original@test.com",
            telefono="1234567890"
        )

    def test_patch_partial_preserves_unmodified_fields(self):
        """PATCH que solo cambia nombre NO debe borrar razon_social, correo, telefono."""
        url = f"/api/v1/terceros/clientes/{self.cliente.id}/"
        payload = {"nombre": "Cliente Modificado"}

        response = self.client_api.patch(url, payload, format="json")

        self.assertEqual(response.status_code, 200)
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.nombre, "Cliente Modificado")
        self.assertEqual(self.cliente.razon_social, "Razón Social Original")
        self.assertEqual(self.cliente.correo, "original@test.com")
        self.assertEqual(self.cliente.telefono, "1234567890")

    def test_patch_partial_can_update_optional_fields(self):
        """PATCH puede actualizar razon_social, correo, telefono explícitamente."""
        url = f"/api/v1/terceros/clientes/{self.cliente.id}/"
        payload = {"razon_social": "Nueva Razón Social"}

        response = self.client_api.patch(url, payload, format="json")

        self.assertEqual(response.status_code, 200)
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.razon_social, "Nueva Razón Social")
        self.assertEqual(self.cliente.correo, "original@test.com")

    def test_patch_partial_can_clear_optional_fields(self):
        """PATCH puede vaciar razon_social, correo, telefono enviándolos como vacío."""
        url = f"/api/v1/terceros/clientes/{self.cliente.id}/"
        payload = {"correo": "", "telefono": ""}

        response = self.client_api.patch(url, payload, format="json")

        self.assertEqual(response.status_code, 200)
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.correo, "")
        self.assertEqual(self.cliente.telefono, "")
        self.assertEqual(self.cliente.razon_social, "Razón Social Original")


class ProveedorEmpresaTests(TestCase):
    """#310: la empresa del proveedor la asigna el servidor. #315: la baja llena fecha_baja."""

    URL = "/api/v1/terceros/proveedores/"

    @classmethod
    def setUpTestData(cls):
        from nucleo.models import Moneda, SatFormaPago, SatMetodoPago, SatRegimenFiscal

        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.otra = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        cls.usuario = Usuario.objects.create(
            username="compras", email="compras@acme.test", empresa=cls.empresa, is_admin_empresa=True,
        )
        cls.regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General")
        cls.forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        cls.metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Una exhibición")
        cls.moneda_global = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.moneda_ajena = Moneda.objects.create(codigo_iso="USD", nombre="Dólar", empresa=cls.otra)

    def setUp(self):
        self.client_api = APIClient()
        self.client_api.force_authenticate(user=self.usuario)

    def _body(self, **extra):
        body = {
            "nombre": "Telas SA", "codigo": "P001", "razon_social": "Telas SA de CV",
            "telefono": "8100000000", "contacto_principal": "Ana", "rfc": "TEL010101AAA",
            "email": "ventas@telas.test", "sat_regimen_fiscal": self.regimen.pk,
            "sat_forma_pago": self.forma.pk, "sat_metodo_pago": self.metodo.pk,
            "moneda": self.moneda_global.pk,
        }
        body.update(extra)
        return body

    def _proveedor(self):
        resp = self.client_api.post(self.URL, self._body(), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        return Proveedor.objects.get(pk=resp.data["id"])

    def test_post_con_empresa_ajena_queda_en_la_del_usuario(self):
        resp = self.client_api.post(self.URL, self._body(empresa=self.otra.pk), format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Proveedor.objects.get(pk=resp.data["id"]).empresa_id, self.empresa.pk)

    def test_post_sin_empresa_queda_visible_para_el_usuario(self):
        proveedor = self._proveedor()

        self.assertEqual(proveedor.empresa_id, self.empresa.pk)
        self.assertEqual(self.client_api.get(f"{self.URL}{proveedor.pk}/").status_code, 200)

    def test_patch_no_cambia_la_empresa(self):
        proveedor = self._proveedor()

        resp = self.client_api.patch(f"{self.URL}{proveedor.pk}/", {"empresa": self.otra.pk}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        proveedor.refresh_from_db()
        self.assertEqual(proveedor.empresa_id, self.empresa.pk)

    def test_moneda_de_otra_empresa_responde_400(self):
        resp = self.client_api.post(self.URL, self._body(moneda=self.moneda_ajena.pk), format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("moneda", resp.data)

    def test_usuario_sin_empresa_no_crea_proveedor(self):
        sin_empresa = Usuario.objects.create(username="libre", email="libre@acme.test")
        self.client_api.force_authenticate(user=sin_empresa)

        resp = self.client_api.post(self.URL, self._body(), format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Proveedor.objects.exists())

    def test_baja_logica_registra_fecha_baja(self):
        proveedor = self._proveedor()

        resp = self.client_api.delete(f"{self.URL}{proveedor.pk}/")

        self.assertEqual(resp.status_code, 204)
        proveedor.refresh_from_db()
        self.assertFalse(proveedor.activo)
        self.assertIsNotNone(proveedor.fecha_baja)


    # --- #315: acumulados de solo lectura -------------------------------------

    ACUMULADOS = {
        "saldo_anterior": "10.00", "saldo_actual": "20.00", "saldo_acumulado": "30.00",
        "plazo_real_dias": 15, "fecha_ultima_compra": "2026-01-01",
        "fecha_ultimo_pago": "2026-01-02", "fecha_baja": "2026-01-03",
    }

    def test_post_y_patch_ignoran_los_acumulados(self):
        resp = self.client_api.post(self.URL, self._body(**self.ACUMULADOS), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        proveedor = Proveedor.objects.get(pk=resp.data["id"])

        self.client_api.patch(f"{self.URL}{proveedor.pk}/", self.ACUMULADOS, format="json")

        proveedor.refresh_from_db()
        self.assertEqual(proveedor.saldo_actual, 0)
        self.assertEqual(proveedor.plazo_real_dias, 0)
        self.assertIsNone(proveedor.fecha_ultima_compra)
        self.assertIsNone(proveedor.fecha_baja)


class ProveedorKpisTests(TestCase):
    """``GET /proveedores/kpis/`` (EC-436): scorecard por proveedor con datos
    reales de compras (sin fabricar capacidad/fechas que no existen)."""

    URL = "/api/v1/terceros/proveedores/kpis/"

    @classmethod
    def setUpTestData(cls):
        from datetime import date
        from decimal import Decimal as D

        from compras.models import (
            CalidadInspeccion, CalidadInspeccionDetalle, OrdenCompra, OrdenCompraDetalle,
            Recepcion, RecepcionDetalle,
        )
        from finanzas.models import FacturaProveedor, FacturaProveedorDetalle
        from hr.models import Empleado, Puesto
        from inventarios.models import Almacen
        from nucleo.models import Departamento, Moneda, SatFormaPago, SatMetodoPago, SatRegimenFiscal, Sucursal

        regimen = SatRegimenFiscal.objects.create(codigo="601", descripcion="General de Ley")
        forma = SatFormaPago.objects.create(codigo="03", descripcion="Transferencia")
        metodo = SatMetodoPago.objects.create(codigo="PUE", descripcion="Pago en una sola exhibición")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="MTY")
        cls.almacen = Almacen.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, codigo="ALM", nombre="Almacen")
        cls.usuario = Usuario.objects.create(
            username="u@acme.test", email="u@acme.test", empresa=cls.empresa, sucursal_default=cls.sucursal,
        )

        def _proveedor(codigo, nombre):
            return Proveedor.objects.create(
                empresa=cls.empresa, nombre=nombre, moneda=cls.moneda, sat_regimen_fiscal=regimen,
                sat_forma_pago=forma, sat_metodo_pago=metodo, codigo=codigo, razon_social=f"{nombre} SA",
                telefono="8100000000", contacto_principal="Contacto", rfc="XAXX010101000", email=f"{codigo}@test.mx",
            )

        cls.bueno = _proveedor("PROV-BUENO", "Proveedor Puntual")
        cls.malo = _proveedor("PROV-MALO", "Proveedor Tardío")

        from catalogo.models import Producto

        cls.producto = Producto.objects.create(empresa=cls.empresa, nombre="Insumo Test")

        departamento = Departamento.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, codigo="CAL", nombre="Calidad",
        )
        puesto = Puesto.objects.create(empresa=cls.empresa, nombre="Inspector")
        inspector = Empleado.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, departamento=departamento, puesto=puesto,
            numero_empleado="EMP-1", nombre="Juan", apellido_paterno="Perez", fecha_ingreso=date(2020, 1, 1),
        )

        def _oc_con_recepcion(proveedor, folio, fecha_oc, fecha_compromiso, fecha_recepcion,
                               cantidad=10, precio=D("5.00"), cantidad_recibida=10, precio_facturado=None):
            oc = OrdenCompra.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal, proveedor=proveedor, moneda=cls.moneda,
                usuario=cls.usuario, fecha_oc=fecha_oc, fecha_entrega_estimada=fecha_compromiso,
                estatus=OrdenCompra.EstatusOrdenCompra.RECIBIDA,
            )
            oc_det = OrdenCompraDetalle.objects.create(
                orden_compra=oc, producto=cls.producto, sucursal=cls.sucursal, cantidad=cantidad, precio=precio,
            )
            rec = Recepcion.objects.create(
                orden_compra=oc, empresa=cls.empresa, sucursal=cls.sucursal, proveedor=proveedor,
                almacen=cls.almacen, usuario=cls.usuario, folio=folio,
                fecha_recepcion=timezone.make_aware(timezone.datetime.combine(fecha_recepcion, timezone.datetime.min.time())),
                estatus=Recepcion.EstatusRecepcion.RECIBIDA,
            )
            rec_det = RecepcionDetalle.objects.create(
                recepcion=rec, orden_compra_detalle=oc_det, producto=cls.producto, cantidad_recibida=cantidad_recibida,
            )
            if precio_facturado is not None:
                factura = FacturaProveedor.objects.create(
                    empresa=cls.empresa, sucursal=cls.sucursal, proveedor=proveedor, oc=oc, recepcion=rec,
                    moneda=cls.moneda, estatus=FacturaProveedor.FacturaProveedorStatus.REGISTRADA,
                )
                FacturaProveedorDetalle.objects.create(
                    factura_proveedor=factura, oc_detalle=oc_det, recepcion_detalle=rec_det,
                    producto=cls.producto, cantidad=cantidad_recibida, precio_unitario=precio_facturado,
                )
            return oc, rec, oc_det, rec_det

        # Proveedor bueno: a tiempo, sin rechazos, completo, precio exacto.
        _oc_con_recepcion(
            cls.bueno, "RC-BUENO-1", date(2026, 1, 1), date(2026, 1, 10), date(2026, 1, 8),
            cantidad=10, precio=D("5.00"), cantidad_recibida=10, precio_facturado=D("5.00"),
        )

        # Proveedor malo: tarde, con rechazo, incompleto, precio arriba del pactado.
        _oc, _rec, _oc_det, rec_det_malo = _oc_con_recepcion(
            cls.malo, "RC-MALO-1", date(2026, 1, 1), date(2026, 1, 10), date(2026, 1, 20),
            cantidad=10, precio=D("5.00"), cantidad_recibida=7, precio_facturado=D("7.00"),
        )
        inspeccion = CalidadInspeccion.objects.create(
            recepcion=_rec, inspector=inspector, fecha=date(2026, 1, 21), estado="rechazada",
        )
        CalidadInspeccionDetalle.objects.create(
            calidad_inspeccion=inspeccion, recepcion_detalle=rec_det_malo,
            cantidad_inspeccionada=7, cantidad_aprobada=4, cantidad_rechazada=3,
            resultado="rechazo", motivo_rechazo="Fuera de especificación",
        )

        cls.sin_empresa = Usuario.objects.create(username="se", email="se@nowhere.test")

    def _get(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.usuario)
        return client.get(self.URL)

    def _fila(self, resp, proveedor_id):
        return next(p for p in resp.data["proveedores"] if p["proveedor_id"] == proveedor_id)

    def test_proveedor_bueno_pasa_los_4_factores_reales(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200, resp.content)
        fila = self._fila(resp, self.bueno.pk)

        self.assertEqual(fila["entrega_a_tiempo"]["pct"], 100.0)
        self.assertEqual(fila["calidad"]["cantidad_rechazada"], 0)
        self.assertEqual(fila["cumplimiento_cantidad"]["pct"], 100.0)
        self.assertEqual(fila["diferencia_precio"]["pct"], 0.0)
        self.assertEqual(fila["scorecard"]["puntaje"], 100.0)
        self.assertEqual(fila["scorecard"]["semaforo"], "verde")

    def test_proveedor_malo_refleja_tardanza_rechazo_y_sobreprecio(self):
        resp = self._get()
        fila = self._fila(resp, self.malo.pk)

        self.assertEqual(fila["entrega_a_tiempo"]["pct"], 0.0)
        self.assertEqual(fila["lead_time"]["dias_promedio_real"], 19.0)
        self.assertEqual(fila["lead_time"]["dias_promedio_pactado"], 9.0)
        self.assertAlmostEqual(fila["calidad"]["pct_rechazado"], 42.9, places=1)
        self.assertAlmostEqual(fila["cumplimiento_cantidad"]["pct"], 70.0, places=1)
        self.assertAlmostEqual(fila["diferencia_precio"]["pct"], 40.0, places=1)
        # Promedio de los 4 factores: a_tiempo(0) + calidad(57.1) + cumplimiento(70) + precio(60).
        self.assertAlmostEqual(fila["scorecard"]["puntaje"], 46.8, places=1)
        self.assertEqual(fila["scorecard"]["semaforo"], "rojo")

    def test_bueno_sale_mejor_rankeado_que_malo(self):
        resp = self._get()
        orden = [p["proveedor_id"] for p in resp.data["proveedores"]]
        self.assertLess(orden.index(self.bueno.pk), orden.index(self.malo.pk))

    def test_usuario_sin_empresa_no_ve_nada(self):
        resp = self._get(self.sin_empresa)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["proveedores"], [])


class ClienteKpisTests(TestCase):
    """``GET /clientes/kpis/`` (EC-422): 3 de 4 KPIs son reales; siempre
    acotado al alcance de ``clientes_visibles`` (un vendedor normal solo ve
    sus propios clientes, igual que el listado). reclamos_devoluciones queda
    ``disponible: False`` -- Devolucion/Entrega no registran cantidad."""

    URL = "/api/v1/terceros/clientes/kpis/"

    @classmethod
    def setUpTestData(cls):
        from decimal import Decimal as D

        from finanzas.models import CuentaPorCobrar, Factura
        from nucleo.models import Moneda, Sucursal
        from ventas.models import Pedido

        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="MTY")
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")

        cls.vendedor_a = Usuario.objects.create(username="va", email="va@acme.test", empresa=cls.empresa)
        cls.vendedor_b = Usuario.objects.create(username="vb", email="vb@acme.test", empresa=cls.empresa)
        cls.admin_empresa = Usuario.objects.create(
            username="admin", email="admin@acme.test", empresa=cls.empresa, is_admin_empresa=True,
        )
        cls.sin_empresa = Usuario.objects.create(username="se", email="se@nowhere.test")

        cls.cliente_a = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente A", correo="a@test.mx")
        cls.cliente_a.vendedores.add(cls.vendedor_a)
        cls.cliente_b = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente B", correo="b@test.mx")
        cls.cliente_b.vendedores.add(cls.vendedor_b)

        def _pedido(cliente, hace_dias):
            p = Pedido.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal, cliente=cliente, moneda=cls.moneda,
                persona_pagos="Pagos", correo_facturas="p@acme.test", telefono_pagos="8100000000",
                forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
            )
            if hace_dias:
                Pedido.objects.filter(pk=p.pk).update(created_at=timezone.now() - timedelta(days=hace_dias))
            return p

        def _factura(cliente, total, estatus=None):
            Estatus = Factura.FacturaStatus
            return Factura.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal, cliente=cliente, moneda=cls.moneda,
                total=D(total), estatus=estatus or Estatus.EMITIDA,
            )

        def _cxc(cliente, factura, saldo, hace_dias_vencida):
            return CuentaPorCobrar.objects.create(
                empresa=cls.empresa, cliente=cliente, factura=factura, total=D(saldo), saldo=D(saldo),
                estatus=CuentaPorCobrar.EstatusCxC.PENDIENTE,
                fecha_vencimiento=timezone.localdate() - timedelta(days=hace_dias_vencida),
            )

        # Cliente A: pedido reciente (activo), facturado 1000 + 500 cancelada (no cuenta).
        _pedido(cls.cliente_a, hace_dias=5)
        factura_a1 = _factura(cls.cliente_a, "1000.00")
        _factura(cls.cliente_a, "500.00", estatus=Factura.FacturaStatus.CANCELADA)
        _cxc(cls.cliente_a, factura_a1, "300.00", hace_dias_vencida=10)  # bucket 0-30

        # Cliente B: pedido viejo (inactivo), facturado 2000.
        _pedido(cls.cliente_b, hace_dias=200)
        factura_b1 = _factura(cls.cliente_b, "2000.00")
        _cxc(cls.cliente_b, factura_b1, "700.00", hace_dias_vencida=45)  # bucket 31-60
        _cxc(cls.cliente_b, factura_b1, "200.00", hace_dias_vencida=90)  # bucket 60+

    def _get(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(self.URL)

    def test_vendedor_solo_ve_sus_propios_clientes(self):
        resp = self._get(self.vendedor_a)

        self.assertEqual(resp.status_code, 200, resp.data)
        ventas = resp.data["ventas_por_cliente"]
        self.assertEqual(ventas["total_facturado"], Decimal("1000.00"))  # excluye la cancelada
        self.assertEqual(ventas["total_clientes_facturados"], 1)
        self.assertEqual([c["cliente_id"] for c in ventas["top_clientes"]], [self.cliente_a.pk])

    def test_admin_ve_todos_los_clientes_de_la_empresa(self):
        resp = self._get(self.admin_empresa)

        ventas = resp.data["ventas_por_cliente"]
        self.assertEqual(ventas["total_facturado"], Decimal("3000.00"))
        self.assertEqual(ventas["total_clientes_facturados"], 2)

    def test_clientes_activos_vs_inactivos(self):
        resp = self._get(self.admin_empresa)

        activos = resp.data["clientes_activos"]
        self.assertEqual(activos["total"], 2)
        self.assertEqual(activos["activos"], 1)  # solo cliente A (pedido hace 5 días)
        self.assertEqual(activos["inactivos"], 1)
        self.assertEqual(activos["pct_activos"], 50.0)

    def test_cartera_antiguedad_buckets(self):
        resp = self._get(self.admin_empresa)

        cartera = resp.data["cartera_antiguedad"]
        self.assertTrue(cartera["disponible"])
        self.assertEqual(cartera["saldo_total_vencido"], Decimal("1200.00"))
        self.assertEqual(cartera["buckets"]["0_30"], {"total": 1, "monto": Decimal("300.00")})
        self.assertEqual(cartera["buckets"]["31_60"], {"total": 1, "monto": Decimal("700.00")})
        self.assertEqual(cartera["buckets"]["60_mas"], {"total": 1, "monto": Decimal("200.00")})

    def test_cartera_acotada_por_vendedor(self):
        resp = self._get(self.vendedor_a)

        cartera = resp.data["cartera_antiguedad"]
        self.assertEqual(cartera["saldo_total_vencido"], Decimal("300.00"))

    def test_reclamos_devoluciones_no_disponible(self):
        resp = self._get(self.admin_empresa)
        self.assertFalse(resp.data["reclamos_devoluciones"]["disponible"])

    def test_usuario_sin_empresa_no_ve_nada(self):
        resp = self._get(self.sin_empresa)

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["ventas_por_cliente"]["disponible"])
