# Email configuration and terminal environment lists

Email setup writes valid YAML using Hermes's PyYAML dependency. Its block
sequences use the same indentation as the list key. The runtime's terminal
environment updater previously recognized only deeper list indentation and
left the original entries behind when replacing the list. Hermes then rejected
the file and could not start the configured email channel.

Consume both supported block-list forms, stop at the next mapping key, and
deduplicate retained environment names. Re-render malformed blocks even when
the name set is unchanged, preserving the surrounding mapping's indentation.
This does not change credential values or an owner's selected model, and
restores force-alias retention for names written as indentless lists.

Verify email configuration followed by terminal-name addition/removal, YAML
parsing, adjacent settings, and repeated updates in
`tests/test_email_onboarding.py`. The same test also covers the mixed list left
by the previous writer. Existing terminal tests cover the original indented form.

For an already affected computer, back up the private config. Updating terminal
environment names with this runtime repairs the malformed list, including when
all names are already registered. Validate the config in the Hermes Python
environment and restart through the public Hermes gateway CLI. Preserve all
model, channel, credential, and delivery-history settings.
