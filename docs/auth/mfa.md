# MFA Method

MFA is disabled by default, in order to enable it, follow these steps:

### Step 1: Initialize a new MFA method setup
Send method to POST /api/auth/mfa/ with:
```json
{
    "method": "string"
}
```

Response includes backup codes and setup instructions
```json
{
  "method": "app",
  "backup_codes": [
    "string"
  ],
  "setup_data": {
    "qr_link": "string"
  }
}
```

### Step 2: Confirm MFA setup

Send method and code (generated from app) to POST /api/auth/mfa/confirm/ with:
```json
{
    "method": "string",
    "code": "string"
}
```

If successful:
```json
{
  "detail": "string"
}
```
