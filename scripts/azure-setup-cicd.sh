#!/usr/bin/env bash
# Configura el despliegue automático GitHub -> Azure SIN contraseñas (OIDC).
# Ejecutar UNA vez en Azure Cloud Shell (Bash):
#   curl -fsSL https://raw.githubusercontent.com/molxno/sesame-bot/main/scripts/azure-setup-cicd.sh | bash
# Crea una identidad administrada (gratis) que solo puede usarse desde la rama main de este repo
# y que solo tiene permisos sobre el grupo de recursos "sesame".
set -euo pipefail

RG=sesame
REPO=molxno/sesame-bot
IDENTITY=sesame-github
LOCATION=northcentralus

az provider register --namespace Microsoft.ManagedIdentity --wait --only-show-errors
az identity create -g "$RG" -n "$IDENTITY" -l "$LOCATION" -o none --only-show-errors
CLIENT_ID=$(az identity show -g "$RG" -n "$IDENTITY" --query clientId -o tsv)
PRINCIPAL_ID=$(az identity show -g "$RG" -n "$IDENTITY" --query principalId -o tsv)

# GitHub puede firmar el token con el nombre del repo o con sus IDs inmutables: se registran ambos.
IDS=$(curl -fsSL "https://api.github.com/repos/$REPO" | jq -r '"\(.owner.login)@\(.owner.id)/\(.name)@\(.id)"')
for pair in "github-main|repo:$REPO:ref:refs/heads/main" "github-main-ids|repo:$IDS:ref:refs/heads/main"; do
  az identity federated-credential create -g "$RG" --identity-name "$IDENTITY" -n "${pair%%|*}" \
    --issuer https://token.actions.githubusercontent.com \
    --subject "${pair#*|}" \
    --audiences api://AzureADTokenExchange -o none --only-show-errors || true
done

RG_ID=$(az group show -n "$RG" --query id -o tsv)
az role assignment create --assignee-object-id "$PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
  --role Contributor --scope "$RG_ID" -o none --only-show-errors || true

echo
echo "Listo. Crea estas 3 variables en GitHub (Settings > Secrets and variables > Actions > Variables):"
echo "AZURE_CLIENT_ID=$CLIENT_ID"
echo "AZURE_TENANT_ID=$(az account show --query tenantId -o tsv)"
echo "AZURE_SUBSCRIPTION_ID=$(az account show --query id -o tsv)"
