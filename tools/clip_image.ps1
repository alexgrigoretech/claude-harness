param(
    [string]$Out,
    [switch]$NoClipboard
)

if ([Threading.Thread]::CurrentThread.GetApartmentState() -ne 'STA') {
    $staArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-STA', '-File', $PSCommandPath)
    if ($PSBoundParameters.ContainsKey('Out')) {
        $staArgs += @('-Out', $Out)
    }
    if ($NoClipboard) {
        $staArgs += '-NoClipboard'
    }
    & powershell.exe @staArgs @args
    exit $LASTEXITCODE
}

$ErrorActionPreference = 'Stop'
try {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    $pasteFolder = Join-Path $env:LOCALAPPDATA 'Temp\claude\paste'
    $cutoff = (Get-Date).AddDays(-7)
    Get-ChildItem -LiteralPath $pasteFolder -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        Remove-Item -Force -ErrorAction SilentlyContinue

    $path = $null
    if ([System.Windows.Forms.Clipboard]::ContainsImage()) {
        if ($Out) {
            $path = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Out)
        } else {
            $path = Join-Path $pasteFolder ((Get-Date -Format 'yyyyMMdd-HHmmss') + '.png')
        }
        [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($path)) | Out-Null
        $image = [System.Windows.Forms.Clipboard]::GetImage()
        try {
            $image.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
        } finally {
            if ($null -ne $image) {
                $image.Dispose()
            }
        }
    } elseif ([System.Windows.Forms.Clipboard]::ContainsFileDropList()) {
        $files = [System.Windows.Forms.Clipboard]::GetFileDropList()
        if ($files.Count -gt 0 -and [System.IO.Path]::GetExtension($files[0]) -match '^\.(png|jpg|jpeg|gif|webp|bmp)$') {
            $path = $files[0]
        }
    }

    if (-not $path) {
        [Console]::Error.WriteLine('clip_image: no image on the clipboard')
        exit 1
    }
    if (-not $NoClipboard) {
        [System.Windows.Forms.Clipboard]::SetText($path)
    }
    [Console]::Out.WriteLine($path)
    exit 0
} catch {
    [Console]::Error.WriteLine('clip_image: ' + $_.Exception.Message)
    exit 1
}
