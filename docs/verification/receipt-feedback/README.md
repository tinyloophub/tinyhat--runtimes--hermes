# Immediate channel receipt verification

The receiver starts bounded native activity after durable ingest, before model
routing or media processing. It does not generate a chat reply.

## Observed provider behavior

A local accepted-event simulation used the changed runtime and response skill,
with real provider credentials kept in memory. No model or worker was started.
Existing receiver ownership and deployed Computers were not changed.

- Telegram accepted `sendChatAction` 0.157 seconds after local ingest. Telegram
  Desktop visibly showed typing. Saving the quiet preference stopped renewal.
- Slack accepted `agents.sessions.setStatus` 0.152 seconds after local ingest.
  Slack Web visibly showed the working indicator. Cleanup returned the agent to
  `active`. The test used a harmless test message as the thread anchor.

![Telegram typing after receipt](telegram-typing.png)

![Slack working after receipt](slack-working.png)

Screenshots contain only the development bot identity and test content.
This proves live outbound receipt feedback, not a new Computer provisioning or
full provider-to-model end-to-end run.

## Regression checks

```sh
python -m unittest discover -s tests -p 'test_channel*.py'
python -m unittest discover -s tests -v
python scripts/check_dev_skills.py
python scripts/check_repo_basics.py
python -m compileall -q hermes_runtime scripts
```

Focused cases cover queued voice messages, duplicates, unauthorized Telegram
updates, stalled provider feedback, Slack thread scoping and fallback, cancelled
status writes, repeated cancellation during cleanup, Slack Stop during receipt, local quiet preferences, and router preference restrictions.
The resilience suite also passes on Linux with Python 3.11.
