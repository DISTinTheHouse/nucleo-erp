class SyncfyWebhookException(Exception):
    def __init__(
        self,
        message,
        code=None,
        status_code=None,
    ):
        self.message = message
        self.code = code
        self.status_code = status_code

        super().__init__(message)