"""Compatibility entry; supply --manifest from the selected run directory."""
import sys
from checks import main

if __name__ == '__main__':
    sys.exit(main(['--stage', 'root', *sys.argv[1:]]))

