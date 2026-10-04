"""Insert only probe/TCP diagnostics panels, preserving existing dashboard settings."""
import copy


def update_network_layout(dashboard, templates):
    result = copy.deepcopy(dashboard)
    panels = result['panels']
    ids = {p['id'] for p in templates}
    found = [p for p in panels if p.get('id') in ids]
    if found:
        if len(found) != len(templates) or {p['title'] for p in found} != {p['title'] for p in templates}:
            raise ValueError('partial/conflicting network monitoring panels')
    elif any(p.get('title') in {t['title'] for t in templates} for p in panels):
        raise ValueError('network panel titles already exist with different IDs')
    def one(title):
        matches = [p for p in panels if p['title'] == title]
        if len(matches) != 1:
            raise ValueError('expected one ' + title)
        return matches[0]
    probe = [p for p in found if p['id'] < 1004]
    health = one('Service resources and collector health')
    if probe:
        first = min(p['gridPos']['y'] for p in probe)
        if panels.index(probe[0]) < panels.index(health) and first + 19 == health['gridPos']['y']:
            return result
        # Relocate the owned 19-unit band, retaining all existing panel settings.
        for p in probe:
            panels.remove(p)
            p['gridPos']['y'] -= first
        for p in panels:
            if p['gridPos']['y'] >= first + 19:
                p['gridPos']['y'] -= 19
    else:
        probe = [copy.deepcopy(p) for p in templates if p['id'] < 1004]
    if not found:
        destinations = one('Active connection destinations')
        y = destinations['gridPos']['y']
        for p in panels:
            if p['gridPos']['y'] >= y:
                p['gridPos']['y'] += 7
        tcp = [copy.deepcopy(p) for p in templates if p['id'] >= 1004]
        for p in tcp:
            p['gridPos']['y'] = y
        index = panels.index(destinations)
        panels[index:index] = tcp
    y = health['gridPos']['y']
    for p in panels:
        if p['gridPos']['y'] >= y:
            p['gridPos']['y'] += 19
    for p in probe:
        p['gridPos']['y'] += y
    index = panels.index(health)
    panels[index:index] = probe
    return result
