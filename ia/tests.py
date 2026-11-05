import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

import ventas.tests as ventas_tests
from ia.api.views import AIAssistantAPIView

CHAT_URL = "/api/v1/ai/chat/"


class UltimaCompraImportesPorPerfilTests(TestCase):
    """EC-784: la herramienta ``get_last_cliente_compra`` del asistente sigue la regla
    de importes del pedido (contabilidad de ventas O de compras). Sin ella omite
    ``last_purchase.gran_total`` tanto en lo que se manda al modelo (mensaje
    ``tool``) como en ``tool_results``; trae ``puede_ver_importes``."""

    @classmethod
    def setUpTestData(cls):
        # Mismo fixture que ``ImportesPedidoPorPerfilTests`` (perfiles, pedidos, otra empresa).
        base = ventas_tests.ImportesPedidoPorPerfilTests
        for helper in ("_usuario", "_pedido"):
            setattr(cls, helper, classmethod(base.__dict__[helper].__func__))
        base.setUpTestData.__func__(cls)

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

        api = APIClient()
        api.force_authenticate(usuario)
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
        # Fuera de los pedidos de la empresa del usuario no hay "última compra" posible.
        for usuario, _ in self.perfiles[:-1]:
            with self.subTest(usuario=usuario.username):
                resultado, _ = self._chat(usuario)
                self.assertNotEqual(resultado["last_purchase"]["pedido_id"], self.pedido_otra.pk)
