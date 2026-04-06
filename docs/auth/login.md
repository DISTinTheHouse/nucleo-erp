# MFA Authentication Flow

When MFA is enabled, the login process becomes a two-step flow:

### Step 1: Username/Password Authentication
User submits username/password to POST /api/auth/login/

```json
{
  "username": "string",
  "password": "string"
}
```

If credentials are valid and MFA is enabled, response includes: - ***ephemeral_token*** - Temporary token for MFA verification - ***method*** - Primary MFA method name - ***mfa_enabled: true*** - Indicates MFA is required

```json
{
    "ephemeral_token": "string",
    "method": "string",
    "mfa_enabled": true
}
```

### Step 2: MFA Code Verification
User receives MFA code (email or generates from app) and submits it to POST /api/auth/login/verify/ with: - ***ephemeral_token*** - From step 1 - ***code*** - MFA verification code or backup code

```json
{
    "ephemeral_token": "string",
    "code": "string"
}
```

Response includes final authentication tokens and user data
```json
{
    "access": "jwt",
    "refresh": "",
    "access_expiration": "date_time",
    "refresh_expiration": "date_time",
    "user": {
        "pk": 1,
        "username": "string",
        "email": "string",
        "first_name": "string",
        "last_name": "string"
    }
}
```