import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalogo', '0004_alter_tipoproducto_codigo'),
        ('nucleo', '0014_remove_departamento_departament_empresa_4c7874_idx_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='producto',
            name='categoria_producto',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='productos', to='catalogo.categoriaproducto'),
        ),
        migrations.AlterField(
            model_name='producto',
            name='descripcion',
            field=models.CharField(blank=True, default='', max_length=150),
        ),
        migrations.AlterField(
            model_name='producto',
            name='impuesto',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='productos', to='nucleo.impuesto'),
        ),
        migrations.AlterField(
            model_name='producto',
            name='sat_prodserv',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='productos', to='nucleo.satclaveprodserv'),
        ),
        migrations.AlterField(
            model_name='producto',
            name='sat_unidad',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='productos', to='nucleo.satclaveunidad'),
        ),
        migrations.AlterField(
            model_name='producto',
            name='tipo',
            field=models.CharField(blank=True, default='', max_length=35),
        ),
        migrations.AlterField(
            model_name='producto',
            name='unidad_medida',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='productos', to='nucleo.unidadmedida'),
        ),
    ]
