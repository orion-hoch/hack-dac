#!/usr/bin/env python3
"""De-identification pipeline CLI.  python3 -I run_pipeline.py --help"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.run import main

if __name__ == '__main__':
    main(sys.argv[1:])
