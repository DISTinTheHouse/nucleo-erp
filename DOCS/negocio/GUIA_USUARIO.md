# 📘 Guía de Uso e Implementación Inicial

Esta guía describe el flujo recomendado para poner en marcha el ERP y operar los módulos principales con orden, trazabilidad y consistencia de datos.

## 🚀 Fase 1: Configuración Global (Super Admin)

Antes de crear empresas y usuarios, el sistema debe tener listos sus catálogos base.

1. **Carga de catálogos globales**:
   - Verifica que existan las **Monedas** necesarias, por ejemplo `MXN` y `USD`.
   - Verifica los **Impuestos** base, como `IVA 16%`.
   - Carga las **Unidades de Medida** que se utilizarán en productos, compras y producción.
   - Los catálogos del SAT se consideran parte del entorno base del sistema.

2. **Revisión de documentación técnica y endpoints**:
   - La referencia funcional para frontend se concentra en `DOCUMENTACION_API.md`.
   - La documentación interactiva de API está disponible en `/api/docs/`.

---

## 🏢 Fase 2: Alta de Organización (Empresa)

En esta fase se crea la estructura operativa de cada empresa.

1. **Crear empresa**:
   - Ve a _Organización > Empresas > Nueva_.
   - Captura razón social, RFC, moneda base y datos generales.
   - El sistema valida formato y consistencia del RFC.

2. **Configuración fiscal (SAT)**:
   - Ve a _Organización > Empresas > Configuración SAT_.
   - Carga los archivos `.cer` y `.key` con su contraseña.
   - El sistema valida:
     - correspondencia entre archivos
     - contraseña correcta
     - RFC del certificado
     - vigencia del certificado

3. **Crear sucursales**:
   - Ve a _Organización > Sucursales_.
   - Crea al menos una sucursal operativa, por ejemplo `Matriz`.
   - Define dirección, código postal y datos de contacto.

4. **Crear departamentos**:
   - Ve a _Organización > Departamentos_.
   - Define áreas operativas por sucursal, por ejemplo `Ventas`, `Almacén`, `Producción`.
   - Usa códigos claros para facilitar asignaciones y filtros por rol.

5. **Configurar series y folios**:
   - Define series para documentos operativos como pedidos, facturas y recepciones.
   - Para recepciones, configura al menos una serie activa como `RC`, `RT` o `RZ`.

---

## 🛡️ Fase 3: Seguridad y Accesos

Aquí se define quién puede operar en cada empresa, sucursal y módulo.

1. **Definir roles**:
   - Ve a _Seguridad > Roles_.
   - Crea roles operativos, por ejemplo `Vendedor`, `Compras`, `Mesa de Control`, `Gerencia`.
   - Si usas clave de departamento, el sistema puede acotar visibilidad automáticamente.

2. **Alta de usuarios**:
   - Ve a _Seguridad > Usuarios_.
   - Asigna:
     - empresa
     - sucursal default
     - sucursales permitidas
     - roles

3. **Permisos en frontend**:
   - El frontend utiliza los permisos devueltos por login para habilitar acciones de lectura, edición y eliminación por módulo.
   - Si un usuario no tiene acceso a una empresa o sucursal, no debe ver información de ese contexto.

---

## ✅ Fase 4: Operación Diaria

Con la configuración inicial terminada, el usuario puede empezar a operar.

1. **Login**:
   - El usuario inicia sesión con sus credenciales.

2. **Dashboard**:
   - El sistema muestra información según empresa, sucursal, rol y permisos asignados.

3. **Transacciones**:
   - Los documentos toman automáticamente el contexto del usuario autenticado.
   - Cuando aplica, el sistema genera folios desde la serie configurada de la sucursal.

### 📦 Inventarios

1. **Consultar existencias**:
   - Ve a _Inventarios > Existencias_.
   - El sistema consulta existencias por producto y mantiene compatibilidad con variantes.
   - La existencia nunca debe quedar en negativo.

