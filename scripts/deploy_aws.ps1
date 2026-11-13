<#
.SYNOPSIS
  Deploy changed VENTHORA files to the AWS server (venthora-app) from this laptop, no browser needed.

.DESCRIPTION
  1. Packs the files (default: every file under src/ that git sees as changed or new) into a .tgz.
  2. Uploads it to the private deploy bucket.
  3. Through SSM (the server has no open ports): backs up the files it replaces, unpacks, fixes ownership,
     restarts venthora-platform and venthora-gateway, and reports whether they are active.
  4. Removes the upload from the bucket.

  Needs AWS CLI v2 signed in (`aws login`) with access to account 852168982659.

.EXAMPLE
  .\scripts\deploy_aws.ps1                       # deploy every changed file under src/
  .\scripts\deploy_aws.ps1 -Files src/venthora/api.py,src/venthora/dashboard/index.html
  .\scripts\deploy_aws.ps1 -NoRestart            # copy only
#>
param(
    [string[]]$Files,
    [switch]$NoRestart
)
$ErrorActionPreference = "Stop"
$Aws = "C:\Program Files\Amazon\AWSCLIV2\aws.exe"
$Instance = "i-0cd93eda104c04be7"
$Bucket = "venthora-deploy-852168982659"
$Region = "us-east-1"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

if (-not $Files) {
    $changed = @(git diff --name-only HEAD -- src) + @(git ls-files --others --exclude-standard -- src)
    $Files = $changed | Where-Object { $_ -and (Test-Path $_) } | Sort-Object -Unique
}
if (-not $Files) { Write-Host "Nothing to deploy: no changed files under src/."; exit 0 }
Write-Host "Deploying $($Files.Count) file(s):"; $Files | ForEach-Object { Write-Host "  $_" }

$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmss")
$name = "patch-$stamp.tgz"
$tgz = Join-Path $env:TEMP $name
tar -czf $tgz @Files
& $Aws s3 cp $tgz "s3://$Bucket/$name" --sse AES256 --only-show-errors --region $Region
Remove-Item $tgz

$list = ($Files | ForEach-Object { $_ -replace '\\', '/' }) -join ' '
$cmds = @(
    "cd /opt/venthora && tar -czf /opt/venthora/backup-$stamp.tgz --ignore-failed-read $list 2>/dev/null; echo backup /opt/venthora/backup-$stamp.tgz",
    "/snap/bin/aws s3 cp s3://$Bucket/$name /tmp/$name --only-show-errors",
    "tar -xzf /tmp/$name -C /opt/venthora && rm -f /tmp/$name",
    "chown -R venthora:venthora /opt/venthora/src"
)
if (-not $NoRestart) {
    $cmds += @("systemctl restart venthora-platform venthora-gateway", "sleep 20", "systemctl is-active venthora-platform venthora-gateway venthora-tunnel")
}
$json = Join-Path $env:TEMP "ssm-$stamp.json"
@{ InstanceIds = @($Instance); DocumentName = "AWS-RunShellScript"; TimeoutSeconds = 300;
   Parameters = @{ commands = $cmds } } | ConvertTo-Json -Depth 4 | Set-Content $json -Encoding ascii
$cid = & $Aws ssm send-command --cli-input-json "file://$json" --region $Region --query Command.CommandId --output text
Remove-Item $json
Write-Host "SSM command $cid sent; waiting..."
do {
    Start-Sleep -Seconds 5
    $status = & $Aws ssm get-command-invocation --command-id $cid --instance-id $Instance --region $Region --query Status --output text 2>$null
} while ($status -in @("Pending", "InProgress", "Delayed", $null, ""))
& $Aws ssm get-command-invocation --command-id $cid --instance-id $Instance --region $Region --query "[Status,StandardOutputContent,StandardErrorContent]" --output text
& $Aws s3 rm "s3://$Bucket/$name" --only-show-errors --region $Region
if ($status -ne "Success") { Write-Host "Deploy finished with status $status"; exit 1 }
Write-Host "Deployed. Check https://app-venthora.muwahid.org"
