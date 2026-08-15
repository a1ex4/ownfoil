# Client capture harness

The three shop clients are only reachable through their own hardware, and each one
identifies itself by the exact set of headers it sends. Guessing those in a test proves
nothing, so the tests replay traffic recorded from the real clients instead.

This harness serves a fixture shop, walks you through one shop state at a time, and writes
what each client actually sent to `tests/captures/<client>/<scenario>.json`.

## Recording

    python tests/capture/run_capture.py --list
    python tests/capture/run_capture.py --client tinfoil
    python tests/capture/run_capture.py --client sphaira --scenario download

Point the client at the printed url and follow each instruction. Scenarios are independent,
so a botched one can be recaptured on its own with `--scenario`; `--fresh` discards the
workdir when the fixture shop needs rebuilding from scratch.

The shop runs on a config dir, database and library of its own under `.workdir`, so nothing
touches your real install. The library is dummy bytes under realistic names, download target
included: the clients transfer it happily, and what the tests check is the transfer.

## What is recorded

Every request and response: method, path, ordered headers, status, body, and which files the
request counted a download for. Alongside them, the shop settings and accounts in force, so
a capture says what was configured as well as what was sent.

Identifying data is replaced before anything reaches disk: the device id (`Uid`) and the
values derived from it (`Hauth`, `Uauth`), the host and address, and any credentials that
aren't the fixture accounts. Replacements are stable across runs through `.workdir`, so
recapturing a scenario doesn't churn the committed files.

## Using them

Tests replay a capture's headers through the app and assert on what comes back. The captures
carry the request shapes only real clients know; everything else - the other content
filters, host verification over HTTPS, an unknown file id - is varied in the tests on top of
those same headers, and needs no hardware.

`fixture.py` defines the accounts and the library. Both the capture server and the tests
build the shop from it, so a capture recorded here replays identically under pytest - and a
change to the fixture library invalidates the captures, which then have to be re-recorded.

Credentials that aren't fixture accounts survive a capture only as placeholders, so the
tests restore what was typed from the scenario definition before replaying.
