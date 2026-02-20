# 📘 Guía de Uso e Implementación Inicial

Esta guía detalla el flujo lógico paso a paso para configurar y comenzar a utilizar el sistema ERP desde cero. Sigue este orden para garantizar la integridad de los datos.

## 🚀 Fase 1: Configuración Global (Super Admin)
Antes de crear cualquier empresa, el sistema debe tener los cimientos listos.

1.  **Carga de Catálogos Globales**:
    *   Asegúrate de que las **Monedas** (MXN, USD) estén creadas.
    *   Verifica los **Impuestos** base (IVA 16%, ISR).
    *   Carga las **Unidades de Medida** (Pieza, Servicio, Kilo).
    *   *Nota*: Los catálogos del SAT ya están precargados (Régimen Fiscal, Uso CFDI, etc.).

---

## 🏢 Fase 2: Alta de Organización (Empresa)
Ahora crearemos la entidad principal.

1.  **Crear Empresa**:
    *   Ve a *Organización > Empresas > Nueva*.
    *   Llena Razón Social, RFC y selecciona la **Moneda Base**.
    *   **Validación Automática**: El sistema validará que el RFC tenga el formato correcto y coincida con su dígito verificador.

2.  **Configuración Fiscal (SAT)**:
    *   Ve a *Organización > Empresas > Configuración SAT*.
    *   **Carga de CSD**: Sube tus archivos `.cer` y `.key` junto con la contraseña.
    *   El sistema validará automáticamente:
        *   Que los archivos correspondan entre sí.
        *   Que la contraseña sea correcta.
        *   Que el RFC del certificado coincida con el de la empresa.
        *   La vigencia del certificado.

3.  **Crear Sucursales**:
    *   Ve a *Organización > Sucursales*.
    *   Crea al menos una sucursal (ej. "Matriz").
    *   Asigna la dirección y código postal (crucial para el timbrado 4.0).

4.  **Crear Departamentos**:
    *   Ve a *Organización > Departamentos*.
    *   Define las áreas operativas por sucursal (ej. "Ventas Matriz", "Almacén Norte").
    *   Usa códigos estándar (ej. "VENTAS", "ALMACEN") para facilitar la asignación de roles automática.

---

## 🛡️ Fase 3: Seguridad y Accesos
Configura quién puede hacer qué.

1.  **Definir Roles**:
    *   Ve a *Seguridad > Roles*.
    *   Crea roles operativos (ej. "Vendedor Mostrador", "Gerente de Tienda").
    *   **Tip Pro**: Si asignas una `Clave Departamento` (ej. "VENTAS") al rol, los usuarios con este rol solo verán información de ese departamento automáticamente.

2.  **Alta de Usuarios**:
    *   Ve a *Seguridad > Usuarios*.
    *   Crea el usuario con su correo y contraseña.
    *   **Asignaciones Críticas**:
        *   **Empresa**: Asigna la empresa a la que pertenece.
        *   **Sucursal Default**: Su ubicación principal.
        *   **Sucursales (Acceso)**: Marca todas las sucursales donde puede operar.
        *   **Roles**: Asigna el rol correspondiente (ej. "Vendedor").

---

## ✅ Fase 4: Operación Diaria
Con la configuración lista, el usuario puede empezar a operar:

1.  **Login**: El usuario ingresa sus credenciales.
68→2.  **Dashboard**: Verá la información filtrada según sus permisos y sucursales asignadas. El frontend (Next.js) utiliza los permisos devueltos por el login para habilitar solo las acciones permitidas (lectura, edición y eliminación por módulo).
3.  **Transacciones**: Al crear documentos (Ventas, Compras), el sistema tomará automáticamente la serie/folio de su sucursal activa.

---

## 🔍 Fase 5: Monitoreo y Auditoría
Para administradores del sistema.

1.  **Logs de Sistema**:
    *   Revisa `sistema.log` para errores técnicos.
    *   Revisa `api.log` para trazar peticiones lentas o sospechosas.
2.  **Auditoría de Cambios**:
    *   Cada cambio importante (ej. cambiar un precio, editar un usuario) queda registrado con el "antes" y "después".
    *   Disponible en el módulo de *Sistemas > Auditoría*.
3.  **Seguridad (Bloqueos)**:
    *   Si un usuario reporta "Acceso Denegado" tras varios intentos, verifica la tabla de bloqueos (`axes`). El bloqueo dura 1 hora por defecto.

---

## 🆘 Solución de Problemas Comunes
*   **"RFC Inválido al crear empresa"**: Verifica que el RFC cumpla con el formato oficial y el dígito verificador. Usa un validador en línea del SAT para confirmar.
*   **"Error al cargar CSD"**: Asegúrate de que la contraseña sea la correcta para la llave privada (`.key`) y no la contraseña del portal del SAT (CIEC). Son diferentes.
*   **"No veo ninguna sucursal"**: Revisa que el usuario tenga asignada al menos una sucursal en el campo "Sucursales permitidas".
