"""Orden canónico de tallas, derivado solo de ``Talla.nombre``.

``Talla`` no tiene columna de orden ni de categoría, así que el orden se deduce
del texto. Secciones (confirmadas por negocio), en este orden:

0. Letras, de chica a grande: ``nXCH`` (n descendente), ``XCH``, ``CH``, ``M``,
   ``G``, ``XG``, ``nXG`` (n ascendente). La regla es algorítmica: un ``3XCH``
   o ``7XG`` futuro cae solo en su lugar.
1. Numéricas pares, ascendente por valor.
2. Numéricas impares, ascendente por valor.
3. Especiales: ``UNI``.
4. Desconocidas: cualquier otro valor, al final y en orden alfabético. Nunca se
   descartan ni truenan.

El nombre se normaliza (``strip`` + mayúsculas) antes de clasificar. Es la
única implementación del orden: la usan ``/catalogo/talla/`` y el onboarding de
cotizaciones; no la repliques.
"""

import re

SECCION_LETRAS = 0
SECCION_PARES = 1
SECCION_IMPARES = 2
SECCION_ESPECIALES = 3
SECCION_DESCONOCIDAS = 4

# Posición de las letras base; los múltiplos X se colocan relativos a ellas.
_LETRAS_BASE = {"CH": 0, "M": 1, "G": 2}
_ESPECIALES = {"UNI": 0}
_MULTIPLO_X = re.compile(r"([0-9]*)X(CH|G)")
_NUMERICA = re.compile(r"[0-9]+")


def talla_sort_key(nombre):
    """Clave de orden ``(seccion, posicion, nombre_normalizado)`` para ``sorted``."""
    texto = str(nombre or "").strip().upper()

    if texto in _LETRAS_BASE:
        return (SECCION_LETRAS, _LETRAS_BASE[texto], texto)

    multiplo = _MULTIPLO_X.fullmatch(texto)
    if multiplo:
        n = int(multiplo.group(1) or 1)
        if n >= 1:
            if multiplo.group(2) == "CH":
                # XCH = -1, 2XCH = -2, ...: más X, más chica.
                return (SECCION_LETRAS, _LETRAS_BASE["CH"] - n, texto)
            # XG = 3, 2XG = 4, ...: más X, más grande.
            return (SECCION_LETRAS, _LETRAS_BASE["G"] + n, texto)

    if _NUMERICA.fullmatch(texto):
        valor = int(texto)
        seccion = SECCION_PARES if valor % 2 == 0 else SECCION_IMPARES
        return (seccion, valor, texto)

    if texto in _ESPECIALES:
        return (SECCION_ESPECIALES, _ESPECIALES[texto], texto)

    return (SECCION_DESCONOCIDAS, 0, texto)
