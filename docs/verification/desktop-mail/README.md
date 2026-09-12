# Desktop Mail verification

The installer was exercised on an Ubuntu AMD64 image using Mozilla's native
Thunderbird package (155.0.1); the Snap placeholder was not used. A Linux desktop
container also opened Thunderbird 140.15 ESR through the `Mail` shortcut using
TLS IMAP 993 and SMTP 465 settings. A disposable real mailbox showed the Tinyhat
welcome message. Account-specific screenshots remain private.

![Mail shortcut on the Linux desktop](desktop-shortcut.png)

The runtime keeps assignment fast: package installation happens during image
construction. Assignment writes the private profile settings and desktop entry;
it does not run apt. Profile and mailbox history survive address changes.

Validation on 2026-09-12:

```text
python -m unittest discover -s tests
Ran 521 tests in 37.096s
OK
```

Compile, repository/skill validators and shell syntax checks also passed.
This is Linux/container verification, not a production image rollout claim.
