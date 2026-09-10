# Ciberseguridad y Blindaje del ERP (Django + Next.js)

Documento orientado a operación (Dirección de Operaciones) para explicar, en términos claros, por qué el ERP es seguro y cómo se protegen el acceso, los datos y la trazabilidad.

## 1) Resumen ejecutivo

El ERP está diseñado bajo un enfoque **“no confiar en el cliente”**: toda regla de negocio y seguridad vive en el backend (Django). La aplicación se divide en dos superficies controladas:

1. **Administración central (Django Admin / Web interna)**: uso restringido para personal técnico/administrativo con privilegios.  
2. **Operación diaria (App Next.js)**: la interfaz del usuario final consume **APIs autenticadas**; el frontend nunca puede “inventar” permisos ni saltarse validaciones.

Con esto se logra:
- **Acceso controlado** por usuario/rol/permisos, aplicado en servidor.
- **Aislamiento por empresa** (multi-tenant) para evitar fuga de información.
- **Protección ante fuerza bruta** en login.
- **Auditoría** de cambios (quién, qué, cuándo, desde dónde).
- **Eliminaciones no destructivas (soft-delete)** para reducir riesgo operativo y permitir recuperación.
- **Transporte cifrado** hacia servicios remotos (TLS/SSL).

## 2) Superficies controladas: Admin vs App (Next.js)

### A) Django Admin / Web interna
- Dirigido a tareas administrativas y de supervisión.
- Basado en sesiones de Django, con **CSRF** y mecanismos estándar del framework.
- Se restringe por autenticación y perfiles (superusuario/staff, administradores).

### B) App Next.js (operación)
- La app no accede directo a la base de datos.
- Consume la API del ERP con **token Bearer** (estándar de clientes web).
- El backend valida autenticación, permisos y alcance antes de permitir cualquier operación.

## 3) Autenticación (quién eres)

Implementado:
- **Token Bearer para APIs** (compatibilidad con Next.js/Postman).  
  - Configuración: `DEFAULT_AUTHENTICATION_CLASSES` incluye `BearerTokenAuthentication`.
- **Login por email** (case-insensitive) en backend.
- **Política de contraseñas** vía validadores de Django (mínimo, comunes, numéricas, similitud).

Protección anti-fuerza bruta:
- **Bloqueo por intentos fallidos (django-axes)**.
  - Límite: 5 intentos fallidos.
  - Bloqueo temporal: 1 hora.
  - Plantilla dedicada de bloqueo: `403.html`.

## 4) Autorización (qué puedes hacer)

Implementado:
- **Permisos por defecto “deny-by-default”** en API: requiere usuario autenticado para acceder a endpoints (`IsAuthenticated`).
- **RBAC (Role-Based Access Control)**:
  - Roles que agrupan permisos por módulo (lectura/edición/eliminación).
  - Overrides por usuario (otorgar o denegar permisos específicos) para casos excepcionales.
- **Aplicación server-side**: el frontend solo muestra/oculta UI, pero la decisión final siempre es del backend.

## 5) Aislamiento multi-empresa (evita fuga de datos)

Implementado:
- El backend filtra datos por **empresa** y, cuando aplica, por **sucursales permitidas**.  
  Esto previene que un usuario pueda leer/escribir datos de otra empresa aunque manipule IDs o URLs.
- Comportamiento esperado en APIs operativas:
  - **Listados**: `200 OK` con `[]` si el usuario no tiene empresa o no hay registros en su empresa.
  - **Detalle**: `404 Not Found` si el registro pertenece a otra empresa.

## 6) Protección de red, navegador y transporte

Implementado:
- **ALLOWED_HOSTS por variable de entorno**: el servidor rechaza `Host` no reconocidos.
- **CORS estricto solo para APIs**:
  - No se permite “cualquier origen”.
  - Se limita a los orígenes autorizados (configurados por variables) y patrones permitidos.
  - Solo aplica a rutas `/api/*`.
- **CSRF trusted origins** configurado por variables para entornos autorizados.
- **No-cache en APIs**: se agregan headers para evitar que respuestas sensibles se queden en caché del navegador/intermediarios.
- **Conexión cifrada a base remota**: al usar base remota se fuerza `ssl_require=True` (TLS/SSL).

Disponible para habilitar en producción (endurecimiento adicional controlado por variables):
- Redirección a HTTPS, cookies seguras y HSTS (activación según despliegue y certificados).

## 7) Eliminaciones no destructivas (Soft-delete)

Implementado:
- El sistema evita borrados físicos como operación por defecto. En su lugar, aplica **soft-delete**:  
  se marca el registro como inactivo (`activo = false`) y se excluye de listados operativos.
- En APIs y módulos clave, la operación “eliminar” ejecuta `soft_delete()` en lugar de borrar filas.

Beneficios operativos:
- Reduce riesgo de pérdida irreversible de información.
- Facilita recuperación y auditoría de incidentes.
- Mantiene integridad referencial en procesos (ventas, inventarios, etc.).

## 8) Auditoría, trazabilidad y monitoreo

Implementado:
- **Log de API**: se registra método, ruta, usuario, código HTTP y duración (útil para detectar abuso, fallas y performance).
- **Auditoría persistente en base de datos**:
  - Registra módulo, acción (CREATE/UPDATE/DELETE/LOGIN/EXPORT), tabla, ID del registro.
  - Guarda “antes/después” cuando aplica.
  - Captura IP y user-agent.
- Acceso a auditoría y logs operativos se mantiene en módulos internos y con controles de acceso (ej. vistas restringidas a superusuario cuando aplica).

## 9) Gestión de secretos y configuración segura

Implementado:
- Secretos sensibles (por ejemplo `SECRET_KEY`, URLs de base de datos, orígenes confiables) se manejan por **variables de entorno**, no en código.
- El repositorio excluye archivos `.env` y entornos virtuales para evitar filtraciones accidentales.

## 10) Recomendaciones operativas (mejores prácticas)

Para elevar el nivel de seguridad en producción de forma continua:
- Activar **HTTPS obligatorio** y **cookies seguras** en despliegues públicos.
- Mantener **rotación de credenciales** (tokens, contraseñas, llaves) en ventanas definidas.
- Aplicar **principio de mínimo privilegio**: solo superusuarios/staff para administración técnica.
- Revisar periódicamente:
  - Reportes de auditoría (acciones inusuales, intentos de login fallidos).
  - Logs de API (errores 4xx/5xx, picos de tráfico).
- Respaldos y pruebas de restauración (disaster recovery) de base de datos según política interna.

---

Este documento resume medidas reales implementadas en el ERP y el esquema de operación seguro para Admin (Django) y operación (Next.js). Si se requiere una versión para auditoría externa (ISO/controles), se puede ampliar con controles, evidencias y procedimientos (rotación, respaldos, monitoreo y respuesta a incidentes).
