# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## The agent and the printer

### PrintForce Link
The shop-LAN agent that talks to Bambu printers on the local network and reports outbound to 3D PrintForce.

Link dials the printers. 3D PrintForce does not.

### Access code
The printer's LAN password. It is the secret Link uses for both the printer's message session and its file upload.

The cloud couriers an access code once and deletes its copy after Link confirms the code is stored. The copy on the shop machine is what later launches use. Losing that copy means the operator enters the code again.

### Printer
A Bambu machine on the shop LAN, identified by serial. The address can change. The serial does not.

## What a report is saying

### Connection
Whether this report is from the current session. The values are live, stale, and offline.

Stale still carries the last merged reading, and the print status on that report is offline. Offline carries no reading. Connection is not the print.

### Print status
The job state Link reports for a printer, mapped from the firmware's print state.

A cloud send runs when the cloud's desired state is idle, the connection is live, and the firmware state is idle, finished, or failed. Idle is not the only print status that passes. A report that is not live says the printer is offline, and that does not pass. Link's own start follows the firmware state: a finished plate can take the next file without a stop, and a plate that is still printing cannot.

## Sending a print

### Cloud send
The cloud's request that Link put one sliced plate on one Printer and start it.

Link downloads the file, uploads it to the printer's card, and starts it. It retries the upload and the start a limited number of times, then reports the send failed with a reason. An upload failure carries its kind (timeout, network, storage, and so on), which points at Wi-Fi or at the card. A failed send is the cloud's to requeue. Link does not move it to another printer.

## Relationships

- PrintForce Link owns the session to each Printer and stores that Printer's Access code locally.
- A report carries Connection and Print status as separate facts. Dispatch looks at Print status, which stays non-idle when Connection is not live.
