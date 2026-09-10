# Responsabilidades frontend / backend / DB — validación de capas

Repo: `nucleo-erp` (backend). Frontend Next.js en otro repo, no auditado — esta revisión valida qué responsabilidad asume realmente cada capa **del lado backend**, contra lo que debería según el modelo declarado en `CLAUDE.md`.

## Modelo esperado

```
Frontend  -> presentación + UX. NO decide reglas de negocio ni seguridad.
Backend   -> única fuente de verdad: multi-tenant, RBAC, reglas de negocio, integridad.
DB        -> último backstop: constraints, FKs, defaults. Nada que dependa solo de la app.
```

---

## 1. Donde la separación SÍ se respeta (contraste)

| Patrón | Evidencia | Por qué es correcto |
|---|---|---|
| Scoping multi-tenant | `get_queryset()` filtra por `empresa` en cada ViewSet tenant-scoped (patrón canónico en `ventas/api/views.py`) | el backend nunca confía en que el cliente mande el filtro correcto |
| `empresa` no es escribible por el cliente | `read_only_fields` incluye `empresa` en `ventas`, `terceros`, `inventarios`, `nucleo`, `wms`; `EmpresaResueltaEnServidorMixin` en `finanzas/api/serializers.py:32-81` | el servidor resuelve la empresa desde `user.empresa`, no desde el body |
| Servicios agnósticos de framework | `finanzas/exceptions.py`: `ErrorDeNegocio` hereda de `django.core.exceptions.ValidationError`, no de la de DRF; se traduce a 400 solo en la frontera HTTP (`ErroresDeNegocioComo400Mixin`, `finanzas/api/views.py`) | la regla de negocio vive en el service, no en la vista; reusable desde management commands/admin |

---

## 2. Fugas de responsabilidad encontradas

### 2.1 RBAC granular: decide el frontend, no el backend (severidad alta)

`Permiso`/`Rol`/`UsuarioRol` existe y `permisos_efectivos(user)` calcula el set completo — pero casi ningún ViewSet lo usa como gate.

Barrido de `permission_classes` en todo el repo:

| Clase usada | Apps | Qué valida realmente |
|---|---|---|
| default (`IsAuthenticated` global, `REST_FRAMEWORK` en `ERP/settings.py:307-309`) | `ventas`, `compras`, `produccion`, `finanzas`, `wms`, `terceros`, `catalogo`, `logistica`, `notificaciones` | solo "está logueado" |
| `IsAuthenticatedAndScoped` (`inventarios/api/views.py:36-51`) | `inventarios` | lectura a cualquiera, escritura a `is_superuser`/`is_admin_empresa` — no consulta `Permiso` |
| `IsSuperUserOrReadOnly` (`seguridad/api/api_views.py:9-24`, `usuarios/api/api_views.py:13-22`) | `seguridad`, `usuarios` | binario superuser sí/no |

**Único enforcement real de `Permiso.clave`**: `/api/v1/search/` (`nucleo/api/search.py:331`), que filtra grupos de entidades por `permisos_visibilidad`. En todos los demás endpoints, `tiene_permiso()`/`permisos_efectivos()` solo se usa para **construir la lista que se le manda al login** — la UI decide qué mostrar/ocultar con esa lista, pero un usuario autenticado sin el permiso puede llamar el endpoint directo (Postman, DevTools) y el backend no lo detiene. Esto es textual en `CLAUDE.md` ("la mayoría de los endpoints no aplican permisos del lado servidor"); este barrido lo confirma con evidencia de código, no es una suposición.

**Riesgo real**: depende de cuánto se confíe en la UI como único gate. Para un ERP con módulos sensibles (finanzas, RH) es una superficie de autorización incompleta, no solo una nota de diseño.

### 2.2 Validación a nivel modelo: código muerto (severidad baja, pero engañoso)

`Model.clean()` solo existe en 2 lugares de todo el repo (`hr/models.py:222-231` en `Contrato`, `:252-257` en `Turno`). **`full_clean()` no se llama en ningún sitio del código** (`grep` exhaustivo, cero resultados fuera de `venv`).

DRF `ModelSerializer` **no** invoca `full_clean()` — solo `Model.full_clean()` vía `ModelForm` lo haría, y aquí no hay `ModelForm` en el camino de la API. Consecuencia: esas dos reglas de `clean()` ("un empleado no puede tener dos contratos activos", "hora de salida > hora de entrada") están duplicadas a mano en `hr/api/serializers.py:174-187` y `:195-205` — que es lo que realmente protege el endpoint. El `clean()` del modelo es decorativo: solo se ejecutaría si alguien llama `full_clean()` explícitamente desde un management command, el admin, o una migración de datos — y nadie lo hace.

