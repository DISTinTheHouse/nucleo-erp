import re
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

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
    if rfc in ['XAXX010101000', 'XEXX010101000']:
        return True, "RFC Genérico Válido"

    # 2. Validación de Dígito Verificador (Checksum)
    try:
        if not _validar_digito_verificador(rfc):
            return False, "El RFC tiene un formato válido pero el dígito verificador es incorrecto (posiblemente falso o mal escrito)."
    except Exception as e:
        # En caso de error en el algoritmo, fallamos seguro por si acaso, o permitimos con warning.
        # Preferible fallar si estamos seguros.
        return False, f"Error al validar dígito verificador: {str(e)}"

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
    
    print(f"Sistema: Simulando consulta SAT para RFC {rfc}...")
    return {
        'exists': True, 
        'status': 'active', 
        'message': 'RFC válido y activo (Simulación)'
    }

def validate_csd(cer_content, key_content, password):
    """
    Valida el par de llaves (CSD) y la contraseña.
    Retorna (True, data_dict) o (False, error_msg).
    data_dict contiene: no_certificado, fecha_expiracion, rfc_certificado
    """
    try:
        # 1. Leer Certificado (.cer)
        try:
            cert = x509.load_der_x509_certificate(cer_content, default_backend())
        except:
            try:
                cert = x509.load_pem_x509_certificate(cer_content, default_backend())
            except:
                 return False, "Formato de archivo .cer no válido (Debe ser DER o PEM)."

        # 2. Leer Llave Privada (.key)
        pwd_bytes = password.encode('utf-8') if password else None
        try:
            private_key = serialization.load_der_private_key(
                key_content,
                password=pwd_bytes,
                backend=default_backend()
            )
        except Exception as e:
             # Try PEM just in case
            try:
                private_key = serialization.load_pem_private_key(
                    key_content,
                    password=pwd_bytes,
                    backend=default_backend()
                )
            except:
                return False, f"Contraseña incorrecta o archivo .key inválido."

        # 3. Validar correspondencia
        cert_public_key = cert.public_key()
        key_public_key = private_key.public_key()
        
        # Compare public numbers (modulus and exponent for RSA)
        if hasattr(cert_public_key, 'public_numbers') and hasattr(key_public_key, 'public_numbers'):
            if cert_public_key.public_numbers() != key_public_key.public_numbers():
                 return False, "El Certificado (.cer) no corresponde a la Llave Privada (.key)."
        else:
            # Fallback for non-RSA
            cert_pem = cert_public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            )
            key_pem = key_public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            )
            if cert_pem != key_pem:
                return False, "El Certificado (.cer) no corresponde a la Llave Privada (.key)."

        # 4. Extraer datos
        # No. Certificado (SAT uses ASCII encoding of hex for 20-digit serials)
        try:
            serial_hex = format(cert.serial_number, 'x')
            if len(serial_hex) % 2 != 0:
                serial_hex = '0' + serial_hex
            # Intentar decodificar como ASCII (formato SAT estándar)
            no_certificado = bytes.fromhex(serial_hex).decode('ascii')
        except:
            # Fallback si no es ASCII (serial estándar)
            no_certificado = str(cert.serial_number)

        expiration = cert.not_valid_after_utc if hasattr(cert, 'not_valid_after_utc') else cert.not_valid_after

        # Extraer RFC (Subject OID 2.5.4.45)
        rfc_certificado = None
        try:
            oids = cert.subject.get_attributes_for_oid(x509.NameOID.X500_UNIQUE_IDENTIFIER)
            if oids:
                rfc_certificado = oids[0].value
            else:
                rfc_certificado = None
        except:
             rfc_certificado = None

        return True, {
            'no_certificado': no_certificado,
            'fecha_expiracion': expiration,
            'rfc': rfc_certificado
        }

    except Exception as e:
        return False, f"Error validando CSD: {str(e)}"
