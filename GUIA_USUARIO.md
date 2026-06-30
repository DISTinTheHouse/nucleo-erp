# 📘 Guía de Uso e Implementación Inicial

Esta guía detalla el flujo lógico paso a paso para configurar y comenzar a utilizar el sistema ERP desde cero. Sigue este orden para garantizar la integridad de los datos.

## 🚀 Fase 1: Configuración Global (Super Admin)

Antes de crear cualquier empresa, el sistema debe tener los cimientos listos.

1.  **Carga de Catálogos Globales**:
    - Asegúrate de que las **Monedas** (MXN, USD) estén creadas.
    - Verifica los **Impuestos** base (IVA 16%, ISR).
    - Carga las **Unidades de Medida** (Pieza, Servicio, Kilo).
    - _Nota_: Los catálogos del SAT ya están precargados (Régimen Fiscal, Uso CFDI, etc.).

---

## 🏢 Fase 2: Alta de Organización (Empresa)

Ahora crearemos la entidad principal.

1.  **Crear Empresa**:
    - Ve a _Organización > Empresas > Nueva_.
    - Llena Razón Social, RFC y selecciona la **Moneda Base**.
    - **Validación Automática**: El sistema validará que el RFC tenga el formato correcto y coincida con su dígito verificador.

2.  **Configuración Fiscal (SAT)**:
    - Ve a _Organización > Empresas > Configuración SAT_.
    - **Carga de CSD**: Sube tus archivos `.cer` y `.key` junto con la contraseña.
    - El sistema validará automáticamente:
      - Que los archivos correspondan entre sí.
      - Que la contraseña sea correcta.
      - Que el RFC del certificado coincida con el de la empresa.
      - La vigencia del certificado.

3.  **Crear Sucursales**:
    - Ve a _Organización > Sucursales_.
    - Crea al menos una sucursal (ej. "Matriz").
    - Asigna la dirección y código postal (crucial para el timbrado 4.0).

4.  **Crear Departamentos**:
    - Ve a _Organización > Departamentos_.
    - Define las áreas operativas por sucursal (ej. "Ventas Matriz", "Almacén Norte").
    - Usa códigos estándar (ej. "VENTAS", "ALMACEN") para facilitar la asignación de roles automática.

---

## 🛡️ Fase 3: Seguridad y Accesos

Configura quién puede hacer qué.

1.  **Definir Roles**:
    - Ve a _Seguridad > Roles_.
    - Crea roles operativos (ej. "Vendedor Mostrador", "Gerente de Tienda").
    - **Tip Pro**: Si asignas una `Clave Departamento` (ej. "VENTAS") al rol, los usuarios con este rol solo verán información de ese departamento automáticamente.

2.  **Alta de Usuarios**:
    - Ve a _Seguridad > Usuarios_.
    - Crea el usuario con su correo y contraseña.
    - **Asignaciones Críticas**:
      - **Empresa**: Asigna la empresa a la que pertenece.
      - **Sucursal Default**: Su ubicación principal.
      - **Sucursales (Acceso)**: Marca todas las sucursales donde puede operar.
      - **Roles**: Asigna el rol correspondiente (ej. "Vendedor").

---

## ✅ Fase 4: Operación Diaria

Con la configuración lista, el usuario puede empezar a operar:

1.  **Login**: El usuario ingresa sus credenciales.
    68→2. **Dashboard**: Verá la información filtrada según sus permisos y sucursales asignadas. El frontend (Next.js) utiliza los permisos devueltos por el login para habilitar solo las acciones permitidas (lectura, edición y eliminación por módulo).
2.  **Transacciones**: Al crear documentos (Ventas, Compras), el sistema tomará automáticamente la serie/folio de su sucursal activa.

### 📦 Inventarios

1.  **Consultar Existencias**:
    - Ve a la sección de _Inventarios > Existencias_.
    - El sistema consulta existencias por **producto** de forma directa y mantiene compatibilidad con `producto_variante` cuando aplique.
    - La existencia nunca debe quedar en negativo.

