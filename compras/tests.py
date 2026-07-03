from django.test import TestCase
from rest_framework.test import APIClient

from catalogo.models import Color, Producto, ProductoVariante, Talla, TipoProducto
from inventarios.models import Almacen
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
        OrdenProduccionDetalle.objects.create(
            op=self.op,
            bom=self.bom,
            cantidad="5.00",
            unidad=self.unidad,
            producto_variante=self.variante,
        )

        Almacen.objects.create(
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
