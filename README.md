# 🚀 Django Backend ERP (Core)

Este repositorio contiene el **Núcleo (Core)** del sistema ERP. Es una aplicación robusta construida con **Django 6.0** y **Django REST Framework**, diseñada para operar como una API segura y escalable.

## 🧠 Arquitectura del Proyecto

El sistema sigue una arquitectura **Headless / Desacoplada**:

*   **Django (Backend/Core):**
    *   Actúa como la "Fuente de la Verdad" y el administrador central.
    *   Gestiona la lógica de negocio compleja, la seguridad, la base de datos y las validaciones fiscales (SAT).
    *   Provee el **Panel de Administración** para Superusuarios (Staff técnico).
    *   Expone una **API RESTful** segura para que los clientes se conecten.

*   **Next.js (Frontend/Cliente):**
    *   Es la cara del usuario final (Clientes, Cajeros, Gerentes).
    *   Consume la API de Django para todas sus operaciones.
    *   Se enfoca en la experiencia de usuario (UX/UI) y la interactividad en tiempo real.

---

## 📚 Mapa de Documentación

Hemos preparado documentación detallada para cada aspecto del sistema. ¿Qué necesitas saber hoy?

### 🔌 Para Desarrolladores Frontend / Integración
> *"Necesito conectar mi app de Next.js con el backend."*
*   👉 **[DOCUMENTACION_API.md](./DOCUMENTACION_API.md)**: Referencia completa de endpoints, métodos, autenticación, payloads JSON y respuestas de error.

### 🏗️ Para Arquitectos de Software / DevOps
> *"¿Cómo está construido esto? ¿Es seguro?"*
*   👉 **[ARQUITECTURA_APP.md](./ARQUITECTURA_APP.md)**: Explica el stack tecnológico, estrategias de seguridad (Blindaje), manejo de sesiones y flujo de datos.
*   👉 **[ESQUEMA_BD.md](./ESQUEMA_BD.md)**: Diagrama y descripción de los modelos de base de datos y sus relaciones.

### 👤 Para Usuarios Finales / Testing
> *"¿Cómo uso la aplicación? ¿Qué hace cada botón?"*
*   👉 **[GUIA_USUARIO.md](./GUIA_USUARIO.md)**: Manual operativo sobre cómo dar de alta empresas, gestionar sucursales y configurar aspectos fiscales.

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
    Crea un archivo `.env` basado en el ejemplo (ver `settings.py`).
4.  **Ejecutar Servidor**:
    ```bash
    python manage.py runserver 0.0.0.0:8003
    ```

## 🚀 Despliegue (Render)

El backend está desplegado en **Render** y accesible en la siguiente URL pública:

*   Backend (Core): `https://nucleo-erp.onrender.com/`
*   Base de datos: **Supabase** (PostgreSQL).
*   Archivos estáticos: **Whitenoise**.
*   Configuración de despliegue: ver `render.yaml` y `build.sh`.
