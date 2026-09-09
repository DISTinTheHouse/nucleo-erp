# Contratos API: validación de request/response

Repo backend `nucleo-erp`. Revisión del contrato real que expone `/api/v1/*` — no de si cada endpoint individual valida bien sus datos (eso ya se cubrió en otros docs), sino de si el **shape** de request/response es consistente entre endpoints, que es lo que le importa a un cliente genérico (Next.js con un wrapper de fetch único, un generador de tipos desde OpenAPI, un manejador de errores central).

Tres inconsistencias, verificadas con evidencia y una con reproducción empírica. Ninguna es "código roto" — cada endpoint individualmente funciona — pero rompen cualquier capa genérica del lado frontend que asuma "la API se comporta igual en todos lados".

---

## 1. La forma de los errores no es consistente

DRF tiene una convención nativa: todo error de validación es **`{campo: [mensaje, ...]}`** — lista, incluso con un solo mensaje. Se puede reproducir:

```python
class S(serializers.Serializer):
    email = serializers.CharField()
    def validate(self, attrs):
        raise serializers.ValidationError('mensaje sin campo')

S(data={'email': 'x'}).is_valid()
# -> {'non_field_errors': ['mensaje sin campo']}   # lista, siempre
```

Pero gran parte del código de negocio en las vistas levanta `ValidationError` a mano con un **string plano** en vez de una lista, y DRF respeta literalmente lo que se le pasa:

```python
# lo que hace la mayoría del código:
raise ValidationError({"cliente": "no pertenece a la empresa"})
# -> respuesta real: {"cliente": "no pertenece a la empresa"}        (string)

# lo que hace DRF nativamente y lo que arregló finanzas recientemente:
raise ValidationError({"cliente": ["no pertenece a la empresa"]})
# -> respuesta real: {"cliente": ["no pertenece a la empresa"]}      (lista)
```

Confirmado corriendo ambos casos por `rest_framework.views.exception_handler` — no es una lectura del código, es el JSON real que sale.

**Conteo por app** (`raise ValidationError({"campo": "string"})` vs `{"campo": ["string"]}`, fuera de tests):

| App | Forma string (rompe la convención) | Forma lista (correcta) |
|---|---|---|
| `finanzas` | 44 | 4 *(fix reciente, parcial — ver abajo)* |
| `produccion` | 4 | 0 |
| `ventas` | 19 | 0 |
| `compras` | 28 | 0 |
| `inventarios` | 17 | 0 |
| `wms` | 12 | 0 |
| `catalogo` | 1 | 0 |
| **Total** | **125** | **4** |

Los 4 casos "lista" son de un fix reciente en `finanzas` (validación de líneas hijas de Cobro/Pago) — arreglaron los sitios que tocaron, no una pasada sistemática. El resto del módulo, y el resto de la API, sigue devolviendo el formato viejo.

**Impacto real**: cualquier frontend que maneje errores de forma genérica (`Object.entries(error.response.data).forEach(([field, messages]) => messages.forEach(...))`, o un generador de tipos que asuma `Record<string, string[]>`) truena o silenciosamente ignora el mensaje en 125 de 129 sitios. Si hoy funciona, es porque el frontend ya tiene un `if (typeof msg === 'string')` por cada caso, o porque solo lee `String(error.response.data)` sin desglosar por campo — ninguna de las dos es sostenible según crece la API.

---

## 2. Paginación: sin envoltorio global, dos excepciones sin avisar

`REST_FRAMEWORK` en `ERP/settings.py:301-316` no define `DEFAULT_PAGINATION_CLASS`. Ningún `ViewSet` del repo asigna `pagination_class` tampoco (`grep` exhaustivo). Consecuencia: **todo `list()` de todo `ModelViewSet` en la API devuelve un array JSON plano**, sin `count`/`next`/`previous`.

Excepción: `ReporteExistenciasPeriodoPagination`/`ReporteMovimientosPeriodoPagination` (`inventarios/api/views.py:130-131`, `:1027-1029`), instanciadas **a mano dentro de la acción**, no como `pagination_class` del ViewSet — así que ni siquiera aparecen como paginación estándar de DRF en el schema. Esos dos endpoints (`reporte-existencias-periodo`, `reporte-movimientos-periodo`) sí devuelven el envoltorio `{count, next, previous, results}`.

