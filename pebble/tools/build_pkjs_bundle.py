#!/usr/bin/env python3
"""
Standalone replica of pebble/wscript's concat_javascript_task.
Produces a single wrapped JS bundle that is self-contained with the custom
__loader module system so it can run without webpack / CommonJS resolution.
"""
import json
import os
import sys

LOADER_PATH = "loader.js"
LOADER_TEMPLATE = ("__loader.define({relpath}, {lineno}, "
                   "function(exports, module, require) {{\n{body}\n}});")
JSON_TEMPLATE = "module.exports = {body};"
APPINFO_PATH = "appinfo.json"

def build(js_root, appinfo_file, out_path):
    # Skip the output file so rebuilds don't fold the previous bundle into
    # itself when out_path lives under js_root.
    out_abs = os.path.abspath(out_path)
    js_nodes = []
    for root, dirs, files in os.walk(js_root):
        for f in files:
            if not (f.endswith('.js') or f.endswith('.json')):
                continue
            path = os.path.join(root, f)
            if os.path.abspath(path) == out_abs:
                continue
            js_nodes.append(path)
    js_nodes.sort()

    sources = []
    loader_body = None

    for path in js_nodes:
        relpath = os.path.relpath(path, js_root).replace(os.sep, '/')
        with open(path, 'r') as f:
            body = f.read()
        if relpath.endswith('.json'):
            body = JSON_TEMPLATE.format(body=body)
        if relpath == LOADER_PATH:
            loader_body = body
            continue
        sources.append({'relpath': relpath, 'body': body})

    # Append appinfo.json
    with open(appinfo_file, 'r') as f:
        body = JSON_TEMPLATE.format(body=f.read())
        sources.append({'relpath': APPINFO_PATH, 'body': body})

    # Kickoff
    sources.append('__loader.require("main");')

    if loader_body is None:
        print("ERROR: loader.js not found under", js_root, file=sys.stderr)
        sys.exit(1)

    def loader_translate(source, lineno):
        return LOADER_TEMPLATE.format(
            relpath=json.dumps(source['relpath']),
            lineno=lineno,
            body=source['body'])

    with open(out_path, 'w') as f:
        f.write(loader_body + '\n')
        lineno = loader_body.count('\n') + 2
        for source in sources:
            if isinstance(source, dict):
                body = loader_translate(source, lineno)
            else:
                body = source
            f.write(body + '\n')
            lineno += body.count('\n') + 1

    print("Wrote", out_path, "({} bytes, {} modules)".format(
        os.path.getsize(out_path), len(sources) - 1))

if __name__ == '__main__':
    pkjs = sys.argv[1]
    appinfo = sys.argv[2]
    out = sys.argv[3]
    build(pkjs, appinfo, out)
