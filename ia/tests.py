import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from ia.api.views import AIAssistantAPIView
from ventas.tests import ImportesPedidoFixture

CHAT_URL = "/api/v1/ai/chat/"


class UltimaCompraImportesPorPerfilTests(ImportesPedidoFixture, TestCase):
    """EC-784: la herramienta ``get_last_cliente_compra`` del asistente sigue la regla
    de importes del pedido (contabilidad de ventas O de compras). Sin ella omite
    ``last_purchase.gran_total`` tanto en lo que se manda al modelo (mensaje
    ``tool``) como en ``tool_results``; trae ``puede_ver_importes``."""

    def _chat(self, usuario):
        """Corre ``/ai/chat/`` con un modelo falso que pide la herramienta y luego
        responde; devuelve la respuesta y lo que se le mandó al modelo."""
        enviados = []
        respuestas = iter([
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "get_last_cliente_compra", "arguments": "{}"},
            }]}}]},
            {"choices": [{"message": {"role": "assistant", "content": "listo"}}]},
        ])

        def falso(_self, base_url, api_key, model, messages, tools):
            enviados.append(json.loads(json.dumps(messages)))
            return next(respuestas)

        api = self._api(usuario)
        with override_settings(OPENAI_API_KEY="sk-test"), patch.object(AIAssistantAPIView, "_openai_chat", falso):
            resp = api.post(CHAT_URL, {"message": "¿Quién fue mi último cliente?"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        al_modelo = json.loads(next(m for m in enviados[-1] if m.get("role") == "tool")["content"])
        return resp.data["tool_results"][0]["result"], al_modelo

    def test_ultima_compra_por_perfil(self):
        for usuario, ve in self.perfiles[:-1]:
            with self.subTest(usuario=usuario.username):
                resultado, al_modelo = self._chat(usuario)
                for datos in (resultado, al_modelo):
                    compra = datos["last_purchase"]
                    # Cada perfil es vendedor de su propio pedido: es su última compra.
                    self.assertEqual(compra["pedido_id"], self.pedidos[usuario.pk].pk)
                    self.assertEqual("gran_total" in compra, ve)
                    self.assertIn("folio", compra)
                    self.assertIs(datos.get("puede_ver_importes"), ve)

    def test_superusuario_ve_el_importe(self):
        resultado, al_modelo = self._chat(self.superusuario)
        for datos in (resultado, al_modelo):
            self.assertIn("gran_total", datos["last_purchase"])
            self.assertIs(datos["puede_ver_importes"], True)

    def test_no_cruza_de_empresa(self):
        """Para cada perfil hay un pedido MÁS NUEVO en la otra empresa del que es
        vendedor (``cotizacion.vendedor``): sin el filtro por empresa sería su
        "última compra". Con el filtro, sigue siendo el pedido de su empresa."""
        for usuario, _ in self.perfiles[:-1]:
            with self.subTest(usuario=usuario.username):
                ajeno = self._pedido(
                    self.otra, self.sucursal_otra, self.cliente_otra, usuario,
                    f"EC784-AJENO-{usuario.pk}", "777.00",
                )
                resultado, al_modelo = self._chat(usuario)
                for datos in (resultado, al_modelo):
                    self.assertEqual(datos["last_purchase"]["pedido_id"], self.pedidos[usuario.pk].pk)
                    self.assertNotEqual(datos["last_purchase"]["pedido_id"], ajeno.pk)
