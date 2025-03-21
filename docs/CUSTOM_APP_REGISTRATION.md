# Custom app registration

The code in this repo is intended to allow for quick-to-try usage. This includes a hard-coded app registration details in the app and service. While this works for minimal setup for development and usage in localhost environments, you will need to create your own Azure app registration and update the app and service files with the new app registration details if you want to use this in a hosted environment.

**DISCLAIMER**: The security considerations of hosting a service with a public endpoint are beyond the scope of this document. Please ensure you understand the implications of hosting a service before doing so. It is **not recommended** to host a publicly available instance of the Semantic Workbench app.

## Create a new Azure app registration

### Prerequisites

In order to complete these steps, you will need to have an Azure account. If you don't have an Azure account, you can create a free account by navigating to https://azure.microsoft.com/en-us/free.

App registration is a free service, but you may need to provide a credit card for verification purposes.

### Steps

- Navigate to the [Azure portal > App registrations](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
  - Click on `New registration` and fill in the details
    | ![image](https://github.com/user-attachments/assets/c9c8bb37-4ce5-40a9-a975-f0bf8fb08bbe) |
    |:-:|
  - Name: `Semantic Workbench` (or any name you prefer)
  - Supported account types: `Accounts in any organizational directory and personal Microsoft accounts`
    | ![image](https://github.com/user-attachments/assets/703f8611-369d-43cf-b9b9-199f1c1e0e03) |
    |:-:|
  - Redirect URI: `Single-page application (SPA)` & `https://<YOUR_HOST>`
    - Example (if using [Codespaces](../.devcontainer/README.md)): `https://<YOUR_CODESPACE_HOST>-4000.app.github.dev`
    - If you don't have a website deployed yet, enter `https://localhost:5001`, you can configure this later.
    - Note: the same Entra App can be reused for multiple websites, and multiple URLs can be added.
      | ![Image](https://github.com/user-attachments/assets/e709ee12-a3ef-4be3-9f2d-46d33c929f42) |
      |:-:|
  - Click on `Register`
- View the `Overview` page for the newly registered app and copy the `Application (client) ID` for the next steps.
- You can return to the list of your Entra apps from [here](https://portal.azure.com/#view/Microsoft_AAD_IAM/ActiveDirectoryMenuBlade/~/RegisteredApps).

## Update your app and service files with the new app registration details

Edit the following files with the new app registration details:

- Semantic Workbench app: [.env.example](../workbench-app/.env.example)

  - Copy the `.env.example` file to `.env`
  - Update the `VITE_SEMANTIC_WORKBENCH_CLIENT_ID` with the `Application (client) ID`

- Semantic Workbench service: [.env.example](../workbench-service/.env.example)

  - Copy the `.env.example` file to `.env`
  - Update the `WORKBENCH__AUTH__ALLOWED_APP_ID` with the `Application (client) ID`
