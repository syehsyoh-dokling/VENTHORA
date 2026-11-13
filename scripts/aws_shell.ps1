<#
.SYNOPSIS
  Open a shell on the VENTHORA AWS server (venthora-app) from PowerShell, through SSM Session Manager.
  The server has no open ports; no SSH key is needed. Type `exit` to leave.

.EXAMPLE
  .\scripts\aws_shell.ps1
  .\scripts\aws_shell.ps1 -Logs      # follow the platform and gateway logs instead (Ctrl+C to stop)
#>
param([switch]$Logs)
$Aws = "C:\Program Files\Amazon\AWSCLIV2\aws.exe"
$Instance = "i-0cd93eda104c04be7"
if ($Logs) {
    & $Aws ssm start-session --target $Instance --region us-east-1 --document-name AWS-StartInteractiveCommand `
        --parameters 'command=["sudo journalctl -u venthora-platform -u venthora-gateway -f -n 50"]'
} else {
    & $Aws ssm start-session --target $Instance --region us-east-1
}
