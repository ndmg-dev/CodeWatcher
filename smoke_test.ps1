<#
    smoke_test.ps1

    Teste de fumaca do Code Watcher: abre watcher_gui.py N vezes seguidas e
    confere se o processo fica responsivo (nao trava) em cada tentativa.

    Motivo de existir: o app ja teve mais de um bug de deadlock intermitente
    na inicializacao da janela (pywebview + WebView2), que so aparecia depois
    de varias aberturas manuais repetidas. Este script automatiza exatamente
    esse teste manual, para rodar depois de qualquer mudanca em watcher_gui.py
    ou watcher/gui/*.py, sem precisar repetir o processo a mao.

    USO:
        powershell -File smoke_test.ps1
        powershell -File smoke_test.ps1 -Tentativas 15

    Sai com codigo 0 se todas as tentativas responderam; 1 se alguma travou.
#>

param(
    [int]$Tentativas = 8,
    [int]$EsperaSegundos = 6
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$watcherScript = Join-Path $scriptDir "watcher_gui.py"

if (-not (Test-Path $watcherScript)) {
    Write-Error "watcher_gui.py nao encontrado em $watcherScript"
    exit 1
}

# IMPORTANTE: nao usar "pythonw" pelado. Nesta maquina isso as vezes resolve
# para o shim do Windows Apps (WindowsApps\pythonw.exe), que re-executa o
# interpretador de verdade como um PROCESSO FILHO SEPARADO — Start-Process
# entao devolve o PID do shim, nao o do processo real, e Stop-Process nesse
# PID deixa o processo real orfao (ja aconteceu, ficou "Code Watcher" duplicado
# rodando depois de um smoke test). Resolvendo o caminho completo de antemao
# (igual ao atalho da Area de Trabalho) evita esse shim por completo.
$pythonwCmd = Get-Command pythonw -ErrorAction SilentlyContinue
$pythonwPath = if ($pythonwCmd -and $pythonwCmd.Source -notlike "*WindowsApps*") {
    $pythonwCmd.Source
} else {
    "C:\Users\User\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
}
if (-not (Test-Path $pythonwPath)) {
    Write-Error "pythonw.exe nao encontrado em $pythonwPath (ajuste a variavel no script)"
    exit 1
}

function Get-WatcherProcesses {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*watcher_gui.py*" }
}

function Stop-LeftoverWatcherProcesses {
    Get-WatcherProcesses | ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
    }
}

Write-Host "=== Smoke test do Code Watcher: $Tentativas tentativas ==="
Write-Host "(usando $pythonwPath)"
Stop-LeftoverWatcherProcesses
Start-Sleep -Seconds 1

$falhas = 0

for ($i = 1; $i -le $Tentativas; $i++) {
    Write-Host "--- tentativa $i/$Tentativas ---"
    Start-Process -FilePath $pythonwPath -ArgumentList "`"$watcherScript`" --show"
    Start-Sleep -Seconds $EsperaSegundos

    # Identifica o processo real pela linha de comando, nao pelo PID que
    # Start-Process devolveu (ver nota acima sobre o shim do WindowsApps).
    $found = Get-WatcherProcesses
    if (-not $found) {
        Write-Host "  FALHA: processo encerrou sozinho (crash na inicializacao)." -ForegroundColor Red
        $falhas++
    } else {
        $travou = $false
        foreach ($w in $found) {
            $proc = Get-Process -Id $w.ProcessId -ErrorAction SilentlyContinue
            if ($proc -and -not $proc.Responding) { $travou = $true }
        }
        if ($travou) {
            Write-Host "  FALHA: processo travado (Not Responding)." -ForegroundColor Red
            $falhas++
        } else {
            Write-Host "  OK: respondendo normalmente." -ForegroundColor Green
        }
    }

    Stop-LeftoverWatcherProcesses
    Start-Sleep -Seconds 2
}

Write-Host ""
if ($falhas -eq 0) {
    Write-Host "=== $Tentativas/$Tentativas OK ===" -ForegroundColor Green
    exit 0
} else {
    Write-Host "=== $falhas de $Tentativas tentativa(s) travaram ===" -ForegroundColor Red
    exit 1
}
