# Problema de identidad de rol — "Mesa de Control"

**Repo:** `nucleo-erp` (Django 6.0 + DRF)
**Estado:** diagnosticado, sin corregir
**Origen del diagnóstico:** investigación de la implementación de notificaciones (commits `b052e07` / `2d76d27`)
**Fecha:** septiembre 2026

---

## 1. Resumen ejecutivo

El sistema necesita responder la pregunta *"¿este usuario pertenece a Mesa de Control?"* en seis lugares distintos del código. **No existe una forma confiable de responderla**, porque el rol no tiene un identificador estable legible por máquina.

El código pregunta por `Rol.codigo == "MESA-DE-CONTROL"`.
La base de datos tiene `Rol.codigo = "MESACONTROL-0002"`.

Nunca coinciden. De ahí se derivan tres fallos independientes, dos de ellos silenciosos.

**Impacto actual:** 4 de los 5 usuarios con rol Mesa de Control están bloqueados en las acciones propias de su rol, y no ven los importes en pedidos ni en compras. El quinto usuario funciona, pero por una razón equivocada (es `is_admin_empresa`), lo cual ha enmascarado el problema.

---

## 2. Datos reales confirmados

Consulta en solo lectura sobre la tabla `roles`, empresa `lazzar-mex-0001` (única empresa en el sistema):

| id | `codigo` | `nombre` | `estatus` | `clave_departamento` |
|---|---|---|---|---|
| 1 | `ventas-0001` | Ventas | activo | `None` |
| 2 | **`MESACONTROL-0002`** | **`Mesa-de-control`** | activo | `None` |
| 3 | `compras-0003` | Compras | activo | `None` |
| 4 | `almacen-mp-0004` | WMS | activo | `None` |
| 5 | `rh-0005` | RH | activo | `None` |
| 6 | `produccion-0006` | Produccion | activo | `None` |
| 7 | `contabilid-0007` | Contabilidad | activo | `None` |

`is_system = False` en las 7 filas. `clave_departamento = None` en las 7 filas.

### Usuarios con el rol `MESACONTROL-0002` (5 titulares)

| email | `is_superuser` | `is_admin_empresa` | `estatus` |
|---|---|---|---|
| `pedidos@lazzar.com.mx` | False | **True** | activo |
| `pedidos@lazzarmexico.com` | False | False | activo |
| `glira@lazzar.com.mx` | False | False | activo |
| `logistica@lazzar.com.mx` | False | False | activo |
| `bordados@lazzarmexico.com` | False | False | activo |

---

## 3. Los tres síntomas

### Síntoma 1 — Notificaciones: nadie las recibe (silencioso)

`notificaciones/services/notificacion_service.py` resuelve destinatarios filtrando:

    UsuarioRol.objects.filter(
        rol__empresa=empresa,
        rol__codigo__iexact=codigo_rol,   # "MESA-DE-CONTROL"
        rol__estatus="activo",
        usuario__estatus="activo",
    )

`"MESACONTROL-0002"` no es igual a `"MESA-DE-CONTROL"` en ningún casing. El queryset devuelve vacío.

El servicio entonces hace:

    if not usuarios_ids:
        return 0

Sin `logger`, sin `warning`, sin excepción.

Y el único llamador (`ventas/api/views.py:1991`, dentro de `enviar_revision`) **descarta el valor de retorno**: no lo asigna, no lo comprueba, no lo incluye en la respuesta HTTP.

Resultado: `enviar_revision` responde **200 OK idéntico** se hayan creado 0 o 5 notificaciones. El fallo es indetectable desde fuera del sistema.

Estado actual de la tabla `notificaciones`: **0 filas**.

> Nota metodológica: hay 0 cotizaciones en `estatus=2`, así que el endpoint aparentemente nunca se ha ejercido. El conteo en cero es *consistente* con el fallo pero no lo prueba. La prueba es la comparación de cadenas.

---

### Síntoma 2 — Autorización de Mesa de Control: 4 de 5 usuarios bloqueados (silencioso hasta que alguien lo intenta)

`_require_mesa_control` evalúa en este orden:

1. `is_superuser` → pasa
2. `is_admin_empresa` → pasa
3. tiene rol Mesa de Control → pasa
4. si no → `ValidationError {"permiso": "Acción disponible solo para mesa de control."}` (**HTTP 400**)

