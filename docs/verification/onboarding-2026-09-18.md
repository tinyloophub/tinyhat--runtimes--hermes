# Hermes onboarding verification — 18 September 2026

A disposable Linux Computer was created without a Hat. Its existing development
bot was connected through a real Telegram Desktop Mini App with the paste-link
option. The Mini App closed automatically. The original prompt was replaced by
an illustration, then its caption became connected after runtime acknowledgement.

![Real native Telegram connection confirmation](onboarding-connected.png)

Live setup exposed two failures: email-dependent initial model access and a
configuration retry corrupting Hermes' valid indentless YAML lists. Both now
have regression tests. Fresh model initialization uses the Computer's existing
platform-provided key; a selected owner model/provider is preserved.

The final runtime a78a73d and plugin a7b8db2 source overlays were tested on that
same disposable Computer. After the official restart command and public `/new`
conversation reset, a real “Hi” produced a single friendly greeting asking how
it could help. It did not mention missing Hats, funding, a profile interview, or
unverified channel readiness. This verifies the fixes together; it is not a
claim that these branches have been released or baked into a production image.

The first installation created SOUL.md before Hermes ran. After Hermes CLI
configuration inspection, its permissions/content and defaults remained:

```text
600 SOUL.md
# Your Tinyhat assistant
You are a personal assistant on the user's Tinyhat Computer. Be warm, direct,
curious, and useful. Talk like a thoughtful person, not a product tour.

hermes config get streaming:
  enabled: true
  transport: auto
hermes config get onboarding.profile_build:
  off
```

The final profile-build setting and expanded voice were applied only to this
disposable fixture when re-exercising first contact. Production updates preserve
existing owner files; fresh defaults require a new verified image and replacement
of unassigned warm Computers only.

705 runtime tests passed on Python 3.13; repository validation and compilation
passed. Camera QR scanning and a signed release/image rollout were not tested.

The review follow-up additionally exercises eleven empty/null configuration
shapes through repeated setup. The final parser was also copied to the disposable
Linux Computer and used twice against its live public configuration: YAML stayed
valid, and its selected model and streaming settings were unchanged.
