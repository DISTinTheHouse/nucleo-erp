# 🏗️ Arquitectura y Funcionamiento de la App

Este documento explica cómo funciona la aplicación a nivel técnico y arquitectónico. El sistema es un **ERP Multi-tenant** construido con Django, diseñado para ser escalable y seguro.

## 1. Concepto Multi-tenant (Empresas)
El núcleo del sistema es el modelo `Empresa`.
- **Aislamiento Lógico**: Aunque todos los datos viven en la misma base de datos, cada registro importante (Sucursal, Usuario, Venta, etc.) tiene una llave foránea (`ForeignKey`) hacia `Empresa`.
- **Seguridad**: Todas las consultas a la base de datos deben filtrar por la empresa del usuario activo para evitar fugas de información entre inquilinos.

## 2. Sistema de Seguridad y Permisos
El sistema utiliza un modelo de seguridad híbrido y robusto:

### A. Autenticación y Protección
- **Token Auth**: API segura usando tokens estándar.
- **Protección Fuerza Bruta**: Integración con `django-axes` para bloquear IPs tras 5 intentos fallidos de login (1 hora de bloqueo).
- **Validación Estricta**: Validaciones regex y checksum para RFCs mexicanos.

### B. Autorización (Roles y Scopes)
La autorización se decide en tres niveles:
1.  **Nivel Empresa (Tenant)**: ¿El usuario pertenece a esta empresa?
2.  **Nivel Sucursal (Scope)**: ¿El usuario tiene acceso a la sucursal donde intenta operar? (Campo `sucursales` M2M).
3.  **Nivel Funcional (RBAC)**: ¿El usuario tiene el **Rol** necesario (ej. "Vendedor") y el **Permiso** específico (ej. `crear_pedido`)?

## 3. Integración Fiscal (SAT México)
La aplicación está diseñada para cumplir con la normativa mexicana.
- **Catálogos SAT**: Base de datos poblada con catálogos oficiales (Uso CFDI, Régimen Fiscal, etc.).
- **Manejo de CSD (Sellos Digitales)**:
  - Almacenamiento seguro de archivos `.key` y `.cer` fuera del directorio público.
  - **Validación Criptográfica**: Uso de librería `cryptography` (OpenSSL) para validar pares de llaves, contraseñas y vigencia al momento de la carga.

## 4. Sistema de Auditoría y Logging
Implementamos una arquitectura de observabilidad en tres capas:

1.  **System Logs (`sistema.log`)**: Errores de bajo nivel y advertencias del framework.
2.  **API Logs (`api.log`)**: Middleware (`APILoggingMiddleware`) que registra cada petición HTTP, payload (sanitizado), respuesta y tiempo de ejecución.
3.  **Audit Logs (`auditoria.log` / DB)**: `AuditLogMixin` en modelos clave que registra *quién* modificó *qué* (creación, edición, eliminación) y el *diff* de los cambios.

## 5. Flujo de Datos (Frontend - Backend)
Arquitectura orientada a servicios (API REST):

1.  **Request**: Next.js envía petición con Token.
2.  **Middleware**:
    - `AxesMiddleware`: Verifica ataques.
    - `APILoggingMiddleware`: Loguea la entrada.
3.  **Vista/API**:
    - `IsAuthenticated`: Verifica token.
    - Serializers: Valida integridad de datos (ej. RFC).
4.  **Response**: JSON estandarizado.

## 6. Tecnologías Clave
- **Backend**: Python 3.12 / Django 6.0
- **Base de Datos**: PostgreSQL.
- **Seguridad**: `django-axes`, `cryptography`.
- **API**: Django REST Framework (DRF).
