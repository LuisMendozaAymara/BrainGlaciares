# Despliega la imagen en Azure Container Apps (Azure for Students) leyendo las claves del archivo .env.
# Las claves viajan como secretos de la aplicación: no quedan en la imagen ni en el repositorio.
#
#   .\scripts\desplegar_azure.ps1 -Imagen tuusuario/agente-glaciares:1.0
#   .\scripts\desplegar_azure.ps1 -Imagen tuusuario/agente-glaciares:1.1     (actualizar a otra versión)
param(
    [Parameter(Mandatory = $true)][string]$Imagen,
    [string]$Grupo = "rg-glaciares",
    [string]$Entorno = "env-glaciares",
    [string]$App = "agente-glaciares",
    [string]$Region = "eastus"
)
$ErrorActionPreference = "Stop"

$rutaEnv = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"
if (-not (Test-Path $rutaEnv)) { throw "No encuentro $rutaEnv. Copia .env.example como .env y completa tus claves." }
$valores = @{}
foreach ($linea in Get-Content $rutaEnv -Encoding UTF8) {
    if ($linea -match '^\s*([A-Z_]+)\s*=\s*(.+?)\s*$') { $valores[$Matches[1]] = $Matches[2].Trim('"', "'") }
}
if (-not ($valores["GROQ_API_KEY"] -or $valores["GOOGLE_API_KEY"])) { throw "El .env necesita GROQ_API_KEY o GOOGLE_API_KEY." }

# Nombre del secreto en Azure -> variable que recibe el contenedor
$mapa = [ordered]@{
    "groq-key"     = "GROQ_API_KEY"
    "google-key"   = "GOOGLE_API_KEY"
    "neo4j-uri"    = "NEO4J_URI"
    "neo4j-pass"   = "NEO4J_PASSWORD"
    "agente-key"   = "AGENTE_API_KEY"
}
$secretos = @(); $variables = @()
foreach ($s in $mapa.Keys) {
    $v = $valores[$mapa[$s]]
    if ($v) { $secretos += "$s=$v"; $variables += "$($mapa[$s])=secretref:$s" }
}
foreach ($k in "NEO4J_USERNAME", "AGENTE_MODELOS") { if ($valores[$k]) { $variables += "$k=$($valores[$k])" } }

az extension add --name containerapp --upgrade --only-show-errors
az provider register --namespace Microsoft.App --only-show-errors
az provider register --namespace Microsoft.OperationalInsights --only-show-errors
az group create --name $Grupo --location $Region --only-show-errors --output none

$existeEnv = az containerapp env list --resource-group $Grupo --query "[?name=='$Entorno'] | length(@)" --output tsv
if ($existeEnv -eq "0") {
    az containerapp env create --name $Entorno --resource-group $Grupo --location $Region --only-show-errors --output none
}

$existeApp = az containerapp list --resource-group $Grupo --query "[?name=='$App'] | length(@)" --output tsv
if ($existeApp -eq "0") {
    # 0 réplicas sin uso: mientras nadie pregunta no consume el crédito; al despertar tarda alrededor de un minuto
    az containerapp create --name $App --resource-group $Grupo --environment $Entorno --image $Imagen `
        --target-port 8080 --ingress external --cpu 2 --memory 4Gi --min-replicas 0 --max-replicas 1 `
        --secrets @secretos --env-vars @variables --only-show-errors --output none
} else {
    az containerapp secret set --name $App --resource-group $Grupo --secrets @secretos --only-show-errors --output none
    az containerapp update --name $App --resource-group $Grupo --image $Imagen --set-env-vars @variables --only-show-errors --output none
}

$dominio = az containerapp show --name $App --resource-group $Grupo --query "properties.configuration.ingress.fqdn" --output tsv
Write-Host "Listo: https://$dominio"
