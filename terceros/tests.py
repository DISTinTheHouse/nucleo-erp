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


_FABRICAS = {}


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

        # Proveedor sin fecha prometida en su OC: no se puede saber si llegó tarde (#387).
        cls.sin_fecha = _proveedor("PROV-SF", "Proveedor Sin Fecha")
        _oc_con_recepcion(
            cls.sin_fecha, "RC-SF-1", date(2026, 1, 1), None, date(2026, 1, 8),
            precio_facturado=D("5.00"),
        )

        cls.sin_empresa = Usuario.objects.create(username="se", email="se@nowhere.test")
        cls.superusuario = Usuario.objects.create(
            username="root", email="root@nowhere.test", is_superuser=True, is_staff=True,
        )
        # Para los tests que arman más datos (no se cuelgan de ``cls``: Django
        # copia los atributos de ``setUpTestData`` por instancia).
        _FABRICAS["proveedor"] = _proveedor
        _FABRICAS["oc_con_recepcion"] = _oc_con_recepcion

    def nuevo_proveedor(self, *args, **kwargs):
        return _FABRICAS["proveedor"](*args, **kwargs)

    def nueva_oc_con_recepcion(self, *args, **kwargs):
        return _FABRICAS["oc_con_recepcion"](*args, **kwargs)

    def _get(self, user=None, query=""):
        client = APIClient()
        client.force_authenticate(user=user or self.usuario)
        return client.get(self.URL + query)

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
        # Recibido NETO (#381): 7 recibidas - 3 rechazadas = 4 de 10.
        self.assertAlmostEqual(fila["cumplimiento_cantidad"]["pct"], 40.0, places=1)
        self.assertAlmostEqual(fila["diferencia_precio"]["pct"], 40.0, places=1)
        # Promedio de los 4 factores: a_tiempo(0) + calidad(57.1) + cumplimiento(40) + precio(60).
        self.assertAlmostEqual(fila["scorecard"]["puntaje"], 39.3, places=1)
        self.assertEqual(fila["scorecard"]["semaforo"], "rojo")

    def test_recepcion_sin_fecha_prometida_no_cuenta_como_tarde(self):
        fila = self._fila(self._get(), self.sin_fecha.pk)

        self.assertIsNone(fila["entrega_a_tiempo"]["pct"])
        self.assertEqual(fila["entrega_a_tiempo"]["recepciones_total"], 1)
        self.assertEqual(fila["entrega_a_tiempo"]["recepciones_con_compromiso"], 0)
        # El scorecard no penaliza el factor sin dato: cumplimiento 100 y precio 100.
        self.assertEqual(fila["scorecard"]["puntaje"], 100.0)

    def test_bueno_sale_mejor_rankeado_que_malo(self):
        resp = self._get()
        orden = [p["proveedor_id"] for p in resp.data["proveedores"]]
        self.assertLess(orden.index(self.bueno.pk), orden.index(self.malo.pk))

    def test_usuario_sin_empresa_no_ve_nada(self):
        resp = self._get(self.sin_empresa)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["proveedores"], [])
        # Distinto de "sin datos" (#390).
        self.assertFalse(resp.data["disponible"])
        self.assertIn("sin empresa", resp.data["motivo"])

    def test_con_empresa_sin_datos_si_esta_disponible(self):
        otra = Empresa.objects.create(codigo="vacia", razon_social="VACIA SA")
        user = Usuario.objects.create(username="uv", email="uv@vacia.test", empresa=otra)

        resp = self._get(user)

        self.assertTrue(resp.data["disponible"])
        self.assertEqual(resp.data["proveedores"], [])

    # --- #390: parámetros, tope, desempate, inactivos ---------------------------

    def test_parametro_desconocido_responde_400(self):
        resp = self._get(query="?peso_precio=0&meta=99")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("meta", str(resp.data["parametros"]))
        self.assertIn("peso_precio", str(resp.data["parametros"]))

    def test_parametros_invalidos_responden_400(self):
        for query in ("?meta_scorecard=abc", "?meta_scorecard=101", "?limite=0", "?limite=999",
                      "?fecha_desde=2026-13-45", "?fecha_desde=2026-02-01&fecha_hasta=2026-01-01",
                      "?proveedor=x"):
            self.assertEqual(self._get(query=query).status_code, 400, query)

    def test_meta_scorecard_cambia_el_semaforo_y_se_devuelve(self):
        resp = self._get(query="?meta_scorecard=30")

        self.assertEqual(resp.data["filtros"]["meta_scorecard"], 30.0)
        self.assertEqual(self._fila(resp, self.malo.pk)["scorecard"]["semaforo"], "verde")  # 39.3 >= 30

    def test_filtro_por_proveedor(self):
        resp = self._get(query=f"?proveedor={self.malo.pk}")

        self.assertEqual([p["proveedor_id"] for p in resp.data["proveedores"]], [self.malo.pk])

    def test_limite_marca_truncado_y_total(self):
        resp = self._get(query="?limite=1")

        self.assertEqual(len(resp.data["proveedores"]), 1)
        self.assertEqual(resp.data["total_proveedores"], 3)
        self.assertTrue(resp.data["truncado"])
        self.assertFalse(self._get().data["truncado"])

    def test_empate_se_desempata_por_nombre(self):
        from datetime import date
        from decimal import Decimal as D

        otro = self.nuevo_proveedor("PROV-AAA", "AAA Igual de Puntual")
        self.nueva_oc_con_recepcion(
            otro, "RC-AAA-1", date(2026, 1, 1), date(2026, 1, 10), date(2026, 1, 8),
            cantidad=10, precio=D("5.00"), cantidad_recibida=10, precio_facturado=D("5.00"),
        )

        orden = [p["proveedor_id"] for p in self._get().data["proveedores"]]

        self.assertEqual(orden[:2], [otro.pk, self.bueno.pk])  # mismo puntaje 100: nombre ascendente

    def test_proveedor_inactivo_no_aparece(self):
        Proveedor.objects.filter(pk=self.malo.pk).update(activo=False)

        ids = [p["proveedor_id"] for p in self._get().data["proveedores"]]

        self.assertNotIn(self.malo.pk, ids)
        self.assertIn(self.bueno.pk, ids)

    # --- #388: lead time y zona horaria ---------------------------------------

    def test_lead_time_real_y_pactado_usan_las_mismas_recepciones(self):
        from datetime import date

        prov = self.nuevo_proveedor("PROV-LT", "Lead Time")
        # Con fecha prometida: real 5 días, pactado 4.
        self.nueva_oc_con_recepcion(prov, "RC-LT-1", date(2026, 2, 1), date(2026, 2, 5), date(2026, 2, 6))
        # Sin fecha prometida y llegó a los 50 días: no puede contaminar el promedio real.
        self.nueva_oc_con_recepcion(prov, "RC-LT-2", date(2026, 2, 1), None, date(2026, 3, 23))

        lead = self._fila(self._get(), prov.pk)["lead_time"]

        self.assertEqual(lead["dias_promedio_real"], 5.0)
        self.assertEqual(lead["dias_promedio_pactado"], 4.0)
        self.assertEqual(lead["recepciones_medidas"], 1)

    def test_puntualidad_usa_la_fecha_local_no_utc(self):
        """Recepción a las 23:30 hora de CDMX = 05:30 UTC del día siguiente."""
        from datetime import date, datetime

        from compras.models import Recepcion

        prov = self.nuevo_proveedor("PROV-TZ", "Zona Horaria")
        _oc, rec, _d, _r = self.nueva_oc_con_recepcion(
            prov, "RC-TZ-1", date(2026, 1, 1), date(2026, 1, 10), date(2026, 1, 10)
        )
        Recepcion.objects.filter(pk=rec.pk).update(fecha_recepcion=timezone.make_aware(datetime(2026, 1, 10, 23, 30)))

        fila = self._fila(self._get(), prov.pk)

        self.assertEqual(fila["entrega_a_tiempo"]["pct"], 100.0)
        self.assertEqual(fila["lead_time"]["dias_promedio_real"], 9.0)

    # --- #389/#378: criterios compartidos -------------------------------------

    def test_factura_en_borrador_no_cuenta_en_la_diferencia_de_precio(self):
        from finanzas.models import FacturaProveedor, FacturaProveedorDetalle

        FacturaProveedor.objects.filter(proveedor=self.malo).update(
            estatus=FacturaProveedor.FacturaProveedorStatus.BORRADOR
        )

        fila = self._fila(self._get(), self.malo.pk)

        self.assertIsNone(fila["diferencia_precio"]["pct"])
        self.assertEqual(fila["diferencia_precio"]["por_moneda"], [])
        self.assertTrue(FacturaProveedorDetalle.objects.filter(factura_proveedor__proveedor=self.malo).exists())

    def test_factura_en_otra_moneda_que_su_oc_no_se_compara(self):
        from nucleo.models import Moneda

        from finanzas.models import FacturaProveedor

        usd = Moneda.objects.create(codigo_iso="USD", nombre="Dólar")
        FacturaProveedor.objects.filter(proveedor=self.malo).update(moneda=usd)

        fila = self._fila(self._get(), self.malo.pk)

        self.assertIsNone(fila["diferencia_precio"]["pct"])

    def test_solo_rechazos_con_resultado_rechazo_cuentan_en_calidad(self):
        from compras.models import CalidadInspeccionDetalle

        CalidadInspeccionDetalle.objects.filter(recepcion_detalle__recepcion__proveedor=self.malo).update(
            resultado="liberado"
        )

        fila = self._fila(self._get(), self.malo.pk)

        self.assertEqual(fila["calidad"]["pct_rechazado"], 0.0)

    def test_dos_unidades_no_se_suman_en_el_cumplimiento(self):
        from datetime import date

        from catalogo.models import Producto
        from nucleo.models import UnidadMedida

        from compras.models import OrdenCompra, OrdenCompraDetalle

        prov = self.nuevo_proveedor("PROV-UM", "Dos Unidades")
        oc, _rec, _d, _r = self.nueva_oc_con_recepcion(prov, "RC-UM-1", date(2026, 1, 1), date(2026, 1, 10), date(2026, 1, 8))
        metros = UnidadMedida.objects.create(clave="MTR", nombre="Metro")
        piezas = UnidadMedida.objects.create(clave="PZA", nombre="Pieza")
        Producto.objects.filter(pk=self.producto.pk).update(unidad_medida=metros)
        otro = Producto.objects.create(empresa=self.empresa, nombre="Botón", unidad_medida=piezas)
        OrdenCompraDetalle.objects.create(orden_compra=oc, producto=otro, sucursal=self.sucursal, cantidad=1000, precio=1)

        cumplimiento = self._fila(self._get(), prov.pk)["cumplimiento_cantidad"]

        self.assertTrue(cumplimiento["mezcla_unidades"])
        self.assertIsNone(cumplimiento["pct"])
        self.assertEqual({u["unidad"] for u in cumplimiento["por_unidad"]}, {"MTR", "PZA"})
        self.assertEqual(OrdenCompra.objects.filter(proveedor=prov).count(), 1)

    # --- aislamiento y superusuario ------------------------------------------

    def test_otra_empresa_no_contamina_ni_se_ve(self):
        from datetime import date

        from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle
        from catalogo.models import Producto
        from inventarios.models import Almacen
        from nucleo.models import Sucursal

        globex = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        suc = Sucursal.objects.create(empresa=globex, codigo="GDL", nombre="GDL")
        alm = Almacen.objects.create(empresa=globex, sucursal=suc, codigo="ALG", nombre="Almacen G")
        usuario_g = Usuario.objects.create(username="ug", email="ug@globex.test", empresa=globex)
        prov = Proveedor.objects.create(
            empresa=globex, nombre="Proveedor Globex", moneda=self.moneda, sat_regimen_fiscal=self.bueno.sat_regimen_fiscal,
            sat_forma_pago=self.bueno.sat_forma_pago, sat_metodo_pago=self.bueno.sat_metodo_pago, codigo="PG",
            razon_social="PG SA", telefono="8100000000", contacto_principal="C", rfc="XAXX010101000", email="pg@t.mx",
        )
        producto = Producto.objects.create(empresa=globex, nombre="Insumo G")
        oc = OrdenCompra.objects.create(
            empresa=globex, sucursal=suc, proveedor=prov, moneda=self.moneda, usuario=usuario_g,
            fecha_oc=date(2026, 1, 1), estatus=OrdenCompra.EstatusOrdenCompra.RECIBIDA,
        )
        det = OrdenCompraDetalle.objects.create(orden_compra=oc, producto=producto, sucursal=suc, cantidad=10, precio=1)
        rec = Recepcion.objects.create(
            orden_compra=oc, empresa=globex, sucursal=suc, proveedor=prov, almacen=alm, usuario=usuario_g,
            folio="RC-G", fecha_recepcion=timezone.now(), estatus=Recepcion.EstatusRecepcion.RECIBIDA,
        )
        RecepcionDetalle.objects.create(recepcion=rec, orden_compra_detalle=det, producto=producto, cantidad_recibida=10)

        ids = [p["proveedor_id"] for p in self._get().data["proveedores"]]

        self.assertNotIn(prov.pk, ids)
        self.assertEqual(self.client_api_get_detalle(self.usuario, prov.pk, "puntualidad").status_code, 404)

    def test_superusuario_ve_todas_las_empresas_con_o_sin_empresa(self):
        sin_empresa = self._get(self.superusuario)
        con_empresa = Usuario.objects.create(
            username="root2", email="root2@acme.test", is_superuser=True, is_staff=True, empresa=self.empresa,
        )

        self.assertTrue(sin_empresa.data["disponible"])
        self.assertEqual(len(sin_empresa.data["proveedores"]), 3)
        self.assertEqual(len(self._get(con_empresa).data["proveedores"]), 3)

    # --- drill-down por proveedor ---------------------------------------------

    def client_api_get_detalle(self, user, proveedor_id, factor, extra=""):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(f"/api/v1/terceros/proveedores/{proveedor_id}/kpis-detalle/?factor={factor}{extra}")

    def test_detalle_de_puntualidad_lista_las_recepciones(self):
        resp = self.client_api_get_detalle(self.usuario, self.malo.pk, "puntualidad")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["total"], 1)
        self.assertFalse(resp.data["truncado"])
        self.assertEqual(resp.data["registros"][0]["recepcion_folio"], "RC-MALO-1")
        self.assertFalse(resp.data["registros"][0]["a_tiempo"])

    def test_detalle_de_calidad_precio_y_cumplimiento(self):
        calidad = self.client_api_get_detalle(self.usuario, self.malo.pk, "calidad").data
        precio = self.client_api_get_detalle(self.usuario, self.malo.pk, "precio").data
        cumplimiento = self.client_api_get_detalle(self.usuario, self.malo.pk, "cumplimiento").data

        self.assertEqual(calidad["registros"][0]["cantidad_rechazada"], 3)
        self.assertEqual(precio["registros"][0]["precio_facturado"], Decimal("7.00"))
        self.assertEqual(cumplimiento["registros"][0]["cantidad_recibida"], Decimal("4"))

    def test_detalle_factor_invalido_responde_400(self):
        self.assertEqual(self.client_api_get_detalle(self.usuario, self.malo.pk, "otro").status_code, 400)


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

        def _pedido(cliente, hace_dias, estatus=3):  # 3 = AUTORIZADA
            p = Pedido.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal, cliente=cliente, moneda=cls.moneda,
                persona_pagos="Pagos", correo_facturas="p@acme.test", telefono_pagos="8100000000",
                forma_pago="03", metodo_pago="PUE", uso_cfdi="G03", estatus=estatus,
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
        # Una CxC por factura (``uq_cxc_factura``): la de 60+ va en su propia factura.
        factura_b2 = _factura(cls.cliente_b, "0.00")
        _cxc(cls.cliente_b, factura_b2, "200.00", hace_dias_vencida=90)  # bucket 60+

    def _get(self, user, query=""):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(self.URL + query)

    @staticmethod
    def _mxn(bloque):
        return next(m for m in bloque["por_moneda"] if m["moneda"] == "MXN")

    def test_vendedor_solo_ve_sus_propios_clientes(self):
        resp = self._get(self.vendedor_a)

        self.assertEqual(resp.status_code, 200, resp.data)
        ventas = self._mxn(resp.data["ventas_por_cliente"])
        self.assertEqual(ventas["total_facturado"], Decimal("1000.00"))  # excluye la cancelada
        self.assertEqual(ventas["total_clientes_facturados"], 1)
        self.assertEqual([c["cliente_id"] for c in ventas["top_clientes"]], [self.cliente_a.pk])

    def test_admin_ve_todos_los_clientes_de_la_empresa(self):
        resp = self._get(self.admin_empresa)

        ventas = self._mxn(resp.data["ventas_por_cliente"])
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
        mxn = self._mxn(cartera)
        self.assertEqual(mxn["saldo_total_vencido"], Decimal("1200.00"))
        self.assertEqual(mxn["buckets"]["0_30"], {"total": 1, "monto": Decimal("300.00")})
        self.assertEqual(mxn["buckets"]["31_60"], {"total": 1, "monto": Decimal("700.00")})
        self.assertEqual(mxn["buckets"]["60_mas"], {"total": 1, "monto": Decimal("200.00")})
        self.assertEqual(cartera["total_cuentas_vencidas"], 3)

    def test_documentos_de_otra_empresa_no_entran(self):
        """#366: pedido, factura y CxC de otra empresa que apuntan a un cliente visible."""
        from finanzas.models import CuentaPorCobrar, Factura
        from nucleo.models import Sucursal
        from ventas.models import Pedido

        otra = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        sucursal = Sucursal.objects.create(empresa=otra, codigo="GDL", nombre="GDL")
        Pedido.objects.create(
            empresa=otra, sucursal=sucursal, cliente=self.cliente_b, moneda=self.moneda,
            persona_pagos="Pagos", correo_facturas="p@globex.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        factura = Factura.objects.create(
            empresa=otra, sucursal=sucursal, cliente=self.cliente_b, moneda=self.moneda,
            total=Decimal("5000.00"), estatus=Factura.FacturaStatus.EMITIDA,
        )
        cxc = CuentaPorCobrar.objects.create(
            empresa=otra, cliente=self.cliente_b, factura=factura, total=Decimal("999.00"),
            saldo=Decimal("999.00"), estatus=CuentaPorCobrar.EstatusCxC.PENDIENTE,
            fecha_vencimiento=timezone.localdate() - timedelta(days=10),
        )

        data = self._get(self.admin_empresa).data

        self.assertEqual(self._mxn(data["ventas_por_cliente"])["total_facturado"], Decimal("3000.00"))
        self.assertEqual(data["clientes_activos"]["activos"], 1)
        self.assertEqual(self._mxn(data["cartera_antiguedad"])["saldo_total_vencido"], Decimal("1200.00"))
        self.assertNotIn(cxc.pk, [f["id"] for f in data["cartera_antiguedad"]["drill_down"]])

    def test_cartera_acotada_por_vendedor(self):
        resp = self._get(self.vendedor_a)

        cartera = resp.data["cartera_antiguedad"]
        self.assertEqual(self._mxn(cartera)["saldo_total_vencido"], Decimal("300.00"))

    def test_reclamos_devoluciones_no_disponible(self):
        resp = self._get(self.admin_empresa)
        self.assertFalse(resp.data["reclamos_devoluciones"]["disponible"])

    def test_usuario_sin_empresa_no_ve_nada(self):
        resp = self._get(self.sin_empresa)

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["ventas_por_cliente"]["disponible"])

    # --- #367: qué cuenta como facturación y como actividad ------------------

    def _factura_extra(self, cliente, total, estatus, moneda=None, **kwargs):
        from finanzas.models import Factura

        return Factura.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=cliente, moneda=moneda or self.moneda,
            total=Decimal(total), estatus=estatus, **kwargs,
        )

    def _pedido_extra(self, cliente, estatus):
        from ventas.models import Pedido

        return Pedido.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=cliente, moneda=self.moneda,
            persona_pagos="Pagos", correo_facturas="p@acme.test", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03", estatus=estatus,
        )

    def test_facturas_borrador_no_cuentan_como_facturacion(self):
        from finanzas.models import Factura

        self._factura_extra(self.cliente_a, "7000.00", Factura.FacturaStatus.BORRADOR)

        ventas = self._mxn(self._get(self.admin_empresa).data["ventas_por_cliente"])

        self.assertEqual(ventas["total_facturado"], Decimal("3000.00"))

    def test_pedidos_borrador_y_cancelado_no_son_actividad(self):
        cliente_c = Cliente.objects.create(empresa=self.empresa, nombre="Cliente C", correo="c@test.mx")
        self._pedido_extra(cliente_c, estatus=1)  # BORRADOR
        self._pedido_extra(cliente_c, estatus=5)  # CANCELADO

        activos = self._get(self.admin_empresa).data["clientes_activos"]
        self.assertEqual(activos["activos"], 1)
        self.assertEqual(activos["inactivos"], 2)

        self._pedido_extra(cliente_c, estatus=2)  # POR AUTORIZAR sí cuenta
        self.assertEqual(self._get(self.admin_empresa).data["clientes_activos"]["activos"], 2)

    # --- #370: monedas -------------------------------------------------------

    def test_ventas_y_cartera_no_mezclan_monedas(self):
        from finanzas.models import CuentaPorCobrar, Factura
        from nucleo.models import Moneda

        usd = Moneda.objects.create(codigo_iso="USD", nombre="Dólar")
        factura = self._factura_extra(self.cliente_a, "100.00", Factura.FacturaStatus.EMITIDA, moneda=usd)
        CuentaPorCobrar.objects.create(
            empresa=self.empresa, cliente=self.cliente_a, factura=factura, total=Decimal("100.00"),
            saldo=Decimal("100.00"), estatus=CuentaPorCobrar.EstatusCxC.PENDIENTE,
            fecha_vencimiento=timezone.localdate() - timedelta(days=5),
        )

        data = self._get(self.admin_empresa).data

        monedas = {m["moneda"]: m for m in data["ventas_por_cliente"]["por_moneda"]}
        self.assertEqual(monedas["MXN"]["total_facturado"], Decimal("3000.00"))
        self.assertEqual(monedas["USD"]["total_facturado"], Decimal("100.00"))
        carteras = {m["moneda"]: m for m in data["cartera_antiguedad"]["por_moneda"]}
        self.assertEqual(carteras["MXN"]["saldo_total_vencido"], Decimal("1200.00"))
        self.assertEqual(carteras["USD"]["saldo_total_vencido"], Decimal("100.00"))
        self.assertEqual({f["moneda"] for f in data["cartera_antiguedad"]["drill_down"]}, {"MXN", "USD"})

    # --- #368: cartera --------------------------------------------------------

    def test_cartera_sin_vencimiento_va_aparte(self):
        from finanzas.models import CuentaPorCobrar, Factura

        factura = self._factura_extra(self.cliente_a, "50.00", Factura.FacturaStatus.EMITIDA)
        CuentaPorCobrar.objects.create(
            empresa=self.empresa, cliente=self.cliente_a, factura=factura, total=Decimal("50.00"),
            saldo=Decimal("50.00"), estatus=CuentaPorCobrar.EstatusCxC.PENDIENTE,
        )

        cartera = self._get(self.admin_empresa).data["cartera_antiguedad"]

        self.assertEqual(cartera["sin_vencimiento"], [{"moneda": "MXN", "total": 1, "monto": Decimal("50.00")}])
        self.assertEqual(self._mxn(cartera)["saldo_total_vencido"], Decimal("1200.00"))  # no se mezcla

    def test_cartera_excluye_facturas_canceladas_o_inactivas(self):
        from finanzas.models import CuentaPorCobrar, Factura

        vencida = timezone.localdate() - timedelta(days=10)
        for estatus, activo in ((Factura.FacturaStatus.CANCELADA, True), (Factura.FacturaStatus.EMITIDA, False)):
            factura = self._factura_extra(self.cliente_a, "10.00", estatus, activo=activo)
            CuentaPorCobrar.objects.create(
                empresa=self.empresa, cliente=self.cliente_a, factura=factura, total=Decimal("10.00"),
                saldo=Decimal("10.00"), estatus=CuentaPorCobrar.EstatusCxC.PENDIENTE, fecha_vencimiento=vencida,
            )

        cartera = self._get(self.admin_empresa).data["cartera_antiguedad"]

        self.assertEqual(self._mxn(cartera)["saldo_total_vencido"], Decimal("1200.00"))
        self.assertEqual(cartera["total_cuentas_vencidas"], 3)
        self.assertEqual(cartera["sin_vencimiento"], [])

    # --- #371: drill-down de activos / inactivos -----------------------------

    def test_drill_down_de_clientes_activos_e_inactivos(self):
        activos = self._get(self.admin_empresa).data["clientes_activos"]

        lista = activos["drill_down"]
        self.assertEqual([c["cliente_id"] for c in lista["activos"]], [self.cliente_a.pk])
        self.assertEqual([c["cliente_id"] for c in lista["inactivos"]], [self.cliente_b.pk])
        # Misma definición que los conteos.
        self.assertEqual(len(lista["activos"]), activos["activos"])
        self.assertEqual(len(lista["inactivos"]), activos["inactivos"])
        self.assertFalse(lista["activos_truncado"])
        self.assertFalse(lista["inactivos_truncado"])

    def test_drill_down_de_clientes_se_trunca_y_lo_indica(self):
        for i in range(25):
            Cliente.objects.create(empresa=self.empresa, nombre=f"Inactivo {i:02d}", correo=f"i{i}@test.mx")

        activos = self._get(self.admin_empresa).data["clientes_activos"]

        self.assertEqual(activos["inactivos"], 26)
        self.assertEqual(len(activos["drill_down"]["inactivos"]), 20)
        self.assertTrue(activos["drill_down"]["inactivos_truncado"])

    def test_drill_down_respeta_el_alcance_del_vendedor(self):
        activos = self._get(self.vendedor_a).data["clientes_activos"]

        self.assertEqual(activos["total"], 1)
        self.assertEqual([c["cliente_id"] for c in activos["drill_down"]["activos"]], [self.cliente_a.pk])
        self.assertEqual(activos["drill_down"]["inactivos"], [])

    def test_parametro_desconocido_responde_400(self):
        resp = self._get(self.admin_empresa, "?fecha_desde=2030-01-01")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("fecha_desde", str(resp.data["parametros"]))

