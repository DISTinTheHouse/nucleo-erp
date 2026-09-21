class FacturamaAPIException(Exception):
    def __init__(
        self,
        message,
        code=None,
        status_code=None,
        details=None
    ):
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details

        super().__init__(message)