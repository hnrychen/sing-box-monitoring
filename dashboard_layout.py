#!/usr/bin/env python3
"""Narrow, idempotent migration of the existing Grafana health layout."""
import copy


def update_layout(dashboard, activity_template):
    result = copy.deepcopy(dashboard)
    panels = result['panels']

    def one(title):
        matches = [p for p in panels if p.get('title') == title]
        if len(matches) != 1:
            raise ValueError('expected exactly one panel: ' + title)
        return matches[0]

    service = one('Service availability')
    tcp = one('TCP collector availability')
    health = one('Service resources and collector health')
    coverage = one('TCP RTT sample coverage')
    ack = one('TCP sample last-ACK age')
    bandwidth = one('Payload bandwidth by host')
    health_index = panels.index(health)
    if any(p.get('type') == 'row' for p in panels[health_index + 1:]):
        raise ValueError('health must be the final section; review custom rows first')
    migrated = [panels.index(p) > health_index for p in (service, tcp)]
    if migrated[0] != migrated[1]:
        raise ValueError('partially migrated availability panels; review layout first')
    existing = [p for p in panels if p.get('title') == 'Service activity']
    if len(existing) > 1:
        raise ValueError('duplicate Service activity panels')
    if not migrated[0]:
        if existing or any(p.get('id') == activity_template['id'] for p in panels):
            raise ValueError('new activity panel conflicts with existing content')
        activity = copy.deepcopy(activity_template)
        activity['gridPos'] = copy.deepcopy(service['gridPos'])
        activity['datasource'] = copy.deepcopy(service['datasource'])
        for target in activity['targets']:
            target['datasource'] = copy.deepcopy(service['datasource'])
        # Keep optional display-only host flags without changing metric identity.
        for override in one('Service uptime')['fieldConfig'].get('overrides', []):
            if override.get('matcher') == dict(id='byRegexp', options='^(Host|instance|nodename)$'):
                activity['fieldConfig']['overrides'].append(copy.deepcopy(override))
        old_slot = panels.index(service)
        for panel in panels[health_index + 1:]:
            panel['gridPos']['y'] += 7
        panels.insert(old_slot, activity)
    elif len(existing) != 1:
        raise ValueError('migrated health graphs require the activity table')
    else:
        # Refresh this migration's owned table queries without discarding its
        # datasource, host mappings, widths or unrelated dashboard settings.
        activity = existing[0]
        expressions = {t['refId']: t['expr'] for t in activity_template['targets']}
        if {t['refId'] for t in activity['targets']} != expressions.keys():
            raise ValueError('unexpected Service activity query layout')
        for target in activity['targets']:
            target['expr'] = expressions[target['refId']]
        widths = {o['matcher']['options']: next(p['value'] for p in o['properties'] if p['id'] == 'custom.width')
                  for o in activity_template['fieldConfig']['overrides'] if o['matcher']['id'] == 'byName'}
        for override in activity['fieldConfig']['overrides']:
            matcher = override.get('matcher', {})
            if matcher.get('id') == 'byName' and matcher.get('options') in widths:
                for prop in override['properties']:
                    if prop['id'] == 'custom.width':
                        prop['value'] = widths[matcher['options']]

    coverage['gridPos'].update(x=0, w=12)
    ack['gridPos'].update(x=12, w=12)
    # Six activity columns should fit beside the bandwidth chart at desktop
    # widths; narrow screens retain Grafana's normal table scrolling.
    activity['gridPos'].update(x=0, w=12)
    bandwidth['gridPos'].update(x=12, w=12)
    for panel, x in ((service, 0), (tcp, 12)):
        panels.remove(panel)
        panel['gridPos'] = dict(x=x, y=health['gridPos']['y'] + 1, w=12, h=7)
    index = panels.index(health) + 1
    panels[index:index] = [service, tcp]
    return result
