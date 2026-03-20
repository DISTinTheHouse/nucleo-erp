# 🏗️ Arquitectura y Blindaje del Sistema

Este documento describe la arquitectura técnica y las medidas de seguridad ("Blindaje") implementadas para operar como un backend robusto basado puramente en APIs.

## 1. Stack Tecnológico
- **Framework**: Django 6.0.2 (Python 3.12)
- **API**: Django REST Framework (DRF) 3.15+
- **Base de Datos**: PostgreSQL
- **Seguridad**: `django-axes`, `cryptography`, `corsheaders`

## 2. Estrategia de Blindaje (Security Hardening)

El sistema ha sido diseñado para "no confiar en el cliente" y validar todo en el servidor.

### A. Aislamiento Multi-tenant (Nivel DB)
Aunque es una base de datos compartida, el aislamiento lógico es absoluto:
- **DRF ViewSets (APIs operativas)**: Sobrescribimos `get_queryset()` en vistas multi-empresa para filtrar por la empresa del usuario.
- **Lógica**: `queryset.filter(empresa=request.user.empresa)` (o filtro equivalente cuando la empresa está en la relación, ej. `cotizacion__empresa`).
- **Resultado**: Un usuario no puede leer datos de otra empresa aunque manipule IDs en la URL.
- **Comportamiento esperado**:
  - Listados: `200 OK` con `[]` si el usuario no tiene empresa o no hay registros de su empresa.
  - Detalle: `404 Not Found` si el recurso existe pero pertenece a otra empresa.

### B. Validación de Datos (Nivel Serializer)
No permitimos basura en la BD.
- **RFCs**: Validación estricta de formato y checksum (algoritmo oficial SAT).
- **Archivos CSD**: Al subir sellos digitales (`.cer`, `.key`), se validan criptográficamente en memoria antes de guardarse. Si la contraseña no abre la llave o el RFC no coincide, se rechaza.

### C. Protección de Red y Transporte
- **CORS Estricto**: Solo se permiten peticiones desde el Frontend autorizado (configurado en `.env`).
- **Allowed Hosts**: El servidor rechaza peticiones con Host headers desconocidos.
- **Rate Limiting**: Protección contra ataques de fuerza bruta en el login (bloqueo temporal de IP).

## 3. Flujo de Trabajo API-First

El backend actúa como una "Caja Negra" segura para el Frontend (Next.js).

1.  **Petición**: El frontend envía JSON + Token Bearer.
2.  **Gatekeeper**: Django verifica Token, IP (bloqueos) y Origen (CORS).
3.  **Contexto**: Se hidrata `request.user` y se determina su `empresa` activa.
4.  **Procesamiento**:
    - Se validan permisos (Role-Based Access Control) usando roles y claves de permiso (`R-XXX`, `E-XXX`, `D-XXX`).
    - Se ejecutan reglas de negocio (ej. validación SAT).
5.  **Respuesta**: JSON estructurado y códigos HTTP semánticos (200, 201, 400, 401, 403).

## 4. Estructura de Endpoints

- **`/api/v1/nucleo/`**: Gestión de estructura organizacional (Empresas, Sucursales).
- **`/api/v1/auth/`**: Gestión de sesión.
- **`/api/v1/sat/`**: Servicios fiscales y catálogos.
- **`/api/v1/inventarios/`**: Almacenes y Ubicaciones (scoping por Empresa/Sucursales; CRUD para admin_empresa/superuser).

## 6. Responsabilidades UI vs Backend

- **Django (Core)**: Servicios y reglas de negocio; HTML solo para vistas internas de lectura (administración técnica).
- **Next.js (Frontend)**: Toda la interacción de usuario final (CRUD de Inventarios, Usuarios, etc.) contra APIs DRF.
- **Permisos y Alcance**: Aplicados en el backend; el frontend no puede elevar privilegios ni salir de su empresa/sucursales.

## 5. Auditoría
Cada escritura crítica genera un rastro:
- **Logs de API**: Tiempos de respuesta, usuario y status code.
- **Logs de Auditoría**: Cambios en modelos sensibles (quién cambió qué valor).

## 7. Sistema de Permisos (RBAC + Overrides)

El sistema implementa un control de acceso robusto y flexible:

1.  **Roles (Base)**: Se asignan roles a los usuarios (ej. "Ventas", "Almacén") que agrupan permisos.
2.  **Overrides (Excepciones)**: Se pueden otorgar o denegar permisos específicos a un usuario, sobrescribiendo sus roles.
    - **GRANT**: Otorga un permiso extra (ej. Usuario de "Ventas" que necesita ver "Inventarios").
    - **DENY**: Revoca un permiso crítico (ej. Usuario de "Admin" que no debe ver "Nómina").
3.  **Resolución**: La API de Login calcula los permisos efectivos (`(Roles + Grant) - Deny`) y los entrega al frontend. El frontend no necesita conocer la lógica compleja, solo recibe la lista final de permisos.
