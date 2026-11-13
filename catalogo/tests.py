import random

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from catalogo.models import (
    CategoriaProducto,
    CategoriaProductoTalla,
    Color,
    Producto,
    ProductoVariante,
    Talla,
    TipoProducto,
)
from catalogo.tallas import talla_sort_key
from nucleo.models import Empresa
from produccion.models import ListaMaterialBom
from usuarios.models import Usuario

TALLAS_URL = "/api/v1/catalogo/talla/"
COLORES_URL = "/api/v1/catalogo/color/"
CATEGORIAS_URL = "/api/v1/catalogo/categoria-producto/"
PRODUCTOS_URL = "/api/v1/catalogo/producto/"
VARIANTES_URL = "/api/v1/catalogo/producto-variante/"

# Las 43 tallas activas reales de producción (tabla ``tallas``, 2026-09-14, ya
# con ``2XC`` fusionada en ``2XCH`` por catalogo/0020), en el orden canónico
# confirmado por negocio. ``24`` y ``26`` no venían en la lista de negocio pero
# son pares y caen por regla.
ORDEN_CANONICO_REAL = [
    # letras
    "2XCH", "XCH", "CH", "M", "G", "XG", "2XG", "3XG", "4XG", "5XG", "6XG",
    # numéricas pares
    "22", "24", "26", "28", "30", "32", "34", "36", "38", "40", "42", "44",
    "46", "48", "50",
    # numéricas impares
    "1", "3", "5", "7", "9", "11", "13", "15", "17", "19", "21", "23", "25",
    "27", "29", "31",
    # especiales
    "UNI",
]


class TallaSortKeyTests(SimpleTestCase):
    def _ordenar(self, nombres):
        return sorted(nombres, key=talla_sort_key)

    def test_orden_canonico_de_las_43_tallas_reales(self):
        self.assertEqual(len(ORDEN_CANONICO_REAL), 43)
        revueltas = list(ORDEN_CANONICO_REAL)
        for semilla in range(5):
            random.Random(semilla).shuffle(revueltas)
            self.assertEqual(self._ordenar(revueltas), ORDEN_CANONICO_REAL)

    def test_secciones(self):
        self.assertEqual(talla_sort_key("2XCH")[0], 0)
        self.assertEqual(talla_sort_key("6XG")[0], 0)
        self.assertEqual(talla_sort_key("22")[0], 1)
        self.assertEqual(talla_sort_key("31")[0], 2)
        self.assertEqual(talla_sort_key("UNI")[0], 3)
        self.assertEqual(talla_sort_key("2XC")[0], 4)

    def test_regla_de_letras_es_algoritmica(self):
        self.assertEqual(
            self._ordenar(["7XG", "M", "3XCH", "XG", "10XG", "XCH", "2XCH"]),
            ["3XCH", "2XCH", "XCH", "M", "XG", "7XG", "10XG"],
        )

    def test_numericas_ordenan_por_valor_no_por_texto(self):
        self.assertEqual(self._ordenar(["100", "9", "10", "8"]), ["8", "10", "100", "9"])

    def test_normaliza_minusculas_y_espacios(self):
        self.assertEqual(talla_sort_key("ch")[:2], talla_sort_key("CH")[:2])
        self.assertEqual(talla_sort_key("  2XG ")[:2], talla_sort_key("2XG")[:2])
        self.assertEqual(talla_sort_key(" uni ")[0], 3)
        self.assertEqual(
            self._ordenar(["  2XG ", "ch", "M"]),
            ["ch", "M", "  2XG "],
        )

    def test_desconocidas_al_final_en_orden_alfabetico_sin_truena(self):
        entrada = ["S", "UNI", "", None, "1", "ABC", "M", "0XG", "XXG", "2XC", "²"]
        salida = self._ordenar(entrada)
        self.assertEqual(len(salida), len(entrada))
        self.assertEqual(salida[:3], ["M", "1", "UNI"])
        self.assertEqual(
            salida[3:], ["", None, "0XG", "2XC", "ABC", "S", "XXG", "²"]
        )


class TallaViewSetOrdenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        revueltas = list(ORDEN_CANONICO_REAL)
        random.Random(42).shuffle(revueltas)
        for nombre in revueltas:
            Talla.objects.create(nombre=nombre)
        Talla.objects.create(nombre="INACTIVA", activo=False)
        cls.usuario = Usuario.objects.create(username="u@x.test", email="u@x.test")

    def _get(self, url=TALLAS_URL):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_list_devuelve_orden_canonico(self):
        self.assertEqual([t["nombre"] for t in self._get()], ORDEN_CANONICO_REAL)

    def test_list_respeta_ordering_explicito(self):
        ids = [t["id"] for t in self._get(f"{TALLAS_URL}?ordering=-id")]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_ordering_invalido_cae_al_orden_canonico(self):
        nombres = [t["nombre"] for t in self._get(f"{TALLAS_URL}?ordering=nombres")]
        self.assertEqual(nombres, ORDEN_CANONICO_REAL)

    def test_retrieve_sigue_funcionando(self):
        talla = Talla.objects.get(nombre="UNI")
        self.assertEqual(self._get(f"{TALLAS_URL}{talla.pk}/")["nombre"], "UNI")


class ColorViewSetOrdenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for nombre in ["Zafiro", "Rojo", "azul", "Verde", "amarillo", "Negro"]:
            Color.objects.create(nombre=nombre, codigo=nombre[:3].upper(), codigo_hex="#000000")
        Color.objects.create(nombre="Aaa inactivo", codigo="INA", codigo_hex="#000000", activo=False)
        cls.usuario = Usuario.objects.create(username="c@x.test", email="c@x.test")

    def test_list_ordena_alfabeticamente_sin_distinguir_mayusculas(self):
        client = APIClient()
        client.force_authenticate(user=self.usuario)
        resp = client.get(COLORES_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            [c["nombre"] for c in resp.json()],
            ["amarillo", "azul", "Negro", "Rojo", "Verde", "Zafiro"],
        )


