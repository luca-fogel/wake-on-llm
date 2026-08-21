# Contributing

Only the Mac half lives here. If you are building the Pi half, the HTTP contract
in the README's API table is the whole interface — no code from this repo is
needed on the other side.

## Before changing anything, two rules earned the hard way

- **Never `kickstart` a LaunchAgent that is not loaded.** Editing
  `com.localai.server.plist` and restarting that way once left LocalAI fully
  unloaded and cost hours of blind remote recovery. Every service this repo owns
  is installed bootout → bootstrap → verify.
- **Never make `/off` stop the API server.** Stopping it is `launchctl bootout`,
  and if that is the last thing you do from a phone, nothing on the tailnet can
  start it again — you have to be physically at the Mac. It stays a separate
  call, behind a confirmation.

## Things that are configuration, not code

Anything that identifies a machine or a network: the tailnet hostname
(`REMOTE_URL`), the bind address, the model name, the tokens. A tailnet name in a
commit is a leak, and a compiled-in hostname makes the app useless to anyone
else. They all live in `~/.config/qwen-control/config.env`, written by
`install.sh` and `chmod 600`.

Do not commit `.agent-token`. It is gitignored; keep it that way.

## Checking a change

```sh
./install.sh              # idempotent; re-run to upgrade
./verify.sh               # post-install self-check, needs no pasted credentials
```

`verify.sh` is the honest one: it checks the token's *shape* without printing it,
and it reports the completion tokens that actually arrived rather than the ones
requested — a `max_tokens=20` request that stops after two tokens otherwise looks
impossibly fast.

## Sign your work (DCO)

Add a `Signed-off-by` line to each commit — `git commit -s` does it for you:

```
Signed-off-by: Your Name <your.email@example.com>
```

That line is the [Developer Certificate of Origin](https://developercertificate.org/):
you are stating that you wrote the patch, or otherwise have the right to submit
it under this project's licence (MIT). It is one line, not a copyright
assignment — you keep the copyright in your own work.
