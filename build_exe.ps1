<#
    build_exe.ps1 — empacota o Code Watcher como um .exe autonomo.

    Rode isso sempre que mudar code_watcher.py, watcher_gui.py ou ui.html
    e quiser atualizar o CodeWatcher.exe usado no boot (startup.ps1).

    Gera dist\CodeWatcher.exe (~15-20MB, standalone, sem precisar de Python
    instalado na maquina). --onefile: um unico arquivo, mais simples de
    distribuir/atualizar, custo de ~2-3s de extracao pra pasta temp a cada
    inicio (aceitavel pra um app que sobe uma vez no boot).
#>

Set-Location $PSScriptRoot

python -m PyInstaller `
    --onefile `
    --windowed `
    --name CodeWatcher `
    --icon icon.ico `
    --add-data "ui.html;." `
    --noconfirm `
    watcher_gui.py

if ($LASTEXITCODE -eq 0) {
    Write-Host "`nBuild ok: $PSScriptRoot\dist\CodeWatcher.exe"
} else {
    Write-Warning "Build falhou (codigo $LASTEXITCODE) - veja o log acima."
}
