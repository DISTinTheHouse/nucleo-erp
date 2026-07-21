# 🚀 Django Backend ERP (Core)

Este repositorio contiene el **Núcleo (Core)** del sistema ERP. Es una aplicación robusta construida con **Django 6.0** y **Django REST Framework**, diseñada para operar como una API segura y escalable.

## 🧠 Arquitectura del Proyecto

El sistema sigue una arquitectura **Headless / Desacoplada**:

- **Django (Backend/Core):**
  - Actúa como la "Fuente de la Verdad" y el administrador central.
  - Gestiona la lógica de negocio compleja, la seguridad, la base de datos y las validaciones fiscales (SAT).
  - Provee el **Panel de Administración** para Superusuarios (Staff técnico).
  - Expone una **API RESTful** segura para que los clientes se conecten.

- **Next.js (Frontend/Cliente):**
  - Es la cara del usuario final (Clientes, Cajeros, Gerentes).
  - Consume la API de Django para todas sus operaciones.
  - Se enfoca en la experiencia de usuario (UX/UI) y la interactividad en tiempo real.

---

## 📚 Mapa de Documentación

Hemos preparado documentación detallada para cada aspecto del sistema. ¿Qué necesitas saber hoy?

### 🔌 Para Desarrolladores Frontend / Integración

> _"Necesito conectar mi app de Next.js con el backend."_

- 👉 **[DOCUMENTACION_API.md](./DOCUMENTACION_API.md)**: Referencia completa de endpoints, métodos, autenticación, payloads JSON y respuestas de error.

### 🏗️ Para Arquitectos de Software / DevOps

> _"¿Cómo está construido esto? ¿Es seguro?"_

- 👉 **[ARQUITECTURA_APP.md](./ARQUITECTURA_APP.md)**: Explica el stack tecnológico, estrategias de seguridad (Blindaje), manejo de sesiones y flujo de datos.
- 👉 **[ESQUEMA_BD.md](./ESQUEMA_BD.md)**: Diagrama y descripción de los modelos de base de datos y sus relaciones.

### 👤 Para Usuarios Finales / Testing

> _"¿Cómo uso la aplicación? ¿Qué hace cada botón?"_

- 👉 **[GUIA_USUARIO.md](./GUIA_USUARIO.md)**: Manual operativo sobre cómo dar de alta empresas, gestionar sucursales y configurar aspectos fiscales.

---

## 🛠️ Configuración Rápida para Desarrollo

1.  **Entorno Virtual**:
    ```bash
    python -m venv venv
    .\venv\Scripts\activate
    ```
2.  **Dependencias**:
    ```bash
    pip install -r requirements.txt
    ```
3.  **Variables de Entorno**:
    Crea un archivo `.env` con las llaves que consume `settings.py`. Recomendadas:
    - SECRET_KEY
    - DEBUG
    - POSTGRESQL_DB_HOST, POSTGRESQL_DB_USER, POSTGRESQL_DB_PASSWORD, POSTGRESQL_DB_NAME, POSTGRESQL_DB_PORT
    - ALLOWED_HOSTS
    - CORS_ALLOWED_ORIGINS, CORS_ALLOW_CREDENTIALS
    - CSRF_TRUSTED_ORIGINS

    Variables para **Google Drive (OAuth 2.0)**:
    - GOOGLE_DRIVE_CLIENT_ID
    - GOOGLE_DRIVE_CLIENT_SECRET
    - GOOGLE_DRIVE_REDIRECT_URI (recomendado en producción; ejemplo `https://<tu-app>.vercel.app/ia/drive/google/callback/`)

    Requisitos en Google Cloud Console:
    - Habilitar **Google Drive API** en el proyecto.
    - Configurar **OAuth consent screen**:
      - Si está en _Testing_, agrega los correos que usarán la app en **Test users** (de lo contrario verán `403 access_denied` por verificación).
      - Para uso abierto (_Production_), prepara **verificación**: logo, nombre, **Privacy Policy URL**, **Homepage**, dominio verificado y justificación de scopes.
    - Crear credenciales **OAuth client ID** tipo **Web application** y registrar _Authorized redirect URIs_:
      - Local: `http://127.0.0.1:8000/ia/drive/google/callback/`
      - Producción: `https://<tu-app>.vercel.app/ia/drive/google/callback/`
    - El flujo se inicia en `/ia/drive/google/connect/` y finaliza en `/ia/drive/google/callback/`.

    Variables opcionales para **2FA (correo/SMS)**:
    - TWO_FACTOR_OTP_LENGTH (default 6)
    - TWO_FACTOR_OTP_TTL_SECONDS (default 300)
    - TWO_FACTOR_MAX_ATTEMPTS (default 5)
    - TWO_FACTOR_DEBUG_SHOW_CODE (default False)
    - TWO_FACTOR_EMAIL_BRAND (ej. "ERP Core")
    - TWO_FACTOR_EMAIL_SUBJECT (ej. "ERP Core - Código de verificación")
    - TWO_FACTOR_SMS_ENABLED (default False)
    - TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER (solo si usas SMS)
    - EMAIL_HOST, EMAIL_PORT, EMAIL_USE_TLS, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, DEFAULT_FROM_EMAIL (para SMTP)

4.  **Ejecutar Servidor**:
    ```bash
    python manage.py runserver 0.0.0.0:8003
    ```

## 🔐 2FA (Correo/SMS) en Login Web (Core)

- **Qué es**: Doble autenticación (2 pasos) para el login web del Core (no APIs).
- **Flujo**:
  - Login: `/` (usuario/contraseña)
  - Verificación: `/two-factor/` (captura del código)
- **Activación por usuario**: campo `two_factor_enabled` en el modelo `Usuario` (se puede alternar desde Core → Usuarios o desde Admin).
- **Entrega del código**:
  - **Email (SMTP)**: HTML + texto plano (recomendado para desarrollo y producción).
  - **SMS (Twilio, opcional)**: vía llamada HTTP al API de Twilio.
- **Paquetes / pip**:
  - No se instaló ninguna librería nueva para 2FA.
  - Se usa Django nativo (`EmailMultiAlternatives`) para el correo y `urllib` estándar para el SMS (Twilio).

## 🚀 Despliegue (Vercel)

El backend está desplegado en **Vercel** como Serverless Function usando `vercel.json` y `api/index.py`.

Guía rápida:

- Configura variables en Vercel: SECRET_KEY, DEBUG=False, y credenciales de PostgreSQL (Supabase).
- CI/CD: los workflows en `.github/workflows` ejecutan checks y despliegue automático a Vercel (Preview en PR, Production en main/master).
- Endpoint de salud: `/healthz/`.

Notas:

- En entornos serverless los logs se escriben en `/tmp/logs` (no persistentes).
- Para auditoría persistente y operación prolongada, Render queda como alternativa de contingencia.

## 🧯 Contingencia (Render)

- Configuración lista con `render.yaml` y `build.sh`.
- Mismo código, mismas variables de entorno.