class AislamientoEmpresaCatalogoTests(TestCase):
    """Categoria, Producto y ProductoVariante acotados a la empresa del usuario.

    Dos puertas: ``get_queryset`` (list/retrieve/update/destroy de filas ajenas)
    y el campo ``empresa`` del serializer (un PATCH no puede reasignar el tenant).
    """

    @classmethod
    def setUpTestData(cls):
        cls.empresa_a = Empresa.objects.create(codigo="cat-a", razon_social="A SA")
        cls.empresa_b = Empresa.objects.create(codigo="cat-b", razon_social="B SA")
        cls.tipo = TipoProducto.objects.create(codigo="PT")
        cls.tipo_otro = TipoProducto.objects.create(codigo="MP")
        cls.color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000")
        cls.talla = Talla.objects.create(nombre="M")

        cls.cat_a = CategoriaProducto.objects.create(
            empresa=cls.empresa_a, nombre="Playeras A", codigo="PLA", descripcion="A",
        )
        cls.cat_b = CategoriaProducto.objects.create(
            empresa=cls.empresa_b, nombre="Playeras B", codigo="PLB", descripcion="B",
        )
        CategoriaProductoTalla.objects.create(categoria_producto=cls.cat_a, talla=cls.talla)
        CategoriaProductoTalla.objects.create(categoria_producto=cls.cat_b, talla=cls.talla)

        cls.prod_a = Producto.objects.create(
            empresa=cls.empresa_a, categoria_producto=cls.cat_a, tipo=cls.tipo,
            nombre="Playera polo", codigo="PLA00", precio_base="100.00",
        )
        cls.prod_a_otro_tipo = Producto.objects.create(
            empresa=cls.empresa_a, categoria_producto=cls.cat_a, tipo=cls.tipo_otro,
            nombre="Hilo polo", codigo="PLA01",
        )
        cls.prod_b = Producto.objects.create(
            empresa=cls.empresa_b, categoria_producto=cls.cat_b, tipo=cls.tipo,
            nombre="Playera polo B", codigo="PLB00", precio_base="100.00",
        )

        cls.var_a = ProductoVariante.objects.create(
            producto=cls.prod_a, empresa=cls.empresa_a, color=cls.color, talla=cls.talla,
            sku="PLA00-NEG-M", precio_base="100.00",
        )
        cls.var_a_sin_bom = ProductoVariante.objects.create(
            producto=cls.prod_a_otro_tipo, empresa=cls.empresa_a, color=cls.color,
            talla=cls.talla, sku="PLA01-NEG-M", precio_base="10.00",
        )
        cls.var_b = ProductoVariante.objects.create(
            producto=cls.prod_b, empresa=cls.empresa_b, color=cls.color, talla=cls.talla,
            sku="PLB00-NEG-M", precio_base="100.00",
        )
        ListaMaterialBom.objects.create(empresa=cls.empresa_a, producto_variante=cls.var_a)

        # Admin de empresa: la categoría solo la escribe él o el superusuario (#921).
        cls.user_a = Usuario.objects.create(
            username="a@cat.test", email="a@cat.test", empresa=cls.empresa_a, is_admin_empresa=True,
        )
        cls.sin_empresa = Usuario.objects.create(username="n@cat.test", email="n@cat.test")
        cls.superuser = Usuario.objects.create(
            username="s@cat.test", email="s@cat.test", is_superuser=True, is_staff=True,
        )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _ids(self, resp):
        self.assertEqual(resp.status_code, 200, resp.content)
        return {row["id"] for row in resp.json()}

    # --- list -----------------------------------------------------------------

    def test_list_solo_devuelve_filas_de_la_empresa_del_usuario(self):
        client = self._client(self.user_a)
        self.assertEqual(self._ids(client.get(CATEGORIAS_URL)), {self.cat_a.pk})
        self.assertEqual(
            self._ids(client.get(PRODUCTOS_URL)), {self.prod_a.pk, self.prod_a_otro_tipo.pk},
        )
        self.assertEqual(
            self._ids(client.get(VARIANTES_URL)), {self.var_a.pk, self.var_a_sin_bom.pk},
        )

    def test_list_usuario_sin_empresa_no_ve_nada(self):
        client = self._client(self.sin_empresa)
        for url in (CATEGORIAS_URL, PRODUCTOS_URL, VARIANTES_URL):
            self.assertEqual(self._ids(client.get(url)), set(), url)

    def test_list_superusuario_ve_todas_las_empresas(self):
        client = self._client(self.superuser)
        self.assertEqual(self._ids(client.get(CATEGORIAS_URL)), {self.cat_a.pk, self.cat_b.pk})
        self.assertEqual(
            self._ids(client.get(PRODUCTOS_URL)),
            {self.prod_a.pk, self.prod_a_otro_tipo.pk, self.prod_b.pk},
        )
        self.assertEqual(
            self._ids(client.get(VARIANTES_URL)),
            {self.var_a.pk, self.var_a_sin_bom.pk, self.var_b.pk},
        )

    def test_filtros_existentes_siguen_funcionando_dentro_del_alcance(self):
        client = self._client(self.user_a)
        # ``?q=polo`` casa con productos de ambas empresas: sólo vuelven los propios.
        self.assertEqual(
            self._ids(client.get(PRODUCTOS_URL, {"q": "polo"})),
            {self.prod_a.pk, self.prod_a_otro_tipo.pk},
        )
        self.assertEqual(
            self._ids(client.get(PRODUCTOS_URL, {"tipo_id": self.tipo.pk})), {self.prod_a.pk},
        )
        self.assertEqual(client.get(PRODUCTOS_URL, {"tipo_id": "x"}).status_code, 400)
        self.assertEqual(
            self._ids(client.get(VARIANTES_URL, {"q": "NEG-M"})),
            {self.var_a.pk, self.var_a_sin_bom.pk},
        )
        self.assertEqual(self._ids(client.get(VARIANTES_URL, {"con_bom": "true"})), {self.var_a.pk})

    def test_orden_existente_se_conserva(self):
        client = self._client(self.user_a)
        ids = [row["id"] for row in client.get(PRODUCTOS_URL).json()]
        self.assertEqual(ids, [self.prod_a_otro_tipo.pk, self.prod_a.pk])

    # --- retrieve / destroy ---------------------------------------------------

    def test_retrieve_de_otra_empresa_es_404(self):
        client = self._client(self.user_a)
        for url, propia, ajena in (
            (CATEGORIAS_URL, self.cat_a, self.cat_b),
            (PRODUCTOS_URL, self.prod_a, self.prod_b),
            (VARIANTES_URL, self.var_a, self.var_b),
        ):
            self.assertEqual(client.get(f"{url}{propia.pk}/").status_code, 200, url)
            self.assertEqual(client.get(f"{url}{ajena.pk}/").status_code, 404, url)

    def test_patch_y_delete_de_otra_empresa_son_404_y_no_tocan_la_fila(self):
        client = self._client(self.user_a)
        for url, ajena in (
            (CATEGORIAS_URL, self.cat_b),
            (PRODUCTOS_URL, self.prod_b),
            (VARIANTES_URL, self.var_b),
        ):
            resp = client.patch(f"{url}{ajena.pk}/", {"nombre": "hackeado"}, format="json")
            self.assertEqual(resp.status_code, 404, url)
            self.assertEqual(client.delete(f"{url}{ajena.pk}/").status_code, 404, url)
            ajena.refresh_from_db()
            self.assertNotEqual(ajena.nombre, "hackeado")

    # --- empresa no reasignable por update -------------------------------------

    def test_patch_con_empresa_en_el_body_no_reasigna_el_tenant(self):
        for user in (self.user_a, self.superuser):
            client = self._client(user)
            for url, propia in (
                (CATEGORIAS_URL, self.cat_a),
                (PRODUCTOS_URL, self.prod_a),
                (VARIANTES_URL, self.var_a),
            ):
                resp = client.patch(
                    f"{url}{propia.pk}/", {"empresa": self.empresa_b.pk}, format="json",
                )
                self.assertEqual(resp.status_code, 200, (user.email, url, resp.content))
                # ``empresa`` sigue en la respuesta: es un cambio de escritura, no de shape.
                self.assertEqual(resp.json()["empresa"], self.empresa_a.pk)
                propia.refresh_from_db()
                self.assertEqual(propia.empresa_id, self.empresa_a.pk)

    def test_put_con_empresa_en_el_body_no_reasigna_el_tenant(self):
        client = self._client(self.user_a)
        resp = client.put(
            f"{CATEGORIAS_URL}{self.cat_a.pk}/",
            {"nombre": "Playeras A2", "codigo": "PLA", "descripcion": "A", "empresa": self.empresa_b.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.cat_a.refresh_from_db()
        self.assertEqual(self.cat_a.nombre, "Playeras A2")
        self.assertEqual(self.cat_a.empresa_id, self.empresa_a.pk)

    # --- create ----------------------------------------------------------------

    def test_create_categoria_valida_empresa(self):
        client = self._client(self.user_a)
        payload = {"nombre": "Gorras", "codigo": "GOR", "descripcion": "g"}
        ajena = client.post(CATEGORIAS_URL, {**payload, "empresa": self.empresa_b.pk}, format="json")
        self.assertEqual(ajena.status_code, 400)
        self.assertIn("empresa", ajena.json())
        propia = client.post(CATEGORIAS_URL, {**payload, "empresa": self.empresa_a.pk}, format="json")
        self.assertEqual(propia.status_code, 201, propia.content)
        self.assertEqual(propia.json()["empresa"], self.empresa_a.pk)

        su = self._client(self.superuser).post(
            CATEGORIAS_URL, {**payload, "empresa": self.empresa_b.pk}, format="json",
        )
        self.assertEqual(su.status_code, 201, su.content)
        self.assertEqual(su.json()["empresa"], self.empresa_b.pk)

    def test_create_variante_valida_empresa(self):
        client = self._client(self.user_a)
        talla = Talla.objects.create(nombre="G")
        CategoriaProductoTalla.objects.create(categoria_producto=self.cat_a, talla=talla)
        payload = {
            "producto": self.prod_a.pk, "color": self.color.pk, "talla": talla.pk,
            "sku": "PLA00-NEG-G", "precio_base": "100.00",
        }
        ajena = client.post(VARIANTES_URL, {**payload, "empresa": self.empresa_b.pk}, format="json")
        self.assertEqual(ajena.status_code, 400)
        self.assertIn("empresa", ajena.json())
        propia = client.post(VARIANTES_URL, {**payload, "empresa": self.empresa_a.pk}, format="json")
        self.assertEqual(propia.status_code, 201, propia.content)
        self.assertEqual(propia.json()["empresa"], self.empresa_a.pk)

    def test_create_producto_fuerza_la_empresa_del_usuario(self):
        # ``perform_create`` ya inyectaba la empresa; lo que mande el cliente se ignora.
        resp = self._client(self.user_a).post(
            PRODUCTOS_URL, {"nombre": "Gorra", "empresa": self.empresa_b.pk}, format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["empresa"], self.empresa_a.pk)

        su = self._client(self.superuser).post(
            PRODUCTOS_URL, {"nombre": "Gorra", "empresa": self.empresa_b.pk}, format="json",
        )
        self.assertEqual(su.status_code, 201, su.content)
        self.assertEqual(su.json()["empresa"], self.empresa_b.pk)

    # --- onboarding (validación propia, se conserva) ---------------------------

    def test_onboarding_producto(self):
        client = self._client(self.user_a)
        base = {"nombre": "Nueva", "tipo": self.tipo.pk, "precio_base": "50.00"}
        ok = client.post(
            f"{PRODUCTOS_URL}onboarding/", {**base, "categoria_producto": self.cat_a.pk}, format="json",
        )
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(ok.json()["empresa"], self.empresa_a.pk)
        self.assertEqual(ok.json()["codigo"], "PLA02")
        ajena = client.post(
            f"{PRODUCTOS_URL}onboarding/", {**base, "categoria_producto": self.cat_b.pk}, format="json",
        )
        self.assertEqual(ajena.status_code, 400)
        self.assertEqual(ajena.json(), {"categoria_producto": "No pertenece a tu empresa."})

    def test_onboarding_producto_usuario_sin_empresa_es_rechazado(self):
        # Antes la guarda era ``empresa and ...``: sin empresa se saltaba y el
        # producto quedaba en la empresa de la categoria ajena.
        resp = self._client(self.sin_empresa).post(
            f"{PRODUCTOS_URL}onboarding/",
            {"nombre": "Sin empresa", "tipo": self.tipo.pk, "categoria_producto": self.cat_b.pk,
             "precio_base": "50.00"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"categoria_producto": "No pertenece a tu empresa."})
        self.assertFalse(Producto.objects.filter(nombre="Sin empresa").exists())

    def test_siguiente_codigo_valida_la_empresa(self):
        url = f"{PRODUCTOS_URL}siguiente-codigo/"
        ok = self._client(self.user_a).get(url, {"categoria_producto": self.cat_a.pk})
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ok.json(), {"codigo": "PLA02"})
        for user in (self.user_a, self.sin_empresa):
            ajena = self._client(user).get(url, {"categoria_producto": self.cat_b.pk})
            self.assertEqual(ajena.status_code, 400, (user.email, ajena.content))
            self.assertEqual(ajena.json(), {"categoria_producto": "No pertenece a tu empresa."})

    def test_onboarding_variante(self):
        client = self._client(self.user_a)
        color = Color.objects.create(nombre="Blanco", codigo="BLA", codigo_hex="#FFFFFF")
        base = {"color": color.pk, "talla": self.talla.pk, "precio_base": "100.00"}
        ok = client.post(
            f"{VARIANTES_URL}onboarding/", {**base, "producto": self.prod_a.pk}, format="json",
        )
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(ok.json()["sku"], "PLA00-BLA-M")
        self.assertEqual(ok.json()["empresa"], self.empresa_a.pk)
        ajena = client.post(
            f"{VARIANTES_URL}onboarding/", {**base, "producto": self.prod_b.pk}, format="json",
        )
        self.assertEqual(ajena.status_code, 400)
        self.assertEqual(ajena.json(), {"producto": "No pertenece a tu empresa."})

    def test_onboarding_variante_usuario_sin_empresa_es_rechazado(self):
        # Antes la guarda era ``empresa and ...``: sin empresa se saltaba y la
        # variante quedaba en la empresa del producto ajeno.
        color = Color.objects.create(nombre="Blanco", codigo="BLA", codigo_hex="#FFFFFF")
        resp = self._client(self.sin_empresa).post(
            f"{VARIANTES_URL}onboarding/",
            {"producto": self.prod_b.pk, "color": color.pk, "talla": self.talla.pk, "precio_base": "100.00"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"producto": "No pertenece a tu empresa."})
        self.assertFalse(ProductoVariante.objects.filter(sku="PLB00-BLA-M").exists())

    # --- producto de la variante == empresa de la variante ----------------------

    def _payload_variante(self, producto, empresa, sku):
        return {
            "producto": producto.pk, "empresa": empresa.pk, "color": self.color.pk,
            "talla": self.talla.pk, "sku": sku, "precio_base": "100.00",
        }

    def test_create_variante_con_producto_de_otra_empresa_es_rechazado(self):
        resp = self._client(self.user_a).post(
            VARIANTES_URL, self._payload_variante(self.prod_b, self.empresa_a, "X-1"), format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("producto", resp.json())
        self.assertFalse(ProductoVariante.objects.filter(sku="X-1").exists())

    def test_create_variante_superusuario_cruzando_empresas_es_rechazado(self):
        client = self._client(self.superuser)
        resp = client.post(
            VARIANTES_URL, self._payload_variante(self.prod_b, self.empresa_a, "X-2"), format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("producto", resp.json())
        self.assertFalse(ProductoVariante.objects.filter(sku="X-2").exists())
        # Sin cruzar sigue pudiendo crear en cualquier empresa.
        ok = client.post(
            VARIANTES_URL, self._payload_variante(self.prod_b, self.empresa_b, "X-3"), format="json",
        )
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(ok.json()["empresa"], self.empresa_b.pk)

    def test_patch_y_put_variante_a_producto_de_otra_empresa_son_rechazados(self):
        for user in (self.user_a, self.superuser):
            client = self._client(user)
            patch = client.patch(
                f"{VARIANTES_URL}{self.var_a.pk}/", {"producto": self.prod_b.pk}, format="json",
            )
            self.assertEqual(patch.status_code, 400, (user.email, patch.content))
            self.assertIn("producto", patch.json())
            put = client.put(
                f"{VARIANTES_URL}{self.var_a.pk}/",
                self._payload_variante(self.prod_b, self.empresa_b, self.var_a.sku),
                format="json",
            )
            self.assertEqual(put.status_code, 400, (user.email, put.content))
            self.assertIn("producto", put.json())
            self.var_a.refresh_from_db()
            self.assertEqual(self.var_a.producto_id, self.prod_a.pk)
            self.assertEqual(self.var_a.empresa_id, self.empresa_a.pk)

    def test_create_y_update_variante_misma_empresa_siguen_funcionando(self):
        client = self._client(self.user_a)
        creada = client.post(
            VARIANTES_URL, self._payload_variante(self.prod_a, self.empresa_a, "X-4"), format="json",
        )
        self.assertEqual(creada.status_code, 201, creada.content)
        # PATCH sólo con ``producto`` (la empresa se resuelve de la instancia).
        patch = client.patch(
            f"{VARIANTES_URL}{self.var_a.pk}/", {"producto": self.prod_a_otro_tipo.pk}, format="json",
        )
        self.assertEqual(patch.status_code, 200, patch.content)
        self.assertEqual(patch.json()["producto"], self.prod_a_otro_tipo.pk)
        # PATCH sin ``producto`` (se resuelve de la instancia).
        precio = client.patch(
            f"{VARIANTES_URL}{self.var_a.pk}/", {"precio_base": "120.00"}, format="json",
        )
        self.assertEqual(precio.status_code, 200, precio.content)
        put = client.put(
            f"{VARIANTES_URL}{self.var_a.pk}/",
            self._payload_variante(self.prod_a, self.empresa_a, self.var_a.sku),
            format="json",
        )
        self.assertEqual(put.status_code, 200, put.content)

    # --- categoria del producto == empresa del producto --------------------------

    def test_create_producto_con_categoria_de_otra_empresa_es_rechazado(self):
        resp = self._client(self.user_a).post(
            PRODUCTOS_URL, {"nombre": "Cruzado", "categoria_producto": self.cat_b.pk}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("categoria_producto", resp.json())
        self.assertFalse(Producto.objects.filter(nombre="Cruzado").exists())

    def test_create_producto_superusuario_cruzando_empresas_es_rechazado(self):
        client = self._client(self.superuser)
        resp = client.post(
            PRODUCTOS_URL,
            {"nombre": "Cruzado", "empresa": self.empresa_a.pk, "categoria_producto": self.cat_b.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("categoria_producto", resp.json())
        self.assertFalse(Producto.objects.filter(nombre="Cruzado").exists())
        # Sin cruzar sigue pudiendo crear en cualquier empresa.
        ok = client.post(
            PRODUCTOS_URL,
            {"nombre": "Gorra B", "empresa": self.empresa_b.pk, "categoria_producto": self.cat_b.pk},
            format="json",
        )
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(ok.json()["empresa"], self.empresa_b.pk)

    def test_patch_y_put_producto_a_categoria_de_otra_empresa_son_rechazados(self):
        for user in (self.user_a, self.superuser):
            client = self._client(user)
            patch = client.patch(
                f"{PRODUCTOS_URL}{self.prod_a.pk}/", {"categoria_producto": self.cat_b.pk}, format="json",
            )
            self.assertEqual(patch.status_code, 400, (user.email, patch.content))
            self.assertIn("categoria_producto", patch.json())
            put = client.put(
                f"{PRODUCTOS_URL}{self.prod_a.pk}/",
                {"nombre": self.prod_a.nombre, "categoria_producto": self.cat_b.pk},
                format="json",
            )
            self.assertEqual(put.status_code, 400, (user.email, put.content))
            self.assertIn("categoria_producto", put.json())
            self.prod_a.refresh_from_db()
            self.assertEqual(self.prod_a.categoria_producto_id, self.cat_a.pk)

    def test_create_y_update_producto_misma_empresa_siguen_funcionando(self):
        client = self._client(self.user_a)
        cat_a2 = CategoriaProducto.objects.create(
            empresa=self.empresa_a, nombre="Gorras A", codigo="GOA", descripcion="A",
        )
        creado = client.post(
            PRODUCTOS_URL, {"nombre": "Gorra", "categoria_producto": self.cat_a.pk}, format="json",
        )
        self.assertEqual(creado.status_code, 201, creado.content)
        self.assertEqual(creado.json()["empresa"], self.empresa_a.pk)
        # PATCH sólo con ``categoria_producto`` (la empresa se resuelve de la instancia).
        patch = client.patch(
            f"{PRODUCTOS_URL}{self.prod_a.pk}/", {"categoria_producto": cat_a2.pk}, format="json",
        )
        self.assertEqual(patch.status_code, 200, patch.content)
        self.assertEqual(patch.json()["categoria_producto"], cat_a2.pk)
        # PATCH sin ``categoria_producto`` (se resuelve de la instancia).
        nombre = client.patch(f"{PRODUCTOS_URL}{self.prod_a.pk}/", {"nombre": "Polo"}, format="json")
        self.assertEqual(nombre.status_code, 200, nombre.content)
        put = client.put(
            f"{PRODUCTOS_URL}{self.prod_a.pk}/",
            {"nombre": "Polo", "categoria_producto": self.cat_a.pk},
            format="json",
        )
        self.assertEqual(put.status_code, 200, put.content)


class ProductoFiltroTipoMultipleTests(TestCase):
    """``?tipo_id`` acepta varios valores (repetido o separado por comas)."""

    @classmethod
    def setUpTestData(cls):
        cls.empresa_a = Empresa.objects.create(codigo="tip-a", razon_social="A SA")
        cls.empresa_b = Empresa.objects.create(codigo="tip-b", razon_social="B SA")
        # Mismos ids que producción: 1=COMPRAS, 2=MP, 3=PT, 5=SERVICIO.
        tipos = {
            pk: TipoProducto.objects.create(pk=pk, codigo=codigo)
            for pk, codigo in ((1, "COMPRAS"), (2, "MP"), (3, "PT"), (5, "SERVICIO"))
        }
        cls.por_tipo = {
            pk: Producto.objects.create(empresa=cls.empresa_a, tipo=tipo, nombre=f"A {tipo.codigo}")
            for pk, tipo in tipos.items()
        }
        cls.ajenos = [
            Producto.objects.create(empresa=cls.empresa_b, tipo=tipos[pk], nombre=f"B {pk}")
            for pk in (1, 3)
        ]
        cls.user_a = Usuario.objects.create(
            username="a@tip.test", email="a@tip.test", empresa=cls.empresa_a,
        )

    def _get(self, query):
        client = APIClient()
        client.force_authenticate(user=self.user_a)
        return client.get(f"{PRODUCTOS_URL}?{query}")

    def _ids(self, query):
        resp = self._get(query)
        self.assertEqual(resp.status_code, 200, resp.content)
        return {row["id"] for row in resp.json()}

    def test_un_solo_valor_se_comporta_igual(self):
        self.assertEqual(self._ids("tipo_id=3"), {self.por_tipo[3].pk})

    def test_param_repetido_devuelve_exactamente_esos_tipos(self):
        self.assertEqual(
            self._ids("tipo_id=1&tipo_id=3"), {self.por_tipo[1].pk, self.por_tipo[3].pk},
        )

    def test_valor_separado_por_comas(self):
        self.assertEqual(self._ids("tipo_id=1,3"), {self.por_tipo[1].pk, self.por_tipo[3].pk})
        self.assertEqual(self._ids("tipo_id=1,%203"), {self.por_tipo[1].pk, self.por_tipo[3].pk})

    def test_valor_invalido_mezclado_da_400_con_el_mensaje_existente(self):
        for query in ("tipo_id=1&tipo_id=x", "tipo_id=1,x", "tipo_id=1,", "tipo_id="):
            resp = self._get(query)
            self.assertEqual(resp.status_code, 400, query)
            self.assertEqual(resp.json(), {"tipo_id": "Must be an integer."}, query)

    def test_varios_tipos_no_amplian_el_alcance_de_empresa(self):
        ids = self._ids("tipo_id=1&tipo_id=3")
        self.assertTrue(ids.isdisjoint({p.pk for p in self.ajenos}))


class CompatibilidadPrendaProcesoTests(TestCase):
    """EC-572 / EC-575: matriz categoría/proceso/ubicación (regla por categoría) y su CRUD."""

    URL = "/api/v1/catalogo/categoria-producto-proceso/"

    @classmethod
    def setUpTestData(cls):
        from catalogo.models import CategoriaProductoProceso
        cls.Proceso = CategoriaProductoProceso.Proceso
        cls.empresa = Empresa.objects.create(codigo="cmp", razon_social="Cmp SA")
        cls.playera = CategoriaProducto.objects.create(empresa=cls.empresa, nombre="Playera", codigo="PLA", descripcion="p")
        cls.pantalon = CategoriaProducto.objects.create(empresa=cls.empresa, nombre="Pantalón", codigo="PAN", descripcion="p")
        cls.user = Usuario.objects.create(username="cmp", email="cmp@x.test", empresa=cls.empresa, is_admin_empresa=True)
        cls.vendedor = Usuario.objects.create(username="cmp-v", email="cmp-v@x.test", empresa=cls.empresa)
        cls.superuser = Usuario.objects.create(username="cmp-su", email="cmp-su@x.test", is_superuser=True)

    def _talla(self, categoria, **kw):
        from types import SimpleNamespace as NS
        base = dict(lleva_bordado=False, bordado_config=None, lleva_reflejante=False, reflejante_config=None,
                    lleva_corte_manga=False, corte_manga_config=None)
        base.update(kw)
        return NS(pedido_detalle=NS(producto=NS(categoria_producto=categoria)), **base)

    def _regla(self, categoria, proceso, ubicacion=""):
        from catalogo.models import CategoriaProductoProceso
        CategoriaProductoProceso.objects.create(empresa=self.empresa, categoria_producto=categoria, proceso=proceso, ubicacion=ubicacion)

    def _api(self, user=None):
        api = APIClient()
        api.force_authenticate(user=user or self.user)
        return api

    def _alta(self, user=None, **datos):
        return self._api(user).post(self.URL, {
            "empresa": self.empresa.pk, "categoria_producto": self.playera.pk, "proceso": "bordado", **datos,
        }, format="json")

    # --- regla -------------------------------------------------------------
    def test_sin_reglas_no_restringe(self):
        from catalogo.compatibilidad import errores_compatibilidad
        self.assertEqual(errores_compatibilidad(self.empresa.pk, [self._talla(self.pantalon, lleva_corte_manga=True)]), [])

    def test_regla_por_categoria(self):
        """Solo «Playera» está configurada: «Pantalón» queda libre (antes, la primera
        fila de un proceso lo prohibía a las demás categorías)."""
        from catalogo.compatibilidad import errores_compatibilidad
        self._regla(self.playera, self.Proceso.CORTE_MANGA)
        self._regla(self.playera, self.Proceso.REFLEJANTE, "ESPALDA")
        err = errores_compatibilidad(self.empresa.pk, [
            self._talla(self.pantalon, lleva_corte_manga=True, lleva_bordado=True),
            self._talla(self.playera, lleva_corte_manga=True),
            self._talla(self.playera, lleva_reflejante=True, reflejante_config=[{"posicion": "espalda"}]),
            self._talla(self.playera, lleva_reflejante=True, reflejante_config=[{"posicion": "Manga"}]),
            self._talla(self.playera, lleva_bordado=True, bordado_config={"ubicaciones": [{"codigo": "A"}]}),
        ])
        self.assertEqual(err, [
            "La categoría «Playera» no admite Reflejante en «MANGA».",
            "La categoría «Playera» no admite Bordado.",
        ])

    def test_procesos_limita_la_revision(self):
        from catalogo.compatibilidad import errores_compatibilidad
        self._regla(self.playera, self.Proceso.CORTE_MANGA)
        talla = self._talla(self.playera, lleva_corte_manga=True, lleva_bordado=True)
        self.assertEqual(errores_compatibilidad(self.empresa.pk, [talla], procesos={self.Proceso.CORTE_MANGA}), [])
        self.assertEqual(len(errores_compatibilidad(self.empresa.pk, [talla])), 1)

    # --- CRUD, permiso, duplicados ------------------------------------------
    def test_api_crud_con_baja_logica(self):
        api = self._api()
        r = self._alta(ubicacion=" Pecho ")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["ubicacion"], "PECHO")
        pk = r.json()["id"]
        self.assertEqual(len(api.get(self.URL, {"categoria_producto": self.playera.pk}).json()), 1)
        # Un PATCH sin ubicación la conserva.
        r = api.patch(f"{self.URL}{pk}/", {"proceso": "reflejante"}, format="json")
        self.assertEqual((r.status_code, r.json()["ubicacion"]), (200, "PECHO"), r.content)
        self.assertEqual(api.delete(f"{self.URL}{pk}/").status_code, 204)
        self.assertEqual(api.get(self.URL).json(), [])

    def test_escribir_exige_superusuario_o_admin_de_empresa(self):
        self.assertEqual(self._alta(self.vendedor, ubicacion="A").status_code, 403)
        self.assertEqual(self._alta(self.user, ubicacion="A").status_code, 201)
        self.assertEqual(self._alta(self.superuser, ubicacion="B").status_code, 201)
        # Leer sigue abierto a la empresa.
        self.assertEqual(len(self._api(self.vendedor).get(self.URL).json()), 2)
        pk = self._api(self.vendedor).get(self.URL).json()[0]["id"]
        self.assertEqual(self._api(self.vendedor).delete(f"{self.URL}{pk}/").status_code, 403)

    def test_duplicado_y_mayusculas_son_400(self):
        from catalogo.api.serializers import REGLA_DUPLICADA
        self.assertEqual(self._alta(ubicacion="Espalda").status_code, 201)
        for ubicacion in ("Espalda", "espalda", " ESPALDA "):
            r = self._alta(ubicacion=ubicacion)
            self.assertEqual((r.status_code, r.json()), (400, {"non_field_errors": [REGLA_DUPLICADA]}), ubicacion)
        # Otra ubicación o proceso sí entra; y la propia fila se puede editar.
        otra = self._alta(ubicacion="Frente")
        self.assertEqual(otra.status_code, 201)
        r = self._api().patch(f"{self.URL}{otra.json()['id']}/", {"ubicacion": "frente"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def test_integrity_error_de_una_carrera_es_400(self):
        from unittest import mock
        from django.db import IntegrityError
        from catalogo.api.serializers import REGLA_DUPLICADA, CategoriaProductoProcesoSerializer
        # Texto con el que PostgreSQL reporta la violación del unique parcial.
        carrera = IntegrityError(
            'duplicate key value violates unique constraint "uq_categoria_proceso_ubicacion_activo"'
        )
        with mock.patch.object(CategoriaProductoProcesoSerializer, "save", side_effect=carrera):
            r = self._alta(ubicacion="A")
        self.assertEqual((r.status_code, r.json()), (400, {"non_field_errors": [REGLA_DUPLICADA]}))

    def test_otro_integrity_error_no_se_reporta_como_duplicado(self):
        from unittest import mock
        from django.db import IntegrityError
        from catalogo.api.serializers import CategoriaProductoProcesoSerializer
        otro = IntegrityError('null value in column "empresa_id" violates not-null constraint')
        with mock.patch.object(CategoriaProductoProcesoSerializer, "save", side_effect=otro):
            with self.assertRaises(IntegrityError):
                self._alta(ubicacion="A")


class LineaCategoriaTests(TestCase):
    """``CategoriaProducto.linea`` y el filtro ``?linea=`` (#922, #923, #925)."""

    @classmethod
    def setUpTestData(cls):
        cls.empresa_a = Empresa.objects.create(codigo="lin-a", razon_social="A SA")
        cls.empresa_b = Empresa.objects.create(codigo="lin-b", razon_social="B SA")
        cls.industrial = CategoriaProducto.objects.create(
            empresa=cls.empresa_a, nombre="Camisa", codigo="100", descripcion="c", linea="INDUSTRIAL",
        )
        cls.epp = CategoriaProducto.objects.create(
            empresa=cls.empresa_a, nombre="Casco", codigo="500", descripcion="c", linea="EPP",
        )
        cls.telas = CategoriaProducto.objects.create(
            empresa=cls.empresa_a, nombre="TELAS", codigo="900", descripcion="t",
        )
        cls.industrial_b = CategoriaProducto.objects.create(
            empresa=cls.empresa_b, nombre="Camisa B", codigo="100", descripcion="c", linea="INDUSTRIAL",
        )
        cls.prod_industrial = Producto.objects.create(
            empresa=cls.empresa_a, categoria_producto=cls.industrial, nombre="Camisa", codigo="10000",
        )
        cls.prod_epp = Producto.objects.create(
            empresa=cls.empresa_a, categoria_producto=cls.epp, nombre="Casco", codigo="50000",
        )
        cls.prod_tela = Producto.objects.create(
            empresa=cls.empresa_a, categoria_producto=cls.telas, nombre="Tela", codigo="90000",
        )
        cls.prod_sin_categoria = Producto.objects.create(empresa=cls.empresa_a, nombre="Suelto")
        cls.user_a = Usuario.objects.create(
            username="a@lin.test", email="a@lin.test", empresa=cls.empresa_a, is_admin_empresa=True,
        )

    def _get(self, url, query=""):
        client = APIClient()
        client.force_authenticate(user=self.user_a)
        return client.get(f"{url}?{query}")

    def _ids(self, url, query):
        resp = self._get(url, query)
        self.assertEqual(resp.status_code, 200, resp.content)
        return {row["id"] for row in resp.json()}

    def test_cada_categoria_trae_su_linea_y_vacia_es_materia_prima(self):
        lineas = {row["id"]: row["linea"] for row in self._get(CATEGORIAS_URL).json()}
        self.assertEqual(lineas, {self.industrial.pk: "INDUSTRIAL", self.epp.pk: "EPP", self.telas.pk: ""})

    def test_filtra_categorias_y_productos_dentro_de_la_empresa(self):
        self.assertEqual(self._ids(CATEGORIAS_URL, "linea=INDUSTRIAL"), {self.industrial.pk})
        self.assertEqual(self._ids(PRODUCTOS_URL, "linea=INDUSTRIAL"), {self.prod_industrial.pk})

    def test_varios_valores_y_minusculas(self):
        ambas = {self.industrial.pk, self.epp.pk}
        self.assertEqual(self._ids(CATEGORIAS_URL, "linea=INDUSTRIAL&linea=EPP"), ambas)
        self.assertEqual(self._ids(CATEGORIAS_URL, "linea=industrial,epp"), ambas)
        self.assertEqual(self._ids(CATEGORIAS_URL, "linea=INDUSTRIAL,"), {self.industrial.pk})

    def test_sin_linea_pide_la_materia_prima(self):
        self.assertEqual(self._ids(CATEGORIAS_URL, "linea=SIN_LINEA"), {self.telas.pk})
        # En productos también entra el que no tiene categoría.
        self.assertEqual(
            self._ids(PRODUCTOS_URL, "linea=SIN_LINEA"), {self.prod_tela.pk, self.prod_sin_categoria.pk},
        )
        self.assertEqual(
            self._ids(PRODUCTOS_URL, "linea=sin_linea,EPP"),
            {self.prod_tela.pk, self.prod_sin_categoria.pk, self.prod_epp.pk},
        )

    def test_vacio_no_filtra(self):
        self.assertEqual(self._ids(CATEGORIAS_URL, "linea="), {self.industrial.pk, self.epp.pk, self.telas.pk})

    def test_valor_invalido_es_400(self):
        for url in (CATEGORIAS_URL, PRODUCTOS_URL):
            for query in ("linea=FOO", "linea=INDUSTRIAL&linea=FOO"):
                resp = self._get(url, query)
                self.assertEqual(resp.status_code, 400, (url, query))
                self.assertIn("linea", resp.json())

    def test_alta_con_linea_fuera_del_catalogo_es_400(self):
        client = APIClient()
        client.force_authenticate(user=self.user_a)
        resp = client.post(CATEGORIAS_URL, {
            "empresa": self.empresa_a.pk, "nombre": "X", "codigo": "999", "descripcion": "x", "linea": "FOO",
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("linea", resp.json())


class EscrituraCatalogoTests(TestCase):
    """Quién escribe categorías (#921), colores (#914) y tipos (#907-#909), y bajas lógicas (#920)."""

    TIPOS_URL = "/api/v1/catalogo/tipo-producto/"

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="esc", razon_social="Esc SA")
        cls.categoria = CategoriaProducto.objects.create(
            empresa=cls.empresa, nombre="Camisa", codigo="100", descripcion="c",
        )
        cls.producto = Producto.objects.create(
            empresa=cls.empresa, categoria_producto=cls.categoria, nombre="Camisa", codigo="10000",
        )
        cls.color = Color.objects.create(nombre="Negro", codigo="NEG", codigo_hex="#000000")
        cls.tipo_baja = TipoProducto.objects.create(codigo="VIEJO", nombre="Viejo", activo=False)
        cls.vendedor = Usuario.objects.create(username="v@esc.test", email="v@esc.test", empresa=cls.empresa)
        cls.admin = Usuario.objects.create(
            username="a@esc.test", email="a@esc.test", empresa=cls.empresa, is_admin_empresa=True,
        )
        cls.superuser = Usuario.objects.create(username="s@esc.test", email="s@esc.test", is_superuser=True)

    def _api(self, user):
        api = APIClient()
        api.force_authenticate(user=user)
        return api

    def test_categoria_solo_la_escribe_el_admin_de_empresa(self):
        url = f"{CATEGORIAS_URL}{self.categoria.pk}/"
        payload = {"empresa": self.empresa.pk, "nombre": "Gorra", "codigo": "300", "descripcion": "g"}
        vendedor = self._api(self.vendedor)
        self.assertEqual(vendedor.get(url).status_code, 200)
        self.assertEqual(vendedor.post(CATEGORIAS_URL, payload, format="json").status_code, 403)
        self.assertEqual(vendedor.patch(url, {"linea": "OTROS"}, format="json").status_code, 403)
        self.assertEqual(vendedor.delete(url).status_code, 403)
        self.assertEqual(self._api(self.admin).post(CATEGORIAS_URL, payload, format="json").status_code, 201)
        self.assertEqual(self._api(self.admin).patch(url, {"linea": "OTROS"}, format="json").status_code, 200)

    def test_delete_de_categoria_es_baja_logica_y_no_toca_sus_productos(self):
        api = self._api(self.admin)
        self.assertEqual(api.delete(f"{CATEGORIAS_URL}{self.categoria.pk}/").status_code, 204)
        self.categoria.refresh_from_db()
        self.assertFalse(self.categoria.activo)
        self.assertTrue(Producto.objects.filter(pk=self.producto.pk, activo=True).exists())
        self.assertEqual(api.get(CATEGORIAS_URL).json(), [])

    def test_color_solo_lo_escribe_el_superusuario_y_delete_es_baja_logica(self):
        url = f"{COLORES_URL}{self.color.pk}/"
        alta = {"nombre": "Rojo", "codigo": "ROJ", "codigo_hex": "#FF0000"}
        self.assertEqual(self._api(self.admin).post(COLORES_URL, alta, format="json").status_code, 403)
        self.assertEqual(self._api(self.admin).delete(url).status_code, 403)
        self.assertEqual(self._api(self.superuser).post(COLORES_URL, alta, format="json").status_code, 201)
        self.assertEqual(self._api(self.superuser).delete(url).status_code, 204)
        self.color.refresh_from_db()
        self.assertFalse(self.color.activo)

    def test_tipos_inactivos_se_listan_y_se_reactivan(self):
        api = self._api(self.superuser)
        self.assertNotIn(self.tipo_baja.pk, {t["id"] for t in api.get(self.TIPOS_URL).json()})
        self.assertEqual([t["id"] for t in api.get(f"{self.TIPOS_URL}?activo=false").json()], [self.tipo_baja.pk])
        self.assertEqual(api.get(f"{self.TIPOS_URL}{self.tipo_baja.pk}/").status_code, 200)
        resp = api.patch(f"{self.TIPOS_URL}{self.tipo_baja.pk}/", {"activo": True}, format="json")
        self.assertEqual((resp.status_code, resp.json()["activo"]), (200, True), resp.content)

    def test_alta_de_tipo_exige_nombre_y_nace_activa(self):
        api = self._api(self.superuser)
        for payload in ({"codigo": "SIN"}, {"codigo": "SIN", "nombre": ""}):
            resp = api.post(self.TIPOS_URL, payload, format="json")
            self.assertEqual(resp.status_code, 400, payload)
            self.assertIn("nombre", resp.json())
        resp = api.post(self.TIPOS_URL, {"codigo": "NUEVO", "nombre": "Nuevo", "activo": False}, format="json")
        self.assertEqual((resp.status_code, resp.json()["activo"]), (201, True), resp.content)


SELECTOR_PRODUCTOS_URL = PRODUCTOS_URL + "selector/"
SELECTOR_VARIANTES_URL = VARIANTES_URL + "selector/"

# Forma de los listados antes de EC-886: no cambia con paginación ni selector.
CAMPOS_LISTA_PRODUCTO = {
    "id", "unidad_id", "unidad_nombre", "unidad_abreviatura", "nombre", "descripcion", "activo",
    "created_at", "updated_at", "precio_base", "cod_proscai", "codigo", "es_compras", "empresa",
    "categoria_producto", "unidad_medida", "impuesto", "sat_prodserv", "sat_unidad", "tipo",
}
CAMPOS_LISTA_VARIANTE = {
    "id", "producto_nombre", "color_nombre", "talla_nombre", "cod_proscai", "sku_produccion",
    "nombre", "sku", "precio_base", "activo", "producto", "empresa", "color", "talla",
}


class CatalogoSelectorBase(TestCase):
    """Datos de EC-886: dos empresas, productos y variantes activos e inactivos, BOM
    activo, inactivo y de otra empresa."""

    @classmethod
    def setUpTestData(cls):
        from nucleo.models import UnidadMedida

        cls.empresa_a = Empresa.objects.create(codigo="sel-a", razon_social="Sel A SA")
        cls.empresa_b = Empresa.objects.create(codigo="sel-b", razon_social="Sel B SA")
        cls.tipo_pt = TipoProducto.objects.create(codigo="PT")
        cls.tipo_mp = TipoProducto.objects.create(codigo="MP")
        cls.unidad = UnidadMedida.objects.create(clave="MTR-S", nombre="Metro")
        cls.color = Color.objects.create(nombre="Rojo", codigo="ROJ", codigo_hex="#ff0000")
        cls.talla = Talla.objects.create(nombre="G")
        cls.cat_a = CategoriaProducto.objects.create(empresa=cls.empresa_a, nombre="Cat A", codigo="CSA")

        def producto(empresa, nombre, codigo, **extra):
            return Producto.objects.create(
                empresa=empresa, nombre=nombre, codigo=codigo, precio_base="10.00", **extra,
            )

        cls.p_activo = producto(
            cls.empresa_a, "Tela algodón", "TA001", tipo=cls.tipo_mp, categoria_producto=cls.cat_a,
            unidad_medida=cls.unidad, descripcion="peinada 30/1",
        )
        cls.p_otro = producto(cls.empresa_a, "Playera", "PL001", tipo=cls.tipo_pt)
        cls.p_inactivo = producto(cls.empresa_a, "Tela vieja", "TV001", tipo=cls.tipo_mp, activo=False)
        cls.p_b = producto(cls.empresa_b, "Tela B", "TB001", tipo=cls.tipo_mp)

        def variante(prod, sku, **extra):
            return ProductoVariante.objects.create(
                producto=prod, empresa=prod.empresa, color=cls.color, talla=cls.talla,
                sku=sku, precio_base="10.00", **extra,
            )

        cls.v_con_bom = variante(cls.p_otro, "PL001-ROJ-G")
        cls.v_sin_bom = variante(cls.p_activo, "TA001-ROJ-G")
        cls.v_bom_inactivo = variante(cls.p_otro, "PL001-ROJ-X")
        cls.v_bom_ajeno = variante(cls.p_otro, "PL001-ROJ-Y")
        cls.v_inactiva = variante(cls.p_otro, "PL001-ROJ-Z", activo=False)
        cls.v_de_producto_inactivo = variante(cls.p_inactivo, "TV001-ROJ-G")
        cls.v_b = variante(cls.p_b, "TB001-ROJ-G")
        ListaMaterialBom.objects.create(empresa=cls.empresa_a, producto_variante=cls.v_con_bom)
        ListaMaterialBom.objects.create(empresa=cls.empresa_a, producto_variante=cls.v_bom_inactivo, activo=False)
        # BOM de otra empresa sobre una variante de A: no cuenta.
        ListaMaterialBom.objects.create(empresa=cls.empresa_b, producto_variante=cls.v_bom_ajeno)

        cls.user_a = Usuario.objects.create(username="sa@sel.test", email="sa@sel.test", empresa=cls.empresa_a)
        cls.user_b = Usuario.objects.create(username="sb@sel.test", email="sb@sel.test", empresa=cls.empresa_b)
        cls.sin_empresa = Usuario.objects.create(username="sn@sel.test", email="sn@sel.test")
        cls.superuser = Usuario.objects.create(
            username="ss@sel.test", email="ss@sel.test", is_superuser=True, is_staff=True, empresa=cls.empresa_a,
        )

    def _get(self, url, user=None, **params):
        client = APIClient()
        client.force_authenticate(user=user or self.user_a)
        return client.get(url, params)

    def _ok(self, url, user=None, **params):
        resp = self._get(url, user, **params)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def _ids(self, data):
        filas = data["results"] if isinstance(data, dict) else data
        return [fila["id"] for fila in filas]


class ListadosCatalogoPaginacionOrdenTests(CatalogoSelectorBase):
    """``producto/`` y ``producto-variante/``: sin parámetros, la misma lista de
    siempre; con ``limit``/``offset``, sobre paginado; ``ordering`` en lista blanca."""

    def test_sin_parametros_lista_plana_con_los_mismos_campos(self):
        for url, campos in ((PRODUCTOS_URL, CAMPOS_LISTA_PRODUCTO), (VARIANTES_URL, CAMPOS_LISTA_VARIANTE)):
            with self.subTest(url=url):
                data = self._ok(url)
                self.assertIsInstance(data, list)
                self.assertTrue(data)
                for fila in data:
                    self.assertEqual(set(fila), campos)

    def test_con_limit_regresa_sobre_paginado(self):
        for url in (PRODUCTOS_URL, VARIANTES_URL):
            with self.subTest(url=url):
                todos = self._ids(self._ok(url))
                data = self._ok(url, limit=2, offset=1)
                self.assertEqual(set(data), {"count", "next", "previous", "results"})
                self.assertEqual(data["count"], len(todos))
                self.assertEqual(self._ids(data), todos[1:3])

    def test_limit_u_offset_invalidos_son_400(self):
        for url in (PRODUCTOS_URL, VARIANTES_URL):
            for params in ({"limit": "abc"}, {"limit": "0"}, {"limit": "-1"}, {"limit": "2", "offset": "-1"}):
                with self.subTest(url=url, params=params):
                    self.assertEqual(self._get(url, **params).status_code, 400)

    def test_variantes_salen_por_id_descendente(self):
        ids = self._ids(self._ok(VARIANTES_URL))
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_productos_conservan_su_orden(self):
        esperado = list(
            Producto.objects.filter(empresa=self.empresa_a, activo=True)
            .order_by("-created_at", "-id").values_list("id", flat=True)
        )
        self.assertEqual(self._ids(self._ok(PRODUCTOS_URL)), esperado)

    def test_ordering_desempata_por_id(self):
        # Todos los ``precio_base`` son iguales: decide el ``id``, en la dirección del campo.
        for url in (PRODUCTOS_URL, VARIANTES_URL):
            with self.subTest(url=url):
                asc = self._ids(self._ok(url, ordering="precio_base"))
                self.assertEqual(asc, sorted(asc))
                desc = self._ids(self._ok(url, ordering="-precio_base"))
                self.assertEqual(desc, sorted(desc, reverse=True))

    def test_ordering_fuera_de_la_lista_blanca_se_ignora(self):
        variantes = self._ids(self._ok(VARIANTES_URL))
        self.assertEqual(self._ids(self._ok(VARIANTES_URL, ordering="empresa")), variantes)
        productos = self._ids(self._ok(PRODUCTOS_URL))
        self.assertEqual(self._ids(self._ok(PRODUCTOS_URL, ordering="sat_prodserv")), productos)

    def test_ordering_por_campo_de_la_lista_blanca(self):
        skus = [f["sku"] for f in self._ok(VARIANTES_URL, ordering="sku")]
        self.assertEqual(skus, sorted(skus))

    def test_q_del_listado_de_productos_no_busca_en_descripcion(self):
        self.assertEqual(self._ok(PRODUCTOS_URL, q="peinada"), [])

    def test_aislamiento_del_listado_con_y_sin_paginar(self):
        casos = (
            (PRODUCTOS_URL, {self.p_activo.pk, self.p_otro.pk}),
            (VARIANTES_URL, {
                self.v_con_bom.pk, self.v_sin_bom.pk, self.v_bom_inactivo.pk,
                self.v_bom_ajeno.pk, self.v_inactiva.pk, self.v_de_producto_inactivo.pk,
            }),
        )
        for url, propios in casos:
            with self.subTest(url=url):
                for user in (self.user_a, self.superuser):
                    self.assertEqual(set(self._ids(self._ok(url, user))), propios)
                    self.assertEqual(set(self._ids(self._ok(url, user, limit=500))), propios)
                self.assertEqual(self._ok(url, self.sin_empresa), [])
                self.assertEqual(self._ok(url, self.sin_empresa, limit=10)["results"], [])


class SelectorVariantesTests(CatalogoSelectorBase):

    CAMPOS = {"id", "sku", "nombre", "cod_proscai", "sku_produccion", "tiene_bom", "activo"}

    def test_siempre_paginado_con_campos_ligeros(self):
        data = self._ok(SELECTOR_VARIANTES_URL)
        self.assertEqual(set(data), {"count", "next", "previous", "results"})
        for fila in data["results"]:
            self.assertEqual(set(fila), self.CAMPOS)

    def test_solo_activas_de_productos_activos_por_id_descendente(self):
        esperado = sorted(
            [self.v_con_bom.pk, self.v_sin_bom.pk, self.v_bom_inactivo.pk, self.v_bom_ajeno.pk], reverse=True,
        )
        self.assertEqual(self._ids(self._ok(SELECTOR_VARIANTES_URL)), esperado)

    def test_tiene_bom_solo_con_bom_activo_de_la_empresa(self):
        filas = {f["id"]: f["tiene_bom"] for f in self._ok(SELECTOR_VARIANTES_URL)["results"]}
        self.assertEqual(filas, {
            self.v_con_bom.pk: True,
            self.v_sin_bom.pk: False,
            self.v_bom_inactivo.pk: False,
            self.v_bom_ajeno.pk: False,
        })

    def test_filtro_tiene_bom(self):
        self.assertEqual(self._ids(self._ok(SELECTOR_VARIANTES_URL, tiene_bom="true")), [self.v_con_bom.pk])
        self.assertNotIn(self.v_con_bom.pk, self._ids(self._ok(SELECTOR_VARIANTES_URL, tiene_bom="0")))
        self.assertEqual(self._get(SELECTOR_VARIANTES_URL, tiene_bom="si").status_code, 400)

    def test_respeta_q_con_bom_y_ordering(self):
        self.assertEqual(self._ids(self._ok(SELECTOR_VARIANTES_URL, q="TA001")), [self.v_sin_bom.pk])
        self.assertEqual(self._ids(self._ok(SELECTOR_VARIANTES_URL, con_bom="true")), [self.v_con_bom.pk])
        skus = [f["sku"] for f in self._ok(SELECTOR_VARIANTES_URL, ordering="sku")["results"]]
        self.assertEqual(skus, sorted(skus))

    def test_ids_regresa_inactivas_pero_no_ajenas(self):
        ids = f"{self.v_inactiva.pk},{self.v_de_producto_inactivo.pk},{self.v_b.pk}"
        data = self._ok(SELECTOR_VARIANTES_URL, ids=ids)
        self.assertEqual(set(self._ids(data)), {self.v_inactiva.pk, self.v_de_producto_inactivo.pk})
        self.assertFalse({f["id"]: f["activo"] for f in data["results"]}[self.v_inactiva.pk])

    def test_ids_invalidos_son_400(self):
        for valor in ("1,abc", "1,,2", "0", "-3", "1.5", ",".join(["1"] * 501)):
            with self.subTest(ids=valor[:20]):
                resp = self._get(SELECTOR_VARIANTES_URL, ids=valor)
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertIn("ids", resp.json())

    def test_aislamiento(self):
        self.assertEqual(self._ids(self._ok(SELECTOR_VARIANTES_URL, self.user_b)), [self.v_b.pk])
        self.assertEqual(
            set(self._ids(self._ok(SELECTOR_VARIANTES_URL, self.superuser))),
            set(self._ids(self._ok(SELECTOR_VARIANTES_URL, self.user_a))),
        )
        self.assertEqual(self._ok(SELECTOR_VARIANTES_URL, self.sin_empresa)["results"], [])
        self.assertEqual(self._ok(SELECTOR_VARIANTES_URL, self.user_b, ids=str(self.v_con_bom.pk))["results"], [])

    def test_consultas_constantes(self):
        # ``count`` + página, sin importar cuántas filas.
        client = APIClient()
        client.force_authenticate(user=self.user_a)
        with self.assertNumQueries(2):
            self.assertEqual(client.get(SELECTOR_VARIANTES_URL).status_code, 200)
        ProductoVariante.objects.bulk_create([
            ProductoVariante(
                producto=self.p_otro, empresa=self.empresa_a, color=self.color, sku=f"MAS-{i}", precio_base="1.00",
            )
            for i in range(30)
        ])
        with self.assertNumQueries(2):
            self.assertEqual(client.get(SELECTOR_VARIANTES_URL).status_code, 200)

    def test_detalle_de_op_sin_tiene_bom(self):
        from produccion.api.serializers import OrdenProduccionDetalleSerializer

        anidado = OrdenProduccionDetalleSerializer().fields["producto_variante"]
        self.assertNotIn("tiene_bom", anidado.fields)
        self.assertEqual(set(anidado.to_representation(self.v_con_bom)), CAMPOS_LISTA_VARIANTE)


class SelectorProductosTests(CatalogoSelectorBase):

    CAMPOS = {
        "id", "nombre", "codigo", "descripcion", "activo", "unidad_id", "unidad_nombre",
        "unidad_abreviatura", "tipo", "categoria_producto",
    }

    def test_siempre_paginado_con_campos_ligeros(self):
        data = self._ok(SELECTOR_PRODUCTOS_URL)
        self.assertEqual(set(data), {"count", "next", "previous", "results"})
        fila = {f["id"]: f for f in data["results"]}[self.p_activo.pk]
        self.assertEqual(set(fila), self.CAMPOS)
        self.assertEqual(
            (fila["unidad_id"], fila["unidad_nombre"], fila["unidad_abreviatura"], fila["tipo"], fila["categoria_producto"]),
            (self.unidad.pk, "Metro", "MTR-S", self.tipo_mp.pk, self.cat_a.pk),
        )

    def test_solo_activos_y_filtros_del_listado(self):
        self.assertEqual(set(self._ids(self._ok(SELECTOR_PRODUCTOS_URL))), {self.p_activo.pk, self.p_otro.pk})
        self.assertEqual(self._ids(self._ok(SELECTOR_PRODUCTOS_URL, tipo_id=self.tipo_pt.pk)), [self.p_otro.pk])
        nombres = [f["nombre"] for f in self._ok(SELECTOR_PRODUCTOS_URL, ordering="nombre")["results"]]
        self.assertEqual(nombres, sorted(nombres))

    def test_q_busca_tambien_en_descripcion(self):
        self.assertEqual(self._ids(self._ok(SELECTOR_PRODUCTOS_URL, q="peinada")), [self.p_activo.pk])
        self.assertEqual(self._ids(self._ok(SELECTOR_PRODUCTOS_URL, q="PL001")), [self.p_otro.pk])

    def test_ids_regresa_inactivos_pero_no_ajenos(self):
        data = self._ok(SELECTOR_PRODUCTOS_URL, ids=f"{self.p_inactivo.pk},{self.p_b.pk},{self.p_otro.pk}")
        self.assertEqual(set(self._ids(data)), {self.p_inactivo.pk, self.p_otro.pk})
        # El listado sigue sin inactivos.
        self.assertNotIn(self.p_inactivo.pk, self._ids(self._ok(PRODUCTOS_URL)))

    def test_ids_invalidos_son_400(self):
        for valor in ("x", "1,,2", "0", ",".join(["2"] * 501)):
            with self.subTest(ids=valor[:20]):
                self.assertEqual(self._get(SELECTOR_PRODUCTOS_URL, ids=valor).status_code, 400)

    def test_aislamiento(self):
        self.assertEqual(self._ids(self._ok(SELECTOR_PRODUCTOS_URL, self.user_b)), [self.p_b.pk])
        self.assertEqual(
            set(self._ids(self._ok(SELECTOR_PRODUCTOS_URL, self.superuser))), {self.p_activo.pk, self.p_otro.pk},
        )
        self.assertEqual(self._ok(SELECTOR_PRODUCTOS_URL, self.sin_empresa)["results"], [])

    def test_consultas_constantes(self):
        client = APIClient()
        client.force_authenticate(user=self.user_a)
        with self.assertNumQueries(2):
            self.assertEqual(client.get(SELECTOR_PRODUCTOS_URL).status_code, 200)
        Producto.objects.bulk_create([
            Producto(empresa=self.empresa_a, nombre=f"Más {i}", codigo=f"M{i:03d}", unidad_medida=self.unidad)
            for i in range(30)
        ])
        with self.assertNumQueries(2):
            self.assertEqual(client.get(SELECTOR_PRODUCTOS_URL).status_code, 200)


class SelectorTopesTests(TestCase):
    """``limit`` por defecto (50) y tope (500) del selector; tope del listado."""

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="sel-tope", razon_social="Tope SA")
        color = Color.objects.create(nombre="Azul", codigo="AZT", codigo_hex="#0000ff")
        producto = Producto.objects.create(empresa=cls.empresa, nombre="Base", codigo="BT001")
        Producto.objects.bulk_create([
            Producto(empresa=cls.empresa, nombre=f"P{i}", codigo=f"T{i:03d}") for i in range(520)
        ])
        ProductoVariante.objects.bulk_create([
            ProductoVariante(producto=producto, empresa=cls.empresa, color=color, sku=f"BT-{i}", precio_base="1.00")
            for i in range(520)
        ])
        cls.user = Usuario.objects.create(username="t@sel.test", email="t@sel.test", empresa=cls.empresa)

    def test_limit_por_defecto_y_tope(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        for url in (SELECTOR_PRODUCTOS_URL, SELECTOR_VARIANTES_URL):
            with self.subTest(url=url):
                data = client.get(url).json()
                self.assertEqual(len(data["results"]), 50)
                self.assertGreater(data["count"], 500)
                self.assertEqual(len(client.get(url, {"limit": 1000}).json()["results"]), 500)
        for url in (PRODUCTOS_URL, VARIANTES_URL):
            with self.subTest(url=url):
                self.assertGreater(len(client.get(url).json()), 500)
                self.assertEqual(len(client.get(url, {"limit": 1000}).json()["results"]), 500)
