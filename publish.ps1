# Construit l'image et la publie sur un registre, depuis ce PC Windows.
# A n'utiliser que si tu deploies la stack via docker-compose.registry.yml.
#
#   .\publish.ps1 -Image ghcr.io/mon-compte/vpn-benchmark:latest
#
# Connexion prealable au registre :
#   GitHub Container Registry : docker login ghcr.io -u MON_COMPTE
#   Docker Hub                : docker login
param(
    [Parameter(Mandatory = $true)][string]$Image
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "construction de $Image" -ForegroundColor Cyan
docker build -t $Image .
if (-not $?) { throw "echec de la construction" }

Write-Host "publication de $Image" -ForegroundColor Cyan
docker push $Image
if (-not $?) { throw "echec de la publication" }

Write-Host ""
Write-Host "Image publiee. Dans Portainer, colle docker-compose.registry.yml" -ForegroundColor Green
Write-Host "et mets BENCH_IMAGE = $Image dans les variables de la stack." -ForegroundColor Green
