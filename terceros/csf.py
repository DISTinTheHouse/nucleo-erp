"""Lectura de la Constancia de Situación Fiscal (PDF digital del SAT) para prellenar un Cliente."""
import re
import unicodedata

from pypdf import PdfReader

from nucleo.models import SatRegimenFiscal
from nucleo.utils import validate_rfc

MAX_BYTES = 5 * 1024 * 1024
MAX_PAGINAS = 10

# Etiqueta de la constancia -> clave interna. Se parte el texto por etiqueta, así
# funciona igual si pypdf entrega "Etiqueta: valor" o la etiqueta y el valor en líneas distintas.
_ETIQUETAS = {
    "rfc": r"RFC",
    "curp": r"CURP",
    "razon": r"Denominaci[oó]n/Raz[oó]n Social",
    "nombre": r"Nombre \(s\)",
    "apellido1": r"Primer Apellido",
    "apellido2": r"Segundo Apellido",
    "cp": r"C[oó]digo Postal",
    "vialidad_tipo": r"Tipo de Vialidad",
    "vialidad": r"Nombre de Vialidad",
    "num_ext": r"N[uú]mero Exterior",
    "num_int": r"N[uú]mero Interior",
    "colonia": r"Nombre de la Colonia",
    "localidad": r"Nombre de la Localidad",
    "municipio": r"Nombre del Municipio o Demarcaci[oó]n Territorial",
    "estado": r"Nombre de la Entidad Federativa",
    "entre_calle": r"Entre Calle",
    "y_calle": r"Y Calle",
    "correo": r"Correo Electr[oó]nico",
    "telefono": r"Tel[eé]fono",
    "fin": r"Fecha inicio de operaciones|Estatus en el padr[oó]n|Fecha de [uú]ltimo cambio de estado|Nombre Comercial|Reg[ií]menes:|Obligaciones:|Actividades Econ[oó]micas:|Datos del domicilio registrado|Datos de Identificaci[oó]n",
}


class CsfInvalida(ValueError):
    pass


def _sin_acentos(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn").lower()


def extraer_texto(archivo):
    try:
        lector = PdfReader(archivo)
        return "\n".join((p.extract_text() or "") for p in lector.pages[:MAX_PAGINAS])
    except Exception:
        raise CsfInvalida("El archivo no es un PDF legible.")


def _valores(texto):
    texto = re.sub(r"[ \t]+", " ", texto)
    patron = re.compile(
        r"(?P<fin>%s)|%s"
        % (_ETIQUETAS["fin"], "|".join(f"(?P<{k}>{v})" for k, v in _ETIQUETAS.items() if k != "fin")),
        re.I,
    )
    hallazgos = [(m.lastgroup, m.start(), m.end()) for m in patron.finditer(texto)]
    valores = {}
    for i, (clave, _, fin) in enumerate(hallazgos):
        if clave == "fin" or clave in valores:
            continue
        siguiente = hallazgos[i + 1][1] if i + 1 < len(hallazgos) else len(texto)
        valor = re.sub(r"\s+", " ", texto[fin:siguiente]).strip(" :\n")
        if valor:
            valores[clave] = valor
    return valores


def _regimen(texto):
    """Primer régimen del contribuyente que coincida con el catálogo SAT (por descripción)."""
    seccion = re.split(r"Reg[ií]menes:", texto, maxsplit=1, flags=re.I)
    if len(seccion) < 2:
        return None
    cuerpo = _sin_acentos(re.split(r"Obligaciones:", seccion[1], maxsplit=1, flags=re.I)[0])
    # Más largas primero: "General de Ley Personas Morales" antes que una descripción corta contenida en otra.
    catalogo = sorted(SatRegimenFiscal.objects.filter(activo=True), key=lambda r: -len(r.descripcion))
    encontrados = [(cuerpo.find(_sin_acentos(r.descripcion)), r) for r in catalogo]
    encontrados = [(pos, r) for pos, r in encontrados if pos >= 0]
    return min(encontrados, key=lambda x: x[0])[1] if encontrados else None


def interpretar(texto):
    v = _valores(texto)
    rfc = re.sub(r"[^A-Z0-9&Ñ]", "", v.get("rfc", "").upper())[:13]
    if not rfc:
        raise CsfInvalida("No se encontró el RFC: ¿es una Constancia de Situación Fiscal digital del SAT?")
    valido, mensaje = validate_rfc(rfc)
    if not valido:
        raise CsfInvalida(mensaje)

    if v.get("razon"):
        razon = v["razon"]
    else:
        razon = " ".join(v[k] for k in ("nombre", "apellido1", "apellido2") if v.get(k))
    calle = " ".join(x for x in (v.get("vialidad_tipo"), v.get("vialidad")) if x)
    regimen = _regimen(texto)
    return {
        "rfc": rfc,
        "curp": v.get("curp", "")[:18] or None,
        "razon_social": razon,
        "nombre": razon[:150],
        "sat_regimen_fiscal": regimen.pk if regimen else None,
        "sat_regimen_fiscal_codigo": regimen.codigo if regimen else None,
        "direccion_fiscal": calle[:255],
        "numero_exterior": v.get("num_ext") or None,
        "numero_interior": v.get("num_int") or None,
        "colonia": v.get("colonia", "")[:120],
        "codigo_postal": v.get("cp", "")[:10],
        "delegacion": v.get("municipio") or None,
        "ciudad": (v.get("localidad") or v.get("municipio") or "")[:120],
        "estado": v.get("estado", "")[:120],
        "correo": v.get("correo", ""),
        "pais": "MEX",
    }


def leer_csf(archivo):
    return interpretar(extraer_texto(archivo))
