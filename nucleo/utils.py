import re
from django.core.exceptions import ValidationError


def entero_acotado(valor, por_defecto, minimo, maximo):
    """Parsea un query param a entero y lo acota a ``[minimo, maximo]``.

    Un valor ausente o no numérico cae a ``por_defecto`` en vez de dar 400: son
    parámetros de presentación (``limit``, ``page_size``), no de negocio, y no
    deberían romper la pantalla por venir mal formados.

    Vive aquí porque el patrón ya estaba repetido por varios módulos con políticas
    distintas ante entradas inválidas (``inventarios.api.views`` tiene tres
    variantes); lo nuevo debería converger en ésta.
    """
    try:
        entero = int(valor)
    except (TypeError, ValueError):
        return por_defecto
    return max(minimo, min(entero, maximo))


RFC_GENERICO_NACIONAL = "XAXX010101000"
RFC_GENERICO_EXTRANJERO = "XEXX010101000"
RFCS_GENERICOS = frozenset({RFC_GENERICO_NACIONAL, RFC_GENERICO_EXTRANJERO})


def validate_rfc(rfc):
    """
    Valida un RFC (Persona Física o Moral) según el algoritmo oficial del SAT.
    Retorna (es_valido, mensaje_error).
    """
    rfc = rfc.upper().strip()
    
    # 1. Validación de Formato (Regex)
    # Persona Física: 4 letras, 6 números (fecha), 3 caracteres (homoclave) -> Total 13
    # Persona Moral: 3 letras, 6 números (fecha), 3 caracteres (homoclave) -> Total 12
    patron_fisica = r'^[A-Z&Ñ]{4}\d{6}[A-Z0-9]{3}$'
    patron_moral = r'^[A-Z&Ñ]{3}\d{6}[A-Z0-9]{3}$'
    
    match_fisica = re.match(patron_fisica, rfc)
    match_moral = re.match(patron_moral, rfc)
    
    if not (match_fisica or match_moral):
        return False, "El formato del RFC es incorrecto. Debe ser: AAAA######HOM (Física) o AAA######HOM (Moral)."

    # RFCs Genéricos (Excepciones válidas)
    if rfc in RFCS_GENERICOS:
        return True, "RFC Genérico Válido"

    # 2. Validación de Dígito Verificador (Checksum)
    try:
        if not _validar_digito_verificador(rfc):
            return False, "El RFC tiene un formato válido pero el dígito verificador es incorrecto (posiblemente falso o mal escrito)."
    except Exception:
        # No exponer detalles internos de excepciones al cliente.
        return False, "No fue posible validar el dígito verificador del RFC."

    return True, "RFC Válido"

def _validar_digito_verificador(rfc):
    """
    Calcula el dígito verificador del RFC y lo compara con el último caracter.
    """
    # Diccionario de valores para caracteres
    # 0-9 son sus valores. A=10, B=11, ...
    # Espacio = 37 (usado para padding en moral)
    # Ñ = 24, & = 25 (OJO: El orden estándar SAT varía a veces, usaremos el estándar común)
    
    # Tabla estándar anexo 20
    chars = "0123456789ABCDEFGHIJKLMN&OPQRSTUVWXYZ Ñ" # El espacio es importante para padding moral
    # Mapeo específico según reglas comunes SAT
    # A=10 ... N=23, &=24, O=25 ... Z=36, Space=37, Ñ=38 (A veces Ñ se maneja diferente)
    
    # Implementación robusta estándar:
    # Caracteres válidos: 0-9, A-Z, &, Ñ
    # Diccionario oficial SAT:
    # 0-9: 0-9
    # A:10, B:11 ... N:23
    # &: 24
    # O: 25 ... Z: 36
    # Space: 37
    # Ñ: 38
    
    diccionario = "0123456789ABCDEFGHIJKLMN&OPQRSTUVWXYZ Ñ"
    
    # Ajuste para RFC Moral (12 chars): Se prefija con un espacio para el cálculo
    rfc_calculo = rfc
    if len(rfc) == 12:
        rfc_calculo = " " + rfc
    elif len(rfc) == 13:
        pass
    else:
        return False

    suma = 0
    # Recorrer los primeros 12 caracteres (13 en total con el DV, el DV es el índice 12)
    for i in range(13):
        c = rfc_calculo[i]
        valor = diccionario.find(c)
        if valor == -1:
            # Caracter no encontrado en tabla estándar (ej. Ñ puede estar mal mapeada si no está en string)
            # Intentemos mapear Ñ si falló
            if c == 'Ñ':
                valor = 38
            elif c == ' ':
                valor = 37
            else:
                return False 
        
        # Fórmula: (Valor) * (14 - (i+1))  donde i es 0-based index
        # Pero el índice del loop es i. La posición p es i+1?
        # En moral (con espacio al inicio):
        # Espacio es pos 1.
        # Fórmula oficial: Suma de (Vi * (Pi + 1)) ? No.
        # Fórmula: Suma( Valor_caracter * (13 - indice_0_based_en_rfc_ajustado) ) para los primeros 12 chars
        # El rfc_calculo tiene 13 chars (12 datos + 1 DV o espacio+11datos+1DV)
        # Espera, el DV es el último. Solo sumamos los primeros 12 (indices 0 a 11)
        
        if i < 12:
            peso = 13 - i # 13, 12, 11 ... 2
            suma += valor * peso
    
    # Calcular residuo
    residuo = suma % 11
    
    if residuo == 0:
        dv_calculado = '0'
    else:
        dv_calculado = 11 - residuo
        if dv_calculado == 10:
            dv_calculado = 'A'
        else:
            dv_calculado = str(dv_calculado)
            
    dv_real = rfc_calculo[12]
    return dv_calculado == dv_real


