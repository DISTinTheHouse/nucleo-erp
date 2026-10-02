<div align="center">
    <p>
        <img src="https://syncfy.com/w/es/assets/base/images/icon-app-sync.svg" width="120">
    </p>
        <h1>Syncfy</h1>
        <h3>The Syncfy API provides a suite of solutions that integrates open data and payments, acting as a bridge that connects third party solutions with financial institutions and payment services in a secure and efficient way.</h3>

  <p>
    <a href="#Auth">Authentication</a> ·
    <a href="#Users">Users</a> ·
    <a href="#Sessions">Sessions</a> ·
    <a href="#Widget">Widget</a> ·
    <a href="#FrontEnd">Frontend Integration</a> ·
  </p>
</div>

<a id="Auth"></a>
## Authentication

Syncfy API has two types of authentication:

<h4>API KEY</h4>

```cURL
curl URL \
-H "Authorization: api_key api_key={API_KEY}" \
-H MEDIA_TYPE \
-X METHOD \
-d PARAMS 
```

<h4>Bearer Token</h4>

```cURL
curl URL \
-H "Authorization: Bearer {TOKEN}" \
-H MEDIA_TYPE \
-X METHOD \
-d PARAMS 
```

Type of Authentication required is defined by Resource

| Resource     | Auth Type |
| --------     | -------   |    
Users   | API KEY |
Sessions | API KEY |
Catalogs | Bearer Token |
Credentials | Bearer Token |
Accounts | Bearer Token |
Transactions | Bearer Token |
Attachments | Bearer Token |
Documents | Bearer Token |

<a id="Users"></a>

## Users
Users are logical segmentations for end-users. It's a best practice to register users in order to have their information grouped and have control on both ends. 

`POST` __Create a new user__
```
 https://opendata-api.syncfy.com/v1/users
```

__Body:__

```json
{
    "id_external": "ACM010101ABC", // (string) username
    "name": "ACME User" // (string) can be null and be used to keep track of that user with an external ID 
}
```

__Response sample:__

```json
 {
    "rid": "4776a7ab-a2e8-4f16-a0cc-7e42d9bf69d0",
    "code": 200,
    "errors": null,
    "status": true,
    "message": null,
    "response": [
        {
            "id_user": "5dde750d8c91e77a123e7013",
            "id_external": "ACM010101ABC",
            "name": "ACME User",
            "dt_create": 1574860045,
            "dt_modify": 1584556803
        }
    ]
} 
```

<a id="Sessions"></a>

## Sessions

Sessions are access tokens for the final user in order to consume Syncfy endpoints. Sessions expire after 5 minutes of inactivity and should be used for creating or updating credentials.

`POST` __Create a session token__
```
https://opendata-api.syncfy.com/v1/sessions
```

__Body:__

```json
{
    "id_user": "{{sync_id_user}}",
}
```

__Response sample:__

```json
 {
    "rid": "b2feccd3-7776-43ed-a198-e94b64f07349",
    "code": 200,
    "errors": null,
    "status": true,
    "message": null,
    "response": {
        "token": "ba477d90a30ed4ab7faaf60124dd0df99508fc7c8668ab714b63c089bada1b81"
    }
} 
```

<a id="Widget"></a>

## Widget

This snippet already has the session token you just created (sessions) and it contains all the necessary logic for you to instantiate the Syncfy Widget. 

```html
<!DOCTYPE html>
<script>(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src='https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);})(window,document,'script','dataLayer','GTM-PJJQHV8');</script>
<html>
  <head>
    <meta charset="utf-8" />
    <link rel="stylesheet" href="https://syncfy.com/widget/v3/syncfy-authentication-widget.css" />
    <title>Syncfy Widget Quickstart</title>
  </head>
  <body>
    <div id="widget"></div>
    <script
      type="text/javascript"
      src="https://syncfy.com/widget/v3/syncfy-authentication-widget.js"
    ></script>
    <script>
      var params = {
        // Set up the token you created in the Quickstart:
        token: 'b7af4690e603a3e709029d4eee765dea3e59a0e9c43fc8a46e72a8f1ed208795',
        config: {
          // Set up the language to use:
          locale: 'es',
          entrypoint: {
            // Set up the country to start:
            country: 'MX',
            // Set up the site organization type to start:
            siteOrganizationType: '56cf4f5b784806cf028b4568',
          },
          navigation: {
            displayStatusInToast: true,
          },
        },
      };
      var syncfyWidget = new SyncfyWidget(params);
      syncfyWidget.open();
    </script>

<noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-PJJQHV8"
height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>
  </body>
</html>
```

 The Syncfy Widget might fail and display an Error: Try Again message. This happens when the current Session entity has expired. Each session has an expiration time of about 5 minutes after it was lastly used. this can be fixed by requesting a new token and update your index.html. Once a new token is obtained, you can copy and paste it your index.html

 <a id="FrontEnd"></a>

 ## Frontend integration

1. The user clicks **"Connect Bank / SAT"**.

2. The frontend sends a `POST` request to `/api/v1/finanazas/syncfy/sessions/` and receives the token.

3. Initialize the widget using the received token. The current package is `@syncfy/authentication-widget`, and it is configured as follows:

```js
import SyncfyWidget from "@syncfy/authentication-widget";
import "@syncfy/authentication-widget/dist/style.css";

const { token } = await api.post("/syncfy/session/");
new SyncfyWidget({
  token,
  element: "#widget",
  config: { locale: "es", entrypoint: { country: "MX" } },
});
```

4. When the user finishes registering or making changes, the actual state is sent to the backend **via webhook** and stored in the ERP database for subsequent retrieval.
