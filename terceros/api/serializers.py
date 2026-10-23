from rest_framework import serializers
from terceros.models import Proveedor, Cliente, DireccionCliente
from nucleo.models import SatRegimenFiscal, SatUsoCfdi
from nucleo.utils import errores_codigo_postal, validate_rfc, validate_telefono
from finanzas.services.facturama.acceso import empresa_usa_facturama

# Definir o cambiar el crédito del cliente es una capacidad aparte de editarlo (#462).
PERMISO_CREDITO_CLIENTE = "E-CRM-CLIENTES-CREDITO"
CAMPOS_CREDITO_CLIENTE = ("dias_credito", "limite_credito")


class ClienteSerializer(serializers.ModelSerializer):
    razon_social = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    correo = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    telefono = serializers.CharField(required=False, allow_blank=True, allow_null=True, validators=[validate_telefono])
    giro_empresarial = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    sat_regimen_fiscal_codigo = serializers.CharField(write_only=True, required=False, allow_blank=True)
    sat_uso_cfdi_codigo = serializers.CharField(write_only=True, required=False, allow_blank=True)

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
        # Facturama rechaza los RFC mal formados: si la empresa sube sus clientes
        # allá, se valida aquí (formato + dígito verificador) para no guardar un
        # cliente que nunca se podrá sincronizar.
        if rfc and empresa_usa_facturama(self.instance.empresa if self.instance else empresa):
            rfc_valido, error_rfc = validate_rfc(rfc)
            if not rfc_valido:
                raise serializers.ValidationError({"rfc": error_rfc})
        if empresa and rfc:
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
    def validate(self, attrs):
        cliente = attrs["cliente"] if "cliente" in attrs else getattr(self.instance, "cliente", None)
        errores_cp = errores_codigo_postal(attrs, ("codigo_postal",), getattr(cliente, "rfc", None))
        if errores_cp:
            raise serializers.ValidationError(errores_cp)
        return attrs

    class Meta:
        model = DireccionCliente
        fields = "__all__"
        read_only_fields = ["activo"]
        extra_kwargs = {
            "telefono_envio": {"validators": [validate_telefono]},
            "celular_envio": {"validators": [validate_telefono]},
        }