El branch 3 fue **añadido por `b052e07`**. Antes de ese commit, la función solo aceptaba superuser y admin de empresa.

Como el branch 3 nunca evalúa a `True` con los datos actuales, **es código muerto**. Solo `pedidos@lazzar.com.mx` pasa, y pasa por el branch 2.

Los otros 4 usuarios reciben 400 en las **6 acciones protegidas**:

| ViewSet | Acción | Línea |
|---|---|---|
| `CotizacionViewSet` | (acción de mesa de control) | `ventas/api/views.py:2027` |
| `CotizacionViewSet` | (acción de mesa de control) | `ventas/api/views.py:2097` |
| `CotizacionViewSet` | (acción de mesa de control) | `ventas/api/views.py:2114` |
| `CotizacionViewSet` | (acción de mesa de control) | `ventas/api/views.py:2162` |
| `PedidoViewSet` | `editar_mesa_control_contexto` | `ventas/api/views.py:2723` |
| `PedidoViewSet` | `editar_mesa_control` | `ventas/api/views.py:2731` |

> **Implicación importante:** las dos últimas son el feature de *edición de pedido desde Mesa de Control*. Si ese feature se verificó con el usuario admin, funcionó por la razón equivocada. Para su público real está inutilizable.

Lo que esos 4 usuarios **sí** pueden hacer: todo lo que no pase por `_require_mesa_control` — listar, leer, crear y editar cotizaciones según el `get_queryset` normal del ViewSet.

---

### Síntoma 3 — Visibilidad de importes: se ocultan a esos mismos 4 (visible hoy)

Mecanismo completamente distinto: `puede_ver_contabilidad`, en
`ventas/services/pedido_field_filter_service.py:23` y
`compras/services/orden_compra_view_service.py:18`.

Normaliza con `re.sub(r"[^A-Z0-9]", "", s.upper())` los **tres** campos del rol (`codigo`, `nombre`, `clave_departamento`) y compara por pertenencia a un set de tokens:

    {"MESACONTROL", "VENTAS", "CONTAVENTAS", "MESACONTROLYVENTAS"}
    # el de compras extiende con: "COMPRAS", "CONTABILIDAD", "CONTACOMPRAS"

Para el rol 2:

| Campo | Valor | Normalizado | ¿En el set? |
|---|---|---|---|
| `codigo` | `MESACONTROL-0002` | `MESACONTROL0002` | ❌ |
| `nombre` | `Mesa-de-control` | `MESADECONTROL` | ❌ |
| `clave_departamento` | `None` | — | ❌ |

Resultado: `puede_ver_contabilidad` devuelve `False`. **A esos 4 usuarios se les ocultan los montos en pedidos y en órdenes de compra.**

Este es el único de los tres síntomas con efecto observable hoy sin necesidad de ejercitar un flujo nuevo.

> Contraste que aísla el problema: `Ventas` sí matchea limpio (`nombre="Ventas"` → `VENTAS`). El mecanismo funciona; lo que falla es específicamente la nomenclatura de este rol.

---

## 4. Causas estructurales

### A. `codigo` nunca fue un identificador — es una etiqueta

Se genera por **tres caminos distintos e inconsistentes**:

| Ruta | Algoritmo | Produce |
|---|---|---|
| `seguridad/views.py:30` (`RolCreateView`, HTML, solo superuser) | `f"{slugify(nombre)[:10].strip('-')}-{pk:04d}"` | `ventas-0001` |
| `ia/api/views.py:608` (herramienta del asistente IA) | `f"{slugify(nombre)[:10]}-{rol.pk}"` | `ventas-8` (sin padding) |
| `seguridad/forms.py:7` + `seguridad/api/serializers.py:15` | **texto libre editable** | cualquier cosa |

Observaciones:

- `MESACONTROL-0002` **no pudo salir de ninguno de los dos algoritmos automáticos**: `slugify("Mesa-de-control")[:10]` da `"mesa-de-co"`, en minúsculas. El valor real tiene mayúsculas y 11 caracteres antes del guion. Fue escrito a mano o introducido por una migración de datos.
- `codigo` es **escribible** desde `RolForm` y desde `RolSerializer` (`read_only_fields` solo cubre `created_at`/`updated_at`). Cualquiera puede romper las seis comparaciones desde la UI.
- El sufijo numérico es el **PK global de `Rol`**, no una secuencia por empresa. Con una segunda empresa, el mismo rol conceptual tendría otro número (`ventas-0001` y `ventas-0008`).
- Unicidad: `UniqueConstraint(["empresa", "codigo"])` — por empresa, no global.

