#!/usr/bin/env python3
"""Keep BMaaS-created lab Agents mapped to their Netris server identities."""
import json
import subprocess
import sys
import time


def reconcile(config):
    oc = [config['oc'], '--request-timeout=20s', '-n', config['namespace']]
    agents = json.loads(subprocess.check_output(oc + ['get', 'agents', '-o', 'json'], timeout=30))
    for agent in agents['items']:
        meta = agent['metadata']
        server = config['servers'].get(meta['name'])
        if not server or meta.get('deletionTimestamp'):
            continue
        if meta.get('labels', {}).get('netris.server/name') == server:
            continue
        # Optimistic concurrency avoids patching a deleted/recreated Agent by name.
        patch = {'metadata': {'resourceVersion': meta['resourceVersion'],
                              'labels': {'netris.server/name': server}}}
        subprocess.run(oc + ['patch', 'agent', meta['name'], '--type=merge',
                            '-p', json.dumps(patch)], check=True, timeout=30)


def main():
    with open(sys.argv[1]) as source:
        config = json.load(source)
    while True:
        reconcile(config)
        if '--once' in sys.argv[2:]:
            return
        time.sleep(2)


if __name__ == '__main__':
    main()