No es un bug hoy (la regla sí está aplicada, solo que en la capa de arriba), pero es una trampa: alguien que agregue un `clean()` nuevo pensando que Django lo va a invocar automáticamente en un `save()` normal se equivoca — Django nunca llama `full_clean()` por su cuenta.

### 2.3 `on_delete` inconsistente: sin backstop de integridad en la cadena financiera (severidad media-alta)

El proyecto tiene un patrón de soft-delete explícito (`StatusLifecycleModel.activo` + `.soft_delete()`) — pero a nivel de FK, borrar un `Cliente`/`Proveedor`/`Producto` de verdad (admin, shell, `.delete()` directo) **no está bloqueado por la DB**:

| Modelo → FK | `on_delete` | Efecto de borrar el padre |
|---|---|---|
| `finanzas.Factura.cliente`, `CuentaPorCobrar.cliente`, `Cobro.cliente`, `NotaCredito.cliente` | `CASCADE` | borra facturas, CxC, cobros y notas de crédito del cliente |
| `finanzas.FacturaProveedor.proveedor`, `CuentaPorPagar.proveedor`, `Pago.proveedor` | `CASCADE` | borra facturas de proveedor, CxP y pagos |
| `finanzas.FacturaDetalle.producto`, `FacturaProveedorDetalle.producto` | `CASCADE` | borra líneas de factura ligadas a ese producto |
| **vs.** `ventas.PedidoDetalle.producto`, `inventarios.Existencia.producto`, `MovimientoInventarioDetalle.producto` | `PROTECT` | bloquea el borrado — correcto |

Misma relación conceptual (producto referenciado desde una línea de documento), dos comportamientos opuestos según el módulo. En `finanzas` — el módulo donde perder historial es más grave — es CASCADE en todos los casos revisados. No hay ningún `CHECK`/trigger de DB que lo compense: si alguien borra un `Cliente` desde el admin de Django o un shell, el historial contable de ese cliente desaparece en cascada, silenciosamente, sin pasar por ningún guard de negocio.

### 2.4 Referencias cruzadas (ya documentadas, no se repiten aquí)

- **Estados declarados que nadie ejerce** (`Picking.estado`, `Packing.estado`, `Pedido.estatus`): el modelo declara la responsabilidad de una máquina de estados, pero ni backend ni frontend la recorren — no hay endpoint que la mueva. Ver [flujo-cotizacion-pedido-wms-inventario.md](flujo-cotizacion-pedido-wms-inventario.md) §5-6.
- **Migraciones no corren en el deploy de Vercel**: hay una ventana donde el código backend asume un esquema que la DB de producción todavía no tiene. Ver [flujo-comunicacion-nextjs-django-postgres.md](flujo-comunicacion-nextjs-django-postgres.md) §5.

---

## 3. Tabla resumen

| Responsabilidad | Dueño esperado | Dueño real hoy | Gap |
|---|---|---|---|
| Multi-tenant scoping | Backend | Backend | ninguno |
| Autoridad sobre `empresa` en writes | Backend | Backend | ninguno |
| RBAC granular por `Permiso.clave` | Backend | **Frontend** (solo UI oculta/muestra) | alto |
| Reglas de negocio (montos, transiciones) | Backend (service) | Backend (service + duplicado en serializer en algún caso) | bajo |
| Integridad referencial al borrar catálogos | DB (constraint) | Backend (convención `activo`, sin backstop en DB) | medio-alto en `finanzas` |
| Esquema de DB sincronizado con el código | Pipeline de deploy | Job aparte en GitHub Actions, no atómico con el deploy | conocido, documentado |

## 4. Recomendaciones, en orden de impacto

1. **RBAC server-side real**: envolver los ViewSets sensibles (`finanzas`, `hr`, `seguridad`) con una `DRFPermission` que llame `tiene_permiso()`/`permisos_efectivos()`, no solo `IsAuthenticated`. Empezar por los módulos con datos más sensibles.
2. **`on_delete=PROTECT` en la cadena financiera**: cambiar `Cliente`/`Proveedor`/`Producto` → `Factura*`/`CuentaPor*`/`Cobro`/`Pago`/`NotaCredito` de `CASCADE` a `PROTECT`. Requiere migración; bajo riesgo funcional porque el borrado real de catálogos ya debería pasar por soft-delete, no por `.delete()`.
3. Documentar (comentario en el modelo) que `Model.clean()` no se invoca automáticamente en este proyecto, para que nadie vuelva a depender de él sin saberlo — o eliminarlo y dejar la regla solo en el serializer para no mantener dos copias.
