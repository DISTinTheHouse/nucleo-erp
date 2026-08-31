"""Tests del buscador global federado (``GET /api/v1/search/``).

Lo que se cubre: aislamiento multi-tenant por entidad (que es lo único que separa
este endpoint de una fuga cross-tenant), la longitud mínima de ``q``, el tope de
``limit``, y que cada entidad conserve EXACTAMENTE el alcance de su propio ViewSet
—incluidos los scopes extra por ``vendedor``/``vendedores`` que no todas tienen—.

Ejecutar SIEMPRE con una BD desechable; el ``.env`` del repo apunta a Supabase de
producción. Ejemplo con un settings de override a SQLite en memoria:

    python manage.py test nucleo --settings=sqlite_settings
"""

from django.test import TestCase
from rest_framework.test import APIClient

from nucleo.models import Empresa, Moneda, Sucursal
from terceros.models import Cliente
from usuarios.models import Usuario
from ventas.models import Cotizacion, Pedido

SEARCH_URL = "/api/v1/search/"
COTIZACIONES_URL = "/api/v1/ventas/cotizaciones/"


class BusquedaGlobalBaseTestCase(TestCase):
    """Dos empresas cuyos datos comparten el término buscado ("acme").

    Es deliberado: si el aislamiento fallara, buscar "acme" traería filas de ambas
    y los tests lo verían.
    """

    @classmethod
    def _tenant(cls, codigo, sufijo_folio, etiqueta):
        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        sucursal = Sucursal.objects.create(
            empresa=empresa, codigo=codigo[:3].upper(), nombre=codigo
        )
        cliente = Cliente.objects.create(
            empresa=empresa,
            nombre=f"Comercial Acme {etiqueta}",
            razon_social=f"COMERCIAL ACME {etiqueta} SA DE CV",
            correo=f"ventas@acme-{etiqueta.lower()}.test",
        )
        admin = Usuario.objects.create(
            username=f"admin-{codigo}",
            email=f"admin@{codigo}.test",
            empresa=empresa,
            sucursal_default=sucursal,
            is_admin_empresa=True,
        )
        pedido = Pedido.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            cliente=cliente,
            moneda=cls.moneda,
            folio=f"P-{sufijo_folio}",
            cliente_nombre=cliente.nombre,
            cliente_razon_social=cliente.razon_social,
            persona_pagos="Pagos",
            correo_facturas=f"pagos@{codigo}.test",
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )
        cotizacion = Cotizacion.objects.create(
            empresa=empresa, sucursal=sucursal, cliente=cliente, vendedor=admin
        )
        return {
            "empresa": empresa,
            "sucursal": sucursal,
            "cliente": cliente,
            "admin": admin,
            "pedido": pedido,
            "cotizacion": cotizacion,
        }

    @classmethod
    def setUpTestData(cls):
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.a = cls._tenant("acme-search", "00027", "Norte")
        cls.b = cls._tenant("globex-search", "00028", "Sur")

        cls.sin_empresa = Usuario.objects.create(
            username="huerfano-search", email="huerfano@nowhere-search.test"
        )
        cls.superuser = Usuario.objects.create(
            username="root-search", email="root@nowhere-search.test", is_superuser=True
        )
        # Usuario normal de la empresa A: NO es admin, NO está en
        # ``cliente.vendedores`` y NO es vendedor de la cotización. Debe ver el
        # pedido (Pedido no tiene scope por vendedor) pero ni el cliente ni la
        # cotización.
        cls.vendedor_a = Usuario.objects.create(
            username="vendedor-a-search",
            email="vendedor@acme-search.test",
            empresa=cls.a["empresa"],
            sucursal_default=cls.a["sucursal"],
        )

    def _buscar(self, user, q, **params):
        client = APIClient()
        client.force_authenticate(user=user)
        query = f"?q={q}" + "".join(f"&{k}={v}" for k, v in params.items())
        resp = client.get(f"{SEARCH_URL}{query}")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def _ids(self, payload, tipo):
        for grupo in payload["grupos"]:
            if grupo["tipo"] == tipo:
                return [fila["id"] for fila in grupo["resultados"]]
        self.fail(f"no vino el grupo {tipo}")


