from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from compras.models import Recepcion, RecepcionDetalle
from catalogo.models import Color, Producto, ProductoVariante, Talla, TipoProducto
from inventarios.models import Almacen, Existencia, MovimientoInventario
from nucleo.models import Empresa, Moneda, SerieFolio, Sucursal, UnidadMedida
from produccion.models import ListaMaterialBom, OrdenProduccion, OrdenProduccionDetalle
from usuarios.models import Usuario


class RecepcionOnboardingGetTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso mexicano")
        self.empresa = Empresa.objects.create(
            codigo="empresa-compras-test",
            razon_social="Empresa Compras Test",
            moneda_base=moneda,
        )
        self.sucursal = Sucursal.objects.create(
            empresa=self.empresa,
            codigo="MTY",
            nombre="Matriz",
        )
        self.user = Usuario.objects.create_user(
            username="compras-tester",
            email="compras-tester@example.com",
            password="secret123",
            empresa=self.empresa,
            sucursal_default=self.sucursal,
        )
        self.user.sucursales.add(self.sucursal)
        self.client.force_authenticate(user=self.user)

        self.unidad = UnidadMedida.objects.create(clave="PZA", nombre="Pieza")
        self.tipo_producto = TipoProducto.objects.create(codigo="PT")
        self.color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000")
        self.talla = Talla.objects.create(nombre="M")

        self.producto = Producto.objects.create(
            empresa=self.empresa,
            nombre="Playera",
            unidad_medida=self.unidad,
            tipo=self.tipo_producto,
        )
        self.variante = ProductoVariante.objects.create(
            producto=self.producto,
            empresa=self.empresa,
            color=self.color,
            talla=self.talla,
            sku="PLAY-NEG-M-COMPRA",
            precio_base="100.00",
        )
        self.bom = ListaMaterialBom.objects.create(
            empresa=self.empresa,
            producto_variante=self.variante,
            version=1,
            activo=True,
        )
        self.op = OrdenProduccion.objects.create(
            empresa=self.empresa,
            sucursal=self.sucursal,
            folio_op="OP-TEST-1",
            cerrar_orden=True,
        )
        self.op_detalle = OrdenProduccionDetalle.objects.create(
            op=self.op,
            bom=self.bom,
            cantidad="5.00",
            unidad=self.unidad,
            producto_variante=self.variante,
        )

        self.almacen = Almacen.objects.create(
            empresa=self.empresa,
            sucursal=self.sucursal,
            codigo="REC",
            nombre="Almacen recepcion",
            estatus="ACTIVO",
        )
        SerieFolio.objects.create(
            empresa=self.empresa,
            sucursal=self.sucursal,
            tipo_documento="RECEPCION",
            serie="RC",
            folio_actual=0,
            folio_inicial=1,
            activo=True,
        )

    def test_onboarding_get_incluye_ordenes_produccion(self):
        response = self.client.get("/api/v1/compras/recepciones/onboarding/", secure=True)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("busqueda", response.data)
        self.assertIn("ordenes_compra", response.data["busqueda"])
        self.assertIn("ordenes_produccion", response.data["busqueda"])

        ordenes_produccion = response.data["busqueda"]["ordenes_produccion"]
        self.assertEqual(len(ordenes_produccion), 1)

        orden = ordenes_produccion[0]
        self.assertEqual(orden["id"], self.op.pk)
        self.assertEqual(orden["folio"], self.op.folio_op)
        self.assertTrue(orden["cerrar_orden"])
        self.assertEqual(orden["estatus"], OrdenProduccion.EstatusOrdenProduccion.PENDIENTE)
        self.assertEqual(len(orden["detalle"]), 1)
        self.assertEqual(orden["detalle"][0]["producto_variante_id"], self.variante.pk)
        self.assertEqual(orden["detalle"][0]["cantidad_pendiente"], "5.00")

    def test_onboarding_get_descuenta_cantidades_recibidas_en_op(self):
        recepcion = Recepcion.objects.create(
            tipo_origen=Recepcion.TipoOrigen.ORDEN_PRODUCCION,
            orden_compra=None,
            op=self.op,
            empresa=self.empresa,
            sucursal=self.sucursal,
            proveedor=None,
            almacen=self.almacen,
            usuario=self.user,
            folio="RC-TEST-OP-GET",
            fecha_recepcion=timezone.now(),
            estatus=Recepcion.EstatusRecepcion.PARCIAL,
            activo=True,
        )
        RecepcionDetalle.objects.create(
            recepcion=recepcion,
            orden_compra_detalle=None,
            orden_produccion_detalle=self.op_detalle,
            producto=self.producto,
            producto_variante=self.variante,
            cantidad_recibida="2.0000",
        )

        response = self.client.get("/api/v1/compras/recepciones/onboarding/", secure=True)

        self.assertEqual(response.status_code, 200, response.data)
        orden = response.data["busqueda"]["ordenes_produccion"][0]
        self.assertEqual(orden["detalle"][0]["cantidad_recibida"], "2.0000")
        self.assertEqual(orden["detalle"][0]["cantidad_pendiente"], "3.0000")