2.  **Operaciones de Inventario**:
    - Las operaciones válidas son **ENTRADA**, **SALIDA** y **AJUSTE**.
    - **ENTRADA**: suma cantidad al inventario.
    - **SALIDA**: resta cantidad; permite llegar a `0`, pero nunca a negativo.
    - **AJUSTE**: reemplaza la cantidad final por el valor enviado.

3.  **Historial de Movimientos**:
    - La sección de _Movimientos_ muestra el historial operativo del inventario.
    - Ya existe opción para consultar el detalle de cada movimiento desde el endpoint de detalle.
    - El backend guarda auditoría y también registros formales de movimiento para trazabilidad.

### 🧾 Compras y Recepciones

1.  **Órdenes de Compra**:
    - El flujo recomendado es tipo onboarding: primero encabezado, después productos.
    - El folio se genera cuando la orden se **acepta**, no cuando se guarda como borrador.

2.  **Recepción de Mercancía**:
    - La recepción es el proceso que realmente afecta existencias.
    - Puede ser **total** o **parcial**.
    - El backend toma el **producto** desde el detalle de la orden de compra; no depende de `producto_variante` para este flujo.
    - Si el almacén requiere ubicación, el usuario debe capturarla; si no, la recepción puede guardarse sin `ubicacion`.

3.  **Series de Recepción**:
    - Las recepciones trabajan con series de folio como `RC`, `RT` o `RZ`.
    - Debe existir al menos una serie activa configurada para recepción en la sucursal/empresa correspondiente.

### 🏭 Producción

1.  **Lista de Materiales (BOM)**:
    - Cada BOM se relaciona con un `producto_variante` y su detalle de materias primas.
    - Ya se puede **crear**, **consultar** y **editar** desde API.

2.  **Edición de BOM**:
    - Al editar una lista de materiales, el encabezado se actualiza normalmente.
    - Si se envía `materia_prima_detalle`, el backend toma ese arreglo como la nueva composición del BOM.

3.  **Órdenes de Producción**:
    - El onboarding de producción ya no requiere que el cliente envíe el `bom` en cada detalle.
    - El backend resuelve automáticamente el BOM activo a partir del `producto_variante`.
    - Al confirmar la orden, el sistema valida existencias de insumos y descuenta automáticamente el consumo desde inventario.
    - Si no existe stock suficiente o el BOM está incompleto, la orden no se confirma.

---

## 🔍 Fase 5: Monitoreo y Auditoría

Para administradores del sistema.

1.  **Logs de Sistema**:
    - Revisa `sistema.log` para errores técnicos.
    - Revisa `api.log` para trazar peticiones lentas o sospechosas.
2.  **Auditoría de Cambios**:
    - Cada cambio importante (ej. cambiar un precio, editar un usuario) queda registrado con el "antes" y "después".
    - Disponible en el módulo de _Sistemas > Auditoría_.
3.  **Seguridad (Bloqueos)**:
    - Si un usuario reporta "Acceso Denegado" tras varios intentos, verifica la tabla de bloqueos (`axes`). El bloqueo dura 1 hora por defecto.

---

## 🆘 Solución de Problemas Comunes

- **"RFC Inválido al crear empresa"**: Verifica que el RFC cumpla con el formato oficial y el dígito verificador. Usa un validador en línea del SAT para confirmar.
- **"Error al cargar CSD"**: Asegúrate de que la contraseña sea la correcta para la llave privada (`.key`) y no la contraseña del portal del SAT (CIEC). Son diferentes.
- **"No veo ninguna sucursal"**: Revisa que el usuario tenga asignada al menos una sucursal en el campo "Sucursales permitidas".
- **"No hay Serie/Folio activa para recepción"**: Configura una serie activa de recepción (`RC`, `RT` o `RZ`) para la empresa y sucursal que está operando.
- **"No puedo hacer salida de inventario"**: Revisa que exista cantidad suficiente; la salida nunca puede dejar la existencia en negativo.
- **"La recepción no afecta existencias"**: Verifica que la recepción se haya guardado correctamente; la orden de compra por sí sola no mueve inventario.
- **"No veo detalle en movimientos"**: Usa el endpoint de detalle del movimiento para consultar el desglose por producto, cantidad y ubicaciones.
