# Asistente IA (ERP) – Manual de Uso

## Qué es
Asistente conversacional integrado al ERP para consultas y acciones comunes. Vive en la UI como un widget flotante y usa un endpoint interno para ejecutar herramientas seguras (conteos, búsquedas y altas controladas).

## Cómo abrirlo
- En cualquier pantalla del ERP, haz clic en el botón flotante con el icono de chat en la esquina inferior derecha. 
- Escribe tu consulta en español y presiona Enter o el botón de enviar.

## Consultas soportadas
- Conteos rápidos
  - “¿Cuántas empresas tengo?”
  - “¿Cuántos usuarios hay?”
  - “¿Cuántas cotizaciones existen?”
- Listados breves
  - “Lista las 5 empresas”
  - “Muéstrame 10 usuarios de mi empresa”
  - “Lista roles disponibles”
- Búsquedas
  - “Busca clientes con RFC XAXX010101000”
  - “Clientes que contengan ‘Lazzar’”
- Detalles
  - “Dame los datos de la empresa lazzar-mex-0001”
  - “Información del usuario juan.perez”
  - “Detalle de la cotización con folio P-1-26”
- Permisos
  - “¿Qué permisos efectivos tengo?”

Notas:
- El asistente usa herramientas internas; no inventa números. Si faltan datos o permisos, te lo indicará.

## Acciones (Crear)
- Empresa (solo superuser)
  - Ejemplo: “Crea una empresa con razón social LAZZAR SA de CV y RFC XAXX010101000”
  - El asistente solicitará campos obligatorios (razón social, RFC, régimen fiscal, código/slug, etc.).
- Rol (solo superuser)
  - Ejemplo: “Crea un rol Ventas con descripción ‘Atiende clientes’”
- Usuario (admin-empresa o superuser)
  - Ejemplo: “Crea un usuario maria.garcia con email maria@empresa.com y rol Ventas”
  - Puede pedir sucursal_default u otros datos mínimos para cumplir reglas de integridad.
- Cliente (admin-empresa o superuser)
  - Ejemplo: “Crea un cliente ‘Comercial XYZ’ con RFC XAXX010101000”
  - Valida RFC y campos SAT clave; si falta algo, lo pide.

## Ejemplos de prompts
- “¿Cuántas cotizaciones tengo este mes?”
- “Lista 10 usuarios ordenados por fecha de creación”
- “Busca cliente por razón social ‘Industrial’”
- “Crea un rol Soporte con descripción ‘Atención postventa’”
- “Dame detalle de la empresa con código lazzar-mex-0001”

## Reglas de permisos (resumen)
- Superuser: puede crear Empresas y Roles; también Usuarios y Clientes.
- Admin de empresa: puede crear Usuarios y Clientes dentro de su empresa.
- Usuario normal: consultas; no crea recursos.

## Validaciones y límites
- El asistente valida campos críticos (por ejemplo, RFC: formato, checksum y excepciones permitidas).
- Si no tiene certeza de los datos, solicita información adicional.
- Si no tienes permisos suficientes, te lo indicará.

## Troubleshooting
- “OPENAI_API_KEY no está configurado”: agrega la variable en `.env` y reinicia el servidor.
- “Error al procesar la respuesta”: revisa conexión a internet o la configuración del modelo.
- Problemas de CSRF en UI: recarga sesión; el widget envía `X-CSRFToken` automáticamente.
- Falta de permisos: solicita la acción a un superuser o admin de empresa.

## Notas técnicas (para desarrolladores)
- Endpoint de la IA: `POST /api/v1/ai/chat/`
  - Cuerpo esperado (ejemplo):
    ```json
    {
      "message": "¿Cuántas empresas tengo?",
      "conversation": [
        {"role": "user", "content": "Hola"},
        {"role": "assistant", "content": "¿En qué te ayudo?"}
      ]
    }
    ```
  - Respuesta exitosa (ejemplo):
    ```json
    {
      "reply": "Tienes 1 empresa.",
      "tool_results": [
        {"name": "count_empresas", "args": {}, "result": {"ok": true, "count": 1}}
      ]
    }
    ```
- Variables de entorno en `ERP/settings.py`:
  - `OPENAI_API_KEY` (obligatoria)
  - `OPENAI_BASE_URL` (opcional, por defecto `https://api.openai.com/v1`)
  - `OPENAI_MODEL` (opcional, por defecto `gpt-4o-mini`)
- Archivos relevantes
  - Widget UI: [base_core.html](file:///c:/Users/Jesús%20Ibarra/Desktop/django-backend-v2/templates/base_core.html)
  - Endpoint DRF: [urls.py](file:///c:/Users/Jesús%20Ibarra/Desktop/django-backend-v2/ia/api/urls.py), [views.py](file:///c:/Users/Jesús%20Ibarra/Desktop/django-backend-v2/ia/api/views.py)
  - Configuración: [settings.py](file:///c:/Users/Jesús%20Ibarra/Desktop/django-backend-v2/ERP/settings.py)
- Seguridad
  - Autenticación: `IsAuthenticated` (hereda sesión activa en UI).
  - El servidor decide qué herramientas ejecutar y valida permisos antes de crear.

## Ejemplo rápido por API (cURL)
```bash
curl -X POST https://tu-dominio/api/v1/ai/chat/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TU_TOKEN_OPCIONAL_SI_APLICA>" \
  -d '{"message":"¿Cuántas empresas tengo?"}'
```

---
Si necesitas que el asistente acepte más operaciones (por ejemplo, más filtros o actualizaciones), indícalo y se agregan nuevas herramientas. 
*** End Patch***} >>