class RecepcionOnboardingPostOrdenProduccionTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso mexicano")
        self.empresa = Empresa.objects.create(
            codigo="empresa-compras-post-op",
            razon_social="Empresa Compras Post OP",
            moneda_base=moneda,
        )
        self.sucursal = Sucursal.objects.create(
            empresa=self.empresa,
            codigo="MTY",
            nombre="Matriz",
        )
        self.user = Usuario.objects.create_user(
            username="compras-post-op",
            email="compras-post-op@example.com",
            password="secret123",
            empresa=self.empresa,
            sucursal_default=self.sucursal,
        )
        self.user.sucursales.add(self.sucursal)
        self.client.force_authenticate(user=self.user)

        self.unidad = UnidadMedida.objects.create(clave="PZA", nombre="Pieza")
        self.tipo_producto = TipoProducto.objects.create(codigo="PT-POST")
        self.color = Color.objects.create(nombre="Azul", codigo="AZU", codigo_hex="#0000FF")
        self.talla = Talla.objects.create(nombre="G")

        self.producto = Producto.objects.create(
            empresa=self.empresa,
            nombre="Sudadera",
            unidad_medida=self.unidad,
            tipo=self.tipo_producto,
        )
        self.variante = ProductoVariante.objects.create(
            producto=self.producto,
            empresa=self.empresa,
            color=self.color,
            talla=self.talla,
            sku="SUD-AZU-G-POST",
            precio_base="250.00",
        )
        self.bom = ListaMaterialBom.objects.create(
            empresa=self.empresa,
            producto_variante=self.variante,
            version=1,
            activo=True,
        )
        self.op = OrdenProduccion.objects.create(
            empresa=self.empresa,
            sucursal=self.sucursal,
            folio_op="OP-POST-1",
            cerrar_orden=True,
        )
        self.op_detalle = OrdenProduccionDetalle.objects.create(
            op=self.op,
            bom=self.bom,
            cantidad="4.00",
            unidad=self.unidad,
            producto_variante=self.variante,
        )
        self.almacen = Almacen.objects.create(
            empresa=self.empresa,
            sucursal=self.sucursal,
            codigo="PT",
            nombre="Almacen producto terminado",
            estatus="ACTIVO",
        )
        SerieFolio.objects.create(
            empresa=self.empresa,
            sucursal=self.sucursal,
            tipo_documento="RECEPCION",
            serie="RC",
            folio_actual=0,
            folio_inicial=1,
            activo=True,
        )

    def test_onboarding_post_crea_recepcion_desde_op_y_liga_movimiento(self):
        payload = {
            "recepcion": {
                "orden_produccion": self.op.pk,
                "almacen": self.almacen.pk,
                "serie_codigo": "RC",
                "observaciones": "Entrada de producto terminado desde OP",
            },
            "detalle": [
                {
                    "orden_produccion_detalle": self.op_detalle.pk,
                    "cantidad_recibida": "4.0000",
                }
            ],
        }

        response = self.client.post(
            "/api/v1/compras/recepciones/onboarding/",
            payload,
            format="json",
            secure=True,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.op.refresh_from_db()

        recepcion = Recepcion.objects.get(pk=response.data["recepcion"]["id"])
        detalle = RecepcionDetalle.objects.get(recepcion=recepcion)
        movimiento = MovimientoInventario.objects.get(pk=response.data["movimiento_inventario_id"])
        existencia = Existencia.objects.get(
            producto=self.producto,
            producto_variante=self.variante,
            almacen=self.almacen,
            ubicacion__isnull=True,
        )

        self.assertEqual(recepcion.tipo_origen, Recepcion.TipoOrigen.ORDEN_PRODUCCION)
        self.assertIsNone(recepcion.orden_compra_id)
        self.assertEqual(recepcion.op_id, self.op.pk)
        self.assertIsNone(recepcion.proveedor_id)
        self.assertEqual(recepcion.estatus, Recepcion.EstatusRecepcion.RECIBIDA)

        self.assertIsNone(detalle.orden_compra_detalle_id)
        self.assertEqual(detalle.orden_produccion_detalle_id, self.op_detalle.pk)
        self.assertEqual(detalle.producto_id, self.producto.pk)
        self.assertEqual(detalle.producto_variante_id, self.variante.pk)
        self.assertEqual(str(detalle.cantidad_recibida), "4.0000")

        self.assertEqual(str(existencia.cantidad), "4.0000")
        self.assertEqual(existencia.producto_variante_id, self.variante.pk)

        self.assertEqual(movimiento.recepcion_id, recepcion.pk)
        self.assertEqual(movimiento.op_id, self.op.pk)
        self.assertEqual(movimiento.tipo_movimiento, "ENTRADA")

        self.assertEqual(self.op.estatus_op, OrdenProduccion.EstatusOrdenProduccion.COMPLETADO)
