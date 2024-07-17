param (
    [Parameter(Mandatory = $true)]
    [string]$TransmissionHost = "localhost",

    [Parameter(Mandatory = $true)]
    [int]$TransmissionPort = 9091,

    [Parameter(Mandatory = $true)]
    [ValidateSet("http", "https")]
    [string]$TransmissionScheme = "http"
)

Write-Host "Transmission server details:"
Write-Host "Host: $TransmissionHost"
Write-Host "Port: $TransmissionPort"

# Prompt for Transmission credentials
$TransmissionUser = Read-Host "Enter your Transmission username"
$TransmissionPassword = Read-Host -AsSecureString "Enter your Transmission password"
$TransmissionPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto([System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($TransmissionPassword))

# Create the basic authentication header
$BasicAuth = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${TransmissionUser}:${TransmissionPassword}"))

# Function to retrieve the current session ID
function Get-TransmissionSessionId {
    $responseHeaders = @{}
    
    # Variable intended not to be used. Do not remove otherwise we can't extract the information of the headers.
    $response = Invoke-RestMethod -ResponseHeadersVariable "responseHeaders" -SkipHttpErrorCheck -Uri ("${TransmissionScheme}://${TransmissionHost}:${TransmissionPort}/transmission/rpc") `
        -Method Get -Headers @{
        Authorization = $BasicAuth
    }

    return $responseHeaders["X-Transmission-Session-Id"]
}

# Get the current session ID
$sessionId = Get-TransmissionSessionId
Write-Host "Session ID: $sessionId"

# Function to retrieve torrent information
function Get-TransmissionTorrents {
    $requestBody = @{
        method    = "torrent-get"
        arguments = @{
            fields = @("id", "name", "percentDone", "isPrivate")
        }
    } | ConvertTo-Json

    $torrents = Invoke-RestMethod -Uri ("${TransmissionScheme}://${TransmissionHost}:${TransmissionPort}/transmission/rpc") `
        -Method Post -Headers @{
        Authorization               = $BasicAuth
        "X-Transmission-Session-Id" = $sessionId
    } -ContentType "application/json" -Body $requestBody

    return $torrents.arguments.torrents
}

# Get all torrents
$allTorrents = Get-TransmissionTorrents

# Initialize counters
$deletedCount = 0
$remainingCount = 0

# Iterate through torrents
foreach ($torrent in $allTorrents) {
    if ($torrent.percentDone -lt 1) {
        Write-Host "Torrent $($torrent.name) (ID $($torrent.id)) is not completed yet, thus ignored."
    }
    elseif ($torrent.isPrivate) {
        Write-Host "Torrent $($torrent.name) (ID $($torrent.id)) is private."
    }
    else {
        Write-Host "Torrent $($torrent.name) (ID $($torrent.id)) is public. Deleting..."

        $requestBody = @{
            method    = "torrent-remove"
            arguments = @{
                ids                 = @($torrent.id)
                "delete-local-data" = $true
            }
        } | ConvertTo-Json

        Invoke-RestMethod -Uri ("${TransmissionScheme}://${TransmissionHost}:${TransmissionPort}/transmission/rpc") -Method Post -Headers @{
            Authorization               = $BasicAuth
            "X-Transmission-Session-Id" = $sessionId
        } -ContentType "application/json" -Body $requestBody

        $deletedCount++
    }
}

# Calculate remaining torrents
$remainingCount = $allTorrents.Count - $deletedCount

# Output results
Write-Host "Total torrents checked: $($allTorrents.Count)"
Write-Host "Torrents deleted: $deletedCount"
Write-Host "Torrents remaining: $remainingCount"
