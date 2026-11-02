# Security policy

## Reporting a vulnerability

Do not open a public issue. Email **support@btlabs.uk**.

Include the affected version or commit, what an attacker controls, what happens, and the smallest
reproduction you have.

## Scope

In scope: server-side request forgery through the quick check, URL fetch or probes; the admin
login, session or CSRF handling; the submission and icon upload paths; leaking a client address
(especially over the onion mirror); anything that lets an outsider change what the board shows.

Issues in the Epic Cash node, wallet or Epicbox relay belong to those projects.

## Supported versions

Only the latest commit on `main` receives fixes.
