#! /usr/bin/env python

import argparse
import csv
from pathlib import Path
import re
import sys


ACC_REGEX = re.compile('(?<=Accuracy:  ).*')
F1_REGEX = re.compile('(?<=F1 score:  ).*')
METRIC_REGEX = re.compile('(?<=Metric:  ).*')

# NOTE: this should be changed to just METRIC_REGEX, but keeping ACC and F1 in for now

def main(args):
    writer = csv.writer(sys.stdout)
    stem = args.input.stem
    fields = stem.split('_')
    with open(args.input, 'r') as f:
        for line in f:
            pass
        match = ACC_REGEX.search(line)
        if match:
            acc = match.group(0)
            writer.writerow((*fields, acc))
        match = F1_REGEX.search(line)
        if match:
            f1 = match.group(0)
            writer.writerow((*fields, f1))
        match = METRIC_REGEX.search(line)
        if match:
            metric = match.group(0)
            writer.writerow((*fields, metric))



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('input', type=Path)
    args = parser.parse_args()

    main(args)
