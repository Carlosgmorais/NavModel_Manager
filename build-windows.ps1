# Build script for NavModel Manager 0.0.12 on Windows 11 x64
[CmdletBinding()]
param(
    [string]$ISCC = '',
    [switch]$SkipInstaller,
    [switch]$UseExistingEngine,
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

# Detect Cargo (Rust compiler toolchain)
$cargoExe = ''
$cargoCandidates = @(
    "$env:USERPROFILE\.cargo\bin\cargo.exe",
    "$env:LOCALAPPDATA\.cargo\bin\cargo.exe",
    "$env:ProgramFiles\Rust\bin\cargo.exe",
    "C:\Users\Carlos Gomes\.cargo\bin\cargo.exe"
)
foreach ($cand in $cargoCandidates) {
    if ($cand -and (Test-Path $cand)) {
        $cargoExe = $cand
        break
    }
}
if (-not $cargoExe) {
    $cmdCargo = Get-Command 'cargo.exe' -ErrorAction SilentlyContinue
    if ($cmdCargo) { $cargoExe = $cmdCargo.Source }
}
if (-not $cargoExe -and -not $UseExistingEngine) {
    throw "Compilador Rust Cargo nao encontrado no sistema. Instale o Rustup."
}

$root = $PSScriptRoot
Push-Location $root
try {
    Write-Host "=========================================================="
    Write-Host "  NAVMODEL MANAGER 0.0.12 - COMPILADOR E GERADOR DE SETUP  "
    Write-Host "=========================================================="
    Write-Host "Python: $pyExe"
    Write-Host "Cargo:  $cargoExe"

    if (-not $UseExistingEngine) {
        # Step 1: Compile Rust DWG Engine (Mandatory Priority 1)
        Write-Host ""
        Write-Host "[1/6] Compilando motor nativo Rust dwg-reader com Cargo (--release)..."
        $manifestPath = Join-Path $root 'dwg-reader\Cargo.toml'
        & $cargoExe build --release --manifest-path $manifestPath
        if ($LASTEXITCODE -ne 0) {
            throw "ERRO FATAL: Falha na compilacao Rust de dwg-reader/Cargo.toml (exit code: $LASTEXITCODE)."
        }

        $builtBin = Join-Path $root 'dwg-reader\target\release\plantview-dwg.exe'
        if (-not (Test-Path $builtBin)) {
            throw "Binario compilado nao encontrado em: $builtBin"
        }

        $engineDir = Join-Path $root 'engine'
        if (-not (Test-Path $engineDir)) {
            New-Item -ItemType Directory -Path $engineDir -Force | Out-Null
        }
        $targetBin = Join-Path $engineDir 'plantview-dwg.exe'
        Copy-Item -Path $builtBin -Destination $targetBin -Force

        $engineHash = (Get-FileHash $targetBin -Algorithm SHA256).Hash
        $engineBytes = (Get-Item $targetBin).Length
        $engineDate = (Get-Item $targetBin).LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
        $engineSizeMb = [math]::Round($engineBytes / 1048576, 2)

        $engineMeta = @{
            name       = "plantview-dwg"
            version    = "0.1.0"
            built_at   = $engineDate
            sha256     = $engineHash
            size_bytes = $engineBytes
        } | ConvertTo-Json -Depth 2
        Set-Content -Path (Join-Path $engineDir 'engine_info.json') -Value $engineMeta -Encoding UTF8

        Write-Host "Motor nativo compilado e copiado para: $targetBin"
        Write-Host "Tamanho: $engineSizeMb MB | SHA256: $engineHash"

    }
    else {
        $enginePath = Join-Path $root 'engine/plantview-dwg.exe'
        $meta = Get-Content (Join-Path $root 'engine/engine_info.json') | ConvertFrom-Json
        if ((Get-FileHash $enginePath -Algorithm SHA256).Hash -ne $meta.sha256) {
            throw 'Motor DWG existente nao corresponde ao SHA256 registrado.'
        }
        Write-Host 'Motor DWG original verificado por SHA256; preservado sem recompilar.'
    }
    # Step 2: Generate icons and assets
    Write-Host ""
    Write-Host "[2/6] Gerando icones de alta resolucao e arte do instalador..."
    & $pyExe scripts/generate_assets.py
    if ($LASTEXITCODE -ne 0) { throw "Falha na geracao de icones." }

    # Step 3: Tests and Validation
    Write-Host ""
    Write-Host "[3/6] Executando testes automatizados e integracao..."
    & $pyExe -m unittest tests.test_gpu_logic_light
    if ($LASTEXITCODE -ne 0) { throw "Falha nos testes de logica GPU." }
    & $pyExe -m unittest tests.test_navmodel_ui
    if ($LASTEXITCODE -ne 0) { throw "Falha nos testes da interface NavModel." }
    & $pyExe -m unittest tests.test_cad_fidelity
    if ($LASTEXITCODE -ne 0) { throw "Falha nos testes de fidelidade CAD." }
    & $pyExe tests/verify.py
    if ($LASTEXITCODE -ne 0) { throw "Falha nos testes de integracao CAD." }
    # The portable web host is embedded into every published EXE when .NET SDK is available.
    $hasDotnetSdk = (Get-Command dotnet -ErrorAction SilentlyContinue) -and (& dotnet --list-sdks 2>$null)
    if ($hasDotnetSdk) {
        & dotnet publish portable_viewer/NavModelWebHost.csproj -c Release -r win-x64 --self-contained true -o build/web-host
        if ($LASTEXITCODE -eq 0 -and (Test-Path 'build/web-host/NavModelWebHost.exe')) {
            Copy-Item -LiteralPath 'build/web-host/NavModelWebHost.exe' -Destination 'resources/web/NavModelWebHost.exe' -Force
        }
    }
    & $pyExe -m unittest tests.test_sectioning tests.test_measurement_acceleration tests.test_web_publish
    if ($LASTEXITCODE -ne 0) { throw "Falha nos testes de cortes, medicao ou publicacao Web." }
    Write-Host "Todos os testes foram aprovados com sucesso!"

    # Step 4: PyInstaller Build
    Write-Host ""
    Write-Host "[4/6] Compilando executavel Windows standalone com PyInstaller..."
    & $pyExe -m PyInstaller --noconfirm --clean PlantViewStudio.spec
    if ($LASTEXITCODE -ne 0) { throw "Falha na compilacao com PyInstaller." }

    $distExe = Join-Path $root 'dist/NavModel Manager/NavModel Manager.exe'
    if (-not (Test-Path $distExe)) {
        throw "Executavel nao encontrado em: $distExe"
    }
    Write-Host "Executavel gerado com sucesso: $distExe"

    # Step 5: Frozen Smoke Test
    Write-Host ""
    Write-Host "[5/6] Executando smoke-test no executavel compilado..."
    $testOutput = Join-Path $root 'test_dist'
    & $distExe --smoke-test $testOutput | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "O teste do executavel compilado falhou."
    }
    else {
        $smoke = Get-Content (Join-Path $testOutput 'smoke-test.json') -Raw | ConvertFrom-Json
        if ($smoke.models.Count -ne 3 -or $smoke.errors.Count -ne 0 -or $smoke.splash_visible_ms -lt 5000 -or $smoke.web.html.bytes -le 0 -or ($smoke.web.PSObject.Properties['exe'] -and $smoke.web.exe.bytes -le 0)) {
            throw 'Smoke test incompleto: exige DWG, STEP, IFC, cortes, publicacao HTML e splash.'
        }
        Write-Host "Smoke test do executavel aprovado com sucesso!"
    }

    # Only generate and copy the standalone installer EXE to Desktop (no extra folders)
    $desktopPath = [Environment]::GetFolderPath('Desktop')
    if (-not $CopyTo -and (Test-Path $desktopPath)) { $CopyTo = $desktopPath }

    # Step 6: Inno Setup Compilation
    if (-not $SkipInstaller) {
        Write-Host ""
        Write-Host "[6/6] Gerando instalador profissional Windows com Inno Setup..."
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

        $installerExe = Join-Path $releaseDir 'NavModel_Manager_0.0.12_Setup_x64.exe'
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
}
finally {
    Pop-Location
}
