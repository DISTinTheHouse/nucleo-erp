# Bloqueo: alta de líneas hijas por API en finanzas

Documento de contexto sobre el bloqueo estructural que impide crear documentos con líneas anidadas en el módulo `finanzas` de `nucleo-erp`. Cubre la causa, los cinco documentos afectados, los tickets que bloquea, y las decisiones de negocio que hay que tomar antes de arreglarlo.

Repo backend: `nucleo-erp` — Django 6.0 + DRF. Todo vive en la app `finanzas`, bajo `/api/v1/finanzas/`.

---

## 1. Resumen en una línea

Cinco documentos de finanzas tienen estructura encabezado + líneas. El encabezado se crea bien por API. **Las líneas hijas no se pueden crear por ninguna ruta HTTP.** Sin líneas, esos documentos no sirven: una póliza sin cargos/abonos no cuadra, una factura sin partidas no tiene monto, un pago sin detalles no aplica a ninguna CxP.

---

## 2. Los cinco documentos afectados

| Documento | Endpoint padre | Modelo detalle | Router propio del detalle |
|---|---|---|---|
| Cobro | `/api/v1/finanzas/cobros/` | `CobroDetalle` | No |
| Pago | `/api/v1/finanzas/pagos/` | `PagoDetalle` | No |
| Póliza | `/api/v1/finanzas/polizas/` | `PolizaDetalle` | No |
| Nota de crédito | `/api/v1/finanzas/notas-credito/` | `NotaCreditoDetalle` | No |
| Factura de proveedor | `/api/v1/finanzas/facturas-proveedor/` | `FacturaProveedorDetalle` | No |

Ninguno de los cinco modelos de detalle está registrado en `finanzas/api/urls.py`, así que tampoco hay una ruta suelta tipo `POST /poliza-detalles/` como alternativa al alta anidada.

---

## 3. La causa: dos patrones distintos, mismo efecto

El bloqueo tiene **dos causas mecánicas diferentes** según el documento. Ambas terminan en lo mismo: no hay forma de meter líneas.

### Causa A — el serializer padre declara el detalle `read_only=True`

Afecta a: **Póliza, Nota de crédito, Factura de proveedor.**

El serializer del encabezado incluye el arreglo de detalles pero lo marca `read_only=True`. DRF entonces **descarta silenciosamente** ese campo en el POST: acepta el request, responde 201, y crea el encabezado sin líneas. No da error, lo cual lo hace más engañoso — "el request tuvo éxito" no significa "se crearon las líneas".

Combinado con que el modelo de detalle no tiene router propio, no existe ninguna vía HTTP para crear una línea.

| Serializer padre | Campo anidado | Estado |
|---|---|---|
| `PolizaSerializer` | `poliza_detalles` | `read_only=True` |
| `NotaCreditoSerializer` | `nota_credito_detalles` | `read_only=True` |
| `FacturaProveedorSerializer` | `factura_proveedor_detalles` | `read_only=True` |

Detalle medido (corrige investigación previa): `PolizaDetalleSerializer.poliza` **no** es un campo de escritura obligatorio — sale `required=False, allow_null=True` porque la FK del modelo es `on_delete=SET_NULL, null=True`. No cambia el bloqueo: el padre declara el anidado `read_only=True` de todos modos, así que el detalle es inalcanzable en el create del padre.

### Causa B — el serializer de detalle exige la FK del padre

Afecta a: **Cobro, Pago.**

Aquí el campo anidado del padre **sí** es escribible (`many=True`), pero el serializer del detalle usa `fields = "__all__"`, lo que convierte la FK al padre (`cobro` / `pago`) en un campo **requerido de escritura**. Eso crea un callejón sin salida:

- Al crear el documento con sus líneas anidadas, el padre todavía no existe, así que no puedes mandar su id → **400** `{"cobro":["Este campo es requerido."]}`.
- Si mandas el id del padre de todos modos, `perform_create` hace `CobroDetalle.objects.create(cobro=cobro, **d)` con `d` ya conteniendo `cobro` → **`TypeError: got multiple values for keyword argument 'cobro'`** → **500**.

| Serializer detalle | FK padre | `required` | Anidado en el padre |
|---|---|---|---|
| `CobroDetalleSerializer` | `cobro` | True | `read_only=False` (escribible) |
| `PagoDetalleSerializer` | `pago` | True | `read_only=False` (escribible) |

