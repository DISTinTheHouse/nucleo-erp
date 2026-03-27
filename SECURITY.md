Seguridad (Resumen)

- Reporte de vulnerabilidades
  - Usa “Security Advisories” en GitHub para reportes privados.
  - Evita abrir issues públicos con detalles técnicos de exploits.
  - Incluye pasos de reproducción, impacto y alcance; no compartas datos sensibles.

- Alcance y principios
  - Autenticación obligatoria en API; acceso filtrado por empresa y roles.
  - Mínimos privilegios: vendedores solo ven/alteran sus recursos; mesa de control y superuser con permisos ampliados.
  - Sin datos de prueba sensibles en entornos públicos.

- Configuración segura (Django)
  - DEBUG=false en producción; ALLOWED_HOSTS configurado.
  - Cookies seguras: SESSION_COOKIE_SECURE, CSRF_COOKIE_SECURE, HTTPOnly.
  - CSRF habilitado en vistas con formularios; CORS restringido a orígenes confiables.
  - SECURE_PROXY_SSL_HEADER/SECURE_* si hay proxy/HTTPS.

- Gestión de secretos
  - Variables en .env/.vault; no comitear llaves ni tokens.
  - Rotación periódica de credenciales y revocación al detectar exposición.

- Dependencias y parches
  - Dependabot activo; aplicar actualizaciones críticas con prioridad.
  - Auditorías periódicas (pip-audit/OWASP) y fijado de versiones cuando corresponda.

- Protección de datos
  - Evitar loggear PII/secretos; sanitizar entradas y salidas.
  - Enmascarar RFC/telefonía/correos en trazas cuando aplique.
  - Copias de seguridad cifradas y acceso restringido.

- Endpoints y límites
  - Validaciones de entrada estrictas; serializadores DRF sin campos peligrosos.
  - Rate limiting recomendado con throttling DRF para login y rutas sensibles.
  - Paginación por defecto para evitar extracción masiva.

- Subida de archivos (si aplica)
  - Validar tipos/tamaños; almacenar fuera de la raíz pública.
  - Escanear archivos en servidores de producción si el flujo lo requiere.

- Flujo de incidentes
  - Documentar incidente, contención, erradicación y recuperación.
  - Post-mortem sin culpables, acciones correctivas y verificación.

- Checklist de despliegue
  - Variables de entorno cargadas y revisadas.
  - DEBUG=false, CORS/CSRF/Headers configurados.
  - Migraciones aplicadas; llaves rotadas si hubo cambios de infraestructura.
