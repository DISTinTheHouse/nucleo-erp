from django.test import TestCase
from rest_framework.test import APIClient

from catalogo.models import CategoriaProducto, Color, Producto, ProductoVariante, Talla, TipoProducto
from inventarios.models import Almacen, Existencia
from nucleo.models import Empresa, Moneda, SerieFolio, Sucursal, UnidadMedida
from produccion.models import (
    BomDetalle,
    ConsumoProduccion,
    ListaMaterialBom,
    OrdenProduccion,
)
from usuarios.models import Usuario


class OrdenProduccionOnboardingTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso mexicano")
        self.empresa = Empresa.objects.create(
            codigo="empresa-test",
            razon_social="Empresa Test",
            moneda_base=moneda,
        )
        self.sucursal = Sucursal.objects.create(
            empresa=self.empresa,
            codigo="MTY",
            nombre="Matriz",
        )
        self.user = Usuario.objects.create_user(
            username="tester",
            email="tester@example.com",
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

        self.producto_terminado = Producto.objects.create(
            empresa=self.empresa,
            nombre="Playera",
            unidad_medida=self.unidad,
            tipo=self.tipo_producto,
        )
        self.variante = ProductoVariante.objects.create(
            producto=self.producto_terminado,
            empresa=self.empresa,
            color=self.color,
            talla=self.talla,
            sku="PLAY-NEG-M",
            precio_base="100.00",
        )

        categoria_mp = CategoriaProducto.objects.create(
            empresa=self.empresa,
            nombre="Materia Prima",
            codigo="MP1",
            descripcion="Materia prima",
        )
        self.insumo = Producto.objects.create(
            empresa=self.empresa,
            nombre="Tela negra",
            categoria_producto=categoria_mp,
            unidad_medida=self.unidad,
        )

        self.bom = ListaMaterialBom.objects.create(
            empresa=self.empresa,
            producto_variante=self.variante,
            version=1,
            activo=True,
        )
        BomDetalle.objects.create(
            bom=self.bom,
            componente=self.insumo,
            cantidad="2.0000",
            unidad=self.unidad,
            desperdicio="0.00",
            obligatorio=True,
            activo=True,
        )

        self.almacen = Almacen.objects.create(
            empresa=self.empresa,
            sucursal=self.sucursal,
            codigo="MP",
            nombre="Almacen MP",
            permite_salida=True,
        )
        self.existencia = Existencia.objects.create(
            producto=self.insumo,
            producto_variante=None,
            almacen=self.almacen,
            ubicacion=None,
            stock=10,
            cantidad="10.0000",
        )

        SerieFolio.objects.create(
            empresa=self.empresa,
            sucursal=self.sucursal,
            tipo_documento="Orden de Produccion",
            serie="OP",
            folio_actual=0,
            folio_inicial=1,
            activo=True,
        )

    def test_onboarding_crea_op_y_descuenta_existencias(self):
        payload = {
            "empresa": self.empresa.pk,
            "sucursal": self.sucursal.pk,
            "prioridad": 1,
            "observaciones": "OP de prueba",
            "orden_produccion_detalle": [
                {
                    "producto_variante_id": self.variante.pk,
                    "cantidad": "3.0000",
                    "unidad": self.unidad.pk,
                    "observaciones": "",
                }
            ],
        }

        response = self.client.post(
            "/api/v1/produccion/orden-produccion/onboarding/",
            payload,
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["msg"], "Orden de producción creada exitosamente")
        self.assertIn("op_id", response.data)
        self.assertIn("folio_op", response.data)
        self.assertIn("consumo_produccion_id", response.data)
        self.assertIn("movimiento_inventario_id", response.data)

        op = OrdenProduccion.objects.get(pk=response.data["op_id"])
        self.assertEqual(op.orden_produccion_detalle.count(), 1)

        self.existencia.refresh_from_db()
        self.assertEqual(str(self.existencia.cantidad), "4.0000")

        consumo = ConsumoProduccion.objects.get(pk=response.data["consumo_produccion_id"])
        detalle = consumo.detalles.get(producto=self.insumo)
        self.assertEqual(str(detalle.cantidad), "6.0000")

    def test_onboarding_falla_si_no_hay_existencias_suficientes(self):
        payload = {
            "empresa": self.empresa.pk,
            "sucursal": self.sucursal.pk,
            "prioridad": 1,
            "orden_produccion_detalle": [
                {
                    "producto_variante_id": self.variante.pk,
                    "cantidad": "6.0000",
                    "unidad": self.unidad.pk,
                }
            ],
        }

        response = self.client.post(
            "/api/v1/produccion/orden-produccion/onboarding/",
            payload,
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("inventario", response.data)
        self.assertEqual(OrdenProduccion.objects.count(), 0)
