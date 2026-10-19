from .client import FacturamaClient

class FacturamaService:

    def __init__(self):
        self.client = FacturamaClient()

    def get_products(self, **params):
        return self.client.get("/products", params=params)

    def get_product(self, product_id):
        return self.client.get(f"/Product/{product_id}")

    def create_product(self, payload):
        return self.client.post("/Product", json=payload)

    def update_product(self, product_id, payload):
        return self.client.put(f"/Product/{product_id}", json=payload)

    def delete_product(self, product_id):
        return self.client.delete(f"/Product/{product_id}")

    def create_cfdi_emision(self, payload):
        return self.client.post("/3/cfdis", json=payload)

    def get_cfdi_file(
        self,
        file_format,
        cfdi_type,
        cfdi_id
    ):
        return self.client.get(f"/cfdi/{file_format}/{cfdi_type}/{cfdi_id}")

    def get_cfdi_acknowledgement(
        self,
        file_format,
        cfdi_type,
        cfdi_id
    ):
        return self.client.get(f"/acuse/{file_format}/{cfdi_type}/{cfdi_id}")