### B. Seis sitios hardcodean el rol, con cuatro formas distintas de comparar

| Archivo:línea | Literal | Campo comparado | Mecanismo | Scope empresa |
|---|---|---|---|---|
| `ventas/api/views.py:1432` | `"MESA-DE-CONTROL"` | `rol.codigo` | `__iexact` | **NO** |
| `ventas/api/views.py:2422` | `"MESA-DE-CONTROL"` | `rol.codigo` | igualdad exacta | sí |
| `ventas/api/views.py:1993` | `"MESA-DE-CONTROL"` | `rol.codigo` (vía `codigo_rol=`) | `__iexact` | sí |
| `notificaciones/services/notificacion_service.py:47` | param `codigo_rol` | `rol.codigo` | `__iexact` | sí |
| `ventas/services/pedido_field_filter_service.py:23` | set de 4 tokens | `codigo` OR `nombre` OR `clave_departamento` | normalización | — |
| `compras/services/orden_compra_view_service.py:18` | set de 7 tokens | `codigo` OR `nombre` OR `clave_departamento` | normalización | — |

**No existe ninguna constante, enum o `choices` compartida.** Cada uno es un literal independiente.

Los dos sets `TOKENS_ROL_VER_TODO` están **duplicados textualmente** en ventas y compras, con contenido divergente (el de compras extiende el de ventas).

### C. Hay DOS `_require_mesa_control`, no una

En el mismo archivo `ventas/api/views.py`, en dos ViewSets distintos, con lógica divergente:

| Línea | ViewSet | Comparación | Sensible a mayúsculas | Filtra por empresa |
|---|---|---|---|---|
| 1423 | `CotizacionViewSet` | `rol__codigo__iexact` | no | **NO** |
| 2411 | `PedidoViewSet` | `rol__codigo` exacto | **sí** | sí |

**La de la línea 1423 no filtra por empresa.** Esto es un riesgo de aislamiento multi-tenant independiente del bug de matching: si en el futuro existe una segunda empresa con un rol cuyo `codigo` coincida, un usuario de una empresa podría pasar la autorización por el rol de otra.

`b052e07` solo tocó la de la línea 1423. La de 2411 es anterior y quedó sin tocar.

Inconsistencia adicional: el servicio de notificaciones exige `usuario__estatus="activo"`; **ninguna** de las dos `_require_mesa_control` mira el estatus del usuario.

### D. El identificador correcto existe y está vacío

`Rol.clave_departamento` — `CharField(50)`, nullable, con `help_text` que dice literalmente *"ej. 'VENTAS'"*.

- **Dos servicios ya lo consultan** (los de tokens): alguien lo diseñó para exactamente este propósito.
- Está `None` en las 7 filas.
- No se puebla en ninguna ruta de creación.
- El diseño quedó a medias.

### E. El fallo es invisible por construcción

Tres capas de silencio, apiladas:

1. El servicio no loguea al resolver 0 destinatarios.
2. El llamador descarta el valor de retorno.
3. El usuario `is_admin_empresa` pasa por otro branch y enmascara el problema para quien pruebe con él.

---

## 5. Alcance del daño — resumen

| Afectado | Qué le pasa | ¿Visible? |
|---|---|---|
| Los 5 usuarios de Mesa de Control | No reciben ninguna notificación | No (nunca ha habido ninguna) |
| 4 usuarios no-admin | 400 en las 6 acciones de mesa de control | Sí, si lo intentan |
| 4 usuarios no-admin | No ven importes en pedidos ni en compras | **Sí, hoy** |
| `pedidos@lazzar.com.mx` (admin) | Funciona todo, por el branch equivocado | — |
| Feature "editar pedido desde Mesa de Control" | Inutilizable para su público real | No, si se probó con el admin |

**No afectado:** los literales `R-MESACONTROL*` en `nucleo/api/search.py` y `nucleo/tests.py` son claves de `Permiso.clave`, un sistema distinto.

---

## 6. Opciones de corrección

### Fase 1 — Hotfix de datos (tamaño 1, sin código, sin deploy)

