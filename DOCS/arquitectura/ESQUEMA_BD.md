# 🗄️ Relación de Base de Datos (Schema)

Este documento describe la estructura de la base de datos, las relaciones entre tablas y sus identificadores principales. El sistema sigue una arquitectura multi-tenant lógica donde la tabla `Empresas` es el eje central.

## 🏗️ Núcleo (`nucleo`)

Módulo principal que define la estructura organizacional y catálogos globales.

### 🏢 Organización
| Tabla | PK (ID) | Relaciones Clave | Descripción |
|-------|---------|------------------|-------------|
| **empresas** | `id_empresa` | `moneda_base` (FK: monedas) | Entidad raíz (Tenant). Contiene configuración global. |
| **sucursales** | `id_sucursal` | `empresa` (FK: empresas) | Ubicaciones físicas o lógicas de una empresa. |
| **departamentos** | `id_departamento` | `empresa` (FK), `sucursal` (FK) | Áreas funcionales (Ventas, RH) dentro de una sucursal. |
| **series_folios** | `id_serie_folio` | `empresa` (FK), `sucursal` (FK) | Control de numeración para documentos (Facturas, Pedidos). |

### 🌎 Catálogos Globales
| Tabla | PK (ID) | Descripción |
|-------|---------|-------------|
| **monedas** | `id` | Catálogo de divisas (MXN, USD). |
| **impuestos** | `id` | Definición de impuestos (IVA, ISR). |
| **unidades_medida** | `id` | Unidades estándar (Pieza, Kg, Servicio). |

### 🏛️ Catálogos SAT (Facturación)
Tablas estáticas proporcionadas por el SAT para cumplimiento fiscal.
- `sat_uso_cfdi`
- `sat_metodo_pago`
- `sat_forma_pago`
- `sat_clave_prodserv`
- `sat_clave_unidad`
- `sat_regimen_fiscal`

#### Configuración Fiscal (`empresa_sat_config`)
Relación 1:1 con `Empresa`.

| Columna | Tipo | Descripción |
|---------|------|-------------|
| `id_empresa_sat_config` | PK | Identificador único. |
| `empresa_id` | FK | Relación 1:1 con tabla Empresas. |
| `regimen_fiscal_id` | FK | Régimen fiscal (`sat_regimen_fiscal`), opcional. |
| `archivo_cer` | FileField | Ruta al archivo .cer. |
| `no_certificado` | Varchar | Número de certificado. |
| `fecha_expiracion` | DateTime | Vencimiento del certificado. |
| `validado` | Boolean | Indica si el CSD se validó. |
| `mensaje_error` | Text | Motivo del último fallo de validación. |
| `activo` | Boolean | Borrado lógico. |

La tabla no guarda la llave privada (.key), su contraseña ni el CSD en PEM (EC-685): el timbrado usa el CSD cargado en la cuenta de Facturama. Hoy no hay flujo que la llene; el admin la expone en solo lectura.

---

## 👥 Usuarios (`usuarios`)

Gestión de identidades y sesiones.

| Tabla | PK (ID) | Relaciones Clave | Descripción | 
|-------|---------|------------------|-------------| 
| **usuarios** | `id` | `empresa` (FK), `sucursal_default` (FK) | Usuario del sistema. Extiende `AbstractUser` de Django. |
| **(M2M) usuarios_sucursales** | - | `usuario_id`, `sucursal_id` | Define el "Scope" geográfico/físico de acceso del usuario. |
| **(M2M) usuarios_departamentos** | - | `usuario_id`, `departamento_id` | Limita la visualización de datos por área (ej. solo ver "Ventas"). |

---

## 🛡️ Seguridad (`seguridad`)

Sistema de permisos basado en roles (RBAC) y granularidad.

| Tabla | PK (ID) | Relaciones Clave | Descripción |
|-------|---------|------------------|-------------|
| **permisos** | `id` | - | Catálogo estático de capacidades del sistema (ej. `ventas.crear`). |
| **roles** | `id` | `empresa` (FK) | Agrupación de permisos (ej. "Vendedor"). Incluye `clave_departamento` para contexto automático. |
| **usuarios_roles** | `id` | `usuario` (FK), `rol` (FK), `empresa` (FK) | Asigna roles a usuarios. |
| **roles_permisos** | `id` | `rol` (FK), `permiso` (FK) | Tabla intermedia que define qué permisos tiene cada rol. |

---

## 📊 Auditoría (`auditoria`)

Aunque gran parte se maneja en logs de archivo, existen estructuras para el seguimiento.

- **Logs de Acceso**: Gestionados por `django-axes` (tabla `axes_accesslog`) para intentos de login.
- **Auditoría de Cambios**: Implementación vía Logs, pero conceptualmente rastrea `actor`, `acción`, `modelo`, `timestamp` y `changes` (JSON diff).

---

## 📐 Diagramas

### Jerarquía de Organización
```
    Empresa[Empresa (Tenant)] --> Sucursal[Sucursales]
    Sucursal --> Departamento[Departamentos]
    Empresa --> Rol[Roles]
    Empresa --> Usuario[Usuarios]
    Empresa --> SatConfig[Config SAT (CSD)]
```

### Relación de Usuario y Accesos
```
    Usuario -->|Pertenece a| Empresa
    Usuario -->|Tiene acceso a| Sucursal(es)
    Usuario -->|Tiene Rol| Rol
    Rol -->|Define| Permisos
    Rol -->|Puede filtrar por| Departamento
```
