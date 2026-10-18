"""Fake Claude option parsing: Commander variadics consume following non-option arguments.

An attached --option=value sets that value without activating variadic consumption.
Only the options exercised by the launcher tests are implemented.
"""
import json
import sys


def parse(args):
    configs, positionals, options = [], [], []
    variadic = False
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            positionals.extend(args[i:])
            break
        if variadic and not arg.startswith("-"):
            configs.append(arg)
            continue
        variadic = False
        if arg == "--mcp-config":
            configs.append(args[i])
            i += 1
            variadic = True
        elif arg.startswith("--mcp-config="):
            configs.append(arg.split("=", 1)[1])
        elif arg in ("--model", "--resume"):
            options.extend((arg, args[i]))
            i += 1
        elif arg.startswith("-"):
            options.append(arg)
        else:
            positionals.append(arg)
    return {"configs": configs, "positionals": positionals, "options": options}


if __name__ == "__main__":
    print(json.dumps(parse(sys.argv[1:])))