class AislamientoMultiTenantTests(BusquedaGlobalBaseTestCase):
    """Lo crítico: la empresa A nunca ve filas de la empresa B."""

    def test_pedido_de_otra_empresa_no_aparece(self):
        payload = self._buscar(self.a["admin"], "acme")
        self.assertEqual(self._ids(payload, "pedido"), [self.a["pedido"].pk])

    def test_cliente_de_otra_empresa_no_aparece(self):
        payload = self._buscar(self.a["admin"], "acme")
        self.assertEqual(self._ids(payload, "cliente"), [self.a["cliente"].pk])

    def test_cotizacion_de_otra_empresa_no_aparece(self):
        payload = self._buscar(self.a["admin"], "acme")
        self.assertEqual(self._ids(payload, "cotizacion"), [self.a["cotizacion"].pk])

    def test_folio_de_otra_empresa_no_aparece_por_prefijo(self):
        """``P-000`` es prefijo de los folios de AMBAS empresas."""
        payload = self._buscar(self.a["admin"], "P-000")
        self.assertEqual(self._ids(payload, "pedido"), [self.a["pedido"].pk])

    def test_usuario_sin_empresa_no_ve_nada(self):
        payload = self._buscar(self.sin_empresa, "acme")
        for grupo in payload["grupos"]:
            self.assertEqual(grupo["resultados"], [], grupo["tipo"])

    def test_usuario_no_autenticado_es_rechazado(self):
        resp = APIClient().get(f"{SEARCH_URL}?q=acme")
        self.assertIn(resp.status_code, (401, 403))


class AlcancePorEntidadTests(BusquedaGlobalBaseTestCase):
    """Cada entidad conserva el alcance de SU ViewSet, no uno inventado aquí."""

    def test_superuser_ve_las_dos_empresas(self):
        payload = self._buscar(self.superuser, "acme")
        self.assertEqual(
            sorted(self._ids(payload, "pedido")),
            sorted([self.a["pedido"].pk, self.b["pedido"].pk]),
        )
        self.assertEqual(
            sorted(self._ids(payload, "cliente")),
            sorted([self.a["cliente"].pk, self.b["cliente"].pk]),
        )
        self.assertEqual(
            sorted(self._ids(payload, "cotizacion")),
            sorted([self.a["cotizacion"].pk, self.b["cotizacion"].pk]),
        )

    def test_vendedor_no_admin_ve_el_pedido_pero_no_el_cliente_ni_la_cotizacion(self):
        """``Pedido`` no tiene scope por vendedor; ``Cliente`` y ``Cotizacion`` sí."""
        payload = self._buscar(self.vendedor_a, "acme")
        self.assertEqual(self._ids(payload, "pedido"), [self.a["pedido"].pk])
        self.assertEqual(self._ids(payload, "cliente"), [])
        self.assertEqual(self._ids(payload, "cotizacion"), [])

    def test_vendedor_asignado_si_ve_su_cliente_y_su_cotizacion(self):
        self.a["cliente"].vendedores.add(self.vendedor_a)
        self.a["cotizacion"].vendedor = self.vendedor_a
        self.a["cotizacion"].save(update_fields=["vendedor"])

        payload = self._buscar(self.vendedor_a, "acme")
        self.assertEqual(self._ids(payload, "cliente"), [self.a["cliente"].pk])
        self.assertEqual(self._ids(payload, "cotizacion"), [self.a["cotizacion"].pk])

    def test_pedido_con_soft_delete_no_aparece(self):
        self.a["pedido"].soft_delete()
        payload = self._buscar(self.a["admin"], "acme")
        self.assertEqual(self._ids(payload, "pedido"), [])


