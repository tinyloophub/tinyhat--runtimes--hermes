# Readable native channel messages

Before: Codex and Claude Code received the entire transport event as their
visible user message. Telegram JSON and unrelated metadata filled the chat.

After: the owner's text appears first. Reply excerpts, attachment paths, source
and a reference to Tinyhat's local original event follow it. The full event is
still supplied to routing and scoped tools. No session or message data is moved
to the platform server, and existing native history is not rewritten.

Synthetic example:

```text
How are you? 👋

— Telegram · message 6

Original update (local): /private/inbox/channels.sqlite3
Table: events · ID: "telegram:42"
```

Verified with the official Codex App Server: a new native session received the
exact formatted input in `thread/read` and replied `READABLE-OK`. This isolated
check used GPT-5.5 because the machine's standalone Codex 0.142 CLI rejected
GPT-6 Astra as requiring a newer CLI. It did not modify the default model or the
production Computer. No live Telegram/Slack delivery or Claude Code response is
claimed by this check.

Regression coverage includes text and Unicode, reply/thread context, captionless
images, voice transcripts, attachment failures, email, metadata line boundaries,
and the worker retaining the original local event and native image references.
Full runtime suite: 665 tests passed. The delivery/resilience changes in PR #185
are separate from this presentation-only change.
