# PrintForce Link installer for Windows.
#
# Don't run this by hand — get your personalized command from 3D Print Force:
#   Integrations -> PrintForce Link -> "Get install command"
# It looks like:
#   irm https://raw.githubusercontent.com/Sam-3DPF/printforce-link/main/install.ps1 | iex; Install-PrintForceLink <PAIR_CODE> <DPF_URL>
#
# Runs in-memory via PowerShell, so no SmartScreen prompt. If Windows Defender removes the
# downloaded agent, open Windows Security and allow/restore it (see 3D Print Force -> Having trouble?).

function Install-PrintForceLink {
  param(
    [Parameter(Mandatory = $true)][string]$PairToken,
    [string]$DpfUrl = "https://app.3dprintforce.com"
  )
  $ErrorActionPreference = "Stop"
  $repo  = "Sam-3DPF/printforce-link"
  $root  = Join-Path $env:LOCALAPPDATA "PrintForceLink"
  $asset = "printforce-link-windows-x86_64.zip"
  $base  = "https://github.com/$repo/releases/latest/download"
  $taskName = "PrintForceLink"

  Write-Host "==> Downloading PrintForce Link..." -ForegroundColor Cyan
  $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("pfl-" + [System.Guid]::NewGuid())
  New-Item -ItemType Directory -Path $tmp | Out-Null
  try {
    Invoke-WebRequest "$base/$asset"     -OutFile (Join-Path $tmp $asset)
    Invoke-WebRequest "$base/SHA256SUMS" -OutFile (Join-Path $tmp "SHA256SUMS")

    Write-Host "==> Verifying the download..." -ForegroundColor Cyan
    $expected = (Select-String -Path (Join-Path $tmp "SHA256SUMS") -Pattern ([regex]::Escape($asset)) |
                 Select-Object -First 1).Line.Split(" ")[0]
    $actual = (Get-FileHash (Join-Path $tmp $asset) -Algorithm SHA256).Hash
    if ($expected.ToLower() -ne $actual.ToLower()) { throw "The download didn't verify. Please run the command again." }

    Write-Host "==> Installing..." -ForegroundColor Cyan
    if (Test-Path (Join-Path $root "printforce-link")) { Remove-Item (Join-Path $root "printforce-link") -Recurse -Force }
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    Expand-Archive -Path (Join-Path $tmp $asset) -DestinationPath $root -Force   # -> $root\printforce-link\

    $configPath = Join-Path $root "config.toml"
    if (-not (Test-Path $configPath)) {
      "dpf_base_url = `"$DpfUrl`"" | Set-Content -Path $configPath -Encoding UTF8
    }

    Write-Host "==> Setting it to start automatically..." -ForegroundColor Cyan
    $exe = Join-Path $root "printforce-link\printforce-link.exe"
    # Per-user logon task (no admin/UAC), with restart-on-failure to approximate KeepAlive.
    $action  = New-ScheduledTaskAction  -Execute $exe -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    # The one-time pair code reaches the agent via an env var on the task (inert after first run).
    [Environment]::SetEnvironmentVariable("BRIDGE_PAIR_TOKEN", $PairToken, "User")
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName

    Write-Host "==> Done! PrintForce Link is installed and connecting." -ForegroundColor Green
    Write-Host "    Go back to 3D Print Force — the PrintForce Link card turns green, then add your printers."
  }
  finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
  }
}
