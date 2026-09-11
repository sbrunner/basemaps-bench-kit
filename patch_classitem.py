#!/usr/bin/env python3
"""
Convert roads layer of highways.map from logical expressions back to
CLASSITEM + plain string comparisons (tbonfort's "move the expression into
the template engine" suggestion), keeping the PR class structure.

Rules:
  ("[type]" =|IN v AND "[bridge]" = "0" AND "[tunnel]" = "0") -> suffixed 00
  ("[type]" =|IN v AND "[bridge]" = "1")                      -> suffixed 10
  ("[type]" =|IN v AND "[tunnel]" = "1")                      -> suffixed 01
  ("[type]" =|IN v)                                           -> plain
  ("[type]" = "service" AND rest)                             -> IN service00/10/01 AND rest
  add CLASSITEM "type" after DATA _roads_data + LABELITEM
"""

import re
import sys


def suffixed(values: list[str], suffix: str) -> str:
    items = [v + suffix for v in values]
    if len(items) == 1:
        return '"%s"' % items[0]
    return "{%s}" % ",".join(items)


def parse_values(match: re.Match[str]) -> list[str]:
    op, val = match.group("op"), match.group("val")
    if op == "IN":
        return val.split(",")
    return [val]


TYPE_COND = r'\[type\]" (?P<op>=|IN) "(?P<val>[a-z_,]+)"'

RULES = [
    # plain, bridges displayed: bridge=0 and tunnel=0 -> suffix 00
    (re.compile(r'EXPRESSION \("' + TYPE_COND + r' AND "\[bridge\]" = "0" AND "\[tunnel\]" = "0"\)', re.M),
     lambda m: "EXPRESSION " + suffixed(parse_values(m), "00")),
    # bridges: bridge=1 -> suffix 10
    (re.compile(r'EXPRESSION \("' + TYPE_COND + r' AND "\[bridge\]" = "1"\)', re.M),
     lambda m: "EXPRESSION " + suffixed(parse_values(m), "10")),
    # tunnels: tunnel=1 -> suffix 01
    (re.compile(r'EXPRESSION \("' + TYPE_COND + r' AND "\[tunnel\]" = "1"\)', re.M),
     lambda m: "EXPRESSION " + suffixed(parse_values(m), "01")),
    # service overlay classes (z14+ only, type is suffixed there)
    (re.compile(r'EXPRESSION \("\[type\]" = "service" AND (?P<rest>.*)\)$', re.M),
     lambda m: 'EXPRESSION ("[type]" IN "service00,service10,service01" AND %s)' % m.group("rest")),
    # plain, no bridges displayed (#else branch): simple list/string
    (re.compile(r'EXPRESSION \("' + TYPE_COND + r'\)', re.M),
     lambda m: "EXPRESSION " + suffixed(parse_values(m), "")),
]


def main(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        content = f.read()

    original = content
    count = 0
    for pattern, repl in RULES:
        content, n = pattern.subn(lambda m, r=repl: r(m), content)
        count += n

    old_layer = '    DATA _roads_data\n    LABELITEM "name"\n'
    new_layer = '    DATA _roads_data\n    LABELITEM "name"\n    CLASSITEM "type"\n'
    if content.count(old_layer) != 1:
        print("ERROR: roads layer anchor not found exactly once", file=sys.stderr)
        return 1
    content = content.replace(old_layer, new_layer)

    if content == original:
        print("ERROR: no change applied", file=sys.stderr)
        return 1

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("expressions converted: %d (+1 CLASSITEM)" % count)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