class CoincidenciaYFormaTests(BusquedaGlobalBaseTestCase):
    def test_q_corta_devuelve_grupos_vacios_no_error(self):
        payload = self._buscar(self.a["admin"], "a")
        self.assertEqual([g["tipo"] for g in payload["grupos"]], ["pedido", "cliente", "cotizacion"])
        for grupo in payload["grupos"]:
            self.assertEqual(grupo["resultados"], [], grupo["tipo"])
            self.assertFalse(grupo["hay_mas"])

    def test_q_ausente_devuelve_grupos_vacios_no_error(self):
        client = APIClient()
        client.force_authenticate(user=self.a["admin"])
        resp = client.get(SEARCH_URL)
        self.assertEqual(resp.status_code, 200)
        for grupo in resp.json()["grupos"]:
            self.assertEqual(grupo["resultados"], [])

    def test_codigo_hace_prefijo_no_subcadena(self):
        """``folio`` es CÓDIGO: ``00027`` NO debe encontrar ``P-00027``."""
        payload = self._buscar(self.a["admin"], "00027")
        self.assertEqual(self._ids(payload, "pedido"), [])
        # El mismo folio, buscado por su prefijo real, sí aparece.
        payload = self._buscar(self.a["admin"], "p-00027")
        self.assertEqual(self._ids(payload, "pedido"), [self.a["pedido"].pk])

    def test_q_de_2_caracteres_solo_consulta_campos_codigo(self):
        """Con 2 caracteres pg_trgm no puede usar el índice: sólo va por CÓDIGO.

        Y lo importante: los grupos sin campos CÓDIGO deben quedar VACÍOS, no
        devolver la tabla entera. Un ``Q()`` vacío no filtra nada.
        """
        payload = self._buscar(self.a["admin"], "P-")
        self.assertEqual(self._ids(payload, "pedido"), [self.a["pedido"].pk])
        self.assertEqual(self._ids(payload, "cliente"), [])
        self.assertEqual(self._ids(payload, "cotizacion"), [])

    def test_q_de_2_caracteres_que_no_es_folio_no_devuelve_nada(self):
        """"Ac" está en el nombre del cliente, pero 2 caracteres no van a NOMBRE."""
        payload = self._buscar(self.a["admin"], "Ac")
        for grupo in payload["grupos"]:
            self.assertEqual(grupo["resultados"], [], grupo["tipo"])

    def test_a_partir_de_3_caracteres_si_entra_el_campo_nombre(self):
        payload = self._buscar(self.a["admin"], "Acm")
        self.assertEqual(self._ids(payload, "cliente"), [self.a["cliente"].pk])

    def test_nombre_hace_subcadena(self):
        """Los campos NOMBRE sí son subcadena: "Acme" está en medio de la razón social."""
        payload = self._buscar(self.a["admin"], "ACME NORTE")
        self.assertEqual(self._ids(payload, "cliente"), [self.a["cliente"].pk])

    def test_cliente_se_encuentra_por_correo(self):
        payload = self._buscar(self.a["admin"], "acme-norte.test")
        self.assertEqual(self._ids(payload, "cliente"), [self.a["cliente"].pk])

    def test_fila_lleva_lo_necesario_para_navegar(self):
        payload = self._buscar(self.a["admin"], "acme")
        fila = next(g for g in payload["grupos"] if g["tipo"] == "pedido")["resultados"][0]
        self.assertEqual(
            set(fila), {"tipo", "id", "codigo", "titulo", "subtitulo", "estatus"}
        )
        self.assertEqual(fila["codigo"], "P-00027")
        self.assertEqual(fila["subtitulo"], self.a["cliente"].razon_social)
        # Nada de URLs en el payload: el frontend arma la ruta con el id/folio.
        self.assertNotIn("url", fila)

    def test_pedido_sin_folio_no_deja_el_titulo_vacio(self):
        """``Pedido.folio`` es NULL-able (un BORRADOR aún no pasó por el folio)."""
        self.a["pedido"].folio = None
        self.a["pedido"].save(update_fields=["folio"])

        payload = self._buscar(self.a["admin"], "acme")
        fila = next(g for g in payload["grupos"] if g["tipo"] == "pedido")["resultados"][0]
        self.assertIsNone(fila["codigo"])
        self.assertEqual(fila["titulo"], self.a["cliente"].razon_social)

    def test_respuesta_expone_el_umbral_real_de_los_campos_nombre(self):
        payload = self._buscar(self.a["admin"], "acme")
        self.assertEqual(payload["longitud_minima"], 2)
        self.assertEqual(payload["longitud_minima_nombre"], 3)

    def test_cotizacion_no_inventa_codigo(self):
        payload = self._buscar(self.a["admin"], "acme")
        fila = next(g for g in payload["grupos"] if g["tipo"] == "cotizacion")["resultados"][0]
        self.assertIsNone(fila["codigo"])
        self.assertEqual(fila["id"], self.a["cotizacion"].pk)


class LimitTests(BusquedaGlobalBaseTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for i in range(8):
            Cliente.objects.create(
                empresa=cls.a["empresa"],
                nombre=f"Comercial Acme Extra {i}",
                razon_social=f"COMERCIAL ACME EXTRA {i}",
            )

    def test_limit_por_defecto_es_5(self):
        payload = self._buscar(self.a["admin"], "acme")
        self.assertEqual(payload["limit"], 5)
        self.assertEqual(len(self._ids(payload, "cliente")), 5)

    def test_limit_recorta_por_grupo_y_marca_hay_mas(self):
        payload = self._buscar(self.a["admin"], "acme", limit=2)
        grupo = next(g for g in payload["grupos"] if g["tipo"] == "cliente")
        self.assertEqual(len(grupo["resultados"]), 2)
        self.assertTrue(grupo["hay_mas"])

    def test_hay_mas_es_falso_cuando_cabe_todo(self):
        payload = self._buscar(self.a["admin"], "acme", limit=25)
        grupo = next(g for g in payload["grupos"] if g["tipo"] == "pedido")
        self.assertFalse(grupo["hay_mas"])

    def test_limit_tiene_tope_duro(self):
        payload = self._buscar(self.a["admin"], "acme", limit=9999)
        self.assertEqual(payload["limit"], 25)

    def test_limit_invalido_cae_al_por_defecto(self):
        self.assertEqual(self._buscar(self.a["admin"], "acme", limit="abc")["limit"], 5)

    def test_limit_cero_o_negativo_se_sube_a_1(self):
        self.assertEqual(self._buscar(self.a["admin"], "acme", limit=0)["limit"], 1)
        self.assertEqual(self._buscar(self.a["admin"], "acme", limit=-3)["limit"], 1)


class CotizacionQFiltroNoRegresionTests(BusquedaGlobalBaseTestCase):
    """El filtro ``?q=`` de cotizaciones no cambió al extraer el aislamiento."""

    def _cotizaciones(self, user, query=""):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(f"{COTIZACIONES_URL}{query}")

    def test_admin_sigue_viendo_solo_su_empresa(self):
        resp = self._cotizaciones(self.a["admin"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["id"] for r in resp.json()], [self.a["cotizacion"].pk])

    def test_filtro_q_por_cliente_sigue_funcionando(self):
        resp = self._cotizaciones(self.a["admin"], "?q=ACME NORTE")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["id"] for r in resp.json()], [self.a["cotizacion"].pk])

    def test_filtro_q_no_cruza_empresas(self):
        resp = self._cotizaciones(self.a["admin"], "?q=ACME SUR")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_usuario_sin_empresa_con_estatus_invalido_sigue_devolviendo_200(self):
        """Sale antes de ``_apply_filters``: antes daba 200 [] y debe seguir igual."""
        resp = self._cotizaciones(self.sin_empresa, "?estatus=abc")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_estatus_invalido_sigue_siendo_400_para_quien_si_tiene_alcance(self):
        resp = self._cotizaciones(self.a["admin"], "?estatus=abc")
        self.assertEqual(resp.status_code, 400)


class MesaControlUsaElAlcanceCanonicoTests(BusquedaGlobalBaseTestCase):
    """Los ViewSets de mesa de control ya no llevan su propia copia del alcance."""

    def _get(self, user, url):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(url)

    def test_clientes_mesa_control_ya_no_sirve_borrados(self):
        """Antes reconstruía desde ``Cliente.objects`` y perdía ``activo=True``."""
        self.a["cliente"].soft_delete()
        resp = self._get(self.a["admin"], "/api/v1/terceros/clientes-mesa-control/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["id"] for r in resp.json()], [])

    def test_clientes_mesa_control_sigue_acotado_a_su_empresa(self):
        resp = self._get(self.a["admin"], "/api/v1/terceros/clientes-mesa-control/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["id"] for r in resp.json()], [self.a["cliente"].pk])

    def test_clientes_mesa_control_no_da_nada_a_usuario_sin_empresa(self):
        resp = self._get(self.sin_empresa, "/api/v1/terceros/clientes-mesa-control/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])
