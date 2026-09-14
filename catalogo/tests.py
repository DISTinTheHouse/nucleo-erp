import random

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from catalogo.models import Talla
from catalogo.tallas import talla_sort_key
from usuarios.models import Usuario

TALLAS_URL = "/api/v1/catalogo/talla/"

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
