from rest_framework import serializers
from terceros.models import Proveedor, Cliente, DireccionCliente
from nucleo.models import SatRegimenFiscal, SatUsoCfdi
from nucleo.utils import (
    RFCS_GENERICOS,
    errores_codigo_postal,
    exigir_codigos_postales,
    validate_rfc,
    validate_telefono,
)
from finanzas.services.facturama.acceso import empresa_usa_facturama

# Definir o cambiar el crédito del cliente es una capacidad aparte de editarlo (#462).
PERMISO_CREDITO_CLIENTE = "E-CRM-CLIENTES-CREDITO"
CAMPOS_CREDITO_CLIENTE = ("dias_credito", "limite_credito")


def verificar_rfc_cliente(rfc, empresa):
    """``(aplica, error)`` del RFC de un cliente de ``empresa``; ``rfc`` ya normalizado.

    Facturama rechaza los RFC mal formados: si la empresa sube sus clientes allá
    (``aplica``), se valida formato + dígito verificador para no guardar un cliente
    que nunca se podrá sincronizar. ``error`` es el mensaje con el que el guardado
    rechaza el campo ``rfc``, o ``None``. Única definición para
    ``ClienteSerializer.validate()`` y ``GET clientes/verificar-rfc/`` (EC-570,
    CL-04): verificar y guardar no pueden dar resultados distintos.
    """
    aplica = bool(rfc) and empresa_usa_facturama(empresa)
    if not aplica:
        return aplica, None
    rfc_valido, error_rfc = validate_rfc(rfc)
    return aplica, None if rfc_valido else error_rfc


class ClienteSerializer(serializers.ModelSerializer):
    razon_social = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    correo = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    telefono = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=Cliente._meta.get_field("telefono").max_length,
        validators=[validate_telefono],
    )
    giro_empresarial = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    sat_regimen_fiscal_codigo = serializers.CharField(write_only=True, required=False, allow_blank=True)
    sat_uso_cfdi_codigo = serializers.CharField(write_only=True, required=False, allow_blank=True)
    # #659: ``null`` si la empresa no usa Facturama; si la usa, ``true`` cuando el
    # cliente ya está en Facturama y completo. ``facturama_faltantes``: qué le falta.
    facturama_verificado = serializers.SerializerMethodField()
    facturama_faltantes = serializers.SerializerMethodField()

    def _usa_facturama(self, obj):
        # Una consulta por respuesta (el contexto lo comparte todo el listado), no por cliente.
        if "_empresa_facturama_id" not in self.context:
            from finanzas.services.facturama.acceso import codigo_empresa_facturama
            from nucleo.models import Empresa

            codigo = codigo_empresa_facturama()
            self.context["_empresa_facturama_id"] = (
                Empresa.objects.filter(codigo=codigo).values_list("pk", flat=True).first() if codigo else None
            )
        return obj.empresa_id is not None and obj.empresa_id == self.context["_empresa_facturama_id"]

    def get_facturama_faltantes(self, obj):
        from terceros.facturama import faltantes

        return faltantes(obj) if self._usa_facturama(obj) else []

    def get_facturama_verificado(self, obj):
        from terceros.facturama import faltantes

        if not self._usa_facturama(obj):
            return None
        return bool(obj.facturama_id) and not faltantes(obj)

    class Meta:
        model = Cliente
        fields = "__all__"
        read_only_fields = ["activo", "empresa", "vendedores", "facturama_id"]
        extra_kwargs = {"telefono_2": {"validators": [validate_telefono]}}

    def _validar_permiso_credito(self, attrs, user):
        """Sin el permiso de crédito solo se puede mandar el valor que ya tiene (o el de alta: 0)."""
        if user is None or user.tiene_permiso(PERMISO_CREDITO_CLIENTE):
            return
        errores = {}
        for campo in CAMPOS_CREDITO_CLIENTE:
            if campo not in attrs:
                continue
            actual = getattr(self.instance, campo) if self.instance is not None else Cliente._meta.get_field(campo).get_default()
            if attrs[campo] != actual:
                errores[campo] = "No tienes permiso para definir las condiciones de crédito."
        if errores:
            raise serializers.ValidationError(errores)

    def validate(self, attrs):
        rfc = (attrs.get("rfc") or "").strip().upper()
        if rfc:
            attrs["rfc"] = rfc
        request = self.context.get("request")
        self._validar_permiso_credito(attrs, getattr(request, "user", None))
        empresa = getattr(getattr(request, "user", None), "empresa", None)
        _, error_rfc = verificar_rfc_cliente(rfc, self.instance.empresa if self.instance else empresa)
        if error_rfc:
            raise serializers.ValidationError({"rfc": error_rfc})
        # Los RFC genéricos (público en general / extranjero) los comparten
        # muchos clientes: no cuentan como duplicado (EC-570, CL-03).
        if empresa and rfc and rfc not in RFCS_GENERICOS:
            qs = Cliente.objects.filter(empresa=empresa, rfc__iexact=rfc, activo=True)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"rfc": "Este cliente ya existe en tu empresa (RFC duplicado)."})
        # El RFC que se guarda: el que viene o, si no viene, el que ya tiene.
        rfc_cliente = attrs["rfc"] if "rfc" in attrs else getattr(self.instance, "rfc", None)
        errores_cp = errores_codigo_postal(attrs, ("codigo_postal",), rfc_cliente)
        if errores_cp:
            raise serializers.ValidationError(errores_cp)
        codigo_regimen = attrs.pop("sat_regimen_fiscal_codigo", None)
        if codigo_regimen and not attrs.get("sat_regimen_fiscal"):
            try:
                attrs["sat_regimen_fiscal"] = SatRegimenFiscal.objects.get(codigo=str(codigo_regimen).strip())
            except SatRegimenFiscal.DoesNotExist:
                raise serializers.ValidationError({"sat_regimen_fiscal_codigo": "Régimen fiscal no encontrado"})
        codigo_uso = attrs.pop("sat_uso_cfdi_codigo", None)
        if codigo_uso and not attrs.get("sat_uso_cfdi"):
            try:
                attrs["sat_uso_cfdi"] = SatUsoCfdi.objects.get(codigo=str(codigo_uso).strip())
            except SatUsoCfdi.DoesNotExist:
                raise serializers.ValidationError({"sat_uso_cfdi_codigo": "Uso CFDI no encontrado"})
        if "telefono" in attrs and not attrs["telefono"]:
            attrs["telefono"] = ""
        if "correo" in attrs and not attrs["correo"]:
            attrs["correo"] = ""
        if "giro_empresarial" in attrs and not attrs["giro_empresarial"]:
            attrs["giro_empresarial"] = ""
        if "razon_social" in attrs and not attrs["razon_social"]:
            attrs["razon_social"] = ""
        return attrs

