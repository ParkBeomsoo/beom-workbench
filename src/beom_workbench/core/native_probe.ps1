# Native window probe for beom_workbench.core.native (Windows PowerShell 5.1+, no extra installs).
# Finds the top-level window of a process tree, runs simple actions through UI Automation,
# collects control names/values, and saves a window screenshot (PrintWindow).
# Prints one JSON line: {ok, title, texts, controls, errors, out}. Keep this file ASCII-only (5.1 reads it as ANSI).
param([Parameter(Mandatory = $true)][string]$SpecFile)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class OssWin {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int ht, bool repaint);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int attr, out RECT r, int size);
}
"@
[void][OssWin]::SetProcessDPIAware()

$spec = Get-Content -Raw -Encoding UTF8 -LiteralPath $SpecFile | ConvertFrom-Json
$errors = New-Object System.Collections.ArrayList
$A = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$True_ = [System.Windows.Automation.Condition]::TrueCondition

function Get-Tree([int]$root) {
  $all = @(Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId)
  $ids = New-Object System.Collections.Generic.HashSet[int]
  [void]$ids.Add($root)
  $grew = $true
  while ($grew) {
    $grew = $false
    foreach ($p in $all) { if ($ids.Contains([int]$p.ParentProcessId) -and $ids.Add([int]$p.ProcessId)) { $grew = $true } }
  }
  return $ids
}

function Find-Window {
  $ids = Get-Tree ([int]$spec.pid)
  $best = $null; $bestArea = 0
  foreach ($w in $A::RootElement.FindAll($TS::Children, $True_)) {
    try {
      $c = $w.Current
      if (-not $ids.Contains([int]$c.ProcessId) -or $c.IsOffscreen) { continue }
      if ($spec.title -and ($c.Name -notmatch $spec.title)) { continue }
      $r = $c.BoundingRectangle
      $area = $r.Width * $r.Height
      if ($area -gt $bestArea) { $best = $w; $bestArea = $area }
    } catch { }
  }
  return $best
}

function Type-Of($el) { return ($el.Current.ControlType.ProgrammaticName -replace '^ControlType\.', '') }

function Find-Target($win, [string]$sel) {
  $kind, $name = $sel -split ':', 2
  if ($null -eq $name) { $name = '' }
  foreach ($el in $win.FindAll($TS::Descendants, $True_)) {
    try {
      $c = $el.Current
      if ((Type-Of $el) -ne $kind.Trim()) { continue }
      if ($name -eq '' -or $c.Name -like "*$($name.Trim())*" -or $c.AutomationId -eq $name.Trim()) { return $el }
    } catch { }
  }
  return $null
}

$deadline = (Get-Date).AddMilliseconds([int]$spec.timeout_ms)
$win = $null
while ((Get-Date) -lt $deadline) {
  $win = Find-Window
  if ($win) { break }
  if (-not (Get-Process -Id ([int]$spec.pid) -ErrorAction SilentlyContinue) -and (Get-Tree ([int]$spec.pid)).Count -le 1) { break }
  Start-Sleep -Milliseconds 300
}
if (-not $win) {
  [void]$errors.Add('app window not found (timeout or app exited)')
  Write-Output (@{ ok = $false; errors = @($errors) } | ConvertTo-Json -Compress -Depth 4)
  exit 0
}
$hwnd = [IntPtr]$win.Current.NativeWindowHandle
[void][OssWin]::ShowWindow($hwnd, 9)
if ([int]$spec.width -gt 0 -and [int]$spec.height -gt 0) {
  $r = New-Object OssWin+RECT
  [void][OssWin]::GetWindowRect($hwnd, [ref]$r)
  [void][OssWin]::MoveWindow($hwnd, [Math]::Max(0, $r.L), [Math]::Max(0, $r.T), [int]$spec.width, [int]$spec.height, $true)
}
[void][OssWin]::SetForegroundWindow($hwnd)
Start-Sleep -Milliseconds ([int]$spec.wait_ms)