**Impacto**: el frontend tiene que saber, endpoint por endpoint, si `response.data` es la lista o si la lista está en `response.data.results`. No hay forma de inferirlo del contrato general — solo probando cada uno.

---

## 3. `estatus`/`status` casi nunca está protegido, pese a que casi todo usa `fields = "__all__"`

`fields = "__all__"` aparece 95 veces en serializers de toda la API (`finanzas` 21, `produccion` 19, `hr` 17, `ventas` 13, `wms` 10, resto menor). Bajo ese patrón, **cualquier campo del modelo es escribible por PATCH/PUT salvo que se liste explícitamente** en `read_only_fields`/`extra_kwargs`.

De esas 95, **solo una** protege su campo de estado: `wms/api/serializers.py:58`, `read_only_fields = [..., "status"]`. En el resto — `Cotizacion.estatus`, `Pedido.estatus`, `OrdenCompra.estatus`, `NotaCredito.estatus` (hasta el fix de esta semana), etc. — el campo de estado es escribible directo por API genérica, y lo único que evita saltarse el flujo de negocio (autorizar, aplicar, cancelar) es un guard manual dentro de `perform_update()`, si existe. Ya se confirmó al menos un caso donde no existía y se corrigió (`NotaCredito`, ver commits de esta semana: transición `Emitida→Borrador` por PATCH desaplicaba el crédito sin revertir la CxC). No hay garantía de que sea el único.

**Nota aparte, no contradictoria**: en `wms` el campo sí está protegido, pero por otro motivo documentado en [doc/flujo-cotizacion-pedido-wms-inventario.md](flujo-cotizacion-pedido-wms-inventario.md) — protegerlo ahí significa que **nada** puede moverlo, ni el backend ni el cliente. Es el extremo opuesto del mismo problema: en un lado el contrato es demasiado permisivo, en el otro demasiado rígido. Ninguno de los dos es "el campo de estado se mueve solo por una transición de negocio validada", que es lo esperable.

---

## 4. El schema OpenAPI no siempre refleja el contrato real

5 archivos de vistas usan `get_serializer_class()` para servir un serializer distinto según la acción (list/retrieve/default — convención documentada en `CLAUDE.md`). `drf_spectacular` (que genera `/api/schema/`, `/api/docs/`, `/api/redoc/`) introspecciona `serializer_class` por defecto; para reflejar un `get_serializer_class()` dinámico necesita una anotación `@extend_schema` explícita por acción. Solo **un** archivo en todo el repo usa `@extend_schema`. Para los otros casos de serializer dinámico, lo más probable es que Swagger/Redoc muestren el shape del serializer por defecto también para `retrieve`/`list`, que puede no ser el que realmente responde el servidor.

No confirmé cuáles de los 5 casos concretos divergen (requeriría comparar cada par de serializers), pero la condición para que diverjan está dada en la mayoría: casi nadie anota.

---

## Resumen y prioridad

| Hallazgo | Alcance | Impacto en frontend | Prioridad |
|---|---|---|---|
| Forma de errores inconsistente (string vs lista) | 125 sitios en 7 apps | rompe manejo de errores genérico | **alta** — es lo primero que un cliente HTTP necesita para poder confiar en la API |
| Paginación sin aviso | 2 endpoints de 100+ | requiere conocimiento caso por caso, no rompe nada hoy | media |
| `estatus` escribible sin guard consistente | 94 de 95 serializers | puede saltarse reglas de negocio vía PATCH directo | alta (ya causó al menos un bug real) |
| Schema OpenAPI desactualizado en serializers dinámicos | ≥4 de 5 casos, sin verificar cuáles divergen | Swagger/tipos autogenerados pueden mentir | media |

**Recomendación de orden**: 1) normalizar la forma de los errores primero — es mecánico (envolver en lista) y de bajo riesgo, se puede hacer con un `sed`/regex dirigido y validar con la suite existente; 2) inventariar qué modelos tienen campo de estado y decidir, por modelo, si se protege con `read_only_fields` + transición vía `@action`, o se documenta explícitamente que es libre; 3) paginación y schema son de menor urgencia, pero baratos de resolver: agregar `DEFAULT_PAGINATION_CLASS` global (con opt-out donde no aplique) y anotar los 5 `get_serializer_class()` con `@extend_schema`.
