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
$Instance = $env:VENTHORA_AWS_INSTANCE   # the server's EC2 instance id; set it in your own shell, never in the repo
if (-not $Instance) { throw "Set VENTHORA_AWS_INSTANCE to the EC2 instance id of the VENTHORA server" }
if ($Logs) {
    & $Aws ssm start-session --target $Instance --region us-east-1 --document-name AWS-StartInteractiveCommand `
        --parameters 'command=["sudo journalctl -u venthora-platform -u venthora-gateway -f -n 50"]'
} else {
    & $Aws ssm start-session --target $Instance --region us-east-1
}
