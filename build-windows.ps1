# Build script for PlantView Studio 0.0.4 on Windows 11 x64
[CmdletBinding()]
param(
    [string]$ISCC = '',
    [switch]$SkipInstaller,
    [string]$CopyTo = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Detect Python 3.12/3.x installation
$pyExe = ''
$candidates = @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:ProgramFiles\Python312\python.exe",
    "$env:ProgramFiles\Python311\python.exe",
    "C:\Users\Carlos Gomes\AppData\Local\Programs\Python\Python312\python.exe"
)
foreach ($c in $candidates) {
    if ($c -and (Test-Path $c)) {
        $pyExe = $c
        break
    }
}
if (-not $pyExe) {
    $cmd = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch 'WindowsApps') {
        $pyExe = $cmd.Source
    }
}
if (-not $pyExe) {
    throw "Interpretador Python nao encontrado no sistema."
}

$pyDir = Split-Path $pyExe
$env:PATH = "$pyDir;$pyDir\Scripts;$env:PATH"
Set-Alias -Name python -Value $pyExe -Scope Global -Force

$root = $PSScriptRoot
Push-Location $root
try {
    Write-Host "=========================================================="
    Write-Host "  PLANTVIEW STUDIO 0.0.4 - COMPILADOR E GERADOR DE SETUP  "
    Write-Host "=========================================================="
    Write-Host "Python: $pyExe"

    # Step 1: Generate icons and assets
    Write-Host ""
    Write-Host "[1/5] Gerando icones de alta resolucao e arte do instalador..."
    & $pyExe scripts/generate_assets.py
    if ($LASTEXITCODE -ne 0) { throw "Falha na geracao de icones." }

    # Step 2: Tests and Validation
    Write-Host ""
    Write-Host "[2/5] Executando testes automatizados e integracao..."
    & $pyExe -m unittest tests.test_gpu_logic_light
    if ($LASTEXITCODE -ne 0) { throw "Falha nos testes de logica GPU." }
    & $pyExe tests/verify.py
    if ($LASTEXITCODE -ne 0) { throw "Falha nos testes de integracao CAD." }
    Write-Host "Todos os testes foram aprovados com sucesso!"

    # Step 3: PyInstaller Build
    Write-Host ""
    Write-Host "[3/5] Compilando executavel Windows standalone com PyInstaller..."
    & $pyExe -m PyInstaller --noconfirm --clean PlantViewStudio.spec
    if ($LASTEXITCODE -ne 0) { throw "Falha na compilacao com PyInstaller." }

    $distExe = Join-Path $root 'dist/PlantView Studio.exe'
    if (-not (Test-Path $distExe)) {
        throw "Executavel nao encontrado em: $distExe"
    }
    Write-Host "Executavel gerado com sucesso: $distExe"

    # Step 4: Frozen Smoke Test
    Write-Host ""
    Write-Host "[4/5] Executando smoke-test no executavel compilado..."
    $testOutput = Join-Path $root 'test_dist'
    & $distExe --smoke-test $testOutput
    if ($LASTEXITCODE -ne 0) {
        throw "O teste do executavel compilado falhou."
    } else {
        Write-Host "Smoke test do executavel aprovado com sucesso!"
    }

    # Step 5: Inno Setup Compilation
    if (-not $SkipInstaller) {
        Write-Host ""
        Write-Host "[5/5] Gerando instalador profissional Windows com Inno Setup..."
        $isccCandidates = @(
            $ISCC,
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
            "C:\Users\Carlos Gomes\AppData\Local\Programs\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        )
        foreach ($cand in $isccCandidates) {
            if ($cand -and (Test-Path $cand)) {
                $ISCC = $cand
                break
            }
        }
        if (-not (Test-Path $ISCC)) {
            $cmd = Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue
            if ($null -ne $cmd) { $ISCC = $cmd.Source }
        }
        if (-not (Test-Path $ISCC)) {
            throw "Compilador Inno Setup nao localizado em: $ISCC"
        }
        Write-Host "Usando Inno Setup: $ISCC"
        $releaseDir = Join-Path $root 'release'
        New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
        
        & $ISCC installer.iss
        if ($LASTEXITCODE -ne 0) { throw "Falha ao gerar o instalador com Inno Setup." }

        $installerExe = Join-Path $releaseDir 'PlantView_Studio_0.0.4_Setup_x64.exe'
        if (-not (Test-Path $installerExe)) {
            throw "Instalador nao encontrado: $installerExe"
        }
        $bytes = (Get-Item $installerExe).Length
        $sizeMb = [math]::Round($bytes / 1048576, 1)

        # Copy to target directory or Desktop if requested
        $desktopPath = [Environment]::GetFolderPath('Desktop')
        if (-not $CopyTo -and (Test-Path $desktopPath)) {
            $CopyTo = $desktopPath
        }

        if ($CopyTo -and (Test-Path $CopyTo)) {
            $destFile = Join-Path $CopyTo (Split-Path $installerExe -Leaf)
            Copy-Item -Path $installerExe -Destination $destFile -Force
            Write-Host "Instalador copiado com sucesso para a Area de Trabalho: $destFile"
        }

        Write-Host ""
        Write-Host "=========================================================="
        Write-Host "  SUCESSO TOTAL! INSTALADOR GERADO COM EXITO:"
        Write-Host "  $installerExe"
        if ($CopyTo) {
            Write-Host "  Copia na Area de Trabalho: $destFile"
        }
        Write-Host "  Tamanho: $sizeMb MB"
        Write-Host "=========================================================="
    }
} finally {
    Pop-Location
}
