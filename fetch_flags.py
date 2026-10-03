#!/usr/bin/env python3
"""Build-time only: bundle MIT flag-icons SVGs for offline Grafana value mappings."""
import base64
import io
import json
import pathlib
import re
import tarfile
import urllib.request

REVISION = '086f7e97d657358203916dbe84f61c2bccaa81eb'


if __name__ == '__main__':
    with urllib.request.urlopen('https://api.github.com/repos/lipis/flag-icons/tarball/' + REVISION, timeout=30) as response:
        blob = response.read(8 * 1024 * 1024 + 1)
    if len(blob) > 8 * 1024 * 1024:
        raise ValueError('flag archive exceeds budget')
    flags, license_text = {}, None
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as archive:
        for member in archive:
            if member.name.endswith('/LICENSE') and member.size < 10000:
                license_text = archive.extractfile(member).read().decode()
            match = re.search(r'/flags/4x3/([a-z]{2})\.svg$', member.name)
            if not match:
                continue
            if member.size > 2 * 1024 * 1024:
                raise ValueError('flag exceeds budget')
            svg = archive.extractfile(member).read()
            if re.search(rb'<script|<!DOCTYPE|(?:xlink:)?href\s*=\s*["\'](?!#)', svg, re.I):
                raise ValueError('flag must be self-contained')
            flags[match[1].upper()] = 'data:image/svg+xml;base64,' + base64.b64encode(svg).decode()
    if len(flags) < 240 or not license_text:
        raise ValueError('incomplete flag bundle')
    root = pathlib.Path(__file__).parent
    (root / '.runtime').mkdir(exist_ok=True)
    (root / '.runtime' / 'svg-flags.json').write_text(json.dumps(flags, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    (root / 'FLAGS-LICENSE.txt').write_text('flag-icons, upstream commit ' + REVISION + '\n\n' + license_text, encoding='utf-8')
    print(f'Fetched {len(flags)} SVG country flags with upstream license; run bundle_flags.cjs to build small PNG mappings')