### Agravante en Cobro y Pago: el estatus por default fuerza la aplicación

`Cobro.estatus` y `Pago.estatus` tienen default `"Aplicado"`, y `aplicar_cobro` / `aplicar_pago` exigen al menos un detalle. Así que la salida "crear sin detalles" tampoco funciona para el caso por default. Pasar `estatus="Borrador"` evita que corra el servicio de aplicación, pero **las dos ramas del detalle mueren igual** — una en la validación del serializer, la otra en `perform_create`.

---

## 4. Medición real (cobros y pagos)

Ambos medidos contra SQLite en memoria, usuario normal (no superusuario), sin mandar `empresa`.

### Cobros

    [POST /cobros/ | detalle SIN "cobro"]  -> 400 {"cobro_detalles":[{"cobro":["Este campo es requerido."]}]}
    [POST /cobros/ | detalle CON "cobro"]  -> TypeError ... got multiple values for 'cobro'  (=> 500)

### Pagos

    [APLICADO  | detalle SIN "pago"]           -> 400 {"pago_detalles":[{"pago":["Este campo es requerido."]}]}
    [APLICADO  | detalle CON "pago" válido]    -> TypeError ... got multiple values for 'pago'  (=> 500), Pago padre queda huérfano
    [APLICADO  | SIN pago_detalles]            -> 400 "El pago debe tener al menos un detalle."
    [BORRADOR  | detalle SIN "pago"]           -> 400 (igual)
    [BORRADOR  | detalle CON "pago" válido]    -> 500 (igual TypeError)

Verdicto medido: **crear un Pago o un Cobro con detalles es imposible hoy por cualquier ruta HTTP.**

Matiz importante: un Pago **sin** detalles sí se crea (`estatus="Borrador"` + `fecha_pago` explícita → 201). Lo imposible es un Pago **con** detalles.

---

## 5. Bug relacionado pero independiente: `fecha_pago` / `fecha_cobro`

Descubierto durante la medición de pagos. **No es el bloqueo de líneas hijas**, pero vive en los mismos dos modelos y se está atacando por separado.

`Pago.fecha_pago` y `Cobro.fecha_cobro` son `DateField(default=timezone.now)`. `timezone.now` devuelve datetime, no date. Un documento creado sin fecha explícita guarda un datetime en un DateField, y DRF se niega a serializarlo → 500. El crash ocurre en `mixins.create` **después** de que `perform_create` (`@transaction.atomic`) ya hizo commit, así que **la fila persiste y el cliente recibe 500 sin conocer el id**. Se acumulan documentos huérfanos en silencio.

Este bug tiene su propio prompt de fix en curso. Se menciona aquí solo porque cualquier trabajo sobre el alta de Pago/Cobro lo va a topar. No es decisión de negocio: es un default mal escrito.

---

## 6. Tickets bloqueados por esto

| Ticket | Documento | Estatus | Qué falta además del alta de líneas |
|---|---|---|---|
| EC-134 | Pagos a proveedores | 🔴 Bloqueado | Confirmado por medición: mismo bloqueo que cobros. También arrastra el bug de `fecha_pago` |
| EC-138 | Notas de crédito | 🔴 Bloqueado | `cancelar` no revierte el saldo de CxC; `folio` no se autogenera |
| EC-141 | Pólizas | 🔴 Bloqueado | `folio` no se autogenera; `validar-cuadre` devuelve `True` hardcodeado |
| EC-142 | Facturas de proveedor | 🔴 Bloqueado | No genera CxP al registrarse; `oc` y `recepcion` son NOT NULL; sin campos SAT |

Cobros no tiene ticket propio en esta tanda, pero comparte exactamente el mismo bloqueo y es el espejo simétrico de pagos.

**Los cuatro tickets comparten la misma raíz.** No son cuatro problemas — es uno. Un solo prompt de backend que habilite el alta de líneas los desbloquea juntos. Por eso conviene resolverlo como un paquete y no ticket por ticket.

---

## 7. Por qué NO es un fix mecánico

Quitar el `read_only` y escribir un `create()` anidado es la parte fácil. Lo que lo hace riesgoso es que **cada documento arrastra reglas de negocio contables que hoy no tienen dónde correr en el alta**:

- **Póliza** debe cuadrar: `total_cargos == total_abonos`. Hoy `PolizaService.validar_suma_cero` existe pero se dispara en `contabilizar`, no en el alta. Si se permite crear líneas, hay que decidir dónde y cuándo se valida el cuadre.
- **Nota de crédito** debería impactar la CxC al emitirse (hoy sí lo hace en emisión, pero `cancelar` no lo revierte). Crear detalles cambia el monto de ese impacto.
- **Factura de proveedor** probablemente debería generar su CxP al registrarse (hoy nada la origina). Eso ya no es DRF, es diseño de flujo.
- **Documentos ya aplicados:** un pago `Aplicado` ya movió saldos. Permitir editar sus líneas después reabre la reversión.
- **`update()` es peor que `create()`:** al editar un documento con líneas, ¿se reemplaza todo el arreglo, o upsert por id? El proyecto ya tiene precedente decidido en otro módulo (upsert por id, guard por identidad posicional, no por longitud del arreglo). Ese precedente debería mandar aquí.

Si se resuelve de un solo tiro sin decidir esto primero, Claude Code tendría que **inventar las reglas de negocio contables que faltan** — justo lo que no se quiere en un módulo contable.

---

## 8. Decisiones de negocio que hay que tomar ANTES de implementar

1. **¿Entra `update()` o solo `create()`?** Empezar solo con alta y diferir edición reduce muchísimo el riesgo.
2. **¿Qué pasa con documentos ya aplicados?** ¿Se pueden editar sus líneas, o quedan inmutables una vez `Aplicado`/`Contabilizada`/`Emitida`?
3. **¿Dónde valida la póliza el cuadre?** ¿En el alta, o se permite crear una póliza descuadrada en `Borrador` y validar solo al contabilizar?
4. **¿Crear partidas de factura de proveedor genera la CxP automáticamente?** Si sí, con qué `fecha_vencimiento` y qué estatus inicial.
5. **¿Cancelar una nota de crédito debe revertir el saldo de CxC?** Hoy no lo hace. Cobros y pagos sí revierten al cancelar; nota de crédito es la excepción.
6. **Estrategia de reemplazo en edición** (si `update()` entra): reemplazo total del arreglo vs upsert por id. Precedente del proyecto: upsert por id.

Estas son decisiones de flujo financiero, no técnicas. El dueño del producto (o quien conozca la intención original de por qué los detalles quedaron `read_only`) debería cerrarlas antes de que se escriba código.

---

## 9. Ruta recomendada

Tres fases, no un solo prompt:

1. **Investigación** (Claude Code, read-only): leer los cinco serializers de detalle, reportar exactamente qué falta en cada uno, dónde viven hoy las validaciones de negocio de cada documento, y qué efectos secundarios dispara cada servicio (`aplicar_pago`, `aplicar_cobro`, `aplicar_nota_credito`, `validar_suma_cero`). Sin escribir código.
2. **Decisión** (el dueño): cerrar las seis preguntas de la sección 8, alcance por documento, `create()` vs `update()`.
3. **Implementación** (Claude Code), con esas decisiones ya fijadas y una restricción dura de no inventar reglas de negocio: si algo no está decidido, para y reporta.

Alternativa pragmática: no atacar los cinco a la vez. Empezar por el par más simétrico y mejor entendido (Pago/Cobro, que solo necesitan la Causa B resuelta + el fix de `fecha_pago`), entregar EC-134, y dejar Póliza / Nota / Factura para una segunda ronda con sus reglas de cuadre e impacto ya decididas.

---

## 10. Restricciones del proyecto que aplican a este trabajo

- **Preservación del contrato de API es restricción dura.** Habilitar el alta de líneas ES un cambio de contrato de entrada — por eso quedó fuera de los fixes anteriores, que tenían prohibido tocar serializers.
- Un prompt por repo. Este trabajo es 100% backend (`nucleo-erp`); no mezclar con frontend.
- Nombres de campo del contrato se preservan en español verbatim.
- Los fixes que cambian contrato se documentan y se difieren, nunca se aplican en silencio. Este es exactamente uno de esos: documentado aquí, pendiente de decisión.
- Multi-tenant: cualquier alta de línea debe validar que la CxC/CxP/cuenta referenciada sea de la empresa del documento padre. La validación por línea de cobros/pagos ya se corrigió (leía `cuenta_por_cobrar`/`cuenta_por_pagar`, campos inexistentes; los reales son `cxc`/`cxp`), pero al habilitar el alta hay que confirmar que corra en todas las rutas.