class ClienteCarteraTests(TestCase):
    """``resumen_comercial.cartera``: saldo por cobrar por moneda y antigüedad (30/60/90/120)."""

    @classmethod
    def setUpTestData(cls):
        from nucleo.models import Moneda, Sucursal

        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="MTY")
        cls.mxn = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.admin = Usuario.objects.create(
            username="admin", email="admin@acme.test", empresa=cls.empresa, is_admin_empresa=True
        )
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente A")

    def _cxc(self, saldo, dias_vencida=None, estatus=None):
        from finanzas.models import CuentaPorCobrar, Factura

        hoy = timezone.localdate()
        factura = Factura.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, cliente=self.cliente, moneda=self.mxn
        )
        cxc = CuentaPorCobrar.objects.create(
            empresa=self.empresa, cliente=self.cliente, factura=factura,
            total=Decimal(saldo), saldo=Decimal(saldo),
            fecha_vencimiento=None if dias_vencida is None else hoy - timedelta(days=dias_vencida),
        )
        if estatus:
            CuentaPorCobrar.objects.filter(pk=cxc.pk).update(estatus=estatus)

    def _cartera(self):
        client = APIClient()
        client.force_authenticate(user=self.admin)
        resp = client.get(f"/api/v1/terceros/clientes/{self.cliente.pk}/")
        self.assertEqual(resp.status_code, 200)
        return resp.data["resumen_comercial"]["cartera"]

    def test_reparte_el_saldo_por_antiguedad(self):
        self._cxc("10.00")            # sin vencimiento: por vencer
        self._cxc("20.00", -5)        # vence en 5 días: por vencer
        self._cxc("30.00", 1)
        self._cxc("40.00", 30)
        self._cxc("50.00", 31)
        self._cxc("60.00", 90)
        self._cxc("70.00", 91)
        self._cxc("80.00", 121)

        (mxn,) = self._cartera()

        self.assertEqual(mxn["moneda"], "MXN")
        self.assertEqual(mxn["por_vencer"], "30.00")
        self.assertEqual(mxn["vencido_1_30"], "70.00")
        self.assertEqual(mxn["vencido_31_60"], "50.00")
        self.assertEqual(mxn["vencido_61_90"], "60.00")
        self.assertEqual(mxn["vencido_91_120"], "70.00")
        self.assertEqual(mxn["vencido_mas_120"], "80.00")
        self.assertEqual(mxn["saldo_total"], "360.00")

    def test_ignora_canceladas_y_saldadas(self):
        self._cxc("100.00", 10, estatus="Cancelada")
        self._cxc("0.00", 10)

        self.assertEqual(self._cartera(), [])