Un solo `UPDATE` sobre la fila `id=2`:

    codigo = "MESA-DE-CONTROL"
    clave_departamento = "MESACONTROL"

Cobertura:

| Síntoma | Campo que lo arregla |
|---|---|
| Notificaciones (`__iexact`) | `codigo` |
| `_require_mesa_control` ×2 (iexact + exacto) | `codigo` |
| `puede_ver_contabilidad` | `clave_departamento` |

> **Advertencia:** cambiar solo `codigo` **no** arregla el síntoma 3. `"MESA-DE-CONTROL"` normalizado da `MESADECONTROL`, que sigue sin estar en el set. Se requieren **ambos** campos.

Verificar antes de aplicar: que `codigo` no se muestre en alguna vista donde romper el patrón `slug-NNNN` sea un problema visual.

Reversible. No toca esquema. Desbloquea a 4 usuarios de inmediato.

### Fase 2 — Refactor (tamaño 3, prompt aparte)

Por superficie, no por dificultad — nada es intelectualmente complejo, pero toca muchos puntos y el área es autorización.

1. **`clave_departamento` como llave canónica.** Backfill de las 7 filas. Poblarla en las tres rutas de creación. Hacerla no libremente editable. Migrar los 6 call sites a leerla a ella y no a `codigo`.
2. **Constante compartida.** Un módulo de constantes o `TextChoices`. Deduplicar los dos sets `TOKENS_ROL_VER_TODO`.
3. **Unificar las dos `_require_mesa_control`.** Incluye corregir el filtro por empresa faltante en la de la línea 1423 — ese es el riesgo real, independiente de todo lo demás. Unificar también el chequeo de `usuario.estatus`.
4. **Hacer el fallo ruidoso.** `logger.warning` cuando el servicio resuelve 0 destinatarios; que el llamador deje de descartar el retorno. *Esta es la parte de mayor valor a largo plazo: sin ella, el próximo bug de esta clase también será invisible.*
5. **`codigo` deja de ser identificador.** Que quede como etiqueta humana. Tres algoritmos de generación divergentes es la señal de que nunca fue una llave.

**Recorte posible (baja a tamaño 2):** hacer solo los puntos 3 y 4. El hotfix sostiene el sistema mientras tanto.

### Riesgo de no hacer nada

Cada módulo nuevo que necesite preguntar "¿es Mesa de Control?" agregará un séptimo literal con una quinta variante de comparación, y fallará en silencio igual.

---

## 7. Verificación en vivo pendiente

Con `glira@lazzar.com.mx` (no admin):

1. Intentar editar un pedido desde Mesa de Control → se espera **400**.
2. Abrir un pedido y una orden de compra → se espera **no ver los importes**.
3. Comparar con `pedidos@lazzar.com.mx` (admin) sobre el mismo pedido → confirmar que la única diferencia es el flag `is_admin_empresa`.

Tras aplicar el hotfix, repetir los tres y confirmar que los tres cambian.

---

## 8. Ambigüedades no resueltas

- **No se determinó si `MESACONTROL-0002` fue editado a propósito o es residuo de una migración de datos.** El valor no es reproducible por ninguna ruta de código actual. `AuditoriaEvento` podría tener el rastro. Determinarlo cambia si el problema es "un dato mal escrito" o "una convención distinta que el código nunca conoció".
- **No se sabe cuál de los tres campos era el identificador previsto por el diseño original.** `clave_departamento` tiene el `help_text` correcto y dos consumidores, pero está vacío en todas las filas.
- **No hay evidencia de que `enviar_revision` se haya ejecutado nunca en producción** (0 cotizaciones en `estatus=2`). No se sabe si es porque el flujo es nuevo o porque los 4 usuarios no-admin nunca llegan a poder usarlo.

---

## 9. Deuda relacionada, fuera de alcance

- `UsuarioRol` tiene los `indexes` **comentados** — cada chequeo de rol hace seq scan.
- `UsuarioRol.empresa` está denormalizada pero **ningún** call site la usa (filtran por `rol__empresa` o por nada).
- `notificaciones/` **no tiene tests**. Cobertura cero en todo el módulo.
- Reparto actual de usuarios por rol: Ventas 11, Mesa-de-control 5, WMS 4, Compras 4. RH, Producción y Contabilidad sin usuarios.
