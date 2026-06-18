# RPC Key Rotation Required

Ticket: T-161

Rotation is an external provider action and was not performed in this repo branch. Any RPC provider token previously exposed through examples, docs, chat transcripts, local logs, or live host files must be considered compromised.

Required rotation artifact:

- provider name and dashboard account
- old token identifier or last four visible characters only
- new token identifier or last four visible characters only
- exact UTC rotation timestamp
- operator id
- affected bot or venue
- verification note confirming the new value exists only in the secret store or local runtime environment

Do not commit provider tokens, full credential-bearing RPC URLs, wallet paths, local environment files, or screenshots containing secrets.
