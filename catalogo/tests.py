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

        cls.user_a = Usuario.objects.create(
            username="a@cat.test", email="a@cat.test", empresa=cls.empresa_a,
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