2. **Operaciones manuales de inventario**:
   - Las operaciones válidas son **ENTRADA**, **SALIDA** y **AJUSTE**.
   - **ENTRADA**: incrementa la existencia.
   - **SALIDA**: disminuye la existencia; puede llegar a `0`, pero nunca a negativo.
   - **AJUSTE**: reemplaza la cantidad final por el valor indicado.
   - Los movimientos pueden relacionarse opcionalmente con pedido, recepción u observaciones para mantener trazabilidad.

3. **Historial de movimientos**:
   - La sección de _Movimientos_ concentra el historial operativo del inventario.
   - Cada movimiento puede consultarse a detalle con producto, almacén, sucursal, ubicación, usuario y cantidades.
   - El backend registra auditoría y movimientos formales de inventario.

4. **Reporte de existencias por periodo**:
   - Ya existe un reporte para conocer cómo cerró el inventario entre dos fechas.
   - Permite filtrar por almacén o consultar todos los almacenes visibles para el usuario.
   - El reporte muestra:
     - existencia inicial
     - entradas
     - salidas
     - existencia final
     - costo de la existencia final

5. **Reporte de movimientos por periodo**:
   - Ya existe un reporte por tipo de movimiento: **ENTRADA**, **SALIDA** y **AJUSTE**.
   - Permite filtrar por rango de fechas y por almacén, incluyendo opción de todos los almacenes.
   - Devuelve información lista para pantalla o exportación, incluyendo SKU, producto, variante, color, talla, usuario, pedido relacionado, observaciones y costo.

### 🧾 Compras y Recepciones

1. **Órdenes de compra**:
   - El flujo recomendado es tipo onboarding: primero encabezado y después productos.
   - El estatus inicial es **Pendiente a confirmar**.
   - La acción de autorizar sigue existiendo.
   - Una orden autorizada o ya recibida no debe editarse.
   - El detalle de una orden de compra ya puede mostrar las recepciones relacionadas.

2. **Impuestos en compras**:
   - La orden de compra ya soporta porcentaje de IVA.
   - El sistema recalcula subtotal, impuestos y gran total al guardar o actualizar la compra.

3. **Recepción de mercancía**:
   - La recepción es el proceso que realmente afecta existencias.
   - Puede ser **total** o **parcial**.
   - La recepción puede originarse desde una **Orden de Compra** o desde una **Orden de Producción**.
   - Para `OC`, el sistema toma el producto desde el detalle de compra.
   - Para `OP`, el sistema toma producto y variante desde el detalle de producción.
   - Si el almacén requiere ubicación, el usuario debe capturarla.
   - La recepción genera folio, actualiza existencias, crea movimiento de inventario y deja auditoría.
   - Si la recepción viene de producción, el movimiento queda ligado también a la orden de producción.

4. **Entrada de producto terminado**:
   - El flujo operativo recomendado es por **Recepciones**.
   - `ProductoTerminadoEntradas` ya no es el flujo principal recomendado para alimentar inventario.

### 🏭 Producción

1. **Lista de materiales (BOM)**:
   - Cada BOM se relaciona con un `producto_variante`.
   - Ya se puede crear, consultar y editar desde API.

2. **Edición de BOM**:
   - Si se envía `materia_prima_detalle`, el sistema toma ese arreglo como la nueva composición del BOM.
   - Si no se envía, conserva el detalle anterior.

3. **Órdenes de producción**:
   - El usuario ya no necesita enviar el BOM manualmente en cada renglón.
   - El sistema resuelve automáticamente el BOM activo por producto variante.
   - Antes de confirmar la orden, el sistema valida:
     - que exista BOM activo
     - que tenga insumos configurados
     - que exista inventario suficiente
   - Si todo es correcto:
     - descuenta insumos
     - registra consumo de producción
     - registra movimiento de inventario
     - deja auditoría
   - Si no hay stock suficiente o el BOM está incompleto, la orden no se confirma.

### 🛒 Ventas, Cotizaciones y Pedidos

1. **Cotizaciones**:
   - La cotización funciona como onboarding comercial completo.
   - Permite capturar cliente, productos, variantes, tallas, direcciones de envío y observaciones.
   - El campo `tipo_pedido` ya forma parte del flujo y se conserva hacia pedido.

