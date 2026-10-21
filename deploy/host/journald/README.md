# journald retention (documentation copy, owner-installed)

`zz-trex-retention.conf` raises the journal from the vendor cap (Pop!_OS
`/usr/lib/systemd/journald.conf.d/pop.conf`: `SystemMaxUse=1000M`, which kept
less than a day on this host) to 6G / 45 days, keeping 20G of the disk free.
Drop-ins are read in filename order and the last setting wins, so the `zz-`
prefix sorts after `pop.conf`. Nothing in this repo installs it; the owner runs:

    sudo install -m 0644 deploy/host/journald/zz-trex-retention.conf /etc/systemd/journald.conf.d/
    sudo systemctl restart systemd-journald

Verify with `systemd-analyze cat-config systemd/journald.conf` and
`journalctl --disk-usage`. Root had about 47G free on 2026-10-21; if free
space falls toward 20G, `SystemKeepFree` stops the journal growing before it
reaches 6G. Durable ops verdicts (for example
`~/.local/state/trex/ops-verdicts/desk-digest-watch/`) do not depend on this.
