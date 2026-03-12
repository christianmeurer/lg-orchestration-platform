@echo off
setlocal EnableExtensions

for %%I in ("%~dp0..") do set "ROOT=%%~fI"

if /I "%~1"=="/?" goto :usage
if not "%~1"=="" set "AZ_IMAGE_TAG=%~1"

if "%AZ_LOCATION%"=="" set "AZ_LOCATION=eastus"
if "%AZ_CONTAINER_PORT%"=="" set "AZ_CONTAINER_PORT=8001"
if "%AZ_CONTAINER_CPU%"=="" set "AZ_CONTAINER_CPU=1.0"
if "%AZ_CONTAINER_MEMORY%"=="" set "AZ_CONTAINER_MEMORY=2.0Gi"
if "%LG_PROFILE%"=="" set "LG_PROFILE=prod"

call :require AZ_RESOURCE_GROUP || goto :usage
call :require AZ_ACR_NAME || goto :usage
call :require AZ_CONTAINERAPP_NAME || goto :usage

if "%AZ_CONTAINERAPP_ENV%"=="" set "AZ_CONTAINERAPP_ENV=%AZ_CONTAINERAPP_NAME%-env"
if "%AZ_IMAGE_NAME%"=="" set "AZ_IMAGE_NAME=%AZ_CONTAINERAPP_NAME%"
if "%AZ_IMAGE_TAG%"=="" set "AZ_IMAGE_TAG=latest"

echo [deploy] root: %ROOT%
echo [deploy] resource group: %AZ_RESOURCE_GROUP%
echo [deploy] container app: %AZ_CONTAINERAPP_NAME%
echo [deploy] image: %AZ_IMAGE_NAME%:%AZ_IMAGE_TAG%

call az extension add --name containerapp --upgrade >nul || exit /b 1
call az provider register --namespace Microsoft.App >nul || exit /b 1
call az provider register --namespace Microsoft.OperationalInsights >nul || exit /b 1
call az group create --name "%AZ_RESOURCE_GROUP%" --location "%AZ_LOCATION%" >nul || exit /b 1

call az acr show --name "%AZ_ACR_NAME%" --resource-group "%AZ_RESOURCE_GROUP%" >nul 2>nul
if errorlevel 1 (
  call az acr create --name "%AZ_ACR_NAME%" --resource-group "%AZ_RESOURCE_GROUP%" --location "%AZ_LOCATION%" --sku Basic --admin-enabled true >nul || exit /b 1
) else (
  call az acr update --name "%AZ_ACR_NAME%" --admin-enabled true >nul || exit /b 1
)

call az acr build --registry "%AZ_ACR_NAME%" --image "%AZ_IMAGE_NAME%:%AZ_IMAGE_TAG%" "%ROOT%" || exit /b 1

for /f "usebackq delims=" %%I in (`az acr show --name "%AZ_ACR_NAME%" --query loginServer -o tsv`) do set "AZ_ACR_LOGIN_SERVER=%%I"
for /f "usebackq delims=" %%I in (`az acr credential show --name "%AZ_ACR_NAME%" --query username -o tsv`) do set "AZ_ACR_USERNAME=%%I"
for /f "usebackq delims=" %%I in (`az acr credential show --name "%AZ_ACR_NAME%" --query passwords[0].value -o tsv`) do set "AZ_ACR_PASSWORD=%%I"
set "AZ_IMAGE=%AZ_ACR_LOGIN_SERVER%/%AZ_IMAGE_NAME%:%AZ_IMAGE_TAG%"

call az containerapp env show --name "%AZ_CONTAINERAPP_ENV%" --resource-group "%AZ_RESOURCE_GROUP%" >nul 2>nul
if errorlevel 1 (
  call az containerapp env create --name "%AZ_CONTAINERAPP_ENV%" --resource-group "%AZ_RESOURCE_GROUP%" --location "%AZ_LOCATION%" >nul || exit /b 1
)

set "ENV_ARGS=LG_PROFILE=%LG_PROFILE% PORT=%AZ_CONTAINER_PORT%"
if not "%LG_RUNNER_API_KEY%"=="" set "ENV_ARGS=%ENV_ARGS% LG_RUNNER_API_KEY=%LG_RUNNER_API_KEY%"
if not "%MODEL_ACCESS_KEY%"=="" set "ENV_ARGS=%ENV_ARGS% MODEL_ACCESS_KEY=%MODEL_ACCESS_KEY%"
if not "%DIGITAL_OCEAN_MODEL_ACCESS_KEY%"=="" set "ENV_ARGS=%ENV_ARGS% DIGITAL_OCEAN_MODEL_ACCESS_KEY=%DIGITAL_OCEAN_MODEL_ACCESS_KEY%"

call az containerapp show --name "%AZ_CONTAINERAPP_NAME%" --resource-group "%AZ_RESOURCE_GROUP%" >nul 2>nul
if errorlevel 1 (
  call az containerapp create --name "%AZ_CONTAINERAPP_NAME%" --resource-group "%AZ_RESOURCE_GROUP%" --environment "%AZ_CONTAINERAPP_ENV%" --image "%AZ_IMAGE%" --ingress external --target-port %AZ_CONTAINER_PORT% --registry-server "%AZ_ACR_LOGIN_SERVER%" --registry-username "%AZ_ACR_USERNAME%" --registry-password "%AZ_ACR_PASSWORD%" --cpu %AZ_CONTAINER_CPU% --memory %AZ_CONTAINER_MEMORY% --env-vars %ENV_ARGS% || exit /b 1
) else (
  call az containerapp registry set --name "%AZ_CONTAINERAPP_NAME%" --resource-group "%AZ_RESOURCE_GROUP%" --server "%AZ_ACR_LOGIN_SERVER%" --username "%AZ_ACR_USERNAME%" --password "%AZ_ACR_PASSWORD%" >nul || exit /b 1
  call az containerapp update --name "%AZ_CONTAINERAPP_NAME%" --resource-group "%AZ_RESOURCE_GROUP%" --image "%AZ_IMAGE%" --cpu %AZ_CONTAINER_CPU% --memory %AZ_CONTAINER_MEMORY% --set-env-vars %ENV_ARGS% || exit /b 1
)

for /f "usebackq delims=" %%I in (`az containerapp show --name "%AZ_CONTAINERAPP_NAME%" --resource-group "%AZ_RESOURCE_GROUP%" --query properties.configuration.ingress.fqdn -o tsv`) do set "AZ_CONTAINERAPP_FQDN=%%I"

if not "%AZ_CONTAINERAPP_FQDN%"=="" echo [deploy] remote api: https://%AZ_CONTAINERAPP_FQDN%

exit /b 0

:require
if defined %~1 exit /b 0
echo Missing environment variable: %~1 1>&2
exit /b 1

:usage
echo Usage: scripts\azure_deploy_personal.cmd [image-tag]
echo Required environment variables: AZ_RESOURCE_GROUP, AZ_ACR_NAME, AZ_CONTAINERAPP_NAME
echo Optional environment variables: AZ_LOCATION, AZ_CONTAINERAPP_ENV, AZ_IMAGE_NAME, AZ_IMAGE_TAG, AZ_CONTAINER_PORT, AZ_CONTAINER_CPU, AZ_CONTAINER_MEMORY, LG_PROFILE, LG_RUNNER_API_KEY, MODEL_ACCESS_KEY, DIGITAL_OCEAN_MODEL_ACCESS_KEY
exit /b 1
