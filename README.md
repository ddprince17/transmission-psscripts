# transmission-psscripts

`Remove-TransmissionPublicTorrents.ps1` removes completed public torrents and their local data by default.

Requires PowerShell 7+ and Transmission 4.1.0 or newer. Requests use the [Transmission JSON-RPC 2.0 API](https://github.com/transmission/transmission/blob/main/docs/rpc-spec.md).

```powershell
./Remove-TransmissionPublicTorrents.ps1 -TransmissionHost localhost -TransmissionPort 9091 -TransmissionScheme http
```

The script prompts for the RPC username and password. HTTP 401 means authentication failed; HTTP 409 supplies a session ID and is retried automatically, including when the session ID expires.

Use `-RemovePrivateOlderThan30Days` to also remove completed private torrents whose `added_date` is older than 30 days.

Run the regression tests against a local mock RPC server with Python 3 and PowerShell 7:

```sh
python3 -B -m unittest discover -s tests -v
```
