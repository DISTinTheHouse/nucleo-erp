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
- **Middleware/ViewSets**: Sobrescribimos `get_queryset()` en todas las vistas.
- **Lógica**: `queryset.filter(empresa=request.user.empresa)`
- **Resultado**: Un usuario jamás puede leer ni escribir datos de otra empresa, incluso si manipula los IDs en la URL.

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
    - Se validan permisos (Role-Based Access Control).
    - Se ejecutan reglas de negocio (ej. validación SAT).
5.  **Respuesta**: JSON estructurado y códigos HTTP semánticos (200, 201, 400, 401, 403).

## 4. Estructura de Endpoints

- **`/api/v1/nucleo/`**: Gestión de estructura organizacional (Empresas, Sucursales).
- **`/api/v1/auth/`**: Gestión de sesión.
- **`/api/v1/sat/`**: Servicios fiscales y catálogos.

## 5. Auditoría
Cada escritura crítica genera un rastro:
- **Logs de API**: Tiempos de respuesta, usuario y status code.
- **Logs de Auditoría**: Cambios en modelos sensibles (quién cambió qué valor).
