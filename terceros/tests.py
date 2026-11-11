from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from usuarios.models import Usuario
from nucleo.models import Empresa
from terceros.models import Cliente, Proveedor
from catalogo.models import TipoProducto
from ventas.tests import ImportesPedidoFixture


def _tipo_producto():
    """Tipo de producto obligatorio (#871); PT tiene pk=3 como en producción."""
    return TipoProducto.objects.get_or_create(codigo="PT", defaults={"pk": 3, "nombre": "Producto terminado"})[0]


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
            "nombre": "Telas SA", "razon_social": "Telas SA de CV",
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

    def test_codigo_consecutivo_ignora_el_del_cliente(self):
        primero = self.client_api.post(self.URL, self._body(codigo="MANUAL"), format="json")
        segundo = self.client_api.post(self.URL, self._body(), format="json")
        self.assertEqual((primero.data["codigo"], segundo.data["codigo"]), ("PROV00001", "PROV00002"))
        resp = self.client_api.patch(f"{self.URL}{segundo.data['id']}/", {"codigo": "X"}, format="json")
        self.assertEqual(resp.data["codigo"], "PROV00002")

    def test_codigo_por_empresa_cuenta_bajas_e_ignora_heredados(self):
        baja = self._proveedor()
        Proveedor.objects.filter(pk=baja.pk).update(codigo="prov00007", activo=False)
        heredado = self._proveedor()
        Proveedor.objects.filter(pk=heredado.pk).update(codigo="PROV-0001")
        Proveedor.objects.filter(pk=self._proveedor().pk).update(empresa=self.otra, codigo="PROV00050")
        self.assertEqual(self._proveedor().codigo, "PROV00008")

    def test_codigo_unico_por_empresa_en_bd(self):
        from django.db import IntegrityError, transaction
        p = self._proveedor()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Proveedor.objects.filter(pk=self._proveedor().pk).update(codigo=p.codigo)

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

    def test_baja_se_abre_en_solo_lectura(self):
        """#888: lo que lista ``?activo=false`` se abre; escribir sobre la baja es 400."""
        proveedor = self._proveedor()
        self.client_api.delete(f"{self.URL}{proveedor.pk}/")
        url = f"{self.URL}{proveedor.pk}/"

        bajas = self.client_api.get(f"{self.URL}?activo=false").json()
        self.assertEqual([p["id"] for p in bajas], [proveedor.pk])
        self.assertEqual(self.client_api.get(url).status_code, 200)
        self.assertEqual(self.client_api.get(f"{url}historial-ordenes-compra/").status_code, 200)
        self.assertEqual(self.client_api.get(f"{url}kpis-detalle/?factor=precio").status_code, 200)
        for resp in (
            self.client_api.patch(url, {"nombre": "Otro"}, format="json"),
            self.client_api.put(url, self._body(), format="json"),
            self.client_api.delete(url),
        ):
            self.assertEqual(resp.status_code, 400, resp.content)
            self.assertEqual(resp.json(), {"detail": "El proveedor está dado de baja."})
        # El listado por defecto sigue sin las bajas.
        self.assertEqual(self.client_api.get(self.URL).json(), [])


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

        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Insumo Test")

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
        self.assertFalse(resp.data["puede_ver_importes"])  # #860

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
        # EC-829: ``-5`` responde el mismo mensaje de rango que ``0``; ``1_0`` no es entero.
        self.assertEqual(self._get(query="?limite=-5").data, self._get(query="?limite=0").data)
        self.assertEqual(self._get(query="?limite=1_0").data, {"limite": "Debe ser un entero válido."})

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
        otro = Producto.objects.create(tipo=_tipo_producto(), empresa=self.empresa, nombre="Botón", unidad_medida=piezas)
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
        producto = Producto.objects.create(tipo=_tipo_producto(), empresa=globex, nombre="Insumo G")
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

    def test_superusuario_queda_acotado_a_su_empresa(self):
        """#834: mismo alcance que el listado de OC; sin empresa, no disponible."""
        sin_empresa = self._get(self.superusuario)
        con_empresa = Usuario.objects.create(
            username="root2", email="root2@acme.test", is_superuser=True, is_staff=True, empresa=self.empresa,
        )

        self.assertFalse(sin_empresa.data["disponible"])
        data = self._get(con_empresa).data
        self.assertEqual(len(data["proveedores"]), 3)
        self.assertTrue(data["puede_ver_importes"])

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
        admin = Usuario.objects.create(
            username="adm@acme.test", email="adm@acme.test", empresa=self.empresa, is_admin_empresa=True,
        )
        calidad = self.client_api_get_detalle(self.usuario, self.malo.pk, "calidad").data
        precio = self.client_api_get_detalle(admin, self.malo.pk, "precio").data
        cumplimiento = self.client_api_get_detalle(self.usuario, self.malo.pk, "cumplimiento").data

        self.assertEqual(calidad["registros"][0]["cantidad_rechazada"], 3)
        self.assertEqual(precio["registros"][0]["precio_facturado"], Decimal("7.00"))
        self.assertEqual(cumplimiento["registros"][0]["cantidad_recibida"], Decimal("4"))

    def test_sin_contabilidad_no_viajan_importes(self):
        """#859: ``pct`` se queda; los importes no salen en la respuesta."""
        resp = self._get()
        fila = self._fila(resp, self.malo.pk)
        precio = self.client_api_get_detalle(self.usuario, self.malo.pk, "precio").data

        self.assertFalse(resp.data["puede_ver_importes"])
        self.assertAlmostEqual(fila["diferencia_precio"]["pct"], 40.0, places=1)
        self.assertEqual(set(fila["diferencia_precio"]["por_moneda"][0]), {"moneda", "pct"})
        self.assertFalse(precio["puede_ver_importes"])
        self.assertNotIn("precio_facturado", precio["registros"][0])
        self.assertNotIn("precio_pactado", precio["registros"][0])

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

    # --- EC-817: clientes nuevos del mes --------------------------------------

    # 15 de noviembre de 2026, 06:00 en el centro de México (UTC-6 todo el año).
    AHORA = datetime(2026, 11, 15, 12, 0, tzinfo=dt_timezone.utc)

    def _alta(self, cliente, *args):
        """Fija ``created_at`` en UTC (``auto_now_add`` no deja ponerlo al crear)."""
        Cliente.objects.filter(pk=cliente.pk).update(created_at=datetime(*args, tzinfo=dt_timezone.utc))

    def _nuevos(self, user, query=""):
        with patch("django.utils.timezone.now", return_value=self.AHORA):
            resp = self._get(user, query)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.json()["clientes_nuevos"]

    def setUp(self):
        # A: 05:59Z del 1 = 23:59 del 31 de octubre local; B: 06:00Z = 00:00 del 1 de noviembre.
        self._alta(self.cliente_a, 2026, 11, 1, 5, 59)
        self._alta(self.cliente_b, 2026, 11, 1, 6, 0)

    def test_clientes_nuevos_usa_el_mes_en_hora_local(self):
        ultimo_minuto = Cliente.objects.create(empresa=self.empresa, nombre="Fin de mes")
        self._alta(ultimo_minuto, 2026, 12, 1, 5, 59)  # 23:59 del 30 de noviembre local
        siguiente_mes = Cliente.objects.create(empresa=self.empresa, nombre="Diciembre")
        self._alta(siguiente_mes, 2026, 12, 1, 6, 0)

        nuevos = self._nuevos(self.admin_empresa)

        self.assertEqual(nuevos, {
            "disponible": True,
            "criterio": "Clientes activos dados de alta en el mes en curso (hora del centro de México).",
            "total": 2,  # B y el del último minuto; A cae en octubre
            "mes": "2026-11",
            "desde": "2026-11-01",
            "hasta": "2026-11-30",  # inclusivo, como ``fecha_hasta``
        })

    def test_clientes_nuevos_excluye_bajas_y_altas_sin_fecha(self):
        baja = Cliente.objects.create(empresa=self.empresa, nombre="Baja")
        self._alta(baja, 2026, 11, 10, 12, 0)
        baja.soft_delete()
        sin_fecha = Cliente.objects.create(empresa=self.empresa, nombre="Sin fecha")
        Cliente.objects.filter(pk=sin_fecha.pk).update(created_at=None)

        self.assertEqual(self._nuevos(self.admin_empresa)["total"], 1)

    def test_clientes_nuevos_de_otra_empresa_no_cuentan(self):
        otra = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        ajeno = Cliente.objects.create(empresa=otra, nombre="Ajeno")
        self._alta(ajeno, 2026, 11, 10, 12, 0)
        admin_otra = Usuario.objects.create(
            username="admin_g", email="admin@globex.test", empresa=otra, is_admin_empresa=True,
        )
        admin_otra.empresas.add(self.empresa)  # el M2M no amplía el alcance (EC-816)

        self.assertEqual(self._nuevos(self.admin_empresa)["total"], 1)
        self.assertEqual(self._nuevos(admin_otra)["total"], 1)  # solo el suyo

    def test_clientes_nuevos_respeta_el_alcance_del_vendedor(self):
        self.assertEqual(self._nuevos(self.vendedor_a)["total"], 0)  # A es de octubre
        self.assertEqual(self._nuevos(self.vendedor_b)["total"], 1)

    def test_clientes_nuevos_superusuario_acotado_a_su_empresa(self):
        otra = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        self._alta(Cliente.objects.create(empresa=otra, nombre="Ajeno"), 2026, 11, 10, 12, 0)
        root = Usuario.objects.create(
            username="root", email="root@acme.test", is_superuser=True, is_staff=True, empresa=self.empresa,
        )
        root_sin_empresa = Usuario.objects.create(
            username="root2", email="root2@acme.test", is_superuser=True, is_staff=True,
        )

        self.assertEqual(self._nuevos(root)["total"], 1)
        self.assertEqual(
            self._nuevos(root_sin_empresa), {"disponible": False, "motivo": "Usuario sin empresa asignada."},
        )

    def test_clientes_nuevos_sin_empresa_no_disponible(self):
        self.assertEqual(
            self._nuevos(self.sin_empresa), {"disponible": False, "motivo": "Usuario sin empresa asignada."},
        )

    def test_clientes_nuevos_no_acepta_parametros(self):
        resp = self._get(self.admin_empresa, "?mes=2026-10")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("mes", str(resp.data["parametros"]))

    def test_clientes_nuevos_agrega_una_sola_consulta(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from terceros.api.views import ClienteViewSet

        def _consultas():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self._get(self.vendedor_a).status_code, 200)
            return len(ctx)

        self._get(self.vendedor_a)  # calienta lo que se cachea por proceso
        con_bloque = _consultas()
        with patch.object(ClienteViewSet, "_kpi_clientes_nuevos", return_value={}):
            sin_bloque = _consultas()

        self.assertEqual(con_bloque - sin_bloque, 1)

    def test_created_at_no_sale_en_listado_detalle_ni_mesa_de_control(self):
        """EC-817 solo lo usa en ``kpis/``; ``ClienteConUltimaCompraSerializer`` (Mesa de Control) hereda el Meta."""
        client = APIClient()
        client.force_authenticate(user=self.admin_empresa)

        def _filas(url):
            resp = client.get(url)
            self.assertEqual(resp.status_code, 200, resp.data)
            data = resp.json()
            return data["results"] if isinstance(data, dict) and "results" in data else data

        listado = _filas("/api/v1/terceros/clientes/")
        mesa = _filas("/api/v1/terceros/clientes-mesa-control/")
        detalle = _filas(f"/api/v1/terceros/clientes/{self.cliente_a.pk}/")

        self.assertTrue(listado and mesa)
        for fila in [*listado, *mesa, detalle]:
            self.assertIn("nombre", fila)
            self.assertNotIn("created_at", fila)