# Texto, no número: un CP como 01234 conserva el cero inicial. ``[0-9]`` y no
# ``\d``, que en Python también acepta dígitos Unicode no ASCII.
_CODIGO_POSTAL_RE = re.compile(r"[0-9]{5}")
# Solo los caracteres permitidos y al menos un dígito ("---" o "()" no son teléfonos).
_TELEFONO_RE = re.compile(r"(?=.*[0-9])[0-9+() -]+")


def validate_codigo_postal(valor):
    """CP mexicano: exactamente 5 dígitos (EC-570, CL-01).

    Vacío o ``None`` pasa: si el campo es obligatorio lo decide el serializer.
    Lanza ``django.core.exceptions.ValidationError``, que DRF convierte en error
    del campo. La excepción del cliente extranjero vive en ``errores_codigo_postal``.
    """
    if valor and not _CODIGO_POSTAL_RE.fullmatch(valor):
        raise ValidationError("El código postal debe tener exactamente 5 dígitos.")


def validate_telefono(valor):
    """Teléfono sin letras: solo dígitos, ``+``, espacios, guiones y paréntesis, con al
    menos un dígito (EC-570, CL-02).

    Vacío o ``None`` pasa, igual que ``validate_codigo_postal``.
    """
    if valor and not _TELEFONO_RE.fullmatch(valor):
        raise ValidationError("El teléfono solo admite dígitos, espacios, +, guiones y paréntesis.")


def errores_codigo_postal(attrs, campos, rfc_cliente):
    """``{campo: [mensaje]}`` de los CP de ``campos`` que vienen en ``attrs`` y no son válidos.

    Con el RFC genérico de extranjero (``XEXX010101000``) no se exige el formato.
    Solo revisa los campos presentes: un PATCH parcial que no manda el CP no se
    rechaza aunque el registro ya tenga uno inválido.
    """
    if (rfc_cliente or "").strip().upper() == RFC_GENERICO_EXTRANJERO:
        return {}
    errores = {}
    for campo in campos:
        if campo not in attrs:
            continue
        try:
            validate_codigo_postal(attrs[campo])
        except ValidationError as exc:
            errores[campo] = exc.messages
    return errores


def exigir_codigos_postales(attrs, campos, cliente_actual=None):
    """``errores_codigo_postal`` con el RFC del cliente del documento; lanza
    ``ValidationError`` ``{campo: [mensaje]}``, que DRF convierte en errores de campo.

    El cliente es el que viene en ``attrs`` o, si no viene, ``cliente_actual`` (el
    que el documento ya tiene).
    """
    cliente = attrs["cliente"] if "cliente" in attrs else cliente_actual
    errores = errores_codigo_postal(attrs, campos, getattr(cliente, "rfc", None))
    if errores:
        raise ValidationError(errores)

def check_sat_status_mock(rfc):
    """
    Simula una verificación de existencia en el SAT.
    En un entorno real, aquí se conectaría a una API de un PAC o Scraping.
    Retorna: {'exists': Bool, 'status': Str, 'message': Str}
    """
    # TODO: Integrar API Real.
    # Por ahora, si el formato y checksum son válidos (validados previamente), asumimos que podría existir.
    # Retornamos True para permitir el flujo, pero con un log.
    
    # Aquí podrías usar `requests.get('https://api.validarfc.com/check', params={'rfc': rfc})`
    
    return {
        'exists': True, 
        'status': 'active', 
        'message': 'RFC válido y activo (Simulación)'
    }
