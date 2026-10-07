<div align="center">
    <p>
        <img src="./facturama_logo.png" width="239">
    </p>
        <h1>Facturama</h1>
        <h3>Genera tus Comprobantes Fiscales de manera rápida y segura con nuestro innovador Software de Facturación .</h3>

  <p>
    <a href="#Productos">Productos</a> ·
    <a href="#Cfdi">CFDI</a> ·
  </p>
</div>


<a id="Productos"></a>
## Productos

__Consultar productos__

```
POST /api/v1/finanzas/facturama/productos
```

<h4>Respuesta (200)</h4> 

```json
[
  {
    "Id": "GtenxmdGJMbQl26GUVmDMQ2",
    "UnitCode": "E48",
    "Unit": "Servicio",
    "IdentificationNumber": "WEB003",
    "Name": "Sitio Web CMS",
    "Description": "Producto de prueba Marca ACME",
    "Price": 999.9999,
    "CodeProdServ": "43232408",
    "ObjetoImp": "02"
  }
]
```

__Consultar por Id__

```
GET /api/v1/finanzas/facturama/productos/{id}
```

<h4>Respuesta (200)</h4>

```json
{
    "Id": "iOWsn89PYb3LzvQEdfnWsA2",
    "UnitCode": "H87",
    "Unit": "PIEZA",
    "IdentificationNumber": "-",
    "Name": "Producto Generico",
    "Description": "Producto General",
    "Category": "GENERAL",
    "Code": "-",
    "Price": 0.0000,
    "CodeProdServ": "01010101",
    "NameCodeProdServ": "",
    "CuentaPredial": "",
    "Taxes": []
}
```

__Nota:__ el id corresponde al valor de facturama, no al del backend (django)

__Crear producto__

```
POST /api/v1/finanzas/facturama/productos/
```

<h4>Body</h4>

```json
{
    "Unit": "",
    "UnitCode": "",
    "IdentificationNumber": "",
    "Name": "",
    "Description": "",
    "Price": 0.0,
    "CodeProdServ": "",
    "ObjetoImp": "",
    "Taxes": [
        {
            "Name": "IVA",
            "Rate": 0.16,
            "IsRetention": false,
            "IsFederalTax": true,
            "IsQuota": false
        }
    ]
}
```

<h4>Respuesta (200)</h4>

```json
{
    "Unit": "Servicio",
    "UnitCode": "E48",
    "IdentificationNumber": "PRUEBA001",
    "Name": "Producto de prueba",
    "Description": "Producto de prueba IVA al 16%",
    "Price": 999.9999,
    "CodeProdServ": "01010101",
    "ObjetoImp": "02",
    "Taxes": [
        {
            "Name": "IVA",
            "Rate": 0.16,
            "IsRetention": false,
            "IsFederalTax": true,
            "IsQuota": false
        }
    ]
}
```

__Actualizar__

```
PUT /api/v1/finanzas/facturama/productos/
```

Envía el objeto completo del producto con los campos modificados. La respuesta sera el mismo objeto con los campos modificados.

__Eliminar un producto__

```
DELETE /api/v1/finanzas/facturama/productos/{id}/
```

Eliminar un producto del catálogo no afecta los CFDIs ya emitidos.

<a id="Cfdi"></a>
## CFDI

__Crear CFDI__
```
POST /api/v1/finanzas/facturama/cfdi/
```

<h4>Body</h4>

```json
{
  "NameId": "1",
  "Currency": "MXN",
  "Folio": "100",
  "Serie": "FAC",
  "CfdiType": "I",
  "PaymentForm": "03",
  "PaymentMethod": "PUE",
  "OrderNumber": "TEST-001",
  "ExpeditionPlace": "78000",
  "Date": "2026-05-15T22:17:44",
  "PaymentConditions": "CREDITO A SIETE DIAS",
  "Observations": "Elemento Observaciones solo visible en PDF",
  "Exportation": "01",
  "Receiver": {
    "Rfc": "URE180429TM6",
    "CfdiUse": "CP01",
    "Name": "UNIVERSIDAD ROBOTICA ESPAÑOLA",
    "FiscalRegime": "601",
    "TaxZipCode": "86991"
  },
  "Items": [
    {
      "ProductCode": "10101504",
      "IdentificationNumber": "EDL",
      "Description": "Estudios de laboratorio",
      "Unit": "NO APLICA",
      "UnitCode": "MTS",
      "UnitPrice": 50,
      "Quantity": 2.0,
      "Subtotal": 100,
      "TaxObject": "02",
      "Taxes": [
        {
          "Total": 16,
          "Name": "IVA",
          "Base": 100,
          "Rate": 0.16,
          "IsRetention": false
        }
      ],
      "Total": 116
    }
  ]
}
```