class ClienteCreatedAtBackfillTests(TestCase):
    """Migración 0024 (EC-817): ``created_at`` sale del registro ``+`` del historial."""

    def _rellenar(self):
        from importlib import import_module

        from django.apps import apps

        migracion = import_module("terceros.migrations.0024_cliente_created_at_backfill")
        migracion.rellenar_created_at(apps, None)

    def test_toma_la_fecha_del_alta_en_el_historial_y_sin_ella_queda_null(self):
        empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        con_alta = Cliente.objects.create(empresa=empresa, nombre="Con alta")
        sin_alta = Cliente.objects.create(empresa=empresa, nombre="Sin alta")
        ya_tiene = Cliente.objects.create(empresa=empresa, nombre="Ya tiene")
        fecha_alta = datetime(2026, 9, 3, 17, 30, tzinfo=dt_timezone.utc)
        fecha_propia = datetime(2026, 10, 1, 12, 0, tzinfo=dt_timezone.utc)
        Cliente.history.filter(id=con_alta.pk, history_type="+").update(history_date=fecha_alta)
        con_alta.nombre = "Con alta editado"
        con_alta.save()  # un ``~`` posterior no cambia la fecha de alta
        Cliente.history.filter(id=sin_alta.pk).delete()
        Cliente.objects.filter(pk__in=[con_alta.pk, sin_alta.pk]).update(created_at=None)
        Cliente.objects.filter(pk=ya_tiene.pk).update(created_at=fecha_propia)

        self._rellenar()

        fechas = dict(Cliente.objects.values_list("pk", "created_at"))
        self.assertEqual(fechas[con_alta.pk], fecha_alta)
        self.assertIsNone(fechas[sin_alta.pk])
        self.assertEqual(fechas[ya_tiene.pk], fecha_propia)

    def test_agregar_la_columna_no_rellena_los_clientes_existentes(self):
        """Con ``auto_now_add`` el ``AddField`` les pondría a todos la hora de la
        migración (y el backfill, que solo toca los NULL, ya no los corregiría)."""
        from importlib import import_module

        from django.db.backends.base.schema import BaseDatabaseSchemaEditor
        from django.db.migrations.operations import AddField

        migracion = import_module("terceros.migrations.0023_cliente_created_at").Migration
        altas = [op for op in migracion.operations if isinstance(op, AddField) and op.name == "created_at"]

        self.assertEqual(len(altas), 2)  # cliente y su historial
        for op in altas:
            self.assertIsNone(BaseDatabaseSchemaEditor._effective_default(op.field), op.model_name)
            self.assertFalse(op.field.has_db_default(), op.model_name)  # el default va después del backfill

    def test_insert_que_omite_la_columna_recibe_la_hora_del_alta(self):
        """El código anterior a ``created_at`` (entre ``migrate`` y el deploy) inserta
        sin la columna: el ``db_default`` le pone la hora en vez de dejarlo NULL."""
        from django.db import connection

        empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        base = Cliente.objects.create(empresa=empresa, nombre="Base")
        columnas = [
            f.column for f in Cliente._meta.concrete_fields if f.column not in ("id", "created_at")
        ]
        lista = ", ".join(connection.ops.quote_name(c) for c in columnas)
        tabla = connection.ops.quote_name(Cliente._meta.db_table)
        antes = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {tabla} ({lista}) SELECT {lista} FROM {tabla} WHERE id = %s RETURNING id",
                [base.pk],
            )
            nuevo_id = cursor.fetchone()[0]

        created_at = Cliente.objects.values_list("created_at", flat=True).get(pk=nuevo_id)
        self.assertIsNotNone(created_at)
        self.assertGreaterEqual(created_at, antes - timedelta(seconds=5))


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


@override_settings(FACTURAMA_EMPRESA_CODIGO="fact")
class ClienteFacturamaTests(TestCase):
    """EC-510/511/512: el cliente vive en Facturama o no se guarda."""

    def setUp(self):
        from nucleo.models import Empresa
        from rest_framework.test import APIClient
        from usuarios.models import Usuario

        empresa = Empresa.objects.create(codigo="fact", razon_social="Fact SA")
        self.user = Usuario.objects.create(username="f@f.test", email="f@f.test", empresa=empresa, is_admin_empresa=True)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        patcher = patch("terceros.facturama.FacturamaService")
        self.fc = patcher.start().return_value.client
        self.addCleanup(patcher.stop)
        from nucleo.models import SatRegimenFiscal, SatUsoCfdi

        # Lo que Facturama exige para dar de alta al cliente (#659).
        self.completo = {
            "nombre": "Cli", "razon_social": "CLI SA DE CV", "rfc": "URE180429TM6", "correo": "pagos@cli.test",
            "codigo_postal": "64000",
            "sat_regimen_fiscal": SatRegimenFiscal.objects.create(codigo="601", descripcion="General").pk,
            "sat_uso_cfdi": SatUsoCfdi.objects.create(codigo="G03", descripcion="Gastos").pk,
        }

    def _alta(self, **datos):
        return self.api.post("/api/v1/terceros/clientes/", {**self.completo, **datos}, format="json")

    def test_sin_correo_se_guarda_sin_verificar_y_sin_llamar_a_facturama(self):  # #659
        resp = self._alta(correo="")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual((resp.data["facturama_verificado"], resp.data["facturama_faltantes"]), (False, ["correo"]))
        self.fc.post.assert_not_called()
        self.fc.post.return_value = {"Id": "abc"}
        resp = self.api.patch(f"/api/v1/terceros/clientes/{resp.data['id']}/", {"correo": "pagos@cli.test"}, format="json")
        self.assertEqual((resp.data["facturama_verificado"], resp.data["facturama_faltantes"]), (True, []))

    def test_alta_guarda_el_id_y_la_edicion_hace_put(self):
        self.fc.post.return_value = {"Id": "abc"}

        resp = self._alta()

        self.assertEqual(resp.status_code, 201, resp.data)
        cliente = Cliente.objects.get(pk=resp.data["id"])
        self.assertEqual(cliente.facturama_id, "abc")
        self.api.patch(f"/api/v1/terceros/clientes/{cliente.pk}/", {"nombre": "Otro"}, format="json")
        self.assertEqual(self.fc.put.call_args.args[0], "/Client/abc")

    def test_si_facturama_rechaza_no_se_guarda(self):
        from finanzas.services.facturama.exceptions import FacturamaAPIException

        self.fc.post.side_effect = FacturamaAPIException("error", status_code=400, details={"Message": "RFC inválido"})

        resp = self._alta()

        self.assertEqual(resp.status_code, 400)
        self.assertIn("RFC inválido", str(resp.data["facturama"]))
        self.assertFalse(Cliente.objects.exists())

    def test_sin_razon_social_no_esta_verificado_aunque_tenga_nombre(self):  # #672
        resp = self._alta(razon_social="")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual((resp.data["facturama_verificado"], resp.data["facturama_faltantes"]), (False, ["razon_social"]))
        self.fc.post.assert_not_called()

    def test_el_asistente_de_ia_tambien_lo_sube_a_facturama(self):  # #671
        from types import SimpleNamespace

        from ia.api.views import AIAssistantAPIView

        self.fc.post.return_value = {"Id": "ia1"}
        request = SimpleNamespace(user=self.user)

        resultado = AIAssistantAPIView()._tool_create_cliente(request, self.completo)

        self.assertTrue(resultado["ok"], resultado)
        self.assertTrue(resultado["facturama_sincronizado"])
        self.assertEqual(Cliente.objects.get(pk=resultado["cliente"]["id"]).facturama_id, "ia1")

        Cliente.objects.all().delete()
        self.fc.post.return_value = {}
        resultado = AIAssistantAPIView()._tool_create_cliente(request, self.completo)
        self.assertEqual((resultado["ok"], list(resultado["details"])), (False, ["facturama"]))
        self.assertFalse(Cliente.objects.exists())

    def test_si_facturama_no_regresa_id_no_se_guarda(self):  # #675
        for respuesta in ({}, "", {"Id": ""}, "ok"):
            with self.subTest(respuesta=respuesta):
                self.fc.post.return_value = respuesta

                resp = self._alta()

                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertIn("facturama", resp.data)
                self.assertFalse(Cliente.objects.exists())


CLIENTES_URL = "/api/v1/terceros/clientes/"
VERIFICAR_RFC_URL = "/api/v1/terceros/clientes/verificar-rfc/"
DIRECCIONES_URL = "/api/v1/terceros/direcciones-clientes/"
CP_INVALIDOS = ("ABCDE123", "1234", "123456", "12 34", "１２３４５")
TELEFONOS_VALIDOS = ("8112345678", "+52 (81) 1234-5678")
TELEFONOS_INVALIDOS = ("ABC TELEFONO", "81-1234-567x", "81.1234.5678", "---", "()", "+")
# Solo caracteres permitidos, pero más largo que la columna (20): 400, no un 500 de la BD.
TELEFONO_LARGO = "+52 (81) 1234-5678 990"


