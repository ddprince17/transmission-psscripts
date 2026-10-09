#Requires -Version 7.0

param (
    [Parameter(Mandatory = $true)]
    [string]$TransmissionHost = "localhost",

    [Parameter(Mandatory = $true)]
    [int]$TransmissionPort = 9091,

    [Parameter(Mandatory = $true)]
    [ValidateSet("http", "https")]
    [string]$TransmissionScheme = "http",

    [switch]$RemovePrivateOlderThan30Days
)

Write-Host "Transmission server details:"
Write-Host "Host: $TransmissionHost"
Write-Host "Port: $TransmissionPort"

# Prompt for Transmission credentials
$TransmissionUser = Read-Host "Enter your Transmission username"
$TransmissionPassword = Read-Host -AsSecureString "Enter your Transmission password"
$credential = [System.Net.NetworkCredential]::new($TransmissionUser, $TransmissionPassword)

# Create the basic authentication header
$BasicAuth = "Basic " + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("$($credential.UserName):$($credential.Password)"))
$TransmissionRpcUri = "${TransmissionScheme}://${TransmissionHost}:${TransmissionPort}/transmission/rpc"
$script:sessionId = $null

function Invoke-TransmissionRpc {
    param (
        [Parameter(Mandatory = $true)]
        [string]$Method,

        [Parameter(Mandatory = $true)]
        [hashtable]$Parameters
    )

    $requestBody = @{
        jsonrpc = "2.0"
        method  = $Method
        params  = $Parameters
        id      = 1
    } | ConvertTo-Json -Depth 10

    # Retry the same request once when Transmission supplies a new CSRF token.
    for ($attempt = 0; $attempt -lt 2; $attempt++) {
        $headers = @{ Authorization = $BasicAuth }
        if ($script:sessionId) {
            $headers["X-Transmission-Session-Id"] = $script:sessionId
        }

        try {
            $response = Invoke-RestMethod -Uri $TransmissionRpcUri -Method Post -Headers $headers `
                -ContentType "application/json; charset=utf-8" -Body $requestBody -ErrorAction Stop
        }
        catch {
            $httpResponse = $_.Exception.Response
            if ($httpResponse.StatusCode -eq [System.Net.HttpStatusCode]::Unauthorized) {
                throw "Transmission RPC authentication failed (HTTP 401). Check the RPC username and password."
            }

            if ($httpResponse.StatusCode -eq [System.Net.HttpStatusCode]::Conflict -and $attempt -eq 0) {
                if (-not $httpResponse.Headers.Contains("X-Transmission-Session-Id")) {
                    throw "Transmission returned HTTP 409 without an X-Transmission-Session-Id header."
                }

                $script:sessionId = @($httpResponse.Headers.GetValues("X-Transmission-Session-Id"))[0]
                continue
            }

            throw
        }

        if ($response.error) {
            throw "Transmission RPC '$Method' failed (code $($response.error.code)): $($response.error.message)"
        }

        if ($response.jsonrpc -ne "2.0") {
            throw "Transmission did not return a JSON-RPC 2.0 response. This script requires Transmission 4.1.0 or newer."
        }

        return $response.result
    }
}

# Function to retrieve torrent information
function Get-TransmissionTorrents {
    $torrents = Invoke-TransmissionRpc -Method "torrent_get" -Parameters @{
        fields = @("id", "name", "percent_done", "is_private", "added_date")
    }

    return $torrents.torrents
}

# Get all torrents
$allTorrents = @(Get-TransmissionTorrents)
$privateDeletionCutoff = (Get-Date).AddDays(-30)

# Initialize counters
$deletedCount = 0
$remainingCount = 0

# Iterate through torrents
foreach ($torrent in $allTorrents) {
    if ($torrent.percent_done -lt 1) {
        Write-Host "Torrent $($torrent.name) (ID $($torrent.id)) is not completed yet, thus ignored."
    }
    elseif ($torrent.is_private) {
        $addedDate = $null

        if ($torrent.added_date -gt 0) {
            $addedDate = [DateTimeOffset]::FromUnixTimeSeconds($torrent.added_date).LocalDateTime
        }

        if ($RemovePrivateOlderThan30Days -and $addedDate -and $addedDate -lt $privateDeletionCutoff) {
            Write-Host "Torrent $($torrent.name) (ID $($torrent.id)) is private and older than 30 days. Deleting..."

            Invoke-TransmissionRpc -Method "torrent_remove" -Parameters @{
                ids               = @($torrent.id)
                delete_local_data = $true
            } | Out-Null

            $deletedCount++
        }
        else {
            if ($RemovePrivateOlderThan30Days -and $addedDate) {
                Write-Host "Torrent $($torrent.name) (ID $($torrent.id)) is private and was added on $($addedDate.ToString("yyyy-MM-dd")). Keeping it."
            }
            else {
                Write-Host "Torrent $($torrent.name) (ID $($torrent.id)) is private."
            }
        }
    }
    else {
        Write-Host "Torrent $($torrent.name) (ID $($torrent.id)) is public. Deleting..."

        Invoke-TransmissionRpc -Method "torrent_remove" -Parameters @{
            ids               = @($torrent.id)
            delete_local_data = $true
        } | Out-Null

        $deletedCount++
    }
}

# Calculate remaining torrents
$remainingCount = $allTorrents.Count - $deletedCount

# Output results
Write-Host "Total torrents checked: $($allTorrents.Count)"
Write-Host "Torrents deleted: $deletedCount"
Write-Host "Torrents remaining: $remainingCount"