2. **Envío a revisión y autorización**:
   - El vendedor genera la cotización y la envía a revisión.
   - La mesa de control valida la información y puede autorizar.
   - Antes de autorizar, ya se puede consultar stock por talla y detalle de disponibilidad.

3. **Conversión a pedido**:
   - Al autorizar una cotización:
     - se crea el pedido
     - se asigna folio
     - se descuentan existencias automáticamente
   - Si no existe inventario suficiente, la autorización se bloquea.

4. **Cambios sobre cotizaciones autorizadas**:
   - Si una cotización autorizada entra a cambios solicitados dentro de la ventana permitida, la mesa de control puede aceptar o rechazar cambios.
   - Cuando cambian cantidades, el sistema recalcula el delta y ajusta inventario de forma automática.

### 💰 Finanzas

1. **Facturación desde pedido**:
   - El sistema ya soporta facturación total del pedido en una sola exhibición.
   - Un pedido no debe facturarse más de una vez mientras exista una factura activa.

2. **Facturas pendientes por cobrar**:
   - Ya se pueden registrar facturas pendientes de cobro aunque no provengan de pedido.
   - Si el usuario no captura folio, el sistema lo genera automáticamente.
   - Al guardar, el sistema crea en una sola operación:
     - la factura
     - la cuenta por cobrar
     - la póliza contable relacionada

3. **Cuentas por cobrar**:
   - Ya existe consulta de cuentas por cobrar por cliente, estatus, saldo pendiente y vencidas.
   - El detalle de una cuenta por cobrar ya devuelve:
     - la factura relacionada
     - el total pagado
     - las pólizas asociadas con sus detalles contables
   - Esto permite seguimiento administrativo y trazabilidad para contabilidad.

4. **Trazabilidad contable**:
   - Al registrar una factura pendiente por cobrar, el sistema genera póliza de ingreso con sus partidas.
   - La intención es que contabilidad pueda rastrear cada cuenta por cobrar desde su origen documental.

---

## 🔍 Fase 5: Monitoreo y Auditoría

Para administradores y responsables del sistema.

1. **Logs de sistema**:
   - Revisa `sistema.log` para errores técnicos.
   - Revisa `api.log` para comportamiento anómalo, tiempos y fallas operativas.

2. **Auditoría de cambios**:
   - Los cambios relevantes quedan registrados con trazabilidad del antes y después.
   - Esto aplica especialmente a operaciones sensibles de inventario, ventas, compras y finanzas.

3. **Seguridad**:
   - Si un usuario reporta bloqueo o acceso denegado tras múltiples intentos, revisa la política de bloqueos activa.

4. **Documentación API**:
   - Si frontend requiere validar contratos, endpoints o ejemplos, la fuente principal es `DOCUMENTACION_API.md`.
   - Para pruebas manuales rápidas, usa `/api/docs/`.

---

## 🆘 Solución de Problemas Comunes

- **"RFC inválido al crear empresa"**: Verifica formato y dígito verificador del RFC.
- **"Error al cargar CSD"**: Asegúrate de usar la contraseña correcta de la llave privada `.key`.
- **"No veo ninguna sucursal"**: Revisa que el usuario tenga sucursal default y sucursales permitidas asignadas.
- **"No hay serie activa para recepción"**: Configura una serie como `RC`, `RT` o `RZ` para la empresa y sucursal.
- **"No puedo hacer salida de inventario"**: Revisa que exista cantidad suficiente; la salida nunca puede dejar stock negativo.
- **"La recepción no afecta existencias"**: Verifica que la recepción realmente se haya guardado; ni la OC ni la OP mueven inventario por sí solas.
- **"No veo detalle en movimientos"**: Usa el detalle del movimiento para consultar desglose por producto, ubicación y cantidades.
- **"No puedo autorizar una cotización"**: Revisa stock disponible real; el sistema bloquea autorización si la existencia no alcanza.
- **"No puedo editar una orden de compra"**: Verifica si ya fue autorizada o recibida; en ese estado deja de ser editable.
- **"No veo cuentas por cobrar o pólizas"**: Revisa que la factura pertenezca a la empresa del usuario y que existan cuentas contables y centro de costo activos para el flujo financiero.
