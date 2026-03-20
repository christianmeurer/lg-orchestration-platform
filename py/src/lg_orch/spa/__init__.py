# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Standalone SPA served from the RemoteAPI.

The SPA provides a live-streaming operator console backed by Server-Sent Events.
Files in this package are served under ``/app`` by the ThreadingHTTPServer in
``remote_api.py``.

Architecture
------------
* ``index.html`` — fully self-contained (inline ``<style>`` + ``<script>``); works
  when opened directly from disk with no server.
* ``style.css`` — human-readable unminified copy of the same styles.
* ``main.js`` — human-readable commented copy of the same JS logic.
* ``router.py`` — :func:`create_spa_router` wires the above into the stdlib server.
"""
