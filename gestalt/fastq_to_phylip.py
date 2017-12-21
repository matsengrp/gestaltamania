"""
Converts the fastq files from simulation.py to input to PHYLIP MIX
Makes the binary file format
"""

from __future__ import print_function
from typing import Dict, List
import argparse
import re
from Bio import SeqIO
import numpy as np
import warnings


def write_seqs_to_phy(processed_seqs: Dict[str, List],
                      all_event_dict: Dict[str, int], phy_file: str,
                      abundance_file: str):
    """
    @param processed_seqs: dict key = sequence id, dict val = [abundance, list of events, cell state]
    @param all_event_dict: dict key = event id, dict val = event phylip id
    @param phy_file: name of file to input to phylip
    @param abundance_file: name of file with abundance values
    """
    num_events = len(all_event_dict)

    # Output file for PHYLIP
    # Format is very dumb: species name must be 10 characters long, followed by sequence of 0 and 1s
    with open(phy_file, "w") as f1, open(abundance_file, "w") as f2:
        f1.write("%d %d\n" % (len(processed_seqs), num_events))
        f2.write('id\tabundance\n')
        for seq_id, seq_data in processed_seqs.items():
            seq_abundance = seq_data[0]
            seq_events = seq_data[1]
            event_idxs = [all_event_dict[seq_ev] for seq_ev in seq_events]
            event_arr = np.zeros((num_events, ), dtype=int)
            event_arr[event_idxs] = 1
            event_encoding = "".join([str(c) for c in event_arr.tolist()])
            seq_name = seq_id
            seq_name += " " * (10 - len(seq_name))
            f1.write("%s%s\n" % (seq_name, event_encoding))
            f2.write('{}\t{}\n'.format(seq_name, seq_abundance))


def main():
    parser = argparse.ArgumentParser(description='convert to MIX')
    parser.add_argument('fastq', type=str, help='fastq input')
    parser.add_argument(
        '--outbase',
        type=str,
        help='output basename for phylip file and abundance weight file')
    args = parser.parse_args()

    # Naive processing of events
    all_events = set()
    processed_seqs = {}
    for record in SeqIO.parse(args.fastq, 'fastq'):
        seq_events = [
            event.group(0)
            for event in re.compile('[0-9]*:[0-9]*:[acgt]*').finditer(
                record.description)
        ]
        record_name = "seq" + record_name
        if record_name not in processed_seqs:
            all_events.update(seq_events)
            # list abundance and indel events
            processed_seqs[record_name] = [1, seq_events]
        else:
            processed_seqs[record_name][0] += 1
            if processed_seqs[record_name][1] != seq_events:
                warnings.warn(
                    'identical sequences have different event calls: {}, {}\nsequence: {}'
                    .format(processed_seqs[record_name][1], seq_events,
                            record_name))
    all_event_dict = {event_id: i for i, event_id in enumerate(all_events)}

    phy_file = args.outbase + '.phy',
    abundance_file = args.outbase + '.abundance'
    write_seqs_to_phy(processed_seqs, all_event_dict, phy_file, abundance_file)


if __name__ == "__main__":
    main()
