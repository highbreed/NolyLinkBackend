"""VSCU integration layer.

Talks to the local KRA VSCU application (KRA's vscu-v2.0.21.jar, run by the client on their
own network — default http://127.0.0.1:8088), never directly to etims-api*.kra.go.ke for VSCU
traffic. That local app owns cmcKey/signKey/intrlKey and proxies to real KRA itself; see
docs/kra_specs/README.md for how this was confirmed (decompiling the actual jar with CFR,
2026-09-01) and app/services/kra_adapter.py for the unrelated, OSCU-only gateway this package
deliberately does not share code with (a real VSCU device speaks a different JSON contract at
different paths, not "the OSCU contract at a different host").
"""
