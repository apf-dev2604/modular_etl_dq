#!/usr/bin/env python3
import sys
from helpers.main import main

if '--brand' not in sys.argv:
    sys.argv.extend(['--brand', 'instaplay'])
main()
