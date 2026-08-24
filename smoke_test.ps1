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

function Stop-LeftoverWatcherProcesses {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*watcher_gui.py*" } |
        ForEach-Object {
            try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
        }
}

Write-Host "=== Smoke test do Code Watcher: $Tentativas tentativas ==="
Stop-LeftoverWatcherProcesses
Start-Sleep -Seconds 1

$falhas = 0

for ($i = 1; $i -le $Tentativas; $i++) {
    Write-Host "--- tentativa $i/$Tentativas ---"
    $p = Start-Process -FilePath "pythonw" -ArgumentList "`"$watcherScript`" --show" -PassThru
    Start-Sleep -Seconds $EsperaSegundos

    $proc = Get-Process -Id $p.Id -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Host "  FALHA: processo encerrou sozinho (crash na inicializacao)." -ForegroundColor Red
        $falhas++
    } elseif (-not $proc.Responding) {
        Write-Host "  FALHA: processo travado (Not Responding)." -ForegroundColor Red
        $falhas++
    } else {
        Write-Host "  OK: respondendo normalmente." -ForegroundColor Green
    }

    if ($proc) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

Stop-LeftoverWatcherProcesses

Write-Host ""
if ($falhas -eq 0) {
    Write-Host "=== $Tentativas/$Tentativas OK ===" -ForegroundColor Green
    exit 0
} else {
    Write-Host "=== $falhas de $Tentativas tentativa(s) travaram ===" -ForegroundColor Red
    exit 1
}