class ClienteCodigoPostalTelefonoTests(TestCase):
    """EC-570 CL-01/CL-02 en ``ClienteSerializer``: CP de 5 dígitos (salvo
    XEXX010101000) y teléfonos sin letras, sin romper PATCH parciales."""

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="cp", razon_social="CP SA")
        cls.user = Usuario.objects.create(username="cp@cp.test", email="cp@cp.test", empresa=cls.empresa, is_admin_empresa=True)

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def _alta(self, **datos):
        return self.api.post(CLIENTES_URL, {"nombre": "Cli", **datos}, format="json")

    def _patch(self, cliente, **datos):
        return self.api.patch(f"{CLIENTES_URL}{cliente.pk}/", datos, format="json")

    def test_cp_de_5_digitos_se_guarda_como_texto_con_su_cero(self):
        resp = self._alta(codigo_postal="01234")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Cliente.objects.get(pk=resp.data["id"]).codigo_postal, "01234")

    def test_cp_invalido_se_rechaza_en_el_campo(self):
        for cp in CP_INVALIDOS:
            with self.subTest(cp=cp):
                resp = self._alta(codigo_postal=cp)
                self.assertEqual(resp.status_code, 400)
                self.assertIn("codigo_postal", resp.data)
        self.assertFalse(Cliente.objects.exists())

    def test_cliente_extranjero_no_exige_cp_de_5_digitos(self):
        resp = self._alta(rfc="xexx010101000", codigo_postal="ABCDE123")
        self.assertEqual(resp.status_code, 201, resp.data)
        # Edición sin mandar el RFC: se toma el del cliente.
        cliente = Cliente.objects.get(pk=resp.data["id"])
        self.assertEqual(self._patch(cliente, codigo_postal="SW1A 1AA").status_code, 200)
        # Si deja de ser extranjero, el CP que manda vuelve a revisarse.
        resp = self._patch(cliente, rfc="URE180429TM6", codigo_postal="SW1A 1AA")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("codigo_postal", resp.data)

    def test_el_generico_nacional_no_es_extranjero(self):
        resp = self._alta(rfc="XAXX010101000", codigo_postal="ABCDE")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("codigo_postal", resp.data)

    def test_telefonos(self):
        for telefono in TELEFONOS_VALIDOS:
            with self.subTest(telefono=telefono):
                resp = self._alta(telefono=telefono, telefono_2=telefono)
                self.assertEqual(resp.status_code, 201, resp.data)
        for telefono in TELEFONOS_INVALIDOS:
            with self.subTest(telefono=telefono):
                resp = self._alta(telefono=telefono, telefono_2=telefono)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(set(resp.data), {"telefono", "telefono_2"})

    def test_vacios_siguen_permitidos(self):
        resp = self._alta(telefono="", telefono_2=None, codigo_postal="")
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_telefono_mas_largo_que_el_modelo_es_400(self):
        self.assertGreater(len(TELEFONO_LARGO), 20)
        resp = self._alta(telefono=TELEFONO_LARGO, telefono_2=TELEFONO_LARGO)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(set(resp.data), {"telefono", "telefono_2"})

    def test_patch_sin_esos_campos_pasa_con_valores_heredados_invalidos(self):
        cliente = Cliente.objects.create(
            empresa=self.empresa, nombre="Legado", codigo_postal="ABCDE123", telefono="ABC TELEFONO", telefono_2="xx"
        )
        self.assertEqual(self._patch(cliente, nombre="Legado 2").status_code, 200)
        # Si el payload sí los trae (p. ej. el PUT completo del front), error normal de campo.
        resp = self.api.put(
            f"{CLIENTES_URL}{cliente.pk}/",
            {"nombre": "Legado 3", "codigo_postal": "ABCDE123", "telefono": "ABC TELEFONO"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("telefono", resp.data)
        resp = self._patch(cliente, codigo_postal="ABCDE123")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("codigo_postal", resp.data)


class ClienteRfcGenericoDuplicadoTests(TestCase):
    """EC-570 CL-03: los RFC genéricos no cuentan como duplicado; los demás sí."""

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="dup", razon_social="Dup SA")
        cls.user = Usuario.objects.create(username="d@d.test", email="d@d.test", empresa=cls.empresa, is_admin_empresa=True)

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def _alta(self, rfc, codigo_postal="64000"):
        return self.api.post(CLIENTES_URL, {"nombre": "Cli", "rfc": rfc, "codigo_postal": codigo_postal}, format="json")

    def test_genericos_se_repiten(self):
        for rfc in ("XAXX010101000", " xaxx010101000 ", "XEXX010101000", "xexx010101000"):
            with self.subTest(rfc=rfc):
                self.assertEqual(self._alta(rfc).status_code, 201)
        self.assertEqual(Cliente.objects.filter(rfc="XAXX010101000").count(), 2)
        self.assertEqual(Cliente.objects.filter(rfc="XEXX010101000").count(), 2)

    def test_un_rfc_normal_sigue_sin_poder_repetirse(self):
        self.assertEqual(self._alta("URE180429TM6").status_code, 201)
        resp = self._alta("ure180429tm6")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("RFC duplicado", str(resp.data["rfc"]))


class VerificarRfcTests(TestCase):
    """EC-570 CL-04: ``verificar-rfc`` da el mismo resultado y mensaje que guardar
    ese RFC, en una empresa con Facturama (formato + dígito) y en una sin él."""

    RFCS = ("QAZ010101AB1", "QAZ010101AB8", "qaz010101ab8", "XAXX010101000", "XAXX010101001", "ABC123", "QAZ010101AB8XX")

    @classmethod
    def setUpTestData(cls):
        cls.con_facturama = Empresa.objects.create(codigo="fact", razon_social="Fact SA")
        cls.sin_facturama = Empresa.objects.create(codigo="otra", razon_social="Otra SA")
        cls.user_fact = Usuario.objects.create(username="a@f.test", email="a@f.test", empresa=cls.con_facturama, is_admin_empresa=True)
        cls.user_otra = Usuario.objects.create(username="a@o.test", email="a@o.test", empresa=cls.sin_facturama, is_admin_empresa=True)

    def setUp(self):
        patcher = patch("terceros.facturama.FacturamaService")
        patcher.start().return_value.client.post.return_value = {"Id": "x"}
        self.addCleanup(patcher.stop)
        settings_patcher = override_settings(FACTURAMA_EMPRESA_CODIGO="fact")
        settings_patcher.enable()
        self.addCleanup(settings_patcher.disable)

    def _api(self, user):
        api = APIClient()
        api.force_authenticate(user)
        return api

    def _comparar(self, user):
        api = self._api(user)
        resultados = {}
        for rfc in self.RFCS:
            with self.subTest(rfc=rfc, empresa=user.empresa.codigo):
                verificado = api.get(VERIFICAR_RFC_URL, {"rfc": rfc})
                self.assertEqual(verificado.status_code, 200, verificado.data)
                Cliente.objects.all().delete()  # el guardado no debe chocar por duplicado
                guardado = api.post(CLIENTES_URL, {"nombre": "Cli", "rfc": rfc}, format="json")
                self.assertIn(guardado.status_code, (201, 400))
                self.assertEqual(verificado.data["valido"], guardado.status_code == 201, guardado.data)
                if guardado.status_code == 400:
                    self.assertEqual(verificado.data["error"], str(guardado.data["rfc"][0]))
                else:
                    self.assertIsNone(verificado.data["error"])
                resultados[rfc] = verificado.data
        return resultados

    def test_empresa_con_facturama_valida_formato_y_digito(self):
        r = self._comparar(self.user_fact)
        self.assertFalse(r["QAZ010101AB1"]["valido"])
        self.assertIn("dígito verificador", r["QAZ010101AB1"]["error"])
        self.assertTrue(r["QAZ010101AB8"]["valido"])
        self.assertEqual(r["qaz010101ab8"]["rfc"], "QAZ010101AB8")
        self.assertTrue(r["XAXX010101000"]["valido"])
        self.assertFalse(r["ABC123"]["valido"])
        self.assertTrue(r["QAZ010101AB8"]["formato_verificado"])
        self.assertEqual(set(r["QAZ010101AB8"]), {"rfc", "valido", "error", "formato_verificado"})

    def test_empresa_sin_facturama_no_valida_el_digito(self):
        r = self._comparar(self.user_otra)
        self.assertTrue(r["QAZ010101AB1"]["valido"])
        self.assertFalse(r["QAZ010101AB1"]["formato_verificado"])
        # La longitud máxima del campo aplica a todas las empresas.
        self.assertFalse(r["QAZ010101AB8XX"]["valido"])

    def test_parametros(self):
        api = self._api(self.user_fact)
        self.assertEqual(api.get(VERIFICAR_RFC_URL).status_code, 400)
        self.assertEqual(api.get(VERIFICAR_RFC_URL, {"rfc": "  "}).status_code, 400)
        self.assertEqual(api.get(VERIFICAR_RFC_URL, {"rfc": "QAZ010101AB8", "otro": "1"}).status_code, 400)
        self.assertIn(APIClient().get(VERIFICAR_RFC_URL, {"rfc": "QAZ010101AB8"}).status_code, (401, 403))
        self.assertEqual(api.get(VERIFICAR_RFC_URL, {"rfc": "QAZ010101AB8", "cliente": "x"}).status_code, 400)

    def test_con_cliente_aplica_la_regla_de_su_empresa_como_la_edicion(self):
        """Un superusuario sin empresa: sin ``cliente`` no aplica el dígito; con el
        cliente de la empresa con Facturama, el mismo resultado que editarlo."""
        superusuario = Usuario.objects.create(username="su@x.test", email="su@x.test", is_superuser=True)
        cliente = Cliente.objects.create(empresa=self.con_facturama, nombre="Cli", rfc="QAZ010101AB8")
        api = self._api(superusuario)
        self.assertTrue(api.get(VERIFICAR_RFC_URL, {"rfc": "QAZ010101AB1"}).data["valido"])
        for rfc in ("QAZ010101AB1", "URE180429TM6"):
            with self.subTest(rfc=rfc):
                verificado = api.get(VERIFICAR_RFC_URL, {"rfc": rfc, "cliente": cliente.pk})
                self.assertEqual(verificado.status_code, 200)
                self.assertTrue(verificado.data["formato_verificado"])
                guardado = api.patch(f"{CLIENTES_URL}{cliente.pk}/", {"rfc": rfc}, format="json")
                self.assertEqual(verificado.data["valido"], guardado.status_code == 200, guardado.data)
                if guardado.status_code == 400:
                    self.assertEqual(verificado.data["error"], str(guardado.data["rfc"][0]))

    def test_cliente_de_otra_empresa_es_404(self):
        ajeno = Cliente.objects.create(empresa=self.con_facturama, nombre="Ajeno")
        propio = Cliente.objects.create(empresa=self.sin_facturama, nombre="Propio")
        api = self._api(self.user_otra)
        self.assertEqual(api.get(VERIFICAR_RFC_URL, {"rfc": "QAZ010101AB1", "cliente": ajeno.pk}).status_code, 404)
        self.assertEqual(api.get(VERIFICAR_RFC_URL, {"rfc": "QAZ010101AB1", "cliente": 999999}).status_code, 404)
        resp = api.get(VERIFICAR_RFC_URL, {"rfc": "QAZ010101AB1", "cliente": propio.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((resp.data["valido"], resp.data["formato_verificado"]), (True, False))


class DireccionClienteTests(TestCase):
    """EC-570: alcance multi-tenant de ``direcciones-clientes`` (lectura y cliente a
    nivel empresa, sin subalcance por vendedor; empresa la fija el servidor) y
    CL-01/CL-02."""

    @classmethod
    def setUpTestData(cls):
        from terceros.models import DireccionCliente

        cls.empresa = Empresa.objects.create(codigo="a", razon_social="A SA")
        cls.otra = Empresa.objects.create(codigo="b", razon_social="B SA")
        cls.admin = Usuario.objects.create(username="adm@a.test", email="adm@a.test", empresa=cls.empresa, is_admin_empresa=True)
        cls.admin_otra = Usuario.objects.create(username="adm@b.test", email="adm@b.test", empresa=cls.otra, is_admin_empresa=True)
        cls.vendedor = Usuario.objects.create(username="v@a.test", email="v@a.test", empresa=cls.empresa)
        cls.superusuario = Usuario.objects.create(username="su@x.test", email="su@x.test", is_superuser=True)
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Propio", rfc="URE180429TM6")
        cls.cliente.vendedores.add(cls.vendedor)
        cls.cliente_sin_asignar = Cliente.objects.create(empresa=cls.empresa, nombre="Sin asignar")
        cls.extranjero = Cliente.objects.create(empresa=cls.empresa, nombre="Extranjero", rfc="XEXX010101000")
        cls.ajeno = Cliente.objects.create(empresa=cls.otra, nombre="Ajeno")
        datos = cls._datos_modelo()
        cls.direccion = DireccionCliente.objects.create(cliente=cls.cliente, empresa=cls.empresa, **datos)
        cls.direccion_sin_asignar = DireccionCliente.objects.create(cliente=cls.cliente_sin_asignar, empresa=cls.empresa, **datos)
        cls.direccion_ajena = DireccionCliente.objects.create(cliente=cls.ajeno, empresa=cls.otra, **datos)

    @staticmethod
    def _datos_modelo(**extra):
        return {
            "destinatario": "Recibe", "empresa_envio": "Paquetería", "telefono_envio": "8100000000",
            "celular_envio": "8100000001", "direccion_envio": "Calle 1", "colonia_envio": "Centro",
            "codigo_postal": "64000", "ciudad_envio": "Monterrey", "estado_envio": "NL", **extra,
        }

    def _api(self, user):
        api = APIClient()
        api.force_authenticate(user)
        return api

    def _alta(self, user, cliente, **extra):
        return self._api(user).post(DIRECCIONES_URL, {"cliente": cliente.pk, **self._datos_modelo(**extra)}, format="json")

    def _ids(self, user):
        resp = self._api(user).get(DIRECCIONES_URL)
        self.assertEqual(resp.status_code, 200)
        filas = resp.data["results"] if isinstance(resp.data, dict) else resp.data
        return {fila["id"] for fila in filas}

    def test_alta_con_cliente_de_otra_empresa_se_rechaza(self):
        resp = self._alta(self.admin, self.ajeno)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("cliente", resp.data)
        # Mismo error que un pk inexistente: no delata que el cliente existe.
        self.assertEqual(resp.data["cliente"][0].code, "does_not_exist")

    def test_empresa_la_fija_el_servidor_con_la_del_cliente(self):
        resp = self._alta(self.admin, self.cliente, empresa=self.otra.pk)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["empresa"], self.empresa.pk)
        resp = self._alta(self.superusuario, self.ajeno)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["empresa"], self.otra.pk)

    def test_edicion_no_mueve_la_direccion_a_un_cliente_ajeno(self):
        url = f"{DIRECCIONES_URL}{self.direccion.pk}/"
        resp = self._api(self.admin).patch(url, {"cliente": self.ajeno.pk}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("cliente", resp.data)
        resp = self._api(self.admin).patch(url, {"empresa": self.otra.pk, "destinatario": "Otro"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.direccion.refresh_from_db()
        self.assertEqual((self.direccion.empresa_id, self.direccion.cliente_id), (self.empresa.pk, self.cliente.pk))

    def test_otra_empresa_no_lee_ni_edita(self):
        url = f"{DIRECCIONES_URL}{self.direccion.pk}/"
        self.assertNotIn(self.direccion.pk, self._ids(self.admin_otra))
        self.assertEqual(self._api(self.admin_otra).get(url).status_code, 404)
        self.assertEqual(self._api(self.admin_otra).patch(url, {"destinatario": "X"}, format="json").status_code, 404)
        self.assertEqual(self._api(self.admin_otra).delete(url).status_code, 404)

    def test_vendedor_ve_y_edita_direcciones_de_clientes_no_asignados_de_su_empresa(self):
        # Quien edita una cotización necesita las direcciones de su cliente aunque
        # no lo tenga asignado: el alcance es la empresa, no el vendedor.
        propias = {self.direccion.pk, self.direccion_sin_asignar.pk}
        self.assertEqual(self._ids(self.vendedor), propias)
        self.assertEqual(self._ids(self.admin), propias)
        self.assertEqual(self._alta(self.vendedor, self.cliente_sin_asignar).status_code, 201)
        url = f"{DIRECCIONES_URL}{self.direccion_sin_asignar.pk}/"
        resp = self._api(self.vendedor).patch(url, {"cliente": self.cliente_sin_asignar.pk, "destinatario": "Nuevo"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._alta(self.vendedor, self.ajeno).status_code, 400)

    def test_sin_empresa_no_ve_ni_crea(self):
        sin_empresa = Usuario.objects.create(username="x@x.test", email="x@x.test")
        self.assertEqual(self._ids(sin_empresa), set())
        resp = self._alta(sin_empresa, self.cliente)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["cliente"][0].code, "does_not_exist")

    def test_cp_y_telefonos(self):
        for cp in CP_INVALIDOS:
            with self.subTest(cp=cp):
                resp = self._alta(self.admin, self.cliente, codigo_postal=cp)
                self.assertEqual(resp.status_code, 400)
                self.assertIn("codigo_postal", resp.data)
        self.assertEqual(self._alta(self.admin, self.cliente, codigo_postal="01234").data["codigo_postal"], "01234")
        self.assertEqual(self._alta(self.admin, self.extranjero, codigo_postal="ABCDE123").status_code, 201)
        resp = self._alta(self.admin, self.cliente, telefono_envio="ABC", celular_envio="81 (12) 34-56+7")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(set(resp.data), {"telefono_envio"})
        resp = self._alta(self.admin, self.cliente, telefono_envio=TELEFONO_LARGO, celular_envio="---")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(set(resp.data), {"telefono_envio", "celular_envio"})

    def test_cliente_dado_de_baja_sigue_editable(self):
        from terceros.models import DireccionCliente

        inactivo = Cliente.objects.create(empresa=self.empresa, nombre="Baja")
        direccion = DireccionCliente.objects.create(cliente=inactivo, empresa=self.empresa, **self._datos_modelo())
        inactivo.soft_delete()
        url = f"{DIRECCIONES_URL}{direccion.pk}/"
        self.assertIn(direccion.pk, self._ids(self.admin))
        # El formulario del front manda ``cliente`` en cada guardado.
        resp = self._api(self.admin).patch(url, {"cliente": inactivo.pk, "destinatario": "Nuevo"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_cliente_heredado_sin_empresa_conserva_la_de_la_direccion(self):
        from terceros.models import DireccionCliente

        sin_empresa = Cliente.objects.create(nombre="Heredado")
        direccion = DireccionCliente.objects.create(cliente=sin_empresa, empresa=self.otra, **self._datos_modelo())
        url = f"{DIRECCIONES_URL}{direccion.pk}/"
        for datos in ({"destinatario": "Nuevo"}, {"cliente": sin_empresa.pk, "destinatario": "Otro"}):
            with self.subTest(datos=datos):
                resp = self._api(self.superusuario).patch(url, datos, format="json")
                self.assertEqual(resp.status_code, 200, resp.data)
                direccion.refresh_from_db()
                self.assertEqual(direccion.empresa_id, self.otra.pk)
        # Para crear una nueva o mover otra a ese cliente sí hace falta empresa.
        resp = self._alta(self.superusuario, sin_empresa)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("cliente", resp.data)

    def test_patch_parcial_con_valores_heredados_invalidos(self):
        from terceros.models import DireccionCliente

        legado = DireccionCliente.objects.create(
            cliente=self.cliente, empresa=self.empresa, **self._datos_modelo(codigo_postal="1234", celular_envio="sadfsd")
        )
        url = f"{DIRECCIONES_URL}{legado.pk}/"
        self.assertEqual(self._api(self.admin).patch(url, {"destinatario": "Nuevo"}, format="json").status_code, 200)
        resp = self._api(self.admin).patch(url, {"codigo_postal": "1234"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("codigo_postal", resp.data)
        # Al cambiar a un cliente extranjero, el CP que se manda ya no se exige.
        resp = self._api(self.admin).patch(url, {"cliente": self.extranjero.pk, "codigo_postal": "1234"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)

    # EC-775: ``?cliente=`` acota el listado después del alcance por empresa.

    def _listado(self, user, **params):
        resp = self._api(user).get(DIRECCIONES_URL, params)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIsInstance(resp.data, list)  # Arreglo plano, sin paginación.
        return [fila["id"] for fila in resp.data]

    def test_filtro_por_cliente_solo_trae_sus_direcciones_activas(self):
        from terceros.models import DireccionCliente

        segunda = DireccionCliente.objects.create(cliente=self.cliente, empresa=self.empresa, **self._datos_modelo())
        baja = DireccionCliente.objects.create(cliente=self.cliente, empresa=self.empresa, **self._datos_modelo())
        baja.soft_delete()
        # El vendedor también: el alcance es la empresa, no sus clientes asignados.
        for user in (self.admin, self.vendedor):
            with self.subTest(user=user.email):
                self.assertEqual(set(self._listado(user, cliente=self.cliente.pk)), {self.direccion.pk, segunda.pk})
                self.assertEqual(self._listado(user, cliente=self.cliente_sin_asignar.pk), [self.direccion_sin_asignar.pk])

    def test_filtro_con_cliente_de_otra_empresa_regresa_lista_vacia(self):
        # 200 [] como un id inexistente: el alcance por empresa ya lo excluye.
        self.assertEqual(self._listado(self.admin, cliente=self.ajeno.pk), [])
        self.assertEqual(self._listado(self.admin, cliente=999999999), [])
        self.assertEqual(self._listado(self.admin_otra, cliente=self.cliente.pk), [])

    def test_filtro_con_valor_invalido_es_400(self):
        # EC-829: ``1_0`` y ``٣`` los aceptaba ``int()`` (10 y 3).
        for valor in ("abc", "1.5", "1,2", "1_0", "٣", "+5", " 5"):
            with self.subTest(valor=valor):
                resp = self._api(self.admin).get(DIRECCIONES_URL, {"cliente": valor})
                self.assertEqual(resp.status_code, 400)
                self.assertIn("cliente", resp.data)

    def test_filtro_invalido_es_400_tambien_sin_empresa(self):
        sin_empresa = Usuario.objects.create(username="sin@x.test", email="sin@x.test")
        resp = self._api(sin_empresa).get(DIRECCIONES_URL, {"cliente": "abc"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data, {"cliente": "Debe ser un entero válido."})
        # Con un entero válido sigue sin ver nada.
        self.assertEqual(self._listado(sin_empresa, cliente=self.cliente.pk), [])

    def test_sin_filtro_o_vacio_regresa_el_listado_completo_de_la_empresa(self):
        propias = {self.direccion.pk, self.direccion_sin_asignar.pk}
        self.assertEqual(set(self._listado(self.admin)), propias)
        self.assertEqual(set(self._listado(self.admin, cliente="")), propias)

    def test_superusuario_sin_empresa_no_ve_direcciones(self):
        # EC-798: sin empresa activa no ve nada, superusuario incluido.
        self.assertEqual(self._listado(self.superusuario), [])

    def test_superusuario_con_empresa_solo_ve_su_empresa(self):
        # EC-798: como produccion (#361) y ventas, sólo la empresa activa.
        superusuario = Usuario.objects.create(
            username="su-a@x.test", email="su-a@x.test", empresa=self.empresa, is_superuser=True
        )
        todas = set(self._listado(superusuario))
        self.assertIn(self.direccion.pk, todas)
        self.assertNotIn(self.direccion_ajena.pk, todas)
        self.assertEqual(self._listado(superusuario, cliente=self.ajeno.pk), [])

    def test_filtro_no_aplica_al_detalle(self):
        # Como en recepciones (#889), el filtro es del listado.
        url = f"{DIRECCIONES_URL}{self.direccion.pk}/"
        self.assertEqual(self._api(self.admin).get(url, {"cliente": self.ajeno.pk}).status_code, 200)

    def test_orden_por_defecto_predeterminada_primero_y_luego_id(self):
        from terceros.models import DireccionCliente

        segunda = DireccionCliente.objects.create(cliente=self.cliente, empresa=self.empresa, **self._datos_modelo())
        predeterminada = DireccionCliente.objects.create(
            cliente=self.cliente, empresa=self.empresa, **self._datos_modelo(is_default=True)
        )
        self.assertEqual(
            self._listado(self.admin, cliente=self.cliente.pk), [predeterminada.pk, self.direccion.pk, segunda.pk]
        )
        self.assertEqual(
            self._listado(self.admin),
            [predeterminada.pk, *sorted([self.direccion.pk, self.direccion_sin_asignar.pk, segunda.pk])],
        )
        # Un ``?ordering=`` explícito reemplaza el orden por defecto: con ``id``
        # ascendente la predeterminada (el id más alto) ya no va primero.
        self.assertEqual(
            self._listado(self.admin, cliente=self.cliente.pk, ordering="id"),
            [self.direccion.pk, segunda.pk, predeterminada.pk],
        )


class ClientesContabilidadTests(TestCase):
    """CONTABILIDAD ve todos los clientes de su empresa, no solo los asignados."""

    def test_contabilidad_ve_clientes_sin_asignar_de_su_empresa(self):
        from nucleo.models import Empresa
        from terceros.scope import clientes_base, clientes_visibles
        from usuarios.models import Usuario

        empresa, otra = Empresa.objects.create(codigo="e1", razon_social="E1"), Empresa.objects.create(codigo="e2", razon_social="E2")
        propio = Cliente.objects.create(empresa=empresa, nombre="Sin asignar")
        Cliente.objects.create(empresa=otra, nombre="Ajeno")
        user = Usuario.objects.create(username="c@e1.test", email="c@e1.test", empresa=empresa)

        self.assertFalse(clientes_visibles(clientes_base(), user).exists())
        with patch("terceros.scope.claves_departamento_usuario", return_value={"CONTABILIDAD"}):
            self.assertEqual(list(clientes_visibles(clientes_base(), user)), [propio])



CSF_MORAL = """CÉDULA DE IDENTIFICACIÓN FISCAL
Datos de Identificación del Contribuyente:
RFC: AAA010101AA1
Denominación/Razón Social: ACME SA DE CV
Fecha inicio de operaciones: 01 DE ENERO DE 2020
Datos del domicilio registrado
Código Postal: 06600 Tipo de Vialidad: CALLE
Nombre de Vialidad: REFORMA Número Exterior: 100
Número Interior: 2 Nombre de la Colonia: JUAREZ
Nombre de la Localidad: CIUDAD DE MEXICO Nombre del Municipio o Demarcación Territorial: CUAUHTEMOC
Nombre de la Entidad Federativa: CIUDAD DE MEXICO Entre Calle: X Y Calle: Z
Regímenes:
Régimen Fecha Inicio Fecha Fin
General de Ley Personas Morales 01/01/2020
Obligaciones:
"""

CSF_FISICA_LINEAS = """RFC:
XAXX010101000
CURP:
PEPJ800101HDFRRN09
Nombre (s):
JUAN
Primer Apellido:
PEREZ
Segundo Apellido:
LOPEZ
Estatus en el padrón: ACTIVO
Código Postal:
44100
Nombre de Vialidad:
HIDALGO
Nombre del Municipio o Demarcación Territorial:
GUADALAJARA
Nombre de la Entidad Federativa:
JALISCO
"""


class ImportarCsfTests(TestCase):
    def setUp(self):
        self.empresa = Empresa.objects.create(codigo="csf", razon_social="Csf Empresa")
        self.user = Usuario.objects.create(
            username="csf", email="csf@test.com", empresa=self.empresa, is_admin_empresa=True
        )
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)
        from nucleo.models import SatRegimenFiscal
        SatRegimenFiscal.objects.create(codigo="601", descripcion="General de Ley Personas Morales", aplica_moral=True)

    def _post(self, contenido, texto=CSF_MORAL):
        from django.core.files.uploadedfile import SimpleUploadedFile
        archivo = SimpleUploadedFile("csf.pdf", contenido, content_type="application/pdf")
        with patch("terceros.csf.extraer_texto", return_value=texto):
            return self.api.post("/api/v1/terceros/clientes/importar-csf/", {"archivo": archivo}, format="multipart")

    def test_persona_moral_prellena_datos_y_no_crea(self):
        resp = self._post(b"%PDF-1.4 x")
        self.assertEqual(resp.status_code, 200, resp.data)
        d = resp.data["datos"]
        self.assertEqual((d["rfc"], d["razon_social"], d["codigo_postal"]), ("AAA010101AA1", "ACME SA DE CV", "06600"))
        self.assertEqual((d["direccion_fiscal"], d["numero_exterior"], d["numero_interior"]), ("CALLE REFORMA", "100", "2"))
        self.assertEqual((d["colonia"], d["delegacion"], d["estado"]), ("JUAREZ", "CUAUHTEMOC", "CIUDAD DE MEXICO"))
        self.assertEqual(d["sat_regimen_fiscal_codigo"], "601")
        self.assertIsNone(resp.data["cliente_existente_id"])
        self.assertEqual(Cliente.objects.count(), 0)

    def test_persona_fisica_con_valores_en_lineas_aparte(self):
        d = self._post(b"%PDF-1.4 x", CSF_FISICA_LINEAS).data["datos"]
        self.assertEqual((d["rfc"], d["razon_social"], d["curp"]), ("XAXX010101000", "JUAN PEREZ LOPEZ", "PEPJ800101HDFRRN09"))
        self.assertEqual((d["codigo_postal"], d["estado"]), ("44100", "JALISCO"))

    def test_avisa_si_el_rfc_ya_es_cliente(self):
        c = Cliente.objects.create(empresa=self.empresa, nombre="Acme", rfc="AAA010101AA1")
        self.assertEqual(self._post(b"%PDF-1.4 x").data["cliente_existente_id"], c.id)

    def test_rechaza_no_pdf_sin_rfc_y_sin_archivo(self):
        self.assertEqual(self._post(b"hola").status_code, 400)
        self.assertEqual(self._post(b"%PDF-1.4 x", "texto cualquiera").status_code, 400)
        self.assertEqual(self.api.post("/api/v1/terceros/clientes/importar-csf/", {}, format="multipart").status_code, 400)

    def test_lectura_es_rapida(self):
        import time
        from terceros import csf
        inicio = time.perf_counter()
        for _ in range(50):
            csf.interpretar(CSF_MORAL)
        self.assertLess(time.perf_counter() - inicio, 2.0)


@override_settings(FACTURAMA_EMPRESA_CODIGO="duena")
class VerificarRfcTest(TestCase):
    URL = "/api/v1/terceros/verificar-rfc/"

    def setUp(self):
        self.duena = Empresa.objects.create(codigo="duena", razon_social="Dueña")
        self.otra = Empresa.objects.create(codigo="otra", razon_social="Otra")
        self.api = APIClient()

    def _login(self, empresa, **kw):
        user = Usuario.objects.create(
            username=f"u-{empresa.codigo}", email=f"u@{empresa.codigo}.test", empresa=empresa, **kw
        )
        self.api.force_authenticate(user=user)

    def test_empresa_duena_consulta_rfc(self):
        self._login(self.duena, is_admin_empresa=True)
        respuesta = {"Rfc": "XAXX010101000", "FormatoCorrecto": True, "Activo": True, "Localizado": False, "Extra": 1}
        with patch("terceros.api.views.FacturamaClient.get", return_value=respuesta) as get:
            r = self.api.get(self.URL, {"rfc": " xaxx010101000 "})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"Rfc": "XAXX010101000", "FormatoCorrecto": True, "Activo": True, "Localizado": False})
        get.assert_called_once_with("customers/status", params={"rfc": "XAXX010101000"})

    def test_rfc_obligatorio(self):
        self._login(self.duena, is_admin_empresa=True)
        self.assertEqual(self.api.get(self.URL).status_code, 400)

    def test_otra_empresa_403(self):
        self._login(self.otra, is_admin_empresa=True)
        self.assertEqual(self.api.get(self.URL, {"rfc": "XAXX010101000"}).status_code, 403)


class ClienteEmpresaSucursalTests(TestCase):
    """#685: la sucursal es de la empresa del cliente. #686: sin empresa no hay alta."""

    URL = "/api/v1/terceros/clientes/"

    @classmethod
    def setUpTestData(cls):
        from nucleo.models import Sucursal

        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.otra = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        cls.propia = Sucursal.objects.create(empresa=cls.empresa, codigo="MTY", nombre="MTY")
        cls.ajena = Sucursal.objects.create(empresa=cls.otra, codigo="GDL", nombre="GDL")
        cls.usuario = Usuario.objects.create(
            username="v@acme.test", email="v@acme.test", empresa=cls.empresa, is_admin_empresa=True
        )
        cls.root = Usuario.objects.create(username="root@x.test", email="root@x.test", is_superuser=True)

    def _api(self, user):
        api = APIClient()
        api.force_authenticate(user)
        return api

    def test_sucursal_de_otra_empresa_responde_400_al_crear_y_editar(self):
        api = self._api(self.usuario)

        resp = api.post(self.URL, {"nombre": "Cli", "sucursal": self.ajena.pk}, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("sucursal", resp.data)

        resp = api.post(self.URL, {"nombre": "Cli", "sucursal": self.propia.pk}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        url = f"{self.URL}{resp.data['id']}/"
        self.assertEqual(api.patch(url, {"sucursal": self.ajena.pk}, format="json").status_code, 400)
        self.assertEqual(Cliente.objects.get(pk=resp.data["id"]).sucursal_id, self.propia.pk)

    def test_sin_empresa_no_crea_cliente_ni_el_superusuario(self):
        sin_empresa = Usuario.objects.create(username="libre@x.test", email="libre@x.test")

        for user in (sin_empresa, self.root):
            resp = self._api(user).post(self.URL, {"nombre": "Cli"}, format="json")
            self.assertEqual(resp.status_code, 400, resp.data)
            self.assertIn("empresa", resp.data)
        self.assertFalse(Cliente.objects.exists())

    def test_asistente_ia_sin_empresa_no_crea_cliente(self):
        from types import SimpleNamespace

        from ia.api.views import AIAssistantAPIView

        resultado = AIAssistantAPIView()._tool_create_cliente(SimpleNamespace(user=self.root), {"nombre": "Cli"})

        self.assertFalse(resultado["ok"])
        self.assertFalse(Cliente.objects.exists())


class ResumenComercialImportesPorPerfilTests(ImportesPedidoFixture, TestCase):
    """EC-784: ``resumen_comercial`` del detalle de cliente sigue la regla de importes
    del pedido (contabilidad de ventas O de compras). Sin ella se omiten
    ``montos_por_moneda`` y el ``gran_total`` de ``pedidos_recientes``/``ultimo_pedido``;
    conteos, folios y ``cartera`` no cambian."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Sin ser Mesa de Control/Contabilidad, un usuario sólo ve sus clientes asignados.
        cls.cliente.vendedores.add(*(usuario for usuario, _ in cls.perfiles))

    def _resumen(self, usuario, cliente):
        return self._api(usuario).get(f"{CLIENTES_URL}{cliente.pk}/")

    def test_detalle_consulta_las_claves_una_vez(self):
        """El alcance (``clientes_visibles``) y la regla de importes comparten las
        claves de departamento: una sola consulta de roles por petición."""
        for usuario in (self.compras, self.sin_permiso):
            with self.subTest(usuario=usuario.username):
                api = self._api(usuario)
                self.assertEqual(
                    self._consultas_de_roles(lambda: api.get(f"{CLIENTES_URL}{self.cliente.pk}/")), 1
                )

    def test_resumen_por_perfil(self):
        for usuario, ve in self.perfiles:
            with self.subTest(usuario=usuario.username):
                resp = self._resumen(usuario, self.cliente)
                self.assertEqual(resp.status_code, 200, resp.content)
                resumen = resp.data["resumen_comercial"]
                self.assertEqual("montos_por_moneda" in resumen, ve)
                self.assertIs(resumen.get("puede_ver_importes"), ve)
                self.assertEqual(resumen["total_pedidos"], len(self.pedidos))
                self.assertEqual(len(resumen["pedidos_recientes"]), 5)
                for pedido in [*resumen["pedidos_recientes"], resumen["ultimo_pedido"]]:
                    self.assertEqual("gran_total" in pedido, ve)
                    self.assertIn("folio", pedido)
                self.assertIn("cartera", resumen)
                if ve:
                    self.assertEqual(resumen["montos_por_moneda"], [{"moneda": "XEC", "total": Decimal("1500.00")}])

    def test_cliente_de_otra_empresa_es_404(self):
        for usuario, _ in self.perfiles[:-1]:
            with self.subTest(usuario=usuario.username):
                self.assertEqual(self._resumen(usuario, self.cliente_otra).status_code, 404)


class ClienteListadoOrderingTests(TestCase):
    """``?ordering=`` del listado de clientes (EC-826): campos de ``Cliente`` y la
    anotación ``fecha_ultima_compra`` (EC-853; los de proveedores que no existen
    aquí respondían 500), NULL al final en
    ambas direcciones y ``nombre, id`` por defecto. Siempre dentro del alcance."""

    URL = "/api/v1/terceros/clientes/"
    CAMPOS = [
        "id", "clave", "nombre", "razon_social", "rfc", "correo", "telefono", "ciudad", "estado",
        "giro_empresarial", "dias_credito", "created_at", "fecha_ultima_compra",
    ]

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="acme", razon_social="ACME SA")
        cls.otra = Empresa.objects.create(codigo="globex", razon_social="GLOBEX SA")
        cls.admin = Usuario.objects.create(
            username="admin@acme.test", email="admin@acme.test", empresa=cls.empresa, is_admin_empresa=True,
        )
        cls.vendedor = Usuario.objects.create(username="v@acme.test", email="v@acme.test", empresa=cls.empresa)

        base = timezone.now() - timedelta(days=30)
        # (nombre, clave, días desde ``base`` o ``None`` = alta desconocida)
        filas = [
            ("Delta", "C-04", 4), ("alfa", None, 1), ("Charlie", "C-02", None), ("Bravo", "C-03", 3),
            ("Echo", None, None), ("Bravo", "C-01", 2), ("Foxtrot", "C-05", None),
        ]
        cls.clientes = []
        for nombre, clave, dias in filas:
            cliente = Cliente.objects.create(empresa=cls.empresa, nombre=nombre, clave=clave)
            Cliente.objects.filter(pk=cliente.pk).update(
                created_at=None if dias is None else base + timedelta(days=dias)
            )
            cls.clientes.append(cliente)
        cls.clientes[0].vendedores.add(cls.vendedor)
        cls.clientes[2].vendedores.add(cls.vendedor)
        # Otra empresa, con fechas más recientes y sin fecha: nunca deben aparecer.
        for nombre, dias in (("Aaa ajeno", 29), ("Zzz ajeno", None)):
            ajeno = Cliente.objects.create(empresa=cls.otra, nombre=nombre)
            Cliente.objects.filter(pk=ajeno.pk).update(
                created_at=None if dias is None else base + timedelta(days=dias)
            )
        cls.ids_empresa = {c.pk for c in cls.clientes}
        cls.sin_fecha = {c.pk for c, (_, _, dias) in zip(cls.clientes, filas) if dias is None}

    def _get(self, user=None, **params):
        api = APIClient()
        api.force_authenticate(user or self.admin)
        return api.get(self.URL, params)

    def _ids(self, user=None, **params):
        resp = self._get(user, **params)
        self.assertEqual(resp.status_code, 200, resp.content)
        datos = resp.data["results"] if "limit" in params else resp.data
        return [fila["id"] for fila in datos]

    def _orden_por_defecto(self):
        return list(
            Cliente.objects.filter(pk__in=self.ids_empresa).order_by("nombre", "id").values_list("pk", flat=True)
        )

    def test_cada_campo_ordenable_responde_200_en_ambas_direcciones(self):
        for campo in self.CAMPOS:
            for termino in (campo, f"-{campo}"):
                with self.subTest(ordering=termino):
                    ids = self._ids(ordering=termino)
                    self.assertCountEqual(ids, self.ids_empresa)

    def test_campos_retirados_ya_no_responden_500_y_quedan_en_el_orden_por_defecto(self):
        # ``fecha_ultima_compra`` volvió como anotación (EC-853): ver
        # ``ClienteListadoFechaUltimaCompraTests``.
        for campo in ("fecha_alta", "updated_at"):
            for termino in (campo, f"-{campo}"):
                with self.subTest(ordering=termino):
                    self.assertEqual(self._ids(ordering=termino), self._orden_por_defecto())

    def test_sin_ordering_conserva_nombre_e_id(self):
        self.assertEqual(self._ids(), self._orden_por_defecto())

    def test_created_at_deja_los_null_al_final_en_ambas_direcciones(self):
        con_fecha = Cliente.objects.filter(pk__in=self.ids_empresa, created_at__isnull=False)
        sin_fecha = Cliente.objects.filter(pk__in=self.sin_fecha)
        casos = {
            "-created_at": [*con_fecha.order_by("-created_at").values_list("pk", flat=True),
                            *sin_fecha.order_by("-id").values_list("pk", flat=True)],
            "created_at": [*con_fecha.order_by("created_at").values_list("pk", flat=True),
                           *sin_fecha.order_by("id").values_list("pk", flat=True)],
        }
        for termino, esperado in casos.items():
            with self.subTest(ordering=termino):
                self.assertEqual(self._ids(ordering=termino), esperado)

    def test_nulls_last_solo_en_campos_null_ables(self):
        """En columnas NOT NULL (``id``, ``nombre``) el término queda simple para que
        un DESC pueda recorrer el índice al revés; ``created_at``/``clave`` llevan
        ``NULLS LAST``."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        casos = {
            "-created_at": '"clientes"."created_at" DESC NULLS LAST, "clientes"."id" DESC',
            "-clave": '"clientes"."clave" DESC NULLS LAST, "clientes"."id" DESC',
            "created_at": '"clientes"."created_at" ASC NULLS LAST, "clientes"."id" ASC',
            "-id": '"clientes"."id" DESC',
            "-nombre": '"clientes"."nombre" DESC, "clientes"."id" DESC',
        }
        for termino, esperado in casos.items():
            with self.subTest(ordering=termino):
                with CaptureQueriesContext(connection) as consultas:
                    self._ids(ordering=termino, limit=50)
                sql = next(q["sql"] for q in consultas.captured_queries
                           if 'FROM "clientes"' in q["sql"] and "LIMIT" in q["sql"])
                self.assertEqual(sql.split("ORDER BY ")[-1].split(" LIMIT")[0], esperado)

    def test_clave_ordena_las_claves_y_deja_los_null_al_final_en_ambas_direcciones(self):
        """Mismo orden que el ORM con el desempate en la dirección del campo; sin
        claves NULL la segunda parte queda vacía y la prueba sigue valiendo."""
        empresa = Cliente.objects.filter(pk__in=self.ids_empresa)
        con_clave = empresa.filter(clave__isnull=False)
        sin_clave = empresa.filter(clave__isnull=True)
        casos = {
            "clave": [*con_clave.order_by("clave", "id").values_list("pk", flat=True),
                      *sin_clave.order_by("id").values_list("pk", flat=True)],
            "-clave": [*con_clave.order_by("-clave", "-id").values_list("pk", flat=True),
                       *sin_clave.order_by("-id").values_list("pk", flat=True)],
        }
        for termino, esperado in casos.items():
            with self.subTest(ordering=termino):
                self.assertEqual(self._ids(ordering=termino), esperado)

    def test_paginas_concatenadas_de_menos_created_at_igualan_la_lista_completa(self):
        completo = self._ids(ordering="-created_at")
        paginas = []
        for offset in range(0, len(completo), 2):
            paginas += self._ids(ordering="-created_at", limit=2, offset=offset)
        self.assertEqual(paginas, completo)
        self.assertEqual(len(paginas), len(set(paginas)))

    def test_el_ordering_no_amplia_el_alcance(self):
        """Admin: toda su empresa y nada de la otra. Vendedor: sólo sus asignados."""
        asignados = {self.clientes[0].pk, self.clientes[2].pk}
        for termino in ("-created_at", "created_at", "-clave", "nombre"):
            with self.subTest(ordering=termino):
                self.assertCountEqual(self._ids(ordering=termino), self.ids_empresa)
                self.assertCountEqual(self._ids(self.vendedor, ordering=termino), asignados)


class ClienteMesaControlFechaUltimaCompraTests(TestCase):
    """``fecha_ultima_compra`` de ``clientes-mesa-control`` (EC-744/EC-830): solo
    cuentan las ventas reales (``ventas.scope.pedidos_venta_real``). MUESTRA,
    PEDIDO DE ERROR, BORRADOR, POR AUTORIZAR y CANCELADO no son compra aunque
    sean más recientes; tampoco un pedido inactivo ni uno de otra empresa."""

    URL = "/api/v1/terceros/clientes-mesa-control/"
    VENTA = datetime(2026, 3, 10, 18, 0, tzinfo=dt_timezone.utc)
    POSTERIOR = datetime(2026, 4, 20, 18, 0, tzinfo=dt_timezone.utc)

    @classmethod
    def setUpTestData(cls):
        from nucleo.models import Moneda, Sucursal

        cls.empresa = Empresa.objects.create(codigo="ec830", razon_social="EC830 SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="EC830", nombre="EC830")
        cls.otra = Empresa.objects.create(codigo="ec830-otra", razon_social="Otra EC830 SA")
        cls.sucursal_otra = Sucursal.objects.create(empresa=cls.otra, codigo="EC830O", nombre="EC830 otra")
        cls.moneda = Moneda.objects.create(codigo_iso="XEC", nombre="Moneda EC830")
        cls.admin = Usuario.objects.create(
            username="admin-ec830", email="admin@ec830.test", empresa=cls.empresa, is_admin_empresa=True,
        )
        cls.admin_otra = Usuario.objects.create(
            username="admin-otra-ec830", email="admin@otra-ec830.test", empresa=cls.otra, is_admin_empresa=True,
        )

    def _cliente(self, nombre="Cliente EC830"):
        return Cliente.objects.create(empresa=self.empresa, nombre=nombre)

    def _pedido(self, cliente, creado, empresa=None, sucursal=None, **campos):
        from ventas.models import Pedido

        campos.setdefault("estatus", Pedido.ESTATUS_AUTORIZADA)
        pedido = Pedido.objects.create(
            empresa=empresa or self.empresa, sucursal=sucursal or self.sucursal, cliente=cliente,
            moneda=self.moneda, persona_pagos="Pagos", correo_facturas="p@ec830.test",
            telefono_pagos="8100000000", forma_pago="03", metodo_pago="PUE", uso_cfdi="G03", **campos,
        )
        Pedido.objects.filter(pk=pedido.pk).update(created_at=creado)
        return pedido

    def _fechas(self, usuario=None):
        client = APIClient()
        client.force_authenticate(user=usuario or self.admin)
        resp = client.get(self.URL)
        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.json()
        filas = data["results"] if isinstance(data, dict) and "results" in data else data
        return {fila["id"]: fila["fecha_ultima_compra"] for fila in filas}

    @staticmethod
    def _local(momento):
        return timezone.localtime(momento).date().isoformat()

    @staticmethod
    def _excluidos():
        from ventas.models import TIPO_PEDIDO_ERROR, TIPO_PEDIDO_MUESTRA, Pedido

        return {
            "MUESTRA": {"tipo_pedido": TIPO_PEDIDO_MUESTRA},
            "PEDIDO DE ERROR": {"tipo_pedido": TIPO_PEDIDO_ERROR},
            "BORRADOR": {"estatus": Pedido.ESTATUS_BORRADOR},
            "POR AUTORIZAR": {"estatus": Pedido.ESTATUS_POR_AUTORIZAR},
            "CANCELADO": {"estatus": Pedido.ESTATUS_CANCELADO},
        }

    def test_excluidos_mas_recientes_no_ganan_a_la_venta_real(self):
        clientes = {}
        for caso, campos in self._excluidos().items():
            cliente = self._cliente(f"Cliente {caso}")
            self._pedido(cliente, self.VENTA)
            self._pedido(cliente, self.POSTERIOR, **campos)
            clientes[caso] = cliente
        fechas = self._fechas()
        for caso, cliente in clientes.items():
            with self.subTest(caso=caso):
                self.assertEqual(fechas[cliente.pk], self._local(self.VENTA))

    def test_solo_excluidos_es_null(self):
        cliente = self._cliente()
        for campos in self._excluidos().values():
            self._pedido(cliente, self.POSTERIOR, **campos)
        sin_pedidos = self._cliente("Sin pedidos")
        fechas = self._fechas()
        self.assertIsNone(fechas[cliente.pk])
        self.assertIsNone(fechas[sin_pedidos.pk])

    def test_autorizada_y_en_proceso_cuentan(self):
        from ventas.models import Pedido

        for estatus in (Pedido.ESTATUS_AUTORIZADA, Pedido.ESTATUS_EN_PROCESO):
            with self.subTest(estatus=estatus):
                cliente = self._cliente(f"Cliente {estatus}")
                self._pedido(cliente, self.VENTA)
                self._pedido(cliente, self.POSTERIOR, estatus=estatus)
                self.assertEqual(self._fechas()[cliente.pk], self._local(self.POSTERIOR))

    def test_inactivo_no_cuenta(self):
        cliente = self._cliente()
        self._pedido(cliente, self.VENTA)
        self._pedido(cliente, self.POSTERIOR, activo=False)
        self.assertEqual(self._fechas()[cliente.pk], self._local(self.VENTA))

    def test_pedido_de_otra_empresa_no_cuenta_ni_se_ve(self):
        cliente = self._cliente()
        self._pedido(cliente, self.VENTA)
        # Pedido de OTRA empresa que apunta a este cliente: no es su compra.
        self._pedido(cliente, self.POSTERIOR, empresa=self.otra, sucursal=self.sucursal_otra)
        self.assertEqual(self._fechas()[cliente.pk], self._local(self.VENTA))
        # El admin de la otra empresa ni siquiera ve al cliente.
        self.assertNotIn(cliente.pk, self._fechas(self.admin_otra))

    def test_fecha_en_hora_local(self):
        # 02:00 UTC del 2 de enero = 1 de enero en America/Mexico_City.
        cliente = self._cliente()
        self._pedido(cliente, datetime(2026, 1, 2, 2, 0, tzinfo=dt_timezone.utc))
        self.assertEqual(self._fechas()[cliente.pk], "2026-01-01")

    def test_consultas_no_crecen_con_las_filas(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from ventas.models import Pedido

        def _consultas():
            with CaptureQueriesContext(connection) as ctx:
                self._fechas()
            return len(ctx)

        self._pedido(self._cliente("Primero"), self.VENTA)
        self._fechas()  # calienta lo que se cachea por proceso
        pocas = _consultas()
        for i in range(5):
            otro = self._cliente(f"Más {i}")
            self._pedido(otro, self.VENTA)
            self._pedido(otro, self.POSTERIOR, estatus=Pedido.ESTATUS_BORRADOR)
        self.assertEqual(_consultas(), pocas)


def _lectura_clientes(usuario):
    """``R-CRM-CLIENTES`` para ``usuario`` en su empresa: el listado de clientes la
    exige (#1020) a quien no es superusuario ni admin de empresa."""
    from seguridad.models import Permiso, UsuarioPermiso

    permiso, _ = Permiso.objects.get_or_create(clave="R-CRM-CLIENTES", defaults={"nombre": "Clientes"})
    UsuarioPermiso.objects.create(
        usuario=usuario, permiso=permiso, tipo=UsuarioPermiso.TIPO_GRANT, empresa=usuario.empresa,
    )


class ClienteListadoFechaUltimaCompraTests(ClienteMesaControlFechaUltimaCompraTests):
    """``fecha_ultima_compra`` del listado y el detalle de ``clientes`` (EC-853): la
    misma anotación que Mesa de Control (``ventas.scope.anotar_fecha_ultima_compra``),
    así que hereda todos sus casos (excluidos, inactivos, otra empresa, hora local,
    consultas constantes) contra ``/clientes/``. Encima: igualdad con Mesa de
    Control, detalle, ``?ordering=`` con NULL al final y paginado, alcance del
    vendedor, y alta/edición/Contabilidad sin el campo."""

    URL = "/api/v1/terceros/clientes/"
    URL_MESA = "/api/v1/terceros/clientes-mesa-control/"

    def _api(self, usuario=None):
        client = APIClient()
        client.force_authenticate(user=usuario or self.admin)
        return client

    def _filas(self, url=None, usuario=None, **params):
        resp = self._api(usuario).get(url or self.URL, params)
        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.json()
        return data["results"] if "limit" in params else data

    def _ids(self, **params):
        return [fila["id"] for fila in self._filas(**params)]

    def test_pedido_de_otra_empresa_no_cuenta_ni_se_ve(self):
        """Como el caso de Mesa de Control, pero con el pedido ajeno sembrado por
        ``update()``: desde #1024 ``Pedido.save()`` ya no deja crearlo, aunque las
        filas anteriores a esa barrera pueden existir (el caso heredado falla igual
        en ``main`` por eso)."""
        from ventas.models import Pedido

        cliente = self._cliente()
        self._pedido(cliente, self.VENTA)
        ajeno = self._pedido(cliente, self.POSTERIOR)
        Pedido.objects.filter(pk=ajeno.pk).update(empresa=self.otra, sucursal=self.sucursal_otra)
        self.assertEqual(self._fechas()[cliente.pk], self._local(self.VENTA))
        self.assertNotIn(cliente.pk, self._fechas(self.admin_otra))

    def test_igual_a_mesa_de_control_para_cada_cliente(self):
        from ventas.models import Pedido

        con_venta = self._cliente("Con venta")
        self._pedido(con_venta, self.VENTA)
        self._pedido(con_venta, self.POSTERIOR, estatus=Pedido.ESTATUS_BORRADOR)
        posterior = self._cliente("Posterior")
        self._pedido(posterior, self.POSTERIOR, estatus=Pedido.ESTATUS_EN_PROCESO)
        self._cliente("Sin pedidos")
        listado = {f["id"]: f["fecha_ultima_compra"] for f in self._filas()}
        mesa = {f["id"]: f["fecha_ultima_compra"] for f in self._filas(self.URL_MESA)}
        self.assertEqual(len(listado), 3)
        self.assertEqual(listado, mesa)
        self.assertEqual(listado[posterior.pk], self._local(self.POSTERIOR))

    def test_detalle_trae_la_misma_fecha(self):
        con_venta = self._cliente("Con venta")
        self._pedido(con_venta, self.VENTA)
        sin_pedidos = self._cliente("Sin pedidos")
        for cliente, esperado in ((con_venta, self._local(self.VENTA)), (sin_pedidos, None)):
            with self.subTest(cliente=cliente.nombre):
                resp = self._api().get(f"{self.URL}{cliente.pk}/")
                self.assertEqual(resp.status_code, 200, resp.data)
                self.assertEqual(resp.data["fecha_ultima_compra"], esperado)
                self.assertIn("resumen_comercial", resp.data)

    def _clientes_para_ordenar(self):
        # Dos con compra (fechas distintas) y dos sin compra, creados intercalados
        # para que el desempate por ``id`` no coincida con el orden por fecha.
        sin_a = self._cliente("Sin A")
        reciente = self._cliente("Reciente")
        self._pedido(reciente, self.POSTERIOR)
        sin_b = self._cliente("Sin B")
        antigua = self._cliente("Antigua")
        self._pedido(antigua, self.VENTA)
        return sin_a, reciente, sin_b, antigua

    def test_ordering_deja_los_null_al_final_en_ambas_direcciones(self):
        sin_a, reciente, sin_b, antigua = self._clientes_para_ordenar()
        casos = {
            "fecha_ultima_compra": [antigua.pk, reciente.pk, sin_a.pk, sin_b.pk],
            "-fecha_ultima_compra": [reciente.pk, antigua.pk, sin_b.pk, sin_a.pk],
        }
        for termino, esperado in casos.items():
            with self.subTest(ordering=termino):
                self.assertEqual(self._ids(ordering=termino), esperado)
                self.assertEqual(self._ids(ordering=termino, limit=50), esperado)

    def test_ordering_lleva_nulls_last_en_el_sql(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._clientes_para_ordenar()
        for termino, direccion in (("fecha_ultima_compra", "ASC"), ("-fecha_ultima_compra", "DESC")):
            with self.subTest(ordering=termino):
                with CaptureQueriesContext(connection) as consultas:
                    self._ids(ordering=termino, limit=50)
                sql = next(q["sql"] for q in consultas.captured_queries
                           if 'FROM "clientes"' in q["sql"] and "LIMIT" in q["sql"])
                # La anotación va en el SELECT; el ORDER BY la cita por posición.
                self.assertRegex(sql, rf'ORDER BY \d+ {direccion} NULLS LAST, "clientes"\."id" {direccion} LIMIT')

    def test_paginas_concatenadas_igualan_la_lista_completa(self):
        self._clientes_para_ordenar()
        for termino in ("fecha_ultima_compra", "-fecha_ultima_compra"):
            with self.subTest(ordering=termino):
                completo = self._ids(ordering=termino)
                paginas = []
                for offset in range(0, len(completo), 3):
                    paginas += self._ids(ordering=termino, limit=3, offset=offset)
                self.assertEqual(paginas, completo)

    def test_consultas_no_crecen_con_las_filas_paginando(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def _consultas():
            with CaptureQueriesContext(connection) as ctx:
                self._filas(ordering="-fecha_ultima_compra", limit=50)
            return len(ctx)

        self._pedido(self._cliente("Primero"), self.VENTA)
        self._filas(limit=50)  # calienta lo que se cachea por proceso
        pocas = _consultas()
        for i in range(5):
            self._pedido(self._cliente(f"Más {i}"), self.POSTERIOR)
        self.assertEqual(_consultas(), pocas)

    def test_vendedor_solo_ve_sus_clientes_con_la_fecha_de_la_empresa(self):
        """La anotación no cambia el alcance; la fecha es la del cliente en su
        empresa (la misma que ve Mesa de Control), no sólo la de sus pedidos."""
        vendedor = Usuario.objects.create(username="v-ec853", email="v@ec853.test", empresa=self.empresa)
        _lectura_clientes(vendedor)
        suyo = self._cliente("Suyo")
        suyo.vendedores.add(vendedor)
        self._pedido(suyo, self.VENTA)
        ajeno = self._cliente("No asignado")
        self._pedido(ajeno, self.POSTERIOR)
        filas = self._filas(usuario=vendedor, ordering="-fecha_ultima_compra")
        self.assertEqual(
            [(f["id"], f["fecha_ultima_compra"]) for f in filas], [(suyo.pk, self._local(self.VENTA))]
        )

    def test_alta_y_edicion_responden_sin_fecha_ultima_compra(self):
        from terceros.api.serializers import ClienteSerializer

        campos = {n for n, f in ClienteSerializer().fields.items() if not f.write_only}
        self.assertNotIn("fecha_ultima_compra", campos)
        api = self._api()
        alta = api.post(self.URL, {"nombre": "Alta EC853"}, format="json")
        self.assertEqual(alta.status_code, 201, alta.data)
        self.assertEqual(set(alta.data) - {"facturama_sincronizado"}, campos)
        cliente = Cliente.objects.get(pk=alta.data["id"])
        self._pedido(cliente, self.VENTA)
        edicion = api.patch(f"{self.URL}{cliente.pk}/", {"nombre": "Editado EC853"}, format="json")
        self.assertEqual(edicion.status_code, 200, edicion.data)
        self.assertEqual(set(edicion.data), campos)

    def test_contabilidad_no_cambia(self):
        self._pedido(self._cliente("Con venta"), self.VENTA)
        resp = self._api().get("/api/v1/finanzas/clientes-contabilidad/")
        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.json()
        filas = data["results"] if isinstance(data, dict) and "results" in data else data
        self.assertTrue(filas)
        for fila in filas:
            self.assertNotIn("fecha_ultima_compra", fila)


class ClienteBusquedaTelefonoTests(TestCase):
    """``?search=`` del listado de clientes también busca en ``telefono`` (EC-853),
    tal cual se guardó (sin normalizar), y nunca amplía el alcance."""

    URL = "/api/v1/terceros/clientes/"

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="ec853", razon_social="EC853 SA")
        cls.otra = Empresa.objects.create(codigo="ec853-otra", razon_social="Otra EC853 SA")
        cls.admin = Usuario.objects.create(
            username="admin-ec853", email="admin@ec853.test", empresa=cls.empresa, is_admin_empresa=True,
        )
        cls.vendedor = Usuario.objects.create(username="v-ec853b", email="vb@ec853.test", empresa=cls.empresa)
        _lectura_clientes(cls.vendedor)
        cls.con_espacios = Cliente.objects.create(empresa=cls.empresa, nombre="Uno", telefono="81 1234 5678")
        cls.con_espacios.vendedores.add(cls.vendedor)
        cls.corrido = Cliente.objects.create(empresa=cls.empresa, nombre="Dos", telefono="8112345600")
        cls.sin_telefono = Cliente.objects.create(empresa=cls.empresa, nombre="Tres", telefono="")
        cls.ajeno = Cliente.objects.create(empresa=cls.otra, nombre="Ajeno", telefono="8112349999")

    def _ids(self, usuario=None, **params):
        api = APIClient()
        api.force_authenticate(user=usuario or self.admin)
        resp = api.get(self.URL, params)
        self.assertEqual(resp.status_code, 200, resp.data)
        filas = resp.data["results"] if "limit" in params else resp.data
        return {fila["id"] for fila in filas}

    def test_busca_por_parte_del_telefono(self):
        for params in ({}, {"limit": 50}):
            with self.subTest(**params):
                self.assertEqual(self._ids(search="1234", **params), {self.con_espacios.pk, self.corrido.pk})
                self.assertEqual(self._ids(search="5600", **params), {self.corrido.pk})

    def test_sin_normalizar_el_formato(self):
        self.assertEqual(self._ids(search="1234 5678"), {self.con_espacios.pk})
        self.assertEqual(self._ids(search="12345678"), set())

    def test_no_amplia_el_alcance(self):
        self.assertNotIn(self.ajeno.pk, self._ids(search="8112349999"))
        self.assertEqual(self._ids(search="8112349999"), set())
        self.assertEqual(self._ids(self.vendedor, search="1234"), {self.con_espacios.pk})
        self.assertEqual(self._ids(self.vendedor, search="5600"), set())

    def test_los_demas_campos_siguen_buscando(self):
        self.assertEqual(self._ids(search="Dos"), {self.corrido.pk})
