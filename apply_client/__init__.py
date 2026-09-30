"""Job Agent apply client — runs on Windows, fills job applications in Chrome.

Polls the Jetson's apply queue, opens each application in a persistent Chrome
profile, fills what it confidently can, and hands the form to the human. It
NEVER clicks Submit — you do.

Standalone package: it must not import anything from `app/`.
"""