class ProveedorSerializer(serializers.ModelSerializer):
    def validate_moneda(self, moneda):
        # Global (sin empresa) o privada de la empresa del proveedor.
        if moneda is None or moneda.empresa_id is None:
            return moneda
        if self.instance is not None:
            empresa_id = self.instance.empresa_id
        else:
            empresa_id = getattr(getattr(self.context.get("request"), "user", None), "empresa_id", None)
        if moneda.empresa_id != empresa_id:
            raise serializers.ValidationError("La moneda no está disponible para la empresa del proveedor.")
        return moneda

    class Meta:
        model = Proveedor
        fields = "__all__"
        # ``empresa`` la asigna el servidor (``ProveedorViewSet.perform_create``);
        # ``fecha_baja`` la llena el DELETE. Los acumulados no se escriben por API.
        read_only_fields = [
            "activo", "empresa", "fecha_baja",
            "saldo_anterior", "saldo_actual", "saldo_acumulado",
            "plazo_real_dias", "fecha_ultima_compra", "fecha_ultimo_pago",
        ]

class DireccionClienteSerializer(serializers.ModelSerializer):
    def validate_cliente(self, cliente):
        # Solo un cliente de la empresa del usuario (el superusuario, de cualquiera),
        # activo o dado de baja. Uno de otra empresa responde igual que un pk
        # inexistente, sin delatar que existe.
        user = getattr(self.context.get("request"), "user", None)
        if not getattr(user, "is_superuser", False) and (
            cliente.empresa_id is None or cliente.empresa_id != getattr(user, "empresa_id", None)
        ):
            raise serializers.ValidationError(
                self.fields["cliente"].error_messages["does_not_exist"].format(pk_value=cliente.pk),
                code="does_not_exist",
            )
        # La dirección toma la empresa del cliente (``DireccionClienteViewSet``); al
        # editar sin cambiar de cliente se conserva la que ya tiene.
        cambia_cliente = self.instance is None or self.instance.cliente_id != cliente.pk
        if cliente.empresa_id is None and cambia_cliente:
            raise serializers.ValidationError("El cliente no tiene empresa asignada.")
        return cliente

    def validate(self, attrs):
        exigir_codigos_postales(attrs, ("codigo_postal",), getattr(self.instance, "cliente", None))
        return attrs

    class Meta:
        model = DireccionCliente
        fields = "__all__"
        # ``empresa`` la fija el servidor con la del cliente; antes la mandaba el cliente HTTP.
        read_only_fields = ["activo", "empresa"]
        extra_kwargs = {
            "telefono_envio": {"validators": [validate_telefono]},
            "celular_envio": {"validators": [validate_telefono]},
        }