<h4>Respuesta (200)</h4>

```json
{
  "Id": "DznlumtVtkA3ya4JXAeGWw2",
  "CfdiType": "ingreso",
  "Type": "I - ingreso",
  "Serie": "FAC",
  "Folio": "100",
  "Date": "2026-06-11T16:05:51",
  "CertNumber": "30001000000500003416",
  "PaymentTerms": "03 - Transferencia electrónica de fondos",
  "PaymentConditions": "CREDITO A SIETE DIAS",
  "PaymentMethod": "PUE - Pago en una sola exhibición",
  "PaymentAccountNumber": "",
  "PaymentBankName": "",
  "ExpeditionPlace": "78000",
  "ExchangeRate": 0.0,
  "Currency": "MXN - Peso Mexicano",
  "Subtotal": 100.0,
  "Discount": 0.0,
  "Total": 116.0,
  "Observations": "Elemento Observaciones solo visible en PDF",
  "OrderNumber": "TEST-001",
  "Issuer": {
    "FiscalRegime": "601 - General de Ley Personas Morales",
    "Rfc": "EKU9003173C9",
    "TaxName": "ESCUELA KEMPER URGATE",
    "Email": "correo@prueba.com",
    "Phone": "9999999999",
    "TaxAddress": {
      "Street": "Calle de prueba",
      "ExteriorNumber": "123",
      "InteriorNumber": "",
      "Neighborhood": "Prueba",
      "ZipCode": "42501",
      "Municipality": "Pruebas",
      "State": "ESTADO DE MEXICO",
      "Country": "México"
    }
  },
  "Receiver": {
    "Rfc": "URE180429TM6",
    "Name": "UNIVERSIDAD ROBOTICA ESPAÑOLA",
    "Email": ""
  },
  "Items": [
    {
      "ProductCode": "10101504",
      "IdentificationNumber": "EDL",
      "UnitCode": "MTS",
      "Discount": 0.0,
      "CuentaPredial": "",
      "Quantity": 2.0,
      "Unit": "MTS - NO APLICA",
      "Description": "Estudios de laboratorio",
      "UnitValue": 50.0,
      "Total": 100.0
    }
  ],
  "Taxes": [
    {
      "Total": 16.0,
      "Name": "IVA",
      "Rate": 0.16,
      "Type": "transferred"
    }
  ],
  "Complement": {
    "TaxStamp": {
      "Uuid": "5e09160f-a206-44d7-b389-4f243585e76a",
      "Date": "2026-06-11T16:05:52",
      "SatCertNumber": "30001000000500003456",
      "RfcProvCertif": "SPR190613I52"
    }
  },
  "Status": "active",
  "OriginalString": "||4.0|FAC|100|2026-06-11T16:05:51|03|30001000000500003416|.......|"
}
```

Mas informacion en: [Facturama CFDI docs.](https://facturama.mx/docs/es/api-web/crear-cfdi#tipos-de-cfdi)

__Descargar por formato__
```
GET /api/v1/finanzas/facturama/cfdi/{file_format}/{cfdi_type}/{cfdi_id}/
```

`format` `string` `string`

Formato del archivo a obtener (pdf | xml | html).
​

`format` `string` `string`

Tipo del CFDI (issued | payroll).
​

`format` `string` `string`

Identificador del CFDI.

__Ejemplos de solicitud:__

```shell
# Descargar PDF
curl --url '{{projectDomain}}/api/v1/finanzas/facturama/cfdi/pdf/issued/jh054AaC04b1wcqSUHYcw2' \
  --header 'Authorization: Basic YOUR_BASE64_CREDENTIALS'

# Descargar XML
curl --url '{{projectDomain}}/api/v1/finanzas/facturama/cfdi/xml/issued/jh054AaC04b1wcqSUHYcw2' \
  --header 'Authorization: Basic YOUR_BASE64_CREDENTIALS'
```

Respuesta: El contenido del archivo en Base64. La respuesta retorna el ‘HTTP Response’ si la petición fue exitosa con un código 200

<h4>Respuesta (200)</h4>

```json
{
    "ContentEncoding": "base64",
    "ContentType": "pdf",
    "ContentLength": 28458,
    "Content": "JVBERi0xLjQKMSAwIG9iago8P....."
}
```