"""Best-effort Windows system notification; no user text is executed as a command."""
import base64
import os
import subprocess
from pathlib import Path

# Fixed, generic text deliberately avoids exposing case names on the desktop.
SCRIPT=r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
try {
  $n.Icon = [System.Drawing.SystemIcons]::Information
  $n.Text = 'Юрий — напоминания'
  $n.Visible = $true
  $n.BalloonTipTitle = 'Юрий — центр напоминаний'
  $n.BalloonTipText = 'Есть новые напоминания. Откройте Юрий → Напоминания. Показ и прочтение этого уведомления не подтверждены.'
  $n.ShowBalloonTip(10000)
  $until = [DateTime]::UtcNow.AddSeconds(12)
  while ([DateTime]::UtcNow -lt $until) {
    [System.Windows.Forms.Application]::DoEvents()
    Start-Sleep -Milliseconds 100
  }
} finally { $n.Visible = $false; $n.Dispose() }
'''


def notify_windows(count):
    if os.name!='nt':return 'unavailable'
    exe=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe'
    if not exe.is_file():return 'unavailable'
    encoded=base64.b64encode(SCRIPT.encode('utf-16le')).decode('ascii')
    try:
        result=subprocess.run([str(exe),'-NoProfile','-NonInteractive','-STA','-EncodedCommand',encoded],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return 'requested-not-confirmed' if result.returncode==0 else 'failed'
    except (OSError,subprocess.TimeoutExpired):return 'failed'