foreach ($act in @($spec.actions)) {
  if ($null -eq $act) { continue }
  try {
    if ($act.wait_ms) { Start-Sleep -Milliseconds ([int]$act.wait_ms); continue }
    $sel = if ($act.set) { $act.set } else { $act.click }
    $el = Find-Target $win $sel
    if (-not $el) { [void]$errors.Add("action target not found: $sel"); continue }
    if ($act.set) {
      $vp = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
      $vp.SetValue([string]$act.value)
    } else {
      $done = $false
      foreach ($pat in @([System.Windows.Automation.InvokePattern]::Pattern, [System.Windows.Automation.TogglePattern]::Pattern,
                         [System.Windows.Automation.SelectionItemPattern]::Pattern)) {
        $o = $null
        if ($el.TryGetCurrentPattern($pat, [ref]$o)) {
          if ($o -is [System.Windows.Automation.InvokePattern]) { $o.Invoke() }
          elseif ($o -is [System.Windows.Automation.TogglePattern]) { $o.Toggle() }
          else { $o.Select() }
          $done = $true; break
        }
      }
      if (-not $done) { [void]$errors.Add("cannot click: $sel") }
    }
    Start-Sleep -Milliseconds 250
  } catch { [void]$errors.Add("action failed: $($_.Exception.Message)") }
}
Start-Sleep -Milliseconds 300

$texts = New-Object System.Collections.ArrayList
$controls = New-Object System.Collections.ArrayList
[void]$texts.Add($win.Current.Name)
$n = 0
foreach ($el in $win.FindAll($TS::Descendants, $True_)) {
  if (++$n -gt 3000) { break }
  try {
    $c = $el.Current
    $val = $null
    $o = $null
    if ($el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$o)) { $val = $o.Current.Value }
    if ($c.Name) { [void]$texts.Add($c.Name) }
    if ($val) { [void]$texts.Add($val) }
    [void]$controls.Add(@{ type = (Type-Of $el); name = $c.Name; id = $c.AutomationId })
  } catch { }
}

$saved = $null
if ($spec.out) {
  try {
    $r = New-Object OssWin+RECT
    [void][OssWin]::GetWindowRect($hwnd, [ref]$r)
    $w = [Math]::Max(1, $r.R - $r.L); $h = [Math]::Max(1, $r.B - $r.T)
    $bmp = New-Object System.Drawing.Bitmap $w, $h
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $dc = $g.GetHdc()
    $ok = [OssWin]::PrintWindow($hwnd, $dc, 2)
    $g.ReleaseHdc($dc)
    if (-not $ok) { $g.CopyFromScreen($r.L, $r.T, 0, 0, $bmp.Size) }
    $g.Dispose()
    # crop the invisible resize border: visible frame = DWMWA_EXTENDED_FRAME_BOUNDS (9)
    $f = New-Object OssWin+RECT
    if ([OssWin]::DwmGetWindowAttribute($hwnd, 9, [ref]$f, 16) -eq 0 -and ($f.R - $f.L) -gt 0 -and ($f.B - $f.T) -gt 0) {
      $cx = [Math]::Max(0, $f.L - $r.L); $cy = [Math]::Max(0, $f.T - $r.T)
      $cw = [Math]::Min($w - $cx, $f.R - $f.L); $ch = [Math]::Min($h - $cy, $f.B - $f.T)
      if ($cw -gt 0 -and $ch -gt 0 -and ($cw -lt $w -or $ch -lt $h)) {
        $crop = $bmp.Clone((New-Object System.Drawing.Rectangle $cx, $cy, $cw, $ch), $bmp.PixelFormat)
        $bmp.Dispose(); $bmp = $crop
      }
    }
    $bmp.Save([string]$spec.out, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    $saved = [string]$spec.out
  } catch { [void]$errors.Add("screenshot failed: $($_.Exception.Message)") }
}
Write-Output (@{ ok = $true; title = $win.Current.Name; texts = @($texts); controls = @($controls); errors = @($errors); out = $saved } |
  ConvertTo-Json -Compress -Depth 4)
