# Plan RFID

## Objetivo

Definir una ruta clara y escalable para incorporar:

- [x] Impresion de etiquetas Zebra normales
- [ ] Impresion de etiquetas RFID
- [ ] Lectura de tags RFID
- [ ] Automatizacion de flujos WMS con RFID

La idea es implementarlo por fases, sin mezclar todo desde el inicio.

---

## Vision General

### Backend Django

- genera datos de etiquetas
- genera ZPL
- registra trazabilidad
- valida reglas de negocio
- guarda eventos RFID

### Frontend Next.js

- muestra interfaces de impresion y lectura
- selecciona impresora
- envia ZPL a Zebra Browser Print
- consume onboarding y endpoints de negocio

### Zebra Browser Print

- puente entre navegador e impresora Zebra instalada en la PC

### Impresora / Lector RFID

- imprime etiquetas
- graba tags RFID
- lee EPC/TID segun el dispositivo disponible

---

## Fase 1 - Imprimir Etiquetas Zebra Normales

### Objetivo

Poder imprimir etiquetas desde Next.js usando Zebra Browser Print.

### Alcance

- [ ] etiqueta de producto
- [ ] etiqueta de ubicacion
- [ ] etiqueta de picking
- [ ] etiqueta de caja o packing

### Flujo

- [x] frontend solicita datos o ZPL al backend
- [x] backend responde ZPL listo
- [x] frontend usa Browser Print
- [x] Zebra imprime localmente

### Entregables

- [x] endpoint para obtener ZPL
- [x] plantilla base de etiquetas
- [x] boton de impresion en frontend
- [x] validacion de impresora disponible

### Ejemplos de endpoints

- [ ] `GET /api/v1/wms/etiquetas/producto/{id}/zpl/`
- [ ] `GET /api/v1/wms/etiquetas/ubicacion/{id}/zpl/`
- [ ] `GET /api/v1/wms/etiquetas/picking/{id}/zpl/`

### Meta de esta fase

Resolver impresion real sin entrar todavia a RFID.

### Validacion QA completada

- [x] Se habilito `GET /QA/imprimir_etiqueta/` para pruebas locales con Zebra Browser Print.
- [x] La pantalla permite buscar por `sku`, `codigo`, `cod_proscai` y nombre.
- [x] Si no existen variantes, la impresion puede hacerse desde `Producto` usando codigo base.
- [x] Se valido impresion real de una etiqueta para `AMBASSADOR` con codigo `93E0`.
- [x] Se habilito el retorno directo a `GET /QA/rfid/recepciones/?encuadre={id}` para cerrar el ciclo de prueba.

### Aprendizajes de implementacion

- [x] En entorno local conviene servir Browser Print desde rutas QA dedicadas para no depender de `collectstatic`.
- [x] El Zebra `MC3300X` pudo participar en el flujo actual como lector de codigo de barras enviando el valor al input web.
- [x] Esta validacion confirma la fase de impresion y captura asistida, pero todavia no constituye lectura RFID real.

---

## Fase 2 - Imprimir Etiquetas RFID

### Objetivo

Grabar tags RFID y al mismo tiempo imprimir la etiqueta fisica.

### Alcance

- [ ] definir estructura EPC
- [ ] generar ZPL compatible con RFID
- [ ] relacionar etiqueta impresa con producto o unidad logistica

### Decisiones necesarias

- que se grabara en el tag:
  - EPC
  - SKU
  - serie
  - lote
  - identificador interno
- que objetos del ERP tendran RFID:
  - producto
  - variante
  - caja
  - pallet
  - ubicacion

### Entregables

- [ ] generador de ZPL RFID
- [ ] estrategia de EPC
- [ ] registro de etiqueta RFID emitida

### Modelo sugerido

- `EtiquetaRFID`
  - epc
  - tid
  - producto
  - producto_variante
  - serie
  - lote
  - estatus
  - fecha_impresion

### Meta de esta fase

Pasar de impresion normal a impresion con identidad RFID trazable.

---

## Fase 3 - Leer Tags RFID

### Objetivo

Poder registrar lecturas RFID dentro del ERP.

### Alcance

- [ ] recibir EPC leido
- [ ] identificar a que producto o unidad corresponde
- [ ] registrar evento
- [ ] mostrar lectura en frontend

### Flujo

1. lector o middleware detecta tag
2. frontend o servicio local envia EPC al backend
3. backend identifica el registro relacionado
4. backend guarda evento de lectura

### Estado actual

- [x] Ya existe una prueba funcional de lectura en `QA/rfid/recepciones/`, pero hoy resuelve por `sku`, `codigo` o `cod_proscai`.
- [ ] El siguiente paso para RFID real es sustituir ese valor por `EPC` leido desde hardware RFID o middleware Zebra compatible.

### Entregables

- [ ] endpoint para registrar lectura RFID
- [ ] historial de lecturas
- [ ] consulta de EPC
- [ ] validaciones de tags no registrados

### Modelos sugeridos

- `RFIDLectura`
  - epc
  - tid
  - dispositivo
  - ubicacion
  - evento
  - fecha_lectura

- `RFIDDispositivo`
  - nombre
  - tipo
  - ip
  - activo

### Meta de esta fase

Tener trazabilidad de lectura, aunque todavia no automatice procesos.

---

## Fase 4 - Automatizar Flujos WMS con RFID

### Objetivo

Usar RFID para mover procesos del WMS con menos captura manual.

### Casos de uso

- [ ] recepcion automatica
- [ ] transferencias automatizadas
- [ ] picking asistido por RFID
- [ ] packing con validacion por lectura
- [ ] despacho con confirmacion RFID
- [ ] conteos ciclicos automatizados

### Integracion con WMS

- recepcion
- reservas
- picking
- packing
- despacho
- conteos

### Regla clave

RFID no debe reemplazar primero la operacion manual; debe entrar despues de que el flujo manual ya este estable.

### Meta de esta fase

Reducir errores, mejorar velocidad y aumentar trazabilidad en almacen.

---

## Orden Recomendado de Implementacion

1. [x] Impresion Zebra normal
2. [ ] ZPL RFID + grabacion de tags
3. [ ] Lectura RFID + registro de eventos
4. [ ] Automatizacion WMS por etapas

---

## Arquitectura Recomendada

### Corto plazo

- [x] Django genera ZPL
- [x] Next.js imprime con Browser Print
- [x] Zebra imprime localmente

### Mediano plazo

- [ ] Django genera ZPL RFID
- [ ] Next.js imprime y graba tags
- [ ] backend registra etiqueta RFID emitida

### Largo plazo

- [ ] lector RFID o middleware envia eventos al backend
- [ ] backend conecta lecturas con WMS

---

## Riesgos y Cuidados

- no depender del navegador sin Browser Print instalado
- no mezclar impresion y lectura en una sola fase
- definir EPC antes de imprimir masivamente
- no automatizar WMS con RFID sin tener trazabilidad base estable
- validar compatibilidad exacta del modelo de impresora Zebra

---

## MVP Recomendado

### MVP 1

- [x] imprimir etiqueta Zebra normal desde Next.js

### MVP 2

- [ ] imprimir etiqueta RFID con EPC generado por backend

### MVP 3

- [ ] registrar lectura de EPC y mostrar historial

### MVP 4

- [ ] usar RFID en picking o despacho

---

## Conclusion

La ruta correcta para este proyecto es:

1. [x] imprimir bien
2. [ ] grabar RFID
3. [ ] leer RFID
4. [ ] automatizar WMS

Ese orden reduce riesgo, facilita mantenimiento y permite avanzar con valor real desde la primera fase.
