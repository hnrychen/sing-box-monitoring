#!/usr/bin/env python3
"""Fetch the pinned, verified optional Grafana flag font for a Docker build."""
import hashlib
import pathlib
import urllib.request

URL = 'https://cdn.jsdelivr.net/npm/country-flag-emoji-polyfill@0.1.8/dist/TwemojiCountryFlags.woff2'
SHA256 = '9f04f14429bb6a9f415c7a4dd902a918d7e81a4f7526c415496fdb063954e3b8'

if __name__ == '__main__':
    path = pathlib.Path(__file__).with_name('TwemojiCountryFlags.woff2')
    with urllib.request.urlopen(URL, timeout=20) as response:
        contents = response.read(200000)
    if contents[:4] != b'wOF2' or hashlib.sha256(contents).hexdigest() != SHA256:
        raise ValueError('font integrity check failed')
    path.write_bytes(contents)
    print(f'{path.name}: {len(contents)} bytes, SHA-256 verified; see FLAG-FONT-LICENSE.md')